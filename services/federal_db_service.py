"""Profile-driven federal protections with injectable data and geo adapters."""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any, Callable, Protocol

from sqlalchemy.orm import Session

from domain.band_profile import BandProfile
from models.models import AdminInjectedData, Cbsd, Grant
from services.geometry import haversine_m
from services.profile_manager import resolve_optional

KIND_META = "federal_sync_meta"
_EMPTY_META = {"fss": 0, "gwbl": 0, "exz": 0, "dpa": 0}


class FederalDataProvider(Protocol):
    """Storage boundary for synchronized protection datasets."""

    def load(self, kind: str) -> list[dict[str, Any]]: ...

    def replace(self, kind: str, records: list[dict[str, Any]]) -> None: ...

    def sync_meta(self) -> dict[str, int]: ...

    def set_sync_meta(self, meta: dict[str, int]) -> None: ...


class SqlAlchemyFederalDataProvider:
    """SQLAlchemy adapter for the existing admin-injected data table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def load(self, kind: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        rows = self._session.query(AdminInjectedData).filter_by(kind=kind).all()
        for row in rows:
            try:
                value = json.loads(row.data_json or "{}")
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                records.append(value)
        return records

    def replace(self, kind: str, records: list[dict[str, Any]]) -> None:
        self._session.query(AdminInjectedData).filter_by(kind=kind).delete()
        for record in records:
            self._session.add(
                AdminInjectedData(kind=kind, data_json=json.dumps(record))
            )

    def sync_meta(self) -> dict[str, int]:
        rows = self.load(KIND_META)
        if not rows:
            return dict(_EMPTY_META)
        latest = rows[-1]
        return {
            key: int(latest.get(key) or 0)
            for key in _EMPTY_META
        }

    def set_sync_meta(self, meta: dict[str, int]) -> None:
        self.replace(KIND_META, [meta])


class DistanceProvider(Protocol):
    def distance_m(
        self, lat_a: float, lon_a: float, lat_b: float, lon_b: float
    ) -> float: ...


class HaversineDistanceProvider:
    def distance_m(
        self, lat_a: float, lon_a: float, lat_b: float, lon_b: float
    ) -> float:
        return haversine_m(lat_a, lon_a, lat_b, lon_b)


def _data_provider(
    db: Session, provider: FederalDataProvider | None
) -> FederalDataProvider:
    return provider or SqlAlchemyFederalDataProvider(db)


def _distance_provider(provider: DistanceProvider | None) -> DistanceProvider:
    return provider or HaversineDistanceProvider()


def _overlaps(a_low: int, a_high: int, b_low: int, b_high: int) -> bool:
    return a_low < b_high and a_high > b_low


def _sync_enabled(profile: BandProfile, key: str) -> bool:
    gates = {
        "fss": profile.protection.fss_gwbl,
        "gwbl": profile.protection.fss_gwbl,
        "exz": profile.protection.exclusion_zones,
        "dpa": profile.protection.dpa,
    }
    return gates.get(key, False)


def get_sync_meta(
    db: Session, *, provider: FederalDataProvider | None = None
) -> dict[str, int]:
    return _data_provider(db, provider).sync_meta()


def bump_sync_meta(
    db: Session,
    key: str,
    *,
    profile: BandProfile | None = None,
    provider: FederalDataProvider | None = None,
) -> int:
    active = resolve_optional(profile)
    data = _data_provider(db, provider)
    meta = data.sync_meta()
    if not _sync_enabled(active, key):
        return int(meta.get(key) or 0)
    meta[key] = int(meta.get(key) or 0) + 1
    data.set_sync_meta(meta)
    return meta[key]


def grant_sync_stamp(
    db: Session, *, provider: FederalDataProvider | None = None
) -> dict[str, int]:
    return get_sync_meta(db, provider=provider)


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
    """Convert a generic synchronized site into the internal FSS shape."""
    try:
        latitude = float(site["earth_station_latitude_decimal"])
        longitude = float(site["earth_station_longitude_decimal"])
    except (KeyError, TypeError, ValueError):
        return None
    low = _parse_mhz_token(site.get("lower_frequency"))
    high = _parse_mhz_token(site.get("upper_frequency"))
    if low is None or high is None:
        return None
    ttc = str(site.get("tracking_telemetry_control") or "").strip().lower()
    return {
        "record": {
            "id": site.get("FSS_number") or site.get("call_sign") or "fss",
            "type": "FSS",
            "deploymentParam": [
                {
                    "installationParam": {
                        "latitude": latitude,
                        "longitude": longitude,
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
        "ttc": ttc in {"true", "1", "yes"},
    }


def replace_fss_from_federal_payload(
    db: Session,
    payload: Any,
    *,
    profile: BandProfile | None = None,
    provider: FederalDataProvider | None = None,
) -> None:
    active = resolve_optional(profile)
    if not active.protection.fss_gwbl:
        return
    raw_sites = payload.get("result") if isinstance(payload, dict) else payload
    sites = raw_sites if isinstance(raw_sites, list) else []
    records = [
        record
        for site in sites
        if isinstance(site, dict)
        if (record := federal_fss_site_to_record(site)) is not None
    ]
    data = _data_provider(db, provider)
    data.replace("fss", records)
    bump_sync_meta(db, "fss", profile=active, provider=data)


def _dms_to_decimal(
    degrees: str, minutes: str, seconds: str, hemisphere: str
) -> float | None:
    try:
        value = (
            float(degrees)
            + float(minutes) / 60.0
            + float(seconds) / 3600.0
        )
    except (TypeError, ValueError):
        return None
    return -value if hemisphere.upper() in {"S", "W"} else value


def parse_gwbl_zip(body: bytes) -> list[dict[str, Any]]:
    """Parse a ULS-style LO.dat archive into generic location records."""
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            name = next(
                (
                    item
                    for item in archive.namelist()
                    if item.upper().endswith("LO.DAT")
                ),
                None,
            )
            if name is None:
                return []
            text = archive.read(name).decode("utf-8", errors="replace")
    except zipfile.BadZipFile:
        return []

    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        parts = line.split("|")
        if not parts or parts[0] != "LO" or len(parts) < 27:
            continue
        latitude = _dms_to_decimal(
            parts[19], parts[20], parts[21], parts[22]
        )
        longitude = _dms_to_decimal(
            parts[23], parts[24], parts[25], parts[26]
        )
        if latitude is None or longitude is None:
            continue
        records.append(
            {
                "id": parts[1],
                "callsign": parts[4],
                "latitude": latitude,
                "longitude": longitude,
            }
        )
    return records


def replace_gwbl_from_zip(
    db: Session,
    body: bytes,
    *,
    profile: BandProfile | None = None,
    provider: FederalDataProvider | None = None,
) -> None:
    active = resolve_optional(profile)
    if not active.protection.fss_gwbl:
        return
    data = _data_provider(db, provider)
    data.replace("gwbl", parse_gwbl_zip(body))
    bump_sync_meta(db, "gwbl", profile=active, provider=data)


def _fss_lat_lon_freq(
    record: dict[str, Any],
) -> tuple[float, float, int, int] | None:
    deployment = (record.get("record") or record).get("deploymentParam") or []
    if not deployment:
        return None
    installation = deployment[0].get("installationParam") or {}
    frequency = (
        deployment[0].get("operationParam") or {}
    ).get("operationFrequencyRange") or {}
    try:
        return (
            float(installation["latitude"]),
            float(installation["longitude"]),
            int(frequency["lowFrequency"]),
            int(frequency["highFrequency"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _cbsd_lat_lon(cbsd: Cbsd | None) -> tuple[float, float] | None:
    if cbsd is None:
        return None
    try:
        registration = json.loads(cbsd.registration_json or "{}")
        installation = registration.get("installationParam") or {}
        return (
            float(installation["latitude"]),
            float(installation["longitude"]),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def _grant_meta(grant: Grant) -> dict[str, Any]:
    try:
        value = json.loads(grant.grant_json or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def fss_sites_near(
    db: Session,
    lat: float,
    lon: float,
    *,
    profile: BandProfile | None = None,
    max_km: float | None = None,
    provider: FederalDataProvider | None = None,
    distance_provider: DistanceProvider | None = None,
) -> list[dict[str, Any]]:
    active = resolve_optional(profile)
    if not active.protection.fss_gwbl:
        return []
    radius_km = active.fss_protection_km if max_km is None else max_km
    data = _data_provider(db, provider)
    distance = _distance_provider(distance_provider)
    nearby: list[dict[str, Any]] = []
    for record in data.load("fss"):
        info = _fss_lat_lon_freq(record)
        if info is None:
            continue
        fss_lat, fss_lon, _, _ = info
        if distance.distance_m(lat, lon, fss_lat, fss_lon) <= radius_km * 1000:
            nearby.append(record)
    return nearby


def fss_has_neighboring_gwbl(
    db: Session,
    fss: dict[str, Any],
    *,
    profile: BandProfile | None = None,
    provider: FederalDataProvider | None = None,
    distance_provider: DistanceProvider | None = None,
) -> bool:
    active = resolve_optional(profile)
    if not active.protection.fss_gwbl:
        return False
    info = _fss_lat_lon_freq(fss)
    if info is None:
        return False
    fss_lat, fss_lon, _, _ = info
    data = _data_provider(db, provider)
    distance = _distance_provider(distance_provider)
    for record in data.load("gwbl"):
        try:
            latitude = float(record["latitude"])
            longitude = float(record["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        if (
            distance.distance_m(
                fss_lat, fss_lon, latitude, longitude
            )
            <= active.fss_protection_km * 1000
        ):
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
    provider: FederalDataProvider | None = None,
    distance_provider: DistanceProvider | None = None,
) -> bool:
    active = resolve_optional(profile)
    policy = active.fss_gwbl_policy
    if not active.protection.fss_gwbl or policy is None:
        return False
    if not _overlaps(low_hz, high_hz, policy.low_hz, policy.high_hz):
        return False
    data = _data_provider(db, provider)
    if not data.load("gwbl"):
        return False
    nearby = fss_sites_near(
        db,
        lat,
        lon,
        profile=active,
        provider=data,
        distance_provider=distance_provider,
    )
    return any(
        fss_has_neighboring_gwbl(
            db,
            site,
            profile=active,
            provider=data,
            distance_provider=distance_provider,
        )
        for site in nearby
    )


ExclusionChecker = Callable[
    [Session, float, float, int, int, BandProfile], bool
]


def _default_exclusion_checker(
    db: Session,
    lat: float,
    lon: float,
    low: int,
    high: int,
    profile: BandProfile,
) -> bool:
    from services.exclusion_zone_service import point_hits_exclusion_zone

    return point_hits_exclusion_zone(
        db, lat, lon, low, high, profile=profile
    )


def _fss_protects_grant(
    nearby: list[dict[str, Any]],
    low: int,
    high: int,
    profile: BandProfile,
) -> bool:
    policy = profile.fss_gwbl_policy
    for record in nearby:
        info = _fss_lat_lon_freq(record)
        if info is None:
            continue
        _, _, fss_low, fss_high = info
        if policy and _overlaps(low, high, policy.low_hz, policy.high_hz):
            return True
        if _overlaps(low, high, fss_low, fss_high):
            return True
    return False


def heartbeat_federal_code(
    db: Session,
    cbsd: Cbsd | None,
    grant: Grant,
    *,
    profile: BandProfile | None = None,
    provider: FederalDataProvider | None = None,
    distance_provider: DistanceProvider | None = None,
    exclusion_checker: ExclusionChecker | None = None,
) -> int | None:
    """Return 500/501 when an enabled synchronized protection applies."""
    active = resolve_optional(profile)
    protections = active.protection
    if not (
        protections.exclusion_zones
        or protections.dpa
        or protections.fss_gwbl
    ):
        return None
    location = _cbsd_lat_lon(cbsd)
    if location is None:
        return None
    lat, lon = location
    data = _data_provider(db, provider)
    meta = data.sync_meta()
    grant_meta = _grant_meta(grant)
    low, high = grant.low_frequency, grant.high_frequency

    checker = exclusion_checker or _default_exclusion_checker
    if (
        protections.exclusion_zones
        and meta.get("exz", 0) > 0
        and checker(db, lat, lon, low, high, active)
    ):
        generation = int(grant_meta.get("exz_gen") or 0)
        return 500 if generation < meta["exz"] else 501

    dpa = active.dpa_policy
    if (
        protections.dpa
        and dpa is not None
        and meta.get("dpa", 0) > 0
        and _overlaps(low, high, dpa.low_hz, dpa.high_hz)
    ):
        generation = int(grant_meta.get("dpa_gen") or 0)
        return 500 if generation < meta["dpa"] else 501

    if not protections.fss_gwbl:
        return None
    nearby = fss_sites_near(
        db,
        lat,
        lon,
        profile=active,
        provider=data,
        distance_provider=distance_provider,
    )
    if not nearby or not _fss_protects_grant(nearby, low, high, active):
        return None

    if meta.get("gwbl", 0) > 0:
        has_neighbor = any(
            fss_has_neighboring_gwbl(
                db,
                site,
                profile=active,
                provider=data,
                distance_provider=distance_provider,
            )
            for site in nearby
        )
        if has_neighbor:
            grant_generation = max(
                int(grant_meta.get("fss_gen") or 0),
                int(grant_meta.get("gwbl_gen") or 0),
            )
            current_generation = max(
                meta.get("fss", 0), meta.get("gwbl", 0)
            )
            if grant_generation < current_generation:
                return 500

    grant_generation = int(grant_meta.get("fss_gen") or 0)
    if grant_generation < meta.get("fss", 0):
        return 500
    return 501
