"""Unit tests for profile-driven federal data adapters and protections."""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from domain.band_profile import cbrs_winnforum_profile
from services.federal_db_service import (
    HaversineDistanceProvider,
    SqlAlchemyFederalDataProvider,
    bump_sync_meta,
    federal_fss_site_to_record,
    fss_has_neighboring_gwbl,
    fss_sites_near,
    get_sync_meta,
    grant_blocked_by_fss_gwbl,
    grant_sync_stamp,
    heartbeat_federal_code,
    parse_gwbl_zip,
    replace_fss_from_federal_payload,
    replace_gwbl_from_zip,
)


class FakeDataProvider:
    def __init__(self) -> None:
        self.records: dict[str, list[dict]] = {}
        self.meta = {"fss": 0, "gwbl": 0, "exz": 0, "dpa": 0}
        self.replacements: list[tuple[str, list[dict]]] = []

    def load(self, kind: str) -> list[dict]:
        return list(self.records.get(kind, []))

    def replace(self, kind: str, records: list[dict]) -> None:
        self.records[kind] = list(records)
        self.replacements.append((kind, list(records)))

    def sync_meta(self) -> dict[str, int]:
        return dict(self.meta)

    def set_sync_meta(self, meta: dict[str, int]) -> None:
        self.meta = dict(meta)


class FakeDistanceProvider:
    def __init__(self, distance: float) -> None:
        self.distance = distance
        self.calls = 0

    def distance_m(self, *args: float) -> float:
        self.calls += 1
        return self.distance


def fss_record(
    *,
    latitude: float = 10.0,
    longitude: float = 20.0,
    low: int = 3_650_000_000,
    high: int = 3_700_000_000,
) -> dict:
    return {
        "record": {
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
            ]
        }
    }


def cbsd(latitude: float = 10.0, longitude: float = 20.0):
    return SimpleNamespace(
        registration_json=json.dumps(
            {
                "installationParam": {
                    "latitude": latitude,
                    "longitude": longitude,
                }
            }
        )
    )


def grant(
    *,
    low: int = 3_650_000_000,
    high: int = 3_660_000_000,
    metadata: dict | None = None,
):
    return SimpleNamespace(
        low_frequency=low,
        high_frequency=high,
        grant_json=json.dumps(metadata or {}),
    )


def profile_with_all_federal_protections_disabled():
    profile = cbrs_winnforum_profile()
    return replace(
        profile,
        protection=replace(
            profile.protection,
            fss_gwbl=False,
            exclusion_zones=False,
            dpa=False,
        ),
        fss_gwbl_policy=None,
        ntia_exclusion_policy=None,
        dpa_policy=None,
    )


def valid_site() -> dict:
    return {
        "earth_station_latitude_decimal": "10.5",
        "earth_station_longitude_decimal": "-20.25",
        "lower_frequency": "3,650",
        "upper_frequency": "3700",
        "tracking_telemetry_control": "yes",
        "FSS_number": "site-1",
    }


def make_zip(lines: list[str], *, filename: str = "LO.dat") -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(filename, "\n".join(lines))
    return output.getvalue()


def lo_line(*, valid_coordinates: bool = True) -> str:
    parts = [""] * 27
    parts[0] = "LO"
    parts[1] = "location-1"
    parts[4] = "CALL"
    parts[19:23] = ["10", "30", "0", "N"]
    parts[23:27] = (
        ["20", "15", "0", "W"]
        if valid_coordinates
        else ["bad", "15", "0", "W"]
    )
    return "|".join(parts)


def test_fss_conversion_accepts_valid_and_rejects_invalid_sites() -> None:
    record = federal_fss_site_to_record(valid_site())
    assert record is not None
    deployment = record["record"]["deploymentParam"][0]
    assert deployment["installationParam"]["latitude"] == 10.5
    assert (
        deployment["operationParam"]["operationFrequencyRange"]["lowFrequency"]
        == 3_650_000_000
    )
    assert record["ttc"] is True

    assert federal_fss_site_to_record({}) is None
    invalid = valid_site()
    invalid["lower_frequency"] = "invalid"
    assert federal_fss_site_to_record(invalid) is None


