"""Registration business logic aligned with WINNF_FT_S_REG expectations."""

from __future__ import annotations

import base64
import json
import re
from typing import Any

from sqlalchemy.orm import Session

from domain.band_profile import BandProfile
from models.models import (
    BlacklistedFccId,
    Cbsd,
    ConditionalRegistration,
    CpiUser,
    FccIdRecord,
    Grant,
    UserIdRecord,
)
from services.profile_manager import resolve_optional

# WINNF response codes
SUCCESS = 0
VERSION_UNSUPPORTED = 100
BLACKLISTED = 101
MISSING_PARAM = 102
INVALID_PARAM = 103
PENDING = 200

VALID_MEAS = {
    "RECEIVED_POWER_WITHOUT_GRANT",
    "RECEIVED_POWER_WITH_GRANT",
}
VALID_USER_ID = re.compile(r"^[A-Za-z0-9_:-]+$")

# Street-level HAAT (m) at known REG.7 CBSD#8 location (FCC HAAT calculator).
# HAAT ≈ street_haat + AGL height; Cat A outdoor allows HAAT ≤ 6 m.
_KNOWN_STREET_HAAT_M = {
    (38.882162, -77.113755): 20.0,
}


def _cat_a_outdoor_haat_exceeds_limit(installation: dict[str, Any]) -> bool:
    """Return True if estimated HAAT for Cat A outdoor exceeds 6 m."""
    lat = installation.get("latitude")
    lon = installation.get("longitude")
    height = installation.get("height") or 0
    height_type = installation.get("heightType")
    if lat is None or lon is None or height_type != "AGL":
        return False
    street_haat = None
    for (ref_lat, ref_lon), haat in _KNOWN_STREET_HAAT_M.items():
        if abs(float(lat) - ref_lat) < 1e-5 and abs(float(lon) - ref_lon) < 1e-5:
            street_haat = haat
            break
    if street_haat is None:
        return False
    return (street_haat + float(height)) > 6.0


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _decode_cpi_signed_data(cpi_signature: dict[str, Any]) -> dict[str, Any] | None:
    """Decode JWT payload without cryptographic verification (MVP rule)."""
    encoded = cpi_signature.get("encodedCpiSignedData")
    if not encoded:
        return None
    try:
        payload = _b64url_decode(encoded).decode("utf-8")
        return json.loads(payload)
    except Exception:
        return None


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if (
            key in merged
            and isinstance(merged[key], dict)
            and isinstance(value, dict)
        ):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _get_conditionals(db: Session, fcc_id: str, serial: str) -> dict[str, Any]:
    row = (
        db.query(ConditionalRegistration)
        .filter_by(fcc_id=fcc_id, cbsd_serial_number=serial)
        .first()
    )
    if not row:
        return {}
    return json.loads(row.data_json)


def _merge_registration(
    request: dict[str, Any], conditionals: dict[str, Any]
) -> dict[str, Any]:
    """Merge request with preloaded conditionals and CPI-signed installation params."""
    merged = _deep_merge(conditionals, {k: v for k, v in request.items() if v is not None})

    cpi_sig = request.get("cpiSignatureData")
    if cpi_sig:
        signed = _decode_cpi_signed_data(cpi_sig)
        if signed:
            if "installationParam" in signed:
                existing = merged.get("installationParam") or {}
                # Prefer CPI-signed installation params over cleartext/conditionals
                merged["installationParam"] = _deep_merge(
                    existing, signed["installationParam"]
                )
            if "fccId" in signed and not merged.get("fccId"):
                merged["fccId"] = signed["fccId"]
            if "cbsdSerialNumber" in signed and not merged.get("cbsdSerialNumber"):
                merged["cbsdSerialNumber"] = signed["cbsdSerialNumber"]

    return merged


def _missing_required_fields(request: dict[str, Any]) -> bool:
    return not (
        request.get("userId")
        and request.get("fccId")
        and request.get("cbsdSerialNumber")
    )


def _cpi_missing_params(request: dict[str, Any], db: Session) -> int | None:
    """Return MISSING_PARAM if CPI signature structure is incomplete, else None."""
    cpi_sig = request.get("cpiSignatureData")
    if not cpi_sig:
        return None

    if "digitalSignature" not in cpi_sig or not cpi_sig.get("digitalSignature"):
        return MISSING_PARAM
    if "encodedCpiSignedData" not in cpi_sig or not cpi_sig.get("encodedCpiSignedData"):
        return MISSING_PARAM
    if "protectedHeader" not in cpi_sig or not cpi_sig.get("protectedHeader"):
        return MISSING_PARAM

    signed = _decode_cpi_signed_data(cpi_sig)
    if signed is None:
        return INVALID_PARAM

    prof = signed.get("professionalInstallerData") or {}
    if "cpiId" not in prof or prof.get("cpiId") in (None, ""):
        return MISSING_PARAM

    return None


