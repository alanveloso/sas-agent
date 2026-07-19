"""Unit tests for the profile-driven grant validation pipeline."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from domain.band_profile import br_anatel_privativa_profile, cbrs_winnforum_profile
from services.authorization_service import SOURCE_TYPE_GAA, SOURCE_TYPE_PAL
from services.grant_service import GrantService
from services.grant_validation import (
    BLACKLISTED,
    GRANT_CONFLICT,
    INTERFERENCE,
    INVALID_PARAM,
    MISSING_PARAM,
    UNSUPPORTED_SPECTRUM,
    GrantContext,
    GrantValidator,
    resolve_channel,
)


class FakeAuthSource:
    def __init__(
        self,
        *,
        source_type: str = SOURCE_TYPE_GAA,
        source_id: str = "FCC_PART_96_GAA",
        expires_at: datetime | None = None,
    ) -> None:
        self.source_type = source_type
        self.source_id = source_id
        self.expires_at = expires_at


class FakeGrantStore:
    def __init__(self) -> None:
        self.blacklisted = False
        self.device_eirp: float | None = 47.0
        self.require_measurements = False
        self.meas_error: int | None = None
        self.cert_mismatch = False
        self.exclusion_hit = False
        self.fss_blocked = False
        self.border_violated = False
        self.peer_grant = False
        self.pal_auth_ok = True
        self.gaa_auth_ok = True
        self.active: list[Any] = []
        self.pals: list[dict[str, Any]] = []
        self.zones: list[dict[str, Any]] = []
        self.saved: list[GrantContext] = []
        self.commit_count = 0
        self.gaa = FakeAuthSource()
        self.cbsd = SimpleNamespace(
            id=1,
            cbsd_id="device_1/serial_1",
            fcc_id="device_1",
            user_id="operator_1",
            cbsd_category="A",
            registration_json=(
                '{"cbsdCategory":"A","installationParam":'
                '{"latitude":37.0,"longitude":-122.0,"height":6.0,'
                '"heightType":"AGL","indoorDeployment":true,"eirpCapability":30},'
                '"measCapability":["RECEIVED_POWER_WITHOUT_GRANT"]}'
            ),
            certificate_hash=None,
        )

    def find_cbsd(self, cbsd_id: str) -> Any | None:
        if cbsd_id == self.cbsd.cbsd_id:
            return self.cbsd
        return None

    def is_device_blacklisted(self, device_id: str) -> bool:
        return self.blacklisted

    def device_max_eirp(self, device_id: str) -> float | None:
        return self.device_eirp

    def measurements_required(self) -> bool:
        return self.require_measurements

    def active_grants(self, cbsd_id: str) -> list[Any]:
        return list(self.active)

    def load_pal_records(self) -> list[dict[str, Any]]:
        return list(self.pals)

    def load_zone_records(self) -> list[dict[str, Any]]:
        return list(self.zones)

    def ensure_gaa_authorization(self) -> FakeAuthSource:
        return self.gaa

    def authorization_valid(
        self, source_type: str, source_id: str
    ) -> tuple[bool, FakeAuthSource | None]:
        if source_type == SOURCE_TYPE_PAL:
            if not self.pal_auth_ok:
                return False, None
            return True, FakeAuthSource(
                source_type=SOURCE_TYPE_PAL,
                source_id=source_id,
                expires_at=datetime.utcnow() + timedelta(days=30),
            )
        if not self.gaa_auth_ok:
            return False, None
        return True, self.gaa

    def hits_exclusion_zone(
        self, lat: float, lon: float, low: int, high: int
    ) -> bool:
        return self.exclusion_hit

    def blocked_by_fss_gwbl(
        self, lat: float, lon: float, low: int, high: int
    ) -> bool:
        return self.fss_blocked

    def violates_border_pfd(
        self,
        installation: dict[str, Any],
        max_eirp: float,
        low: int,
        high: int,
    ) -> bool:
        return self.border_violated

    def peer_has_grant(self, cbsd: Any) -> bool:
        return self.peer_grant

    def certificate_mismatch(
        self, cbsd: Any, certificate_hash: str | None
    ) -> bool:
        return self.cert_mismatch

    def validate_meas_report(self, meas_report: Any | None) -> int | None:
        return self.meas_error

    def grant_sync_stamp(self) -> dict[str, int]:
        return {"fss": 0, "gwbl": 0, "exz": 0, "dpa": 0}

    def ensure_default_gaa(self) -> None:
        return None

    def save_grant(self, context: GrantContext) -> dict[str, Any]:
        self.saved.append(context)
        return {
            "cbsdId": context.cbsd_id,
            "grantId": "grant/test",
            "grantExpireTime": "2099-01-01T00:00:00Z",
            "heartbeatInterval": context.profile.heartbeat_interval_sec,
            "channelType": context.channel_type,
            "response": {"responseCode": 0},
        }

    def commit(self) -> None:
        self.commit_count += 1


def grant_request(**overrides: Any) -> dict[str, Any]:
    request = {
        "cbsdId": "device_1/serial_1",
        "operationParam": {
            "maxEirp": 20.0,
            "operationFrequencyRange": {
                "lowFrequency": 3_550_000_000,
                "highFrequency": 3_560_000_000,
            },
        },
    }
    request.update(overrides)
    return request


def anatel_grant_request(**overrides: Any) -> dict[str, Any]:
    request = grant_request(
        operationParam={
            "maxEirp": 5.0,
            "operationFrequencyRange": {
                "lowFrequency": 3_700_000_000,
                "highFrequency": 3_710_000_000,
            },
        }
    )
    request.update(overrides)
    return request


def test_missing_cbsd_id_returns_102_without_echo() -> None:
    response = GrantService(
        cbrs_winnforum_profile(), FakeGrantStore()
    ).process([{}])[0]
    assert response == {"response": {"responseCode": MISSING_PARAM}}


def test_unknown_cbsd_returns_103_without_echo() -> None:
    response = GrantService(
        cbrs_winnforum_profile(), FakeGrantStore()
    ).process([grant_request(cbsdId="missing/serial")])[0]
    assert response == {"response": {"responseCode": INVALID_PARAM}}


def test_certificate_mismatch_returns_103_without_echo() -> None:
    store = FakeGrantStore()
    store.cert_mismatch = True
    response = GrantService(cbrs_winnforum_profile(), store).process(
        [grant_request()]
    )[0]
    assert response == {"response": {"responseCode": INVALID_PARAM}}


def test_blacklist_returns_101_with_cbsd_id() -> None:
    store = FakeGrantStore()
    store.blacklisted = True
    response = GrantService(cbrs_winnforum_profile(), store).process(
        [grant_request()]
    )[0]
    assert response["response"]["responseCode"] == BLACKLISTED
    assert response["cbsdId"] == "device_1/serial_1"


def test_frequency_outside_profile_band_returns_300() -> None:
    response = GrantService(
        cbrs_winnforum_profile(), FakeGrantStore()
    ).process(
        [
            grant_request(
                operationParam={
                    "maxEirp": 20.0,
                    "operationFrequencyRange": {
                        "lowFrequency": 3_700_000_000,
                        "highFrequency": 3_710_000_000,
                    },
                }
            )
        ]
    )[0]
    assert response["response"]["responseCode"] == UNSUPPORTED_SPECTRUM


def test_eirp_above_profile_limit_returns_103() -> None:
    response = GrantService(
        cbrs_winnforum_profile(), FakeGrantStore()
    ).process(
        [
            grant_request(
                operationParam={
                    "maxEirp": 40.0,
                    "operationFrequencyRange": {
                        "lowFrequency": 3_550_000_000,
                        "highFrequency": 3_560_000_000,
                    },
                }
            )
        ]
    )[0]
    assert response["response"]["responseCode"] == INVALID_PARAM


def test_anatel_profile_skips_pal_gaa_and_issues_gaa() -> None:
    store = FakeGrantStore()
    response = GrantService(br_anatel_privativa_profile(), store).process(
        [anatel_grant_request()]
    )[0]
    assert response["response"]["responseCode"] == 0
    assert response["channelType"] == SOURCE_TYPE_GAA
    assert store.saved[0].channel_type == SOURCE_TYPE_GAA


def test_protection_flags_drive_interference_checks() -> None:
    store = FakeGrantStore()
    store.exclusion_hit = True
    response = GrantService(cbrs_winnforum_profile(), store).process(
        [grant_request()]
    )[0]
    assert response["response"]["responseCode"] == INTERFERENCE

    store = FakeGrantStore()
    store.exclusion_hit = True
    response = GrantService(br_anatel_privativa_profile(), store).process(
        [anatel_grant_request()]
    )[0]
    # Anatel keeps exclusion_zones=True by default.
    assert response["response"]["responseCode"] == INTERFERENCE


def test_batch_conflict_uses_pending_ranges() -> None:
    store = FakeGrantStore()
    responses = GrantService(cbrs_winnforum_profile(), store).process(
        [grant_request(), grant_request()]
    )
    assert responses[0]["response"]["responseCode"] == 0
    assert responses[1]["response"]["responseCode"] == GRANT_CONFLICT


def test_peer_grant_conflict() -> None:
    store = FakeGrantStore()
    store.peer_grant = True
    response = GrantService(cbrs_winnforum_profile(), store).process(
        [grant_request()]
    )[0]
    assert response["response"]["responseCode"] == GRANT_CONFLICT


def test_meas_report_gate_is_honored() -> None:
    store = FakeGrantStore()
    store.require_measurements = True
    store.meas_error = MISSING_PARAM
    response = GrantService(cbrs_winnforum_profile(), store).process(
        [grant_request()]
    )[0]
    assert response["response"]["responseCode"] == MISSING_PARAM


def test_validator_pipeline_is_injectable() -> None:
    class RejectAll:
        def validate(self, context: GrantContext) -> int:
            return INVALID_PARAM

    store = FakeGrantStore()
    response = GrantService(
        cbrs_winnforum_profile(),
        store,
        GrantValidator((RejectAll(),)),
    ).process([grant_request()])[0]
    assert response["response"]["responseCode"] == INVALID_PARAM
    assert store.saved == []
    assert store.commit_count == 1


def test_resolve_channel_rejects_partial_pal_overlap_for_cluster() -> None:
    contexts = [
        {
            "pal_id": "pal/1",
            "low": 3_550_000_000,
            "high": 3_560_000_000,
            "in_cluster": True,
            "in_ppa": True,
            "license_exp": (datetime.utcnow() + timedelta(days=10)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "user_id": "operator_1",
        }
    ]
    error, *_ = resolve_channel(
        contexts,
        3_555_000_000,
        3_565_000_000,
        cbsd_user_id="operator_1",
    )
    assert error == INVALID_PARAM


def test_resolve_channel_marks_unauthorized_pal_as_interference() -> None:
    contexts = [
        {
            "pal_id": "pal/1",
            "low": 3_550_000_000,
            "high": 3_560_000_000,
            "in_cluster": False,
            "in_ppa": True,
            "license_exp": (datetime.utcnow() + timedelta(days=10)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "user_id": "someone_else",
        }
    ]
    error, *_ = resolve_channel(
        contexts,
        3_550_000_000,
        3_560_000_000,
        cbsd_user_id="operator_1",
    )
    assert error == INTERFERENCE
