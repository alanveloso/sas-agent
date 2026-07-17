"""Federal database helpers (FSS / GWBL / sync generation) for FDB suites."""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any

from sqlalchemy.orm import Session

from domain.band_profile import BandProfile
from models.models import AdminInjectedData, Cbsd, Grant
from services.geometry import haversine_m
from services.profile_manager import resolve_optional

KIND_META = "federal_sync_meta"


def _overlaps(a_low: int, a_high: int, b_low: int, b_high: int) -> bool:
    return a_low < b_high and a_high > b_low


def _load_kind(db: Session, kind: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in db.query(AdminInjectedData).filter_by(kind=kind).all():
        try:
            out.append(json.loads(row.data_json or "{}"))
        except json.JSONDecodeError:
            continue
    return out


def get_sync_meta(db: Session) -> dict[str, int]:
    rows = _load_kind(db, KIND_META)
    if not rows:
        return {"fss": 0, "gwbl": 0, "exz": 0, "dpa": 0}
    meta = rows[-1]
    return {
        "fss": int(meta.get("fss") or 0),
        "gwbl": int(meta.get("gwbl") or 0),
        "exz": int(meta.get("exz") or 0),
        "dpa": int(meta.get("dpa") or 0),
    }


def bump_sync_meta(db: Session, key: str) -> int:
    meta = get_sync_meta(db)
    meta[key] = int(meta.get(key) or 0) + 1
    db.query(AdminInjectedData).filter_by(kind=KIND_META).delete()
    db.add(AdminInjectedData(kind=KIND_META, data_json=json.dumps(meta)))
    return meta[key]


def grant_sync_stamp(db: Session) -> dict[str, int]:
    return get_sync_meta(db)


def _parse_mhz_token(text: str | None) -> int | None:
    if text is None:
        return None
    cleaned = str(text).replace(",", "").strip()
    if not cleaned:
        return None
    try:
        return int(float(cleaned) * 1_000_000)
    except (TypeError, ValueError):
        return None


def federal_fss_site_to_record(site: dict[str, Any]) -> dict[str, Any] | None:
    """Convert NTIA/FCC allsitedata site dict into WINNF FSS inject shape."""
    try:
        lat = float(site["earth_station_latitude_decimal"])
        lon = float(site["earth_station_longitude_decimal"])
    except (KeyError, TypeError, ValueError):
        return None
    low = _parse_mhz_token(site.get("lower_frequency"))
    high = _parse_mhz_token(site.get("upper_frequency"))
    if low is None or high is None:
        return None
    ttc_raw = str(site.get("tracking_telemetry_control") or "").strip().lower()
    return {
        "record": {
            "id": site.get("FSS_number") or site.get("call_sign") or "fss",
            "type": "FSS",
            "deploymentParam": [
                {
                    "installationParam": {
                        "latitude": lat,
                        "longitude": lon,
                    },
                    "operationParam": {
                        "operationFrequencyRange": {
                            "lowFrequency": low,
                            "highFrequency": high,
                        }
                    },
                }
            ],
        },
        "ttc": ttc_raw in ("true", "1", "yes"),
    }


def replace_fss_from_federal_payload(db: Session, payload: Any) -> None:
    sites: list[dict[str, Any]] = []
    if isinstance(payload, dict) and isinstance(payload.get("result"), list):
        sites = [s for s in payload["result"] if isinstance(s, dict)]
    elif isinstance(payload, list):
        sites = [s for s in payload if isinstance(s, dict)]

    db.query(AdminInjectedData).filter_by(kind="fss").delete()
    for site in sites:
        rec = federal_fss_site_to_record(site)
        if rec:
            db.add(AdminInjectedData(kind="fss", data_json=json.dumps(rec)))
    bump_sync_meta(db, "fss")


def _dms_to_decimal(deg: str, minutes: str, seconds: str, hem: str) -> float | None:
    try:
        value = float(deg) + float(minutes) / 60.0 + float(seconds) / 3600.0
    except (TypeError, ValueError):
        return None
    if hem.upper() in ("S", "W"):
        value = -value
    return value


def parse_gwbl_zip(body: bytes) -> list[dict[str, Any]]:
    """Parse FCC ULS LO.dat inside a GWBL zip into location records."""
    out: list[dict[str, Any]] = []
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            lo_name = next(
                (n for n in zf.namelist() if n.upper().endswith("LO.DAT")), None
            )
            if not lo_name:
                return out
            text = zf.read(lo_name).decode("utf-8", errors="replace")
    except zipfile.BadZipFile:
        return out

    for line in text.splitlines():
        parts = line.split("|")
        if not parts or parts[0] != "LO" or len(parts) < 27:
            continue
        lat = _dms_to_decimal(parts[19], parts[20], parts[21], parts[22])
        lon = _dms_to_decimal(parts[23], parts[24], parts[25], parts[26])
        if lat is None or lon is None:
            continue
        out.append(
            {
                "id": parts[1],
                "callsign": parts[4],
                "latitude": lat,
                "longitude": lon,
            }
        )
    return out


def replace_gwbl_from_zip(db: Session, body: bytes) -> None:
    records = parse_gwbl_zip(body)
    db.query(AdminInjectedData).filter_by(kind="gwbl").delete()
    for rec in records:
        db.add(AdminInjectedData(kind="gwbl", data_json=json.dumps(rec)))
    bump_sync_meta(db, "gwbl")


def _fss_lat_lon_freq(fss: dict[str, Any]) -> tuple[float, float, int, int] | None:
    from services.spectrum_inquiry_service import _fss_location_and_freq

    return _fss_location_and_freq(fss)


def _cbsd_lat_lon(cbsd: Cbsd | None) -> tuple[float, float] | None:
    if not cbsd:
        return None
    try:
        reg = json.loads(cbsd.registration_json or "{}")
    except json.JSONDecodeError:
        return None
    inst = reg.get("installationParam") or {}
    try:
        return float(inst["latitude"]), float(inst["longitude"])
    except (KeyError, TypeError, ValueError):
        return None


def _grant_meta(grant: Grant) -> dict[str, Any]:
    try:
        data = json.loads(grant.grant_json or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def fss_sites_near(
    db: Session,
    lat: float,
    lon: float,
    *,
    profile: BandProfile | None = None,
    max_km: float | None = None,
) -> list[dict[str, Any]]:
    active = resolve_optional(profile)
    radius_km = active.fss_protection_km if max_km is None else max_km
    near: list[dict[str, Any]] = []
    for fss in _load_kind(db, "fss"):
        info = _fss_lat_lon_freq(fss)
        if not info:
            continue
        f_lat, f_lon, _low, _high = info
        if haversine_m(lat, lon, f_lat, f_lon) <= radius_km * 1000.0:
            near.append(fss)
    return near


def fss_has_neighboring_gwbl(
    db: Session, fss: dict[str, Any], *, profile: BandProfile | None = None
) -> bool:
    active = resolve_optional(profile)
    info = _fss_lat_lon_freq(fss)
    if not info:
        return False
    f_lat, f_lon, _low, _high = info
    radius_m = active.fss_protection_km * 1000.0
    for gwbl in _load_kind(db, "gwbl"):
        try:
            g_lat = float(gwbl["latitude"])
            g_lon = float(gwbl["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        if haversine_m(f_lat, f_lon, g_lat, g_lon) <= radius_m:
            return True
    return False


def grant_blocked_by_fss_gwbl(
    db: Session,
    lat: float,
    lon: float,
    low_hz: int,
    high_hz: int,
    *,
    profile: BandProfile | None = None,
) -> bool:
    """True when CBSD near an FSS that has a GWBL within the profile radius."""
    active = resolve_optional(profile)
    if not active.protection.fss_gwbl or active.fss_gwbl_policy is None:
        return False
    policy = active.fss_gwbl_policy
    if not _overlaps(low_hz, high_hz, policy.low_hz, policy.high_hz):
        return False
    if not _load_kind(db, "gwbl"):
        return False
    for fss in fss_sites_near(db, lat, lon, profile=active):
        if fss_has_neighboring_gwbl(db, fss, profile=active):
            return True
    return False


def heartbeat_federal_code(
    db: Session,
    cbsd: Cbsd | None,
    grant: Grant,
    *,
    profile: BandProfile | None = None,
) -> int | None:
    """Return 500/501 when federal DB protection applies, else None."""
    from services.exclusion_zone_service import point_hits_exclusion_zone

    active = resolve_optional(profile)
    loc = _cbsd_lat_lon(cbsd)
    if loc is None:
        return None
    lat, lon = loc
    meta = get_sync_meta(db)
    gmeta = _grant_meta(grant)
    low, high = grant.low_frequency, grant.high_frequency

    # Exclusion zones from federal EXZ KML.
    if (
        active.protection.exclusion_zones
        and meta.get("exz", 0) > 0
        and point_hits_exclusion_zone(db, lat, lon, low, high, profile=active)
    ):
        grant_exz = int(gmeta.get("exz_gen") or 0)
        return 500 if grant_exz < meta["exz"] else 501

    # Scheduled portal DPAs.
    dpa = active.dpa_policy
    if (
        active.protection.dpa
        and dpa is not None
        and meta.get("dpa", 0) > 0
        and _overlaps(low, high, dpa.low_hz, dpa.high_hz)
    ):
        grant_dpa = int(gmeta.get("dpa_gen") or 0)
        return 500 if grant_dpa < meta["dpa"] else 501

    if not active.protection.fss_gwbl:
        return None

    # FSS neighborhood using the profile coexistence sub-band / site freqs.
    near = fss_sites_near(db, lat, lon, profile=active)
    if not near:
        return None

    policy = active.fss_gwbl_policy
    protects = False
    for fss in near:
        info = _fss_lat_lon_freq(fss)
        if not info:
            continue
        _fla, _flo, f_low, f_high = info
        if policy is not None and _overlaps(low, high, policy.low_hz, policy.high_hz):
            protects = True
            break
        if _overlaps(low, high, f_low, f_high):
            protects = True
            break
    if not protects:
        return None

    # FDB.5: adding GWBL near FSS terminates existing grants (500).
    if meta.get("gwbl", 0) > 0:
        for fss in near:
            if fss_has_neighboring_gwbl(db, fss, profile=active):
                grant_combo = max(
                    int(gmeta.get("fss_gen") or 0), int(gmeta.get("gwbl_gen") or 0)
                )
                current = max(meta.get("fss", 0), meta.get("gwbl", 0))
                if grant_combo < current:
                    return 500

    grant_fss = int(gmeta.get("fss_gen") or 0)
    if grant_fss < meta.get("fss", 0):
        return 500
    return 501