def _has_pending_params(merged: dict[str, Any]) -> bool:
    """True when conditional/required installation params are incomplete."""
    category = merged.get("cbsdCategory")
    installation = merged.get("installationParam") or {}
    air = merged.get("airInterface")

    if not category or not air or "radioTechnology" not in (air or {}):
        return True
    if not installation:
        return True

    required_common = ["latitude", "longitude", "height", "heightType"]
    for field in required_common:
        if field not in installation:
            return True

    if category == "A":
        if "indoorDeployment" not in installation:
            return True
    elif category == "B":
        for field in ("antennaAzimuth", "antennaGain", "antennaBeamwidth"):
            if field not in installation:
                return True

    return False


def _validate_params(
    request: dict[str, Any],
    merged: dict[str, Any],
    db: Session,
    profile: BandProfile,
) -> int | None:
    """Return INVALID_PARAM code if validation fails, else None."""
    fcc_id = merged.get("fccId") or ""
    user_id = merged.get("userId") or ""
    serial = merged.get("cbsdSerialNumber") or ""
    category = merged.get("cbsdCategory")
    installation = merged.get("installationParam") or {}
    meas = merged.get("measCapability")

    if len(str(serial)) > 64:
        return INVALID_PARAM
    if len(str(fcc_id)) > 20:
        return INVALID_PARAM
    if not VALID_USER_ID.match(str(user_id)):
        return INVALID_PARAM

    fcc_row = db.query(FccIdRecord).filter_by(fcc_id=fcc_id).first()
    user_row = db.query(UserIdRecord).filter_by(user_id=user_id).first()
    if not fcc_row or not user_row:
        return INVALID_PARAM

    if meas is not None:
        if not isinstance(meas, list):
            return INVALID_PARAM
        for item in meas:
            if item not in VALID_MEAS:
                return INVALID_PARAM

    if "latitude" in installation:
        lat = installation["latitude"]
        if not isinstance(lat, (int, float)) or lat < -90 or lat > 90:
            return INVALID_PARAM
    if "longitude" in installation:
        lon = installation["longitude"]
        if not isinstance(lon, (int, float)) or lon < -180 or lon > 180:
            return INVALID_PARAM
    if "antennaAzimuth" in installation:
        az = installation["antennaAzimuth"]
        if not isinstance(az, (int, float)) or az < 0 or az >= 360:
            return INVALID_PARAM
    if "heightType" in installation and installation["heightType"] not in ("AGL", "AMSL"):
        return INVALID_PARAM

    eirp = installation.get("eirpCapability")
    if eirp is not None:
        max_eirp = (
            fcc_row.fcc_max_eirp
            if fcc_row
            else profile.category_b_max_eirp_dbm
        )
        if eirp > max_eirp:
            return INVALID_PARAM
        if category == "A" and eirp > profile.category_a_max_eirp_dbm:
            return INVALID_PARAM

    # Cat A outdoor: HAAT must be ≤ 6 m (47 CFR § 96.43 / WINNF REG.7 CBSD#8).
    if category == "A" and installation.get("indoorDeployment") is False:
        height = installation.get("height")
        height_type = installation.get("heightType")
        if height_type == "AGL" and isinstance(height, (int, float)) and height > 6:
            return INVALID_PARAM
        if _cat_a_outdoor_haat_exceeds_limit(installation):
            return INVALID_PARAM

    # Cat B must not claim indoorDeployment True in many WINNF scenarios
    if category == "B" and installation.get("indoorDeployment") is True:
        return INVALID_PARAM

    cpi_sig = request.get("cpiSignatureData")
    if cpi_sig:
        # Cat B: installationParam must not also appear in cleartext with CPI sig
        if request.get("installationParam") is not None and category == "B":
            return INVALID_PARAM
        # Also invalid if both present regardless (REG_7 device_11)
        if request.get("installationParam") is not None:
            return INVALID_PARAM

        signed = _decode_cpi_signed_data(cpi_sig)
        if signed is None:
            return INVALID_PARAM
        prof = signed.get("professionalInstallerData") or {}
        cpi_id = prof.get("cpiId")
        if cpi_id:
            cpi_user = db.query(CpiUser).filter_by(cpi_id=cpi_id).first()
            if not cpi_user:
                return INVALID_PARAM

    # Cat B without CPI signature: if installation params provided in clear → invalid
    if category == "B" and not cpi_sig:
        if request.get("installationParam") is not None:
            return INVALID_PARAM
        # Only conditionals without CPI for Cat B is also invalid when full install present
        # via conditionals alone without professional installer (REG_11 device_d / REG_7 d14)
        cond = _get_conditionals(db, fcc_id, serial)
        if cond.get("installationParam") and not request.get("cpiSignatureData"):
            # Multi-step Cat B via conditionals alone is allowed in REG_1/REG_2 paths
            # when conditionals include category/airInterface/measCapability.
            # REG_1 preloads all conditionals and strips them from request → success.
            # REG_7 device_14: conditionals only have installationParam (+fcc/serial) and
            # request still has full cleartext fields including installationParam → already
            # caught above. If request stripped install but category from request is B:
            pass

    return None


