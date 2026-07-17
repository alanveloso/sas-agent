"""Grant business logic aligned with WINNF_FT_S_GRA expectations (MVP simulation)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from domain.band_profile import BandProfile
from models.models import BlacklistedFccId, Cbsd, FccIdRecord, Grant
from services.authorization_service import (
    SOURCE_TYPE_GAA,
    SOURCE_TYPE_PAL,
    clamp_datetime,
    ensure_gaa_authorization,
    is_authorization_valid,
    parse_utc,
    sync_grant_authorization_snapshot,
    utc_now,
)
from services.geometry import point_in_geojson
from services.profile_manager import resolve_optional
from services.spectrum_inquiry_service import (
    _load_injected,
    _overlaps,
    _pal_freq,
)

SUCCESS = 0
BLACKLISTED = 101
MISSING_PARAM = 102
INVALID_PARAM = 103
UNSUPPORTED_SPECTRUM = 300
INTERFERENCE = 400
GRANT_CONFLICT = 401


def _boot_profile() -> BandProfile:
    return resolve_optional(None)


# Legacy aliases derived from the active / default profile.
_boot = _boot_profile()
HEARTBEAT_INTERVAL_SEC = _boot.heartbeat_interval_sec
# Long enough for HBT.12 (sleep 240s + optional 300s) yet < 24h for HBT.6.
DEFAULT_GRANT_DURATION_SEC = _boot.grant_duration_sec
CAT_A_MAX_EIRP_10MHZ = _boot.category_a_max_eirp_dbm
CAT_B_MAX_EIRP_10MHZ = _boot.category_b_max_eirp_dbm


def _resp(code: int, *, cbsd_id: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"response": {"responseCode": code}}
    if cbsd_id is not None:
        out["cbsdId"] = cbsd_id
    return out


def _cbsd_reg(cbsd: Cbsd) -> dict[str, Any]:
    try:
        return json.loads(cbsd.registration_json or "{}")
    except json.JSONDecodeError:
        return {}


def _cbsd_location(cbsd: Cbsd) -> tuple[float | None, float | None]:
    inst = _cbsd_reg(cbsd).get("installationParam") or {}
    lat, lon = inst.get("latitude"), inst.get("longitude")
    if lat is None or lon is None:
        return None, None
    return float(lat), float(lon)


def _max_allowed_eirp_mhz(
    cbsd: Cbsd, fcc_max_eirp: float, profile: BandProfile
) -> float:
    """WINNF: maxEirp is dBm/MHz; category limits are dBm/(reference bandwidth)."""
    reg = _cbsd_reg(cbsd)
    cat = (reg.get("cbsdCategory") or cbsd.cbsd_category or "A").upper()
    eirp_cap = (reg.get("installationParam") or {}).get("eirpCapability")
    return profile.max_allowed_eirp_mhz(
        cat,
        fcc_max_eirp=fcc_max_eirp,
        eirp_capability=float(eirp_cap) if eirp_cap is not None else None,
    )


def _parse_freq(
    op: dict[str, Any] | None,
    profile: BandProfile,
) -> tuple[int | None, int | None, int | None]:
    """Return (error_code, low, high). error_code set on failure."""
    if op is None:
        return MISSING_PARAM, None, None
    freq = op.get("operationFrequencyRange")
    if not isinstance(freq, dict):
        return MISSING_PARAM, None, None
    if "lowFrequency" not in freq or "highFrequency" not in freq:
        return MISSING_PARAM, None, None
    low, high = freq.get("lowFrequency"), freq.get("highFrequency")
    if low is None or high is None:
        return MISSING_PARAM, None, None
    try:
        low_i, high_i = int(low), int(high)
    except (TypeError, ValueError):
        return INVALID_PARAM, None, None
    if high_i <= low_i:
        return INVALID_PARAM, None, None
    # Fully or partially outside band → 300.
    if low_i < profile.band_low_hz or high_i > profile.band_high_hz:
        return UNSUPPORTED_SPECTRUM, None, None
    return None, low_i, high_i


def _active_grants(db: Session, cbsd_id: str) -> list[Grant]:
    return (
        db.query(Grant)
        .filter_by(cbsd_id=cbsd_id, terminated=False)
        .all()
    )


def _has_freq_conflict(
    existing: list[Grant],
    low: int,
    high: int,
    *,
    also_pending: list[tuple[int, int]] | None = None,
) -> bool:
    for g in existing:
        if _overlaps(g.low_frequency, g.high_frequency, low, high):
            return True
    for plow, phigh in also_pending or []:
        if _overlaps(plow, phigh, low, high):
            return True
    return False


def _ppa_pal_context(
    db: Session, cbsd: Cbsd
) -> list[dict[str, Any]]:
    """Return list of PAL contexts for linked PPAs (includes palId / status)."""
    lat, lon = _cbsd_location(cbsd)
    # Active-only PALs: revoked/expired licenses never authorize a Grant.
    pals = _load_injected(db, "pal")
    zones = _load_injected(db, "zone")
    pal_by_id = {p.get("palId"): p for p in pals if p.get("palId")}
    contexts: list[dict[str, Any]] = []

    for zone_payload in zones:
        record = zone_payload.get("record") or zone_payload
        if record.get("usage") != "PPA" and "ppaInfo" not in record:
            continue
        ppa_info = record.get("ppaInfo") or {}
        cluster = set(ppa_info.get("cbsdReferenceId") or [])
        in_cluster = cbsd.cbsd_id in cluster
        in_ppa = False
        if lat is not None and lon is not None:
            in_ppa = point_in_geojson(lat, lon, record.get("zone"))

        for pal_id in ppa_info.get("palId") or []:
            pal = pal_by_id.get(pal_id)
            if not pal:
                continue
            pf = _pal_freq(pal)
            if not pf:
                continue
            license_exp = (pal.get("license") or {}).get("licenseExpiration")
            contexts.append(
                {
                    "pal_id": pal_id,
                    "low": pf[0],
                    "high": pf[1],
                    "in_cluster": in_cluster,
                    "in_ppa": in_ppa,
                    "license_exp": license_exp,
                    "license_status": pal.get("licenseStatus") or "VALID",
                    "user_id": pal.get("userId"),
                }
            )
    return contexts


def _resolve_channel(
    contexts: list[dict[str, Any]], low: int, high: int, *, cbsd_user_id: str
) -> tuple[int | None, str | None, datetime | None, str | None]:
    """
    Return (error_code, channel_type, pal_license_exp, pal_id).
    error_code None means OK.
    """
    covering_pal: list[dict[str, Any]] = []
    overlapping_pal: list[dict[str, Any]] = []
    for ctx in contexts:
        if _overlaps(low, high, ctx["low"], ctx["high"]):
            overlapping_pal.append(ctx)
            if low >= ctx["low"] and high <= ctx["high"]:
                covering_pal.append(ctx)

    # Inside claimed PPA but not in cluster → interference on PAL overlap.
    for ctx in overlapping_pal:
        if ctx["in_ppa"] and not ctx["in_cluster"]:
            return INTERFERENCE, None, None, None

    # Mix of PAL + GAA: request overlaps PAL for cluster member but not fully inside.
    for ctx in overlapping_pal:
        if ctx["in_cluster"] and not (low >= ctx["low"] and high <= ctx["high"]):
            return INVALID_PARAM, None, None, None

    if covering_pal:
        for ctx in covering_pal:
            if ctx["in_cluster"] and ctx.get("user_id") == cbsd_user_id:
                exp = parse_utc(ctx.get("license_exp"))
                # Expired / malformed PAL must not issue a Grant.
                if ctx.get("license_exp") and exp is None:
                    return INTERFERENCE, None, None, None
                if exp is not None and exp <= utc_now():
                    return INTERFERENCE, None, None, None
                return None, SOURCE_TYPE_PAL, exp, ctx.get("pal_id")
        # Fully inside a PAL channel but not authorized → interference.
        return INTERFERENCE, None, None, None

    return None, SOURCE_TYPE_GAA, None, None


def _grant_expire_time(
    auth_expires_at: datetime | None, profile: BandProfile
) -> datetime:
    default = utc_now() + timedelta(seconds=profile.grant_duration_sec)
    # Must be ≤ authorization expiration when present (GRA.13 / PAL license).
    return clamp_datetime(default, auth_expires_at)


def process_grant(
    db: Session,
    requests: list[dict[str, Any]],
    *,
    certificate_hash: str | None = None,
    profile: BandProfile | None = None,
) -> list[dict[str, Any]]:
    from services.meas_report import (
        FLAG_MEAS_REG,
        MEAS_WITHOUT_GRANT,
        admin_flag_set,
        cbsd_meas_capabilities,
        validate_meas_report,
    )
    from services.spectrum_inquiry_service import _cert_mismatch

    active = resolve_optional(profile)
    ask_meas = admin_flag_set(db, FLAG_MEAS_REG)
    responses: list[dict[str, Any]] = []
    # Frequencies approved earlier in this same batch (conflict within batch).
    pending_by_cbsd: dict[str, list[tuple[int, int]]] = {}
    ensure_gaa_authorization(db, profile=active)

    for req in requests:
        cbsd_id = req.get("cbsdId")
        if not cbsd_id:
            responses.append(_resp(MISSING_PARAM))
            continue

        cbsd = db.query(Cbsd).filter_by(cbsd_id=cbsd_id).first()
        if not cbsd:
            # Unknown CBSD → 103 without echoing cbsdId (GRA.3).
            responses.append(_resp(INVALID_PARAM))
            continue

        # Wrong client cert for this cbsdId → 103 without cbsdId/grantId (GRA.4).
        if _cert_mismatch(cbsd, certificate_hash):
            responses.append(_resp(INVALID_PARAM))
            continue

        if db.query(BlacklistedFccId).filter_by(fcc_id=cbsd.fcc_id).first():
            responses.append(_resp(BLACKLISTED, cbsd_id=cbsd_id))
            continue

        caps = cbsd_meas_capabilities(cbsd.registration_json)
        if ask_meas and MEAS_WITHOUT_GRANT in caps:
            meas_err = validate_meas_report(
                req.get("measReport"),
                require_full_cbrs=True,
                profile=active,
            )
            if meas_err is not None:
                responses.append(_resp(meas_err, cbsd_id=cbsd_id))
                continue

        op = req.get("operationParam")
        if not isinstance(op, dict):
            responses.append(_resp(MISSING_PARAM, cbsd_id=cbsd_id))
            continue
        if "maxEirp" not in op or op.get("maxEirp") is None:
            responses.append(_resp(MISSING_PARAM, cbsd_id=cbsd_id))
            continue

        freq_err, low, high = _parse_freq(op, active)
        if freq_err is not None:
            responses.append(_resp(freq_err, cbsd_id=cbsd_id))
            continue
        assert low is not None and high is not None

        try:
            max_eirp = float(op["maxEirp"])
        except (TypeError, ValueError):
            responses.append(_resp(INVALID_PARAM, cbsd_id=cbsd_id))
            continue

        fcc = db.query(FccIdRecord).filter_by(fcc_id=cbsd.fcc_id).first()
        fcc_max = (
            float(fcc.fcc_max_eirp)
            if fcc
            else active.category_b_max_eirp_dbm
        )
        if max_eirp > _max_allowed_eirp_mhz(cbsd, fcc_max, active):
            responses.append(_resp(INVALID_PARAM, cbsd_id=cbsd_id))
            continue

        if active.protection.pal_gaa:
            contexts = _ppa_pal_context(db, cbsd)
            ch_err, channel_type, pal_exp, pal_id = _resolve_channel(
                contexts, low, high, cbsd_user_id=cbsd.user_id
            )
            if ch_err is not None:
                responses.append(_resp(ch_err, cbsd_id=cbsd_id))
                continue
        else:
            channel_type, pal_exp, pal_id = SOURCE_TYPE_GAA, None, None
        assert channel_type is not None

        # Resolve and re-validate the authorization source immediately before persist.
        if channel_type == SOURCE_TYPE_PAL:
            if not pal_id:
                responses.append(_resp(INTERFERENCE, cbsd_id=cbsd_id))
                continue
            auth_ok, _reason, auth_source = is_authorization_valid(
                db, SOURCE_TYPE_PAL, str(pal_id)
            )
            if not auth_ok or auth_source is None:
                responses.append(_resp(INTERFERENCE, cbsd_id=cbsd_id))
                continue
            auth_expires = auth_source.expires_at or pal_exp
        else:
            auth_source = ensure_gaa_authorization(db, profile=active)
            auth_ok, _reason, _ = is_authorization_valid(
                db, SOURCE_TYPE_GAA, auth_source.source_id
            )
            if not auth_ok:
                responses.append(_resp(INTERFERENCE, cbsd_id=cbsd_id))
                continue
            auth_expires = None

        # EXZ: CBSD inside / within 50 m of an exclusion zone with overlapping freq → 400.
        lat, lon = _cbsd_location(cbsd)
        if lat is not None and lon is not None:
            if active.protection.exclusion_zones:
                from services.exclusion_zone_service import point_hits_exclusion_zone

                if point_hits_exclusion_zone(
                    db, lat, lon, low, high, profile=active
                ):
                    responses.append(_resp(INTERFERENCE, cbsd_id=cbsd_id))
                    continue

            # FDB.6: FSS with neighboring GWBL within profile radius → 400.
            if active.protection.fss_gwbl:
                from services.federal_db_service import grant_blocked_by_fss_gwbl

                if grant_blocked_by_fss_gwbl(
                    db, lat, lon, low, high, profile=active
                ):
                    responses.append(_resp(INTERFERENCE, cbsd_id=cbsd_id))
                    continue

        # BPR: Border Sharing Zone PFD above profile limit → 400.
        reg = _cbsd_reg(cbsd)
        installation = reg.get("installationParam") or {}
        if installation and active.protection.border_protection:
            from services.border_protection import violates_canadian_border_pfd

            if violates_canadian_border_pfd(
                installation, max_eirp, low, high, profile=active
            ):
                responses.append(_resp(INTERFERENCE, cbsd_id=cbsd_id))
                continue

        existing = _active_grants(db, cbsd_id)
        pending = pending_by_cbsd.get(cbsd_id, [])
        if _has_freq_conflict(existing, low, high, also_pending=pending):
            responses.append(_resp(GRANT_CONFLICT, cbsd_id=cbsd_id))
            continue

        # GRA_5: CBSD already has an active grant on a peer SAS (same cbsdReferenceId).
        from services.cpas_service import peer_has_grant_for_cbsd

        if peer_has_grant_for_cbsd(db, cbsd):
            responses.append(_resp(GRANT_CONFLICT, cbsd_id=cbsd_id))
            continue

        grant_id = f"grant/{uuid.uuid4().hex}"
        expire = _grant_expire_time(auth_expires, active)
        # Do not issue a grant whose authorization ceiling is already past.
        if auth_expires is not None and expire <= utc_now():
            responses.append(_resp(INTERFERENCE, cbsd_id=cbsd_id))
            continue

        from services.federal_db_service import grant_sync_stamp

        stamp = grant_sync_stamp(db)
        grant_payload = dict(req) if isinstance(req, dict) else {}
        grant_payload["fss_gen"] = stamp.get("fss", 0)
        grant_payload["gwbl_gen"] = stamp.get("gwbl", 0)
        grant_payload["exz_gen"] = stamp.get("exz", 0)
        grant_payload["dpa_gen"] = stamp.get("dpa", 0)
        grant = Grant(
            grant_id=grant_id,
            cbsd_pk=cbsd.id,
            cbsd_id=cbsd_id,
            channel_type=channel_type,
            low_frequency=low,
            high_frequency=high,
            max_eirp=max_eirp,
            grant_expire_time=expire,
            heartbeat_interval=active.heartbeat_interval_sec,
            grant_json=json.dumps(grant_payload),
        )
        sync_grant_authorization_snapshot(grant, auth_source)
        if grant.authorization_expires_at is None and auth_expires is not None:
            grant.authorization_expires_at = auth_expires
        db.add(grant)
        pending_by_cbsd.setdefault(cbsd_id, []).append((low, high))
        responses.append(
            {
                "cbsdId": cbsd_id,
                "grantId": grant_id,
                "grantExpireTime": expire.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "heartbeatInterval": active.heartbeat_interval_sec,
                "channelType": channel_type,
                "response": {"responseCode": SUCCESS},
            }
        )

    db.commit()
    return responses
