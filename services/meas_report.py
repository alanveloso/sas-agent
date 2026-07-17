"""Shared measurement-report helpers for Registration / SIQ / Grant / Heartbeat."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from domain.band_profile import BandProfile
from models.models import AdminInjectedData
from services.profile_manager import resolve_optional

MISSING_PARAM = 102
INVALID_PARAM = 103

MEAS_WITHOUT_GRANT = "RECEIVED_POWER_WITHOUT_GRANT"
MEAS_WITH_GRANT = "RECEIVED_POWER_WITH_GRANT"
FLAG_MEAS_REG = "meas_report_registration"
FLAG_MEAS_HBT = "meas_report_heartbeat"
FLAG_DPA_ACTIVE = "dpa_active"


def _boot_profile() -> BandProfile:
    return resolve_optional(None)


# Legacy aliases (CBRS defaults / active profile snapshot).
_boot = _boot_profile()
MIN_WITHOUT_GRANT_REPORTS = _boot.min_without_grant_reports
EXPECTED_BANDWIDTH_HZ = _boot.channel_width_hz


def admin_flag_set(db: Session, kind: str) -> bool:
    return db.query(AdminInjectedData).filter_by(kind=kind).first() is not None


def set_admin_flag(db: Session, kind: str, payload: dict[str, Any] | None = None) -> None:
    import json

    existing = db.query(AdminInjectedData).filter_by(kind=kind).first()
    data = json.dumps(payload or {})
    if existing:
        existing.data_json = data
    else:
        db.add(AdminInjectedData(kind=kind, data_json=data))
    db.commit()


def clear_admin_flags(db: Session, kind: str) -> None:
    db.query(AdminInjectedData).filter_by(kind=kind).delete()
    db.commit()


def cbsd_meas_capabilities(registration_json: str | None) -> list[str]:
    import json

    try:
        data = json.loads(registration_json or "{}")
    except json.JSONDecodeError:
        return []
    meas = data.get("measCapability") or []
    return list(meas) if isinstance(meas, list) else []


def validate_meas_report(
    meas_report: Any | None,
    *,
    require_full_cbrs: bool = False,
    profile: BandProfile | None = None,
) -> int | None:
    """
    Validate a CBSD measReport object.
    Returns responseCode on failure, or None if OK.
    """
    active = resolve_optional(profile)
    if meas_report is None:
        return MISSING_PARAM
    if not isinstance(meas_report, dict):
        return INVALID_PARAM
    if "rcvdPowerMeasReports" not in meas_report:
        return MISSING_PARAM
    reports = meas_report.get("rcvdPowerMeasReports")
    if not isinstance(reports, list) or len(reports) == 0:
        return MISSING_PARAM
    if require_full_cbrs and len(reports) < active.min_without_grant_reports:
        return MISSING_PARAM

    expected_bw = active.channel_width_hz
    band_low = active.band_low_hz
    band_high = active.band_high_hz

    for item in reports:
        if not isinstance(item, dict):
            return INVALID_PARAM
        if (
            "measFrequency" not in item
            or "measBandwidth" not in item
            or "measRcvdPower" not in item
        ):
            return MISSING_PARAM
        try:
            freq = int(item["measFrequency"])
            bw = int(item["measBandwidth"])
        except (TypeError, ValueError):
            return INVALID_PARAM
        if bw != expected_bw:
            return INVALID_PARAM
        # Frequency must fall within the active band.
        if freq < band_low or freq >= band_high:
            return INVALID_PARAM
    return None
