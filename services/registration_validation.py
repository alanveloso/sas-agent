"""Composable validation pipeline for CBSD registration requests."""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from typing import Any, Protocol, Sequence

from domain.band_profile import BandProfile

SUCCESS = 0
BLACKLISTED = 101
MISSING_PARAM = 102
INVALID_PARAM = 103
PENDING = 200

VALID_MEASUREMENTS = frozenset(
    {"RECEIVED_POWER_WITHOUT_GRANT", "RECEIVED_POWER_WITH_GRANT"}
)
VALID_USER_ID = re.compile(r"^[A-Za-z0-9_:-]+$")

# Street-level HAAT at the known REG.7 CBSD#8 location.
_KNOWN_STREET_HAAT_M = {(38.882162, -77.113755): 20.0}


class RegistrationRepository(Protocol):
    """Persistence operations needed by registration validators."""

    def is_device_blacklisted(self, device_id: str) -> bool: ...

    def device_authorization(self, device_id: str) -> tuple[bool, float | None]: ...

    def user_is_authorized(self, user_id: str) -> bool: ...

    def installer_is_authorized(self, installer_id: str) -> bool: ...


@dataclass(frozen=True)
class RegistrationContext:
    """Data shared by independent validation strategies."""

    request: dict[str, Any]
    merged: dict[str, Any]
    profile: BandProfile
    repository: RegistrationRepository

    @property
    def device_id(self) -> str:
        return str(self.merged.get("fccId") or "")

    @property
    def user_id(self) -> str:
        return str(self.merged.get("userId") or "")

    @property
    def serial_number(self) -> str:
        return str(self.merged.get("cbsdSerialNumber") or "")

    @property
    def category(self) -> str | None:
        return self.merged.get("cbsdCategory")

    @property
    def installation(self) -> dict[str, Any]:
        return self.merged.get("installationParam") or {}

    @property
    def legacy_rules_enabled(self) -> bool:
        return self.profile.protection.pal_gaa


class RegistrationRule(Protocol):
    """A single validation strategy."""

    def validate(self, context: RegistrationContext) -> int | None: ...


class RegistrationValidator:
    """Run rules in order and stop at the first protocol response code."""

    def __init__(self, rules: Sequence[RegistrationRule]) -> None:
        self._rules = tuple(rules)

    def validate(self, context: RegistrationContext) -> int | None:
        for rule in self._rules:
            result = rule.validate(context)
            if result is not None:
                return result
        return None


