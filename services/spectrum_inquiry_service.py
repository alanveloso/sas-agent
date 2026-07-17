"""Spectrum Inquiry business logic for WINNF_FT_S_SIQ expectations (MVP simulation)."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from domain.band_profile import BandProfile
from models.models import AdminInjectedData, BlacklistedFccId, Cbsd
from services.profile_manager import resolve_optional

SUCCESS = 0
BLACKLISTED = 101
MISSING_PARAM = 102
INVALID_PARAM = 103
UNSUPPORTED_SPECTRUM = 300


def _default_profile() -> BandProfile:
    return resolve_optional(None)


# Eager ints for `from ... import CBRS_LOW_HZ` (snapshot at import / active profile).
# Prefer injecting BandProfile into process_* entry points.
_boot = _default_profile()
CBRS_LOW_HZ = _boot.band_low_hz
CBRS_HIGH_HZ = _boot.band_high_hz
CHANNEL_HZ = _boot.channel_width_hz
FSS_PROTECTION_KM = _boot.fss_protection_km
FSS_EXCLUSION_LOW_HZ = (
    _boot.fss_exclusion_low_hz
    if _boot.fss_exclusion_low_hz is not None
    else _boot.band_low_hz
)
RULE_APPLIED = _boot.regulatory_rule_id


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    from services.geometry import haversine_m

    return haversine_m(lat1, lon1, lat2, lon2) / 1000.0


def _point_in_geojson(lat: float, lon: float, zone: dict[str, Any] | None) -> bool:
    """Backward-compatible alias used by grant_service imports historically."""
    from services.geometry import point_in_geojson

    return point_in_geojson(lat, lon, zone)


def _overlaps(a_low: int, a_high: int, b_low: int, b_high: int) -> bool:
    return a_low < b_high and a_high > b_low


def _subtract_range(
    segments: list[tuple[int, int]], ex_low: int, ex_high: int
) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    for low, high in segments:
        if not _overlaps(low, high, ex_low, ex_high):
            result.append((low, high))
            continue
        if low < ex_low:
            result.append((low, min(high, ex_low)))
        if high > ex_high:
            result.append((max(low, ex_high), high))
    return [(lo, hi) for lo, hi in result if hi > lo]


def _split_10mhz(low: int, high: int, profile: BandProfile | None = None) -> list[tuple[int, int]]:
    """Split spectrum using the active profile channel raster (legacy name kept)."""
    return resolve_optional(profile).split_channels(low, high)


def _cbsd_location(cbsd: Cbsd) -> tuple[float | None, float | None]:
    try:
        data = json.loads(cbsd.registration_json or "{}")
    except json.JSONDecodeError:
        return None, None
    inst = data.get("installationParam") or {}
    lat = inst.get("latitude")
    lon = inst.get("longitude")
    if lat is None or lon is None:
        return None, None
    return float(lat), float(lon)


def _load_injected(db: Session, kind: str) -> list[dict[str, Any]]:
    if kind == "pal":
        from services.pal_service import load_pal_records

        # Only VALID, non-expired PALs participate in availability.
        return load_pal_records(db, active_only=True)
    rows = db.query(AdminInjectedData).filter_by(kind=kind).all()
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            out.append(json.loads(row.data_json))
        except json.JSONDecodeError:
            continue
    return out


def _pal_freq(pal: dict[str, Any]) -> tuple[int, int] | None:
    assignment = (pal.get("channelAssignment") or {}).get("primaryAssignment") or {}
    low = assignment.get("lowFrequency")
    high = assignment.get("highFrequency")
    if low is None or high is None:
        return None
    return int(low), int(high)


def _wisp_freq(wisp: dict[str, Any]) -> tuple[int, int] | None:
    record = wisp.get("record") or wisp
    deps = record.get("deploymentParam") or []
    if not deps:
        return None
    fr = (
        (deps[0].get("operationParam") or {}).get("operationFrequencyRange") or {}
    )
    low, high = fr.get("lowFrequency"), fr.get("highFrequency")
    if low is None or high is None:
        return None
    return int(low), int(high)


def _fss_location_and_freq(
    fss_payload: dict[str, Any],
) -> tuple[float, float, int, int] | None:
    record = fss_payload.get("record") or fss_payload
    deps = record.get("deploymentParam") or []
    if not deps:
        return None
    inst = deps[0].get("installationParam") or {}
    fr = (deps[0].get("operationParam") or {}).get("operationFrequencyRange") or {}
    lat, lon = inst.get("latitude"), inst.get("longitude")
    low, high = fr.get("lowFrequency"), fr.get("highFrequency")
    if None in (lat, lon, low, high):
        return None
    return float(lat), float(lon), int(low), int(high)


def _build_available_channels(
    db: Session,
    cbsd: Cbsd,
    inquired: list[dict[str, Any]],
    profile: BandProfile,
) -> list[dict[str, Any]]:
    from services.geometry import point_in_geojson

    lat, lon = _cbsd_location(cbsd)
    wisps = _load_injected(db, "wisp")
    fsses = _load_injected(db, "fss")
    pals = _load_injected(db, "pal")
    zones = _load_injected(db, "zone")

    band_low = profile.band_low_hz
    band_high = profile.band_high_hz
    rule_applied = profile.regulatory_rule_id
    fss_excl_low = (
        profile.fss_exclusion_low_hz
        if profile.fss_exclusion_low_hz is not None
        else band_low
    )

    # Segments requested ∩ band
    segments: list[tuple[int, int]] = []
    for fr in inquired:
        low = int(fr["lowFrequency"])
        high = int(fr["highFrequency"])
        clipped_low = max(low, band_low)
        clipped_high = min(high, band_high)
        if clipped_high > clipped_low:
            segments.append((clipped_low, clipped_high))

    # Exclusions from GWPZ / WISP when CBSD is inside the zone.
    if lat is not None and lon is not None:
        if profile.protection.exclusion_zones:
            from services.exclusion_zone_service import exclusion_freq_ranges_at_point

            for ex_low, ex_high in exclusion_freq_ranges_at_point(
                db, lat, lon, profile=profile
            ):
                segments = _subtract_range(segments, ex_low, ex_high)

        for wisp in wisps:
            zone = wisp.get("zone")
            freq = _wisp_freq(wisp)
            if freq and point_in_geojson(lat, lon, zone):
                segments = _subtract_range(segments, freq[0], freq[1])

        # FSS neighborhood: exclude fss_exclusion_low–band_high within protection radius.
        if profile.protection.fss_gwbl:
            for fss in fsses:
                info = _fss_location_and_freq(fss)
                if not info:
                    continue
                f_lat, f_lon, _fl, _fh = info
                if _haversine_km(lat, lon, f_lat, f_lon) <= profile.fss_protection_km:
                    segments = _subtract_range(segments, fss_excl_low, band_high)

    # PAL / PPA handling.
    pal_channels: list[tuple[int, int]] = []
    if profile.protection.pal_gaa:
        pal_by_id = {p.get("palId"): p for p in pals if p.get("palId")}
        user_pal_ids = {
            p.get("palId")
            for p in pals
            if p.get("userId") == cbsd.user_id and p.get("palId")
        }

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
                owned_by_cbsd = pal_id in user_pal_ids
                if in_cluster and owned_by_cbsd:
                    # Cluster CBSD operated by PAL licensee may use PAL.
                    if any(_overlaps(s[0], s[1], pf[0], pf[1]) for s in segments):
                        pal_channels.append(pf)
                    segments = _subtract_range(segments, pf[0], pf[1])
                elif in_cluster:
                    # Cluster member without matching userId: protect PAL only.
                    segments = _subtract_range(segments, pf[0], pf[1])
                elif in_ppa:
                    # Inside PPA but not in cluster → protect PAL (SIQ.5 / SIQ.2-like).
                    segments = _subtract_range(segments, pf[0], pf[1])
                # Outside PPA: PAL freqs remain GAA (no special handling).

    channels: list[dict[str, Any]] = []
    for plow, phigh in pal_channels:
        channels.append(
            {
                "frequencyRange": {
                    "lowFrequency": plow,
                    "highFrequency": phigh,
                },
                "channelType": "PAL",
                "ruleApplied": rule_applied,
            }
        )

    for low, high in segments:
        for clow, chigh in profile.split_channels(low, high):
            channels.append(
                {
                    "frequencyRange": {
                        "lowFrequency": clow,
                        "highFrequency": chigh,
                    },
                    "channelType": "GAA",
                    "ruleApplied": rule_applied,
                }
            )

    channels.sort(
        key=lambda ch: (
            ch["frequencyRange"]["lowFrequency"],
            ch["frequencyRange"]["highFrequency"],
        )
    )
    return channels


def _validate_inquired(
    inquired: list[dict[str, Any]] | None,
    profile: BandProfile,
) -> tuple[int | None, list[dict[str, Any]] | None]:
    """Return (error_code, inquired) — error_code None means OK."""
    if inquired is None:
        return MISSING_PARAM, None
    if not isinstance(inquired, list) or len(inquired) == 0:
        return MISSING_PARAM, None

    band_low = profile.band_low_hz
    band_high = profile.band_high_hz

    for fr in inquired:
        if not isinstance(fr, dict):
            return MISSING_PARAM, None
        if "lowFrequency" not in fr or "highFrequency" not in fr:
            return MISSING_PARAM, None
        low = fr["lowFrequency"]
        high = fr["highFrequency"]
        if low is None or high is None:
            return MISSING_PARAM, None
        try:
            low_i, high_i = int(low), int(high)
        except (TypeError, ValueError):
            return INVALID_PARAM, None
        if high_i <= low_i:
            return INVALID_PARAM, None
        # Fully or partially outside band → unsupported spectrum (SIQ.11).
        if low_i < band_low or high_i > band_high:
            return UNSUPPORTED_SPECTRUM, None

    return None, inquired


def _cert_mismatch(cbsd: Cbsd, certificate_hash: str | None) -> bool:
    """True when the request cert does not belong to the CBSD that owns cbsdId."""
    stored = cbsd.certificate_hash
    if not stored:
        return False
    if not certificate_hash:
        return True
    return stored.upper() != certificate_hash.upper()


def process_spectrum_inquiry(
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

    active = resolve_optional(profile)
    ask_meas = admin_flag_set(db, FLAG_MEAS_REG)
    responses: list[dict[str, Any]] = []
    for req in requests:
        cbsd_id = req.get("cbsdId")
        if not cbsd_id:
            responses.append({"response": {"responseCode": MISSING_PARAM}})
            continue

        cbsd = db.query(Cbsd).filter_by(cbsd_id=cbsd_id).first()
        if not cbsd:
            # Non-existent / unknown cbsdId → 103 without echoing cbsdId (SIQ.6).
            responses.append({"response": {"responseCode": INVALID_PARAM}})
            continue

        # Wrong client cert for this cbsdId → 103 without echoing cbsdId (SIQ.7).
        if _cert_mismatch(cbsd, certificate_hash):
            responses.append({"response": {"responseCode": INVALID_PARAM}})
            continue

        if db.query(BlacklistedFccId).filter_by(fcc_id=cbsd.fcc_id).first():
            responses.append(
                {
                    "cbsdId": cbsd_id,
                    "response": {"responseCode": BLACKLISTED},
                }
            )
            continue

        caps = cbsd_meas_capabilities(cbsd.registration_json)
        if ask_meas and MEAS_WITHOUT_GRANT in caps:
            meas_err = validate_meas_report(
                req.get("measReport"),
                require_full_cbrs=True,
                profile=active,
            )
            if meas_err is not None:
                responses.append(
                    {
                        "cbsdId": cbsd_id,
                        "response": {"responseCode": meas_err},
                    }
                )
                continue

        err, inquired = _validate_inquired(req.get("inquiredSpectrum"), active)
        if err is not None:
            responses.append(
                {
                    "cbsdId": cbsd_id,
                    "response": {"responseCode": err},
                }
            )
            continue

        assert inquired is not None
        available = _build_available_channels(db, cbsd, inquired, active)
        responses.append(
            {
                "cbsdId": cbsd_id,
                "availableChannel": available,
                "response": {"responseCode": SUCCESS},
            }
        )
    return responses
