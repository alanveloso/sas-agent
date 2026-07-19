"""Unit tests for the profile-driven registration validation pipeline."""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from domain.band_profile import br_anatel_privativa_profile, cbrs_winnforum_profile
from services.registration_service import RegistrationService
from services.registration_validation import (
    BLACKLISTED,
    INVALID_PARAM,
    MISSING_PARAM,
    PENDING,
    RegistrationContext,
    RegistrationValidator,
    default_registration_validator,
    merge_registration,
)


class FakeRegistrationStore:
    def __init__(self) -> None:
        self.blacklisted = False
        self.device_authorized = True
        self.user_authorized = True
        self.installer_authorized = True
        self.max_eirp = 47.0
        self.measurements_enabled = False
        self.saved: list[RegistrationContext] = []
        self.commit_count = 0

    def is_device_blacklisted(self, device_id: str) -> bool:
        return self.blacklisted

    def device_authorization(self, device_id: str) -> tuple[bool, float | None]:
        return self.device_authorized, self.max_eirp

    def user_is_authorized(self, user_id: str) -> bool:
        return self.user_authorized

    def installer_is_authorized(self, installer_id: str) -> bool:
        return self.installer_authorized

    def conditionals(
        self, device_id: str, serial_number: str
    ) -> dict[str, Any]:
        return {}

    def save_registration(
        self,
        context: RegistrationContext,
        certificate_hash: str | None,
    ) -> str:
        self.saved.append(context)
        return f"{context.device_id}/{context.serial_number}"

    def registration_measurements_enabled(self) -> bool:
        return self.measurements_enabled

    def commit(self) -> None:
        self.commit_count += 1


def registration_request(**overrides: Any) -> dict[str, Any]:
    request = {
        "userId": "operator_1",
        "fccId": "device_1",
        "cbsdSerialNumber": "serial_1",
        "cbsdCategory": "A",
        "airInterface": {"radioTechnology": "E_UTRA"},
        "installationParam": {
            "latitude": -15.8,
            "longitude": -47.9,
            "height": 4.0,
            "heightType": "AGL",
            "indoorDeployment": True,
            "eirpCapability": 20.0,
        },
    }
    request.update(overrides)
    return request


def signed_data(payload: dict[str, Any]) -> dict[str, str]:
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    return {
        "digitalSignature": "test-signature",
        "encodedCpiSignedData": encoded.rstrip("="),
        "protectedHeader": "test-header",
    }


@pytest.mark.parametrize("field", ["userId", "fccId", "cbsdSerialNumber"])
def test_required_interface_fields_return_102(field: str) -> None:
    store = FakeRegistrationStore()
    request = registration_request()
    request.pop(field)

    response = RegistrationService(
        cbrs_winnforum_profile(), store
    ).process([request])[0]

    assert response["response"]["responseCode"] == MISSING_PARAM
    assert store.saved == []


def test_cbrs_profile_preserves_blacklist_gate() -> None:
    store = FakeRegistrationStore()
    store.blacklisted = True

    response = RegistrationService(
        cbrs_winnforum_profile(), store
    ).process([registration_request()])[0]

    assert response["response"]["responseCode"] == BLACKLISTED


def test_non_legacy_profile_does_not_require_fcc_registry() -> None:
    store = FakeRegistrationStore()
    store.blacklisted = True
    store.device_authorized = False
    store.user_authorized = False

    response = RegistrationService(
        br_anatel_privativa_profile(), store
    ).process([registration_request()])[0]

    assert response["response"]["responseCode"] == 0
    assert response["cbsdId"] == "device_1/serial_1"


def test_eirp_limit_comes_from_active_profile() -> None:
    store = FakeRegistrationStore()
    installation = registration_request()["installationParam"]
    installation["eirpCapability"] = 48.0

    response = RegistrationService(
        br_anatel_privativa_profile(), store
    ).process([registration_request(installationParam=installation)])[0]

    assert response["response"]["responseCode"] == INVALID_PARAM


def test_incomplete_installation_returns_pending() -> None:
    store = FakeRegistrationStore()
    request = registration_request(installationParam={"latitude": -15.8})

    response = RegistrationService(
        cbrs_winnforum_profile(), store
    ).process([request])[0]

    assert response["response"]["responseCode"] == PENDING


def test_invalid_coordinates_are_rejected_by_independent_rule() -> None:
    store = FakeRegistrationStore()
    installation = registration_request()["installationParam"]
    installation["latitude"] = 91

    response = RegistrationService(
        cbrs_winnforum_profile(), store
    ).process([registration_request(installationParam=installation)])[0]

    assert response["response"]["responseCode"] == INVALID_PARAM


@pytest.mark.parametrize(
    ("capabilities", "expected"),
    [
        ("RECEIVED_POWER_WITHOUT_GRANT", INVALID_PARAM),
        (["UNKNOWN_MEASUREMENT"], INVALID_PARAM),
        (["RECEIVED_POWER_WITH_GRANT"], 0),
    ],
)
def test_measurement_capabilities_are_validated(
    capabilities: Any, expected: int
) -> None:
    store = FakeRegistrationStore()
    request = registration_request(measCapability=capabilities)

    response = RegistrationService(
        cbrs_winnforum_profile(), store
    ).process([request])[0]

    assert response["response"]["responseCode"] == expected