def test_replace_fss_uses_provider_and_profile_gate() -> None:
    provider = FakeDataProvider()
    db = MagicMock()
    replace_fss_from_federal_payload(
        db,
        {"result": [valid_site(), {}, "ignored"]},
        profile=cbrs_winnforum_profile(),
        provider=provider,
    )
    assert len(provider.records["fss"]) == 1
    assert provider.meta["fss"] == 1

    disabled = profile_with_all_federal_protections_disabled()
    untouched = FakeDataProvider()
    replace_fss_from_federal_payload(
        db, [valid_site()], profile=disabled, provider=untouched
    )
    assert untouched.replacements == []


def test_parse_and_replace_gwbl_zip() -> None:
    body = make_zip([lo_line(), "BAD|short", lo_line(valid_coordinates=False)])
    records = parse_gwbl_zip(body)
    assert records == [
        {
            "id": "location-1",
            "callsign": "CALL",
            "latitude": 10.5,
            "longitude": -20.25,
        }
    ]
    assert parse_gwbl_zip(b"not-a-zip") == []
    assert parse_gwbl_zip(make_zip(["anything"], filename="OTHER.dat")) == []

    provider = FakeDataProvider()
    replace_gwbl_from_zip(
        MagicMock(),
        body,
        profile=cbrs_winnforum_profile(),
        provider=provider,
    )
    assert provider.records["gwbl"] == records
    assert provider.meta["gwbl"] == 1

    disabled = FakeDataProvider()
    replace_gwbl_from_zip(
        MagicMock(),
        body,
        profile=profile_with_all_federal_protections_disabled(),
        provider=disabled,
    )
    assert disabled.replacements == []


def test_sync_metadata_respects_profile_flags() -> None:
    provider = FakeDataProvider()
    db = MagicMock()
    assert bump_sync_meta(
        db,
        "fss",
        profile=cbrs_winnforum_profile(),
        provider=provider,
    ) == 1
    assert get_sync_meta(db, provider=provider)["fss"] == 1
    assert grant_sync_stamp(db, provider=provider)["fss"] == 1

    disabled = profile_with_all_federal_protections_disabled()
    assert bump_sync_meta(
        db, "dpa", profile=disabled, provider=provider
    ) == 0
    assert bump_sync_meta(
        db, "unknown", profile=cbrs_winnforum_profile(), provider=provider
    ) == 0


def test_nearby_and_neighbor_queries_use_injected_distance() -> None:
    provider = FakeDataProvider()
    provider.records["fss"] = [fss_record(), {"invalid": True}]
    provider.records["gwbl"] = [
        {"latitude": 10.1, "longitude": 20.1},
        {"latitude": "bad"},
    ]
    nearby_distance = FakeDistanceProvider(100)
    far_distance = FakeDistanceProvider(999_999)
    profile = cbrs_winnforum_profile()

    assert fss_sites_near(
        MagicMock(),
        10,
        20,
        profile=profile,
        provider=provider,
        distance_provider=nearby_distance,
    ) == [provider.records["fss"][0]]
    assert fss_sites_near(
        MagicMock(),
        10,
        20,
        profile=profile,
        max_km=1,
        provider=provider,
        distance_provider=far_distance,
    ) == []
    assert fss_has_neighboring_gwbl(
        MagicMock(),
        provider.records["fss"][0],
        profile=profile,
        provider=provider,
        distance_provider=nearby_distance,
    )
    assert not fss_has_neighboring_gwbl(
        MagicMock(),
        {"invalid": True},
        profile=profile,
        provider=provider,
        distance_provider=nearby_distance,
    )

    disabled = profile_with_all_federal_protections_disabled()
    assert fss_sites_near(
        MagicMock(), 10, 20, profile=disabled, provider=provider
    ) == []
    assert not fss_has_neighboring_gwbl(
        MagicMock(), fss_record(), profile=disabled, provider=provider
    )


@pytest.mark.parametrize(
    ("frequency", "has_gwbl", "distance", "expected"),
    [
        ((3_650_000_000, 3_660_000_000), True, 100, True),
        ((3_550_000_000, 3_560_000_000), True, 100, False),
        ((3_650_000_000, 3_660_000_000), False, 100, False),
        ((3_650_000_000, 3_660_000_000), True, 999_999, False),
    ],
)
def test_grant_blocking_strategy(
    frequency, has_gwbl, distance, expected
) -> None:
    provider = FakeDataProvider()
    provider.records["fss"] = [fss_record()]
    provider.records["gwbl"] = (
        [{"latitude": 10.1, "longitude": 20.1}] if has_gwbl else []
    )
    assert (
        grant_blocked_by_fss_gwbl(
            MagicMock(),
            10,
            20,
            frequency[0],
            frequency[1],
            profile=cbrs_winnforum_profile(),
            provider=provider,
            distance_provider=FakeDistanceProvider(distance),
        )
        is expected
    )