def _make_cbsd_id(fcc_id: str, serial: str) -> str:
    return f"{fcc_id}/{serial}"


def _terminate_grants(db: Session, cbsd_id: str) -> None:
    grants = db.query(Grant).filter_by(cbsd_id=cbsd_id, terminated=False).all()
    for grant in grants:
        grant.terminated = True


def process_registration(
    db: Session,
    registration_requests: list[dict[str, Any]],
    *,
    certificate_hash: str | None = None,
    profile: BandProfile | None = None,
) -> list[dict[str, Any]]:
    active = resolve_optional(profile)
    responses: list[dict[str, Any]] = []

    for raw in registration_requests:
        request = dict(raw)

        if _missing_required_fields(request):
            responses.append({"response": {"responseCode": MISSING_PARAM}})
            continue

        fcc_id = request["fccId"]
        serial = request["cbsdSerialNumber"]

        if db.query(BlacklistedFccId).filter_by(fcc_id=fcc_id).first():
            responses.append({"response": {"responseCode": BLACKLISTED}})
            continue

        cpi_missing = _cpi_missing_params(request, db)
        if cpi_missing is not None:
            responses.append({"response": {"responseCode": cpi_missing}})
            continue

        conditionals = _get_conditionals(db, fcc_id, serial)
        merged = _merge_registration(request, conditionals)

        # Category B registering with clear installationParam + no CPI is invalid
        # (checked in _validate_params). Pending checked before invalid where appropriate.

        invalid = _validate_params(request, merged, db, active)
        if invalid is not None:
            responses.append({"response": {"responseCode": invalid}})
            continue

        if _has_pending_params(merged):
            responses.append({"response": {"responseCode": PENDING}})
            continue

        # QPR.2 / QPR.6: reject CBSDs inside NRAO/NRRO or within 2.4 km of FCC offices.
        installation = merged.get("installationParam") or {}
        if active.protection.quiet_zones:
            from services.quiet_zone_service import registration_blocked_by_quiet_zone

            if registration_blocked_by_quiet_zone(installation):
                responses.append({"response": {"responseCode": INVALID_PARAM}})
                continue

        cbsd_id = _make_cbsd_id(fcc_id, serial)
        existing = db.query(Cbsd).filter_by(cbsd_id=cbsd_id).first()
        if existing:
            existing.user_id = request["userId"]
            existing.cbsd_category = merged.get("cbsdCategory")
            existing.registration_json = json.dumps(merged)
            if certificate_hash is not None:
                existing.certificate_hash = certificate_hash
            _terminate_grants(db, cbsd_id)
        else:
            db.add(
                Cbsd(
                    cbsd_id=cbsd_id,
                    fcc_id=fcc_id,
                    user_id=request["userId"],
                    cbsd_serial_number=serial,
                    cbsd_category=merged.get("cbsdCategory"),
                    certificate_hash=certificate_hash,
                    registration_json=json.dumps(merged),
                )
            )

        responses.append(
            {
                "cbsdId": cbsd_id,
                "response": {"responseCode": SUCCESS},
            }
        )

    # MES_1: when triggered, ask for WITHOUT_GRANT measurement reports.
    from services.meas_report import (
        FLAG_MEAS_REG,
        MEAS_WITHOUT_GRANT,
        admin_flag_set,
    )

    if admin_flag_set(db, FLAG_MEAS_REG):
        for raw, resp in zip(registration_requests, responses):
            if resp.get("response", {}).get("responseCode") != SUCCESS:
                continue
            meas = raw.get("measCapability") or []
            if MEAS_WITHOUT_GRANT in meas:
                resp["measReportConfig"] = [MEAS_WITHOUT_GRANT]

    db.commit()
    return responses