@pytest.mark.parametrize(
    "installation",
    [
        {
            **registration_request()["installationParam"],
            "heightType": "INVALID",
        },
        {
            **registration_request()["installationParam"],
            "longitude": "west",
        },
        {
            **registration_request()["installationParam"],
            "antennaAzimuth": 360,
        },
    ],
)
def test_installation_ranges_reject_invalid_values(
    installation: dict[str, Any],
) -> None:
    response = RegistrationService(
        cbrs_winnforum_profile(), FakeRegistrationStore()
    ).process([registration_request(installationParam=installation)])[0]

    assert response["response"]["responseCode"] == INVALID_PARAM


def test_cbrs_identity_registry_is_enforced() -> None:
    store = FakeRegistrationStore()
    store.device_authorized = False

    response = RegistrationService(
        cbrs_winnforum_profile(), store
    ).process([registration_request()])[0]

    assert response["response"]["responseCode"] == INVALID_PARAM


@pytest.mark.parametrize(
    "installation",
    [
        {
            **registration_request()["installationParam"],
            "indoorDeployment": False,
            "height": 7,
        },
        {
            **registration_request()["installationParam"],
            "latitude": 38.882162,
            "longitude": -77.113755,
            "indoorDeployment": False,
        },
    ],
)
def test_cbrs_outdoor_height_rules_are_preserved(
    installation: dict[str, Any],
) -> None:
    response = RegistrationService(
        cbrs_winnforum_profile(), FakeRegistrationStore()
    ).process([registration_request(installationParam=installation)])[0]

    assert response["response"]["responseCode"] == INVALID_PARAM


def test_category_b_cleartext_installation_is_rejected_for_cbrs() -> None:
    installation = {
        **registration_request()["installationParam"],
        "indoorDeployment": False,
        "antennaAzimuth": 0,
        "antennaGain": 0,
        "antennaBeamwidth": 60,
    }
    response = RegistrationService(
        cbrs_winnforum_profile(), FakeRegistrationStore()
    ).process(
        [
            registration_request(
                cbsdCategory="B",
                installationParam=installation,
            )
        ]
    )[0]

    assert response["response"]["responseCode"] == INVALID_PARAM


@pytest.mark.parametrize(
    "signature",
    [
        {"digitalSignature": "only-one-field"},
        {
            "digitalSignature": "x",
            "encodedCpiSignedData": "not-json",
            "protectedHeader": "x",
        },
        signed_data({"professionalInstallerData": {}}),
    ],
)
def test_cbrs_signature_structure_errors_are_protocol_responses(
    signature: dict[str, str],
) -> None:
    response = RegistrationService(
        cbrs_winnforum_profile(), FakeRegistrationStore()
    ).process([registration_request(cpiSignatureData=signature)])[0]

    assert response["response"]["responseCode"] in {MISSING_PARAM, INVALID_PARAM}


def test_signed_installation_is_merged_and_installer_is_checked() -> None:
    store = FakeRegistrationStore()
    store.installer_authorized = False
    payload = {
        "professionalInstallerData": {"cpiId": "installer-1"},
        "installationParam": registration_request()["installationParam"],
    }
    request = registration_request(
        installationParam=None,
        cpiSignatureData=signed_data(payload),
    )

    rejected = RegistrationService(
        cbrs_winnforum_profile(), store
    ).process([request])[0]
    store.installer_authorized = True
    accepted = RegistrationService(
        cbrs_winnforum_profile(), store
    ).process([request])[0]

    assert rejected["response"]["responseCode"] == INVALID_PARAM
    assert accepted["response"]["responseCode"] == 0


def test_merge_registration_recursively_prefers_request_and_signed_data() -> None:
    request = {
        "fccId": "request-device",
        "cbsdSerialNumber": "serial",
        "installationParam": {"height": 3},
        "cpiSignatureData": signed_data(
            {"installationParam": {"height": 6, "heightType": "AGL"}}
        ),
    }

    merged = merge_registration(
        request,
        {
            "fccId": "conditional-device",
            "installationParam": {"latitude": 1, "height": 1},
        },
    )

    assert merged["fccId"] == "request-device"
    assert merged["installationParam"] == {
        "latitude": 1,
        "height": 6,
        "heightType": "AGL",
    }


def test_quiet_zone_strategy_is_injected_into_pipeline() -> None:
    store = FakeRegistrationStore()
    validator = default_registration_validator(lambda installation: True)

    response = RegistrationService(
        cbrs_winnforum_profile(), store, validator
    ).process([registration_request()])[0]

    assert response["response"]["responseCode"] == INVALID_PARAM


def test_validator_pipeline_is_injectable() -> None:
    class RejectAll:
        def validate(self, context: RegistrationContext) -> int:
            return INVALID_PARAM

    store = FakeRegistrationStore()
    service = RegistrationService(
        br_anatel_privativa_profile(),
        store,
        RegistrationValidator((RejectAll(),)),
    )

    response = service.process([registration_request()])[0]

    assert response["response"]["responseCode"] == INVALID_PARAM
    assert store.saved == []
    assert store.commit_count == 1


def test_measurement_response_uses_injected_store_state() -> None:
    store = FakeRegistrationStore()
    store.measurements_enabled = True
    request = registration_request(
        measCapability=["RECEIVED_POWER_WITHOUT_GRANT"]
    )

    response = RegistrationService(
        br_anatel_privativa_profile(), store
    ).process([request])[0]

    assert response["measReportConfig"] == ["RECEIVED_POWER_WITHOUT_GRANT"]