def test_heartbeat_bypasses_before_data_access_when_disabled() -> None:
    provider = MagicMock()
    assert (
        heartbeat_federal_code(
            MagicMock(),
            cbsd(),
            grant(),
            profile=profile_with_all_federal_protections_disabled(),
            provider=provider,
        )
        is None
    )
    provider.sync_meta.assert_not_called()
    assert (
        heartbeat_federal_code(
            MagicMock(),
            SimpleNamespace(registration_json="invalid"),
            grant(),
            profile=cbrs_winnforum_profile(),
            provider=FakeDataProvider(),
        )
        is None
    )


@pytest.mark.parametrize(
    ("grant_generation", "expected"),
    [(0, 500), (2, 501)],
)
def test_heartbeat_exclusion_generation(
    grant_generation: int, expected: int
) -> None:
    provider = FakeDataProvider()
    provider.meta["exz"] = 2
    code = heartbeat_federal_code(
        MagicMock(),
        cbsd(),
        grant(metadata={"exz_gen": grant_generation}),
        profile=cbrs_winnforum_profile(),
        provider=provider,
        exclusion_checker=lambda *args: True,
    )
    assert code == expected


@pytest.mark.parametrize(
    ("grant_generation", "expected"),
    [(0, 500), (2, 501)],
)
def test_heartbeat_dpa_generation(
    grant_generation: int, expected: int
) -> None:
    profile = cbrs_winnforum_profile()
    profile = replace(
        profile,
        protection=replace(profile.protection, exclusion_zones=False),
    )
    provider = FakeDataProvider()
    provider.meta["dpa"] = 2
    code = heartbeat_federal_code(
        MagicMock(),
        cbsd(),
        grant(
            low=3_550_000_000,
            high=3_560_000_000,
            metadata={"dpa_gen": grant_generation},
        ),
        profile=profile,
        provider=provider,
    )
    assert code == expected


def test_heartbeat_fss_generations_and_current_state() -> None:
    profile = cbrs_winnforum_profile()
    profile = replace(
        profile,
        protection=replace(
            profile.protection, exclusion_zones=False, dpa=False
        ),
    )
    provider = FakeDataProvider()
    provider.records["fss"] = [fss_record()]
    provider.records["gwbl"] = [{"latitude": 10.1, "longitude": 20.1}]
    provider.meta.update({"fss": 2, "gwbl": 2})
    distance = FakeDistanceProvider(100)

    stale = heartbeat_federal_code(
        MagicMock(),
        cbsd(),
        grant(metadata={"fss_gen": 1, "gwbl_gen": 1}),
        profile=profile,
        provider=provider,
        distance_provider=distance,
    )
    current = heartbeat_federal_code(
        MagicMock(),
        cbsd(),
        grant(metadata={"fss_gen": 2, "gwbl_gen": 2}),
        profile=profile,
        provider=provider,
        distance_provider=distance,
    )
    assert stale == 500
    assert current == 501

    provider.records["fss"] = []
    assert (
        heartbeat_federal_code(
            MagicMock(),
            cbsd(),
            grant(),
            profile=profile,
            provider=provider,
            distance_provider=distance,
        )
        is None
    )


def test_sqlalchemy_provider_load_replace_and_meta() -> None:
    session = MagicMock()
    query = session.query.return_value
    query.filter_by.return_value.all.return_value = [
        SimpleNamespace(data_json='{"fss": 3}'),
        SimpleNamespace(data_json="invalid"),
    ]
    provider = SqlAlchemyFederalDataProvider(session)
    assert provider.load("federal_sync_meta") == [{"fss": 3}]
    assert provider.sync_meta() == {
        "fss": 3,
        "gwbl": 0,
        "exz": 0,
        "dpa": 0,
    }

    provider.replace("fss", [{"record": {"id": "x"}}])
    query.filter_by.return_value.delete.assert_called()
    session.add.assert_called()
    provider.set_sync_meta({"fss": 1, "gwbl": 0, "exz": 0, "dpa": 0})


def test_haversine_distance_adapter() -> None:
    assert HaversineDistanceProvider().distance_m(0, 0, 0, 0) == 0