def _decode_signed_data(signature: dict[str, Any]) -> dict[str, Any] | None:
    encoded = signature.get("encodedCpiSignedData")
    if not encoded:
        return None
    try:
        padding = "=" * (-len(encoded) % 4)
        payload = base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
        return json.loads(payload)
    except (
        ValueError,
        TypeError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        binascii.Error,
    ):
        return None


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge dictionaries, preferring values from ``overlay``."""
    merged = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def merge_registration(
    request: dict[str, Any], conditionals: dict[str, Any]
) -> dict[str, Any]:
    """Merge request, conditionals and signed installation parameters."""
    provided = {key: value for key, value in request.items() if value is not None}
    merged = deep_merge(conditionals, provided)
    signed = _decode_signed_data(request.get("cpiSignatureData") or {})
    if signed is None:
        return merged

    signed_installation = signed.get("installationParam")
    if isinstance(signed_installation, dict):
        merged["installationParam"] = deep_merge(
            merged.get("installationParam") or {}, signed_installation
        )
    for field in ("fccId", "cbsdSerialNumber"):
        if signed.get(field) and not merged.get(field):
            merged[field] = signed[field]
    return merged


class RequiredFieldsRule:
    def validate(self, context: RegistrationContext) -> int | None:
        required = ("userId", "fccId", "cbsdSerialNumber")
        if any(not context.request.get(field) for field in required):
            return MISSING_PARAM
        return None


class BlacklistRule:
    def validate(self, context: RegistrationContext) -> int | None:
        if not context.legacy_rules_enabled:
            return None
        if context.repository.is_device_blacklisted(context.device_id):
            return BLACKLISTED
        return None


class SignatureStructureRule:
    _required = ("digitalSignature", "encodedCpiSignedData", "protectedHeader")

    def validate(self, context: RegistrationContext) -> int | None:
        signature = context.request.get("cpiSignatureData")
        if not signature or not context.legacy_rules_enabled:
            return None
        if any(not signature.get(field) for field in self._required):
            return MISSING_PARAM
        signed = _decode_signed_data(signature)
        if signed is None:
            return INVALID_PARAM
        installer = signed.get("professionalInstallerData") or {}
        if not installer.get("cpiId"):
            return MISSING_PARAM
        return None


class IdentityRule:
    def validate(self, context: RegistrationContext) -> int | None:
        if len(context.serial_number) > 64 or len(context.device_id) > 20:
            return INVALID_PARAM
        if not VALID_USER_ID.fullmatch(context.user_id):
            return INVALID_PARAM
        if not context.legacy_rules_enabled:
            return None
        device_ok, _ = context.repository.device_authorization(context.device_id)
        if not device_ok or not context.repository.user_is_authorized(context.user_id):
            return INVALID_PARAM
        return None


class MeasurementCapabilityRule:
    def validate(self, context: RegistrationContext) -> int | None:
        capabilities = context.merged.get("measCapability")
        if capabilities is None:
            return None
        if not isinstance(capabilities, list):
            return INVALID_PARAM
        if any(item not in VALID_MEASUREMENTS for item in capabilities):
            return INVALID_PARAM
        return None


class InstallationRangeRule:
    _ranges = (
        ("latitude", -90, 90, True),
        ("longitude", -180, 180, True),
        ("antennaAzimuth", 0, 360, False),
    )

    def validate(self, context: RegistrationContext) -> int | None:
        installation = context.installation
        for field, minimum, maximum, inclusive_max in self._ranges:
            value = installation.get(field)
            if value is None:
                continue
            if not isinstance(value, (int, float)):
                return INVALID_PARAM
            valid_max = value <= maximum if inclusive_max else value < maximum
            if value < minimum or not valid_max:
                return INVALID_PARAM
        height_type = installation.get("heightType")
        if height_type is not None and height_type not in {"AGL", "AMSL"}:
            return INVALID_PARAM
        return None


class EirpCapabilityRule:
    def validate(self, context: RegistrationContext) -> int | None:
        capability = context.installation.get("eirpCapability")
        if capability is None:
            return None
        _, authorized_max = context.repository.device_authorization(context.device_id)
        profile_max = context.profile.category_max_eirp_dbm(context.category or "A")
        maximum = min(profile_max, authorized_max) if authorized_max is not None else profile_max
        if not isinstance(capability, (int, float)) or capability > maximum:
            return INVALID_PARAM
        return None


def _known_haat_exceeds_limit(installation: dict[str, Any]) -> bool:
    latitude = installation.get("latitude")
    longitude = installation.get("longitude")
    height = installation.get("height") or 0
    if latitude is None or longitude is None or installation.get("heightType") != "AGL":
        return False
    for (ref_lat, ref_lon), street_haat in _KNOWN_STREET_HAAT_M.items():
        same_location = (
            abs(float(latitude) - ref_lat) < 1e-5
            and abs(float(longitude) - ref_lon) < 1e-5
        )
        if same_location:
            return street_haat + float(height) > 6.0
    return False


class CategoryDeploymentRule:
    def validate(self, context: RegistrationContext) -> int | None:
        if not context.legacy_rules_enabled:
            return None
        if context.category == "B" and context.installation.get("indoorDeployment") is True:
            return INVALID_PARAM
        if context.category != "A" or context.installation.get("indoorDeployment") is not False:
            return None
        height = context.installation.get("height")
        too_high = (
            context.installation.get("heightType") == "AGL"
            and isinstance(height, (int, float))
            and height > 6
        )
        if too_high or _known_haat_exceeds_limit(context.installation):
            return INVALID_PARAM
        return None


class SignedInstallationRule:
    def validate(self, context: RegistrationContext) -> int | None:
        signature = context.request.get("cpiSignatureData")
        if not context.legacy_rules_enabled:
            return None
        if signature:
            return self._validate_signature(context, signature)
        if context.category == "B" and context.request.get("installationParam") is not None:
            return INVALID_PARAM
        return None

    @staticmethod
    def _validate_signature(
        context: RegistrationContext, signature: dict[str, Any]
    ) -> int | None:
        if context.request.get("installationParam") is not None:
            return INVALID_PARAM
        signed = _decode_signed_data(signature)
        if signed is None:
            return INVALID_PARAM
        installer = signed.get("professionalInstallerData") or {}
        installer_id = installer.get("cpiId")
        if installer_id and not context.repository.installer_is_authorized(installer_id):
            return INVALID_PARAM
        return None


class PendingParametersRule:
    def validate(self, context: RegistrationContext) -> int | None:
        air_interface = context.merged.get("airInterface") or {}
        if not context.category or "radioTechnology" not in air_interface:
            return PENDING
        installation = context.installation
        if not installation:
            return PENDING
        common = ("latitude", "longitude", "height", "heightType")
        if any(field not in installation for field in common):
            return PENDING
        if context.category == "A" and "indoorDeployment" not in installation:
            return PENDING
        category_b = ("antennaAzimuth", "antennaGain", "antennaBeamwidth")
        if context.category == "B" and any(field not in installation for field in category_b):
            return PENDING
        return None


class QuietZoneRule:
    def __init__(self, is_blocked: Any) -> None:
        self._is_blocked = is_blocked

    def validate(self, context: RegistrationContext) -> int | None:
        if context.profile.protection.quiet_zones and self._is_blocked(context.installation):
            return INVALID_PARAM
        return None


def default_registration_validator(is_quiet_zone_blocked: Any) -> RegistrationValidator:
    """Build the default ordered Chain of Responsibility."""
    return RegistrationValidator(
        (
            RequiredFieldsRule(),
            BlacklistRule(),
            SignatureStructureRule(),
            IdentityRule(),
            MeasurementCapabilityRule(),
            InstallationRangeRule(),
            EirpCapabilityRule(),
            CategoryDeploymentRule(),
            SignedInstallationRule(),
            PendingParametersRule(),
            QuietZoneRule(is_quiet_zone_blocked),
        )
    )
