"""Exclusion zone (EXZ) persistence and grant/SIQ interference checks."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from models.models import AdminInjectedData
from services.geometry import within_geojson_buffer_m
from services.meas_report import admin_flag_set, set_admin_flag

KIND_EXCLUSION_ZONE = "exclusion_zone"
FLAG_NTIA_15_517 = "ntia_15_517"
KIND_NTIA_ZONES = "ntia_exclusion_zones"


def _overlaps(a_low: int, a_high: int, b_low: int, b_high: int) -> bool:
    return a_low < b_high and a_high > b_low


def _load_injected(db: Session, kind: str) -> list[dict[str, Any]]:
    rows = db.query(AdminInjectedData).filter_by(kind=kind).all()
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            out.append(json.loads(row.data_json))
        except json.JSONDecodeError:
            continue
    return out

# WINNF EXZ: CBSD inside zone or within 50 m of boundary → interference.
EXZ_BUFFER_M = 50.0

# NTIA coastal combined contour placemark names (geometry only; freqs come from profile).
NTIA_COASTAL_NAMES = ("West Combined Contour", "East-Gulf Combined Contour")


def _repo_ntia_kml() -> Path:
    # sas_mvp_core/services/ → repo root → data/ntia/protection_zones.kml
    return (
        Path(__file__).resolve().parents[2] / "data" / "ntia" / "protection_zones.kml"
    )


def _parse_kml_coordinates(text: str | None) -> list[list[float]]:
    ring: list[list[float]] = []
    for tok in (text or "").split():
        parts = tok.split(",")
        if len(parts) >= 2:
            try:
                ring.append([float(parts[0]), float(parts[1])])
            except ValueError:
                continue
    return ring


def load_ntia_coastal_geojson(kml_path: Path | None = None) -> dict[str, Any]:
    """Parse West / East-Gulf Combined Contours from protection_zones.kml."""
    path = kml_path or _repo_ntia_kml()
    if not path.is_file():
        return {"type": "FeatureCollection", "features": []}

    root = ET.parse(path).getroot()
    features: list[dict[str, Any]] = []
    wanted = set(NTIA_COASTAL_NAMES)

    for pm in root.iter("{http://www.opengis.net/kml/2.2}Placemark"):
        name_el = pm.find("{http://www.opengis.net/kml/2.2}name")
        if name_el is None or name_el.text not in wanted:
            continue
        outer = pm.find(
            ".//{http://www.opengis.net/kml/2.2}outerBoundaryIs"
            "/{http://www.opengis.net/kml/2.2}LinearRing"
            "/{http://www.opengis.net/kml/2.2}coordinates"
        )
        coords_el = outer
        if coords_el is None:
            coords_el = pm.find(".//{http://www.opengis.net/kml/2.2}coordinates")
        ring = _parse_kml_coordinates(coords_el.text if coords_el is not None else None)
        if len(ring) < 3:
            continue
        if ring[0] != ring[-1]:
            ring.append(list(ring[0]))
        features.append(
            {
                "type": "Feature",
                "properties": {"name": name_el.text},
                "geometry": {"type": "Polygon", "coordinates": [ring]},
            }
        )
        wanted.discard(name_el.text)
        if not wanted:
            break

    return {"type": "FeatureCollection", "features": features}


def persist_exclusion_zone(db: Session, payload: dict[str, Any]) -> None:
    """Store InjectExclusionZone body: {zone, frequencyRanges}."""
    db.add(
        AdminInjectedData(
            kind=KIND_EXCLUSION_ZONE,
            data_json=json.dumps(payload if payload is not None else {}),
        )
    )
    db.commit()


def enable_ntia_exclusion_zones(db: Session, *, profile=None) -> None:
    """Activate NTIA coastal exclusion zones using the active profile frequency envelope."""
    from services.profile_manager import resolve_optional

    active = resolve_optional(profile)
    if not active.protection.exclusion_zones:
        return
    policy = active.ntia_exclusion_policy
    if policy is None:
        return

    set_admin_flag(db, FLAG_NTIA_15_517)
    geojson = load_ntia_coastal_geojson()
    existing = db.query(AdminInjectedData).filter_by(kind=KIND_NTIA_ZONES).first()
    payload = json.dumps(
        {
            "zone": geojson,
            "frequencyRanges": [
                {"lowFrequency": policy.low_hz, "highFrequency": policy.high_hz}
            ],
        }
    )
    if existing:
        existing.data_json = payload
    else:
        db.add(AdminInjectedData(kind=KIND_NTIA_ZONES, data_json=payload))
    db.commit()


def _zone_records(db: Session, *, profile=None) -> list[dict[str, Any]]:
    records = list(_load_injected(db, KIND_EXCLUSION_ZONE))
    if admin_flag_set(db, FLAG_NTIA_15_517):
        ntia = _load_injected(db, KIND_NTIA_ZONES)
        if ntia:
            records.extend(ntia)
        else:
            # Flag set but cache missing (e.g. after partial reset) — rebuild.
            enable_ntia_exclusion_zones(db, profile=profile)
            records.extend(_load_injected(db, KIND_NTIA_ZONES))
    return records


def _freq_ranges(record: dict[str, Any]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for fr in record.get("frequencyRanges") or []:
        if not isinstance(fr, dict):
            continue
        low, high = fr.get("lowFrequency"), fr.get("highFrequency")
        if low is None or high is None:
            continue
        try:
            ranges.append((int(low), int(high)))
        except (TypeError, ValueError):
            continue
    return ranges


def point_hits_exclusion_zone(
    db: Session,
    lat: float,
    lon: float,
    low_hz: int | None = None,
    high_hz: int | None = None,
    *,
    profile=None,
) -> bool:
    """True if (lat, lon) is inside/near an active EXZ overlapping [low_hz, high_hz].

    When low/high are None, any overlapping frequency check is skipped (location only).
    """
    for record in _zone_records(db, profile=profile):
        zone = record.get("zone")
        if not within_geojson_buffer_m(lat, lon, zone, EXZ_BUFFER_M):
            continue
        freq_ranges = _freq_ranges(record)
        if not freq_ranges:
            return True
        if low_hz is None or high_hz is None:
            return True
        for zlow, zhigh in freq_ranges:
            if _overlaps(low_hz, high_hz, zlow, zhigh):
                return True
    return False


def exclusion_freq_ranges_at_point(
    db: Session, lat: float, lon: float, *, profile=None
) -> list[tuple[int, int]]:
    """Frequency ranges protected by EXZs covering this point (incl. 50 m buffer)."""
    out: list[tuple[int, int]] = []
    for record in _zone_records(db, profile=profile):
        zone = record.get("zone")
        if not within_geojson_buffer_m(lat, lon, zone, EXZ_BUFFER_M):
            continue
        out.extend(_freq_ranges(record))
    return out
