"""Composable validation pipeline for CBSD grant requests."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol, Sequence

from domain.band_profile import BandProfile
from models.models import AuthorizationSource, Cbsd, Grant
from services.authorization_service import (
    SOURCE_TYPE_GAA,
    SOURCE_TYPE_PAL,
    clamp_datetime,
    parse_utc,
    utc_now,
)
from services.geometry import point_in_geojson
from services.spectrum_inquiry_service import _overlaps, _pal_freq

SUCCESS = 0
BLACKLISTED = 101
MISSING_PARAM = 102
INVALID_PARAM = 103
UNSUPPORTED_SPECTRUM = 300
INTERFERENCE = 400
GRANT_CONFLICT = 401


class GrantRepository(Protocol):
    """Persistence and protection lookups needed by grant validators."""

    def find_cbsd(self, cbsd_id: str) -> Cbsd | None: ...

    def is_device_blacklisted(self, device_id: str) -> bool: ...

    def device_max_eirp(self, device_id: str) -> float | None: ...

    def measurements_required(self) -> bool: ...

    def active_grants(self, cbsd_id: str) -> list[Grant]: ...

    def load_pal_records(self) -> list[dict[str, Any]]: ...

    def load_zone_records(self) -> list[dict[str, Any]]: ...

    def ensure_gaa_authorization(self) -> AuthorizationSource: ...

    def authorization_valid(
        self, source_type: str, source_id: str
    ) -> tuple[bool, AuthorizationSource | None]: ...

    def hits_exclusion_zone(
        self, lat: float, lon: float, low: int, high: int
    ) -> bool: ...

    def blocked_by_fss_gwbl(
        self, lat: float, lon: float, low: int, high: int
    ) -> bool: ...

    def violates_border_pfd(
        self,
        installation: dict[str, Any],
        max_eirp: float,
        low: int,
        high: int,
    ) -> bool: ...

    def peer_has_grant(self, cbsd: Cbsd) -> bool: ...

    def certificate_mismatch(
        self, cbsd: Cbsd, certificate_hash: str | None
    ) -> bool: ...

    def validate_meas_report(self, meas_report: Any | None) -> int | None: ...

    def grant_sync_stamp(self) -> dict[str, int]: ...


@dataclass
class GrantContext:
    """Mutable shared state for independent grant validation strategies."""

    request: dict[str, Any]
    profile: BandProfile
    repository: GrantRepository
    certificate_hash: str | None = None
    pending_ranges: list[tuple[int, int]] = field(default_factory=list)
    echo_cbsd_id: bool = False
    cbsd: Cbsd | None = None
    registration: dict[str, Any] = field(default_factory=dict)
    low: int | None = None
    high: int | None = None
    max_eirp: float | None = None
    channel_type: str | None = None
    pal_id: str | None = None
    pal_exp: datetime | None = None
    auth_source: AuthorizationSource | None = None
    auth_expires: datetime | None = None

    @property
    def cbsd_id(self) -> str | None:
        value = self.request.get("cbsdId")
        return str(value) if value else None

    @property
    def installation(self) -> dict[str, Any]:
        return self.registration.get("installationParam") or {}

    @property
    def location(self) -> tuple[float | None, float | None]:
        latitude = self.installation.get("latitude")
        longitude = self.installation.get("longitude")
        if latitude is None or longitude is None:
            return None, None
        return float(latitude), float(longitude)


class GrantRule(Protocol):
    def validate(self, context: GrantContext) -> int | None: ...


class GrantValidator:
    """Run ordered grant rules and stop at the first protocol response code."""

    def __init__(self, rules: Sequence[GrantRule]) -> None:
        self._rules = tuple(rules)

    def validate(self, context: GrantContext) -> int | None:
        for rule in self._rules:
            result = rule.validate(context)
            if result is not None:
                return result
        return None


def registration_data(cbsd: Cbsd) -> dict[str, Any]:
    try:
        return json.loads(cbsd.registration_json or "{}")
    except json.JSONDecodeError:
        return {}


def grant_expire_time(
    auth_expires_at: datetime | None, profile: BandProfile
) -> datetime:
    default = utc_now() + timedelta(seconds=profile.grant_duration_sec)
    return clamp_datetime(default, auth_expires_at)


def _ppa_pal_contexts(
    repository: GrantRepository, cbsd: Cbsd, registration: dict[str, Any]
) -> list[dict[str, Any]]:
    installation = registration.get("installationParam") or {}
    latitude = installation.get("latitude")
    longitude = installation.get("longitude")
    lat = float(latitude) if latitude is not None else None
    lon = float(longitude) if longitude is not None else None

    pals = repository.load_pal_records()
    zones = repository.load_zone_records()
    pal_by_id = {pal.get("palId"): pal for pal in pals if pal.get("palId")}
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
            frequencies = _pal_freq(pal)
            if not frequencies:
                continue
            license_exp = (pal.get("license") or {}).get("licenseExpiration")
            contexts.append(
                {
                    "pal_id": pal_id,
                    "low": frequencies[0],
                    "high": frequencies[1],
                    "in_cluster": in_cluster,
                    "in_ppa": in_ppa,
                    "license_exp": license_exp,
                    "license_status": pal.get("licenseStatus") or "VALID",
                    "user_id": pal.get("userId"),
                }
            )
    return contexts


def resolve_channel(
    contexts: list[dict[str, Any]],
    low: int,
    high: int,
    *,
    cbsd_user_id: str,
) -> tuple[int | None, str | None, datetime | None, str | None]:
    """Return (error_code, channel_type, pal_license_exp, pal_id)."""
    covering_pal: list[dict[str, Any]] = []
    overlapping_pal: list[dict[str, Any]] = []
    for ctx in contexts:
        if _overlaps(low, high, ctx["low"], ctx["high"]):
            overlapping_pal.append(ctx)
            if low >= ctx["low"] and high <= ctx["high"]:
                covering_pal.append(ctx)

    for ctx in overlapping_pal:
        if ctx["in_ppa"] and not ctx["in_cluster"]:
            return INTERFERENCE, None, None, None

    for ctx in overlapping_pal:
        if ctx["in_cluster"] and not (low >= ctx["low"] and high <= ctx["high"]):
            return INVALID_PARAM, None, None, None

    if covering_pal:
        for ctx in covering_pal:
            if ctx["in_cluster"] and ctx.get("user_id") == cbsd_user_id:
                exp = parse_utc(ctx.get("license_exp"))
                if ctx.get("license_exp") and exp is None:
                    return INTERFERENCE, None, None, None
                if exp is not None and exp <= utc_now():
                    return INTERFERENCE, None, None, None
                return None, SOURCE_TYPE_PAL, exp, ctx.get("pal_id")
        return INTERFERENCE, None, None, None

    return None, SOURCE_TYPE_GAA, None, None


def has_frequency_conflict(
    existing: list[Grant],
    low: int,
    high: int,
    *,
    also_pending: list[tuple[int, int]] | None = None,
) -> bool:
    for grant in existing:
        if _overlaps(grant.low_frequency, grant.high_frequency, low, high):
            return True
    for pending_low, pending_high in also_pending or []:
        if _overlaps(pending_low, pending_high, low, high):
            return True
    return False


class CbsdIdentityRule:
    def validate(self, context: GrantContext) -> int | None:
        cbsd_id = context.cbsd_id
        if not cbsd_id:
            return MISSING_PARAM

        cbsd = context.repository.find_cbsd(cbsd_id)
        if cbsd is None:
            return INVALID_PARAM

        if context.repository.certificate_mismatch(cbsd, context.certificate_hash):
            return INVALID_PARAM

        context.cbsd = cbsd
        context.registration = registration_data(cbsd)
        context.echo_cbsd_id = True
        return None


class BlacklistRule:
    def validate(self, context: GrantContext) -> int | None:
        assert context.cbsd is not None
        if context.repository.is_device_blacklisted(context.cbsd.fcc_id):
            return BLACKLISTED
        return None


class MeasurementReportRule:
    def validate(self, context: GrantContext) -> int | None:
        assert context.cbsd is not None
        from services.meas_report import MEAS_WITHOUT_GRANT, cbsd_meas_capabilities

        if not context.repository.measurements_required():
            return None
        capabilities = cbsd_meas_capabilities(context.cbsd.registration_json)
        if MEAS_WITHOUT_GRANT not in capabilities:
            return None
        return context.repository.validate_meas_report(
            context.request.get("measReport")
        )


class OperationParamRule:
    def validate(self, context: GrantContext) -> int | None:
        operation = context.request.get("operationParam")
        if not isinstance(operation, dict):
            return MISSING_PARAM
        if "maxEirp" not in operation or operation.get("maxEirp") is None:
            return MISSING_PARAM
        return None


class FrequencyRangeRule:
    def validate(self, context: GrantContext) -> int | None:
        operation = context.request.get("operationParam")
        assert isinstance(operation, dict)
        frequency = operation.get("operationFrequencyRange")
        if not isinstance(frequency, dict):
            return MISSING_PARAM
        if "lowFrequency" not in frequency or "highFrequency" not in frequency:
            return MISSING_PARAM
        low = frequency.get("lowFrequency")
        high = frequency.get("highFrequency")
        if low is None or high is None:
            return MISSING_PARAM
        try:
            low_i, high_i = int(low), int(high)
        except (TypeError, ValueError):
            return INVALID_PARAM
        if high_i <= low_i:
            return INVALID_PARAM
        if (
            low_i < context.profile.band_low_hz
            or high_i > context.profile.band_high_hz
        ):
            return UNSUPPORTED_SPECTRUM
        context.low = low_i
        context.high = high_i
        return None


class EirpCapabilityRule:
    def validate(self, context: GrantContext) -> int | None:
        assert context.cbsd is not None
        operation = context.request.get("operationParam")
        assert isinstance(operation, dict)
        try:
            max_eirp = float(operation["maxEirp"])
        except (TypeError, ValueError):
            return INVALID_PARAM

        authorized_max = context.repository.device_max_eirp(context.cbsd.fcc_id)
        fcc_max = (
            authorized_max
            if authorized_max is not None
            else context.profile.category_b_max_eirp_dbm
        )
        category = (
            context.registration.get("cbsdCategory")
            or context.cbsd.cbsd_category
            or "A"
        ).upper()
        eirp_capability = context.installation.get("eirpCapability")
        allowed = context.profile.max_allowed_eirp_mhz(
            category,
            fcc_max_eirp=fcc_max,
            eirp_capability=(
                float(eirp_capability) if eirp_capability is not None else None
            ),
        )
        if max_eirp > allowed:
            return INVALID_PARAM
        context.max_eirp = max_eirp
        return None


class ChannelAuthorizationRule:
    def validate(self, context: GrantContext) -> int | None:
        assert context.cbsd is not None
        assert context.low is not None and context.high is not None

        if not context.profile.protection.pal_gaa:
            context.channel_type = SOURCE_TYPE_GAA
            context.pal_exp = None
            context.pal_id = None
        else:
            channels = _ppa_pal_contexts(
                context.repository, context.cbsd, context.registration
            )
            error, channel_type, pal_exp, pal_id = resolve_channel(
                channels,
                context.low,
                context.high,
                cbsd_user_id=context.cbsd.user_id,
            )
            if error is not None:
                return error
            context.channel_type = channel_type
            context.pal_exp = pal_exp
            context.pal_id = pal_id

        assert context.channel_type is not None
        return self._bind_authorization(context)

    @staticmethod
    def _bind_authorization(context: GrantContext) -> int | None:
        if context.channel_type == SOURCE_TYPE_PAL:
            if not context.pal_id:
                return INTERFERENCE
            auth_ok, auth_source = context.repository.authorization_valid(
                SOURCE_TYPE_PAL, str(context.pal_id)
            )
            if not auth_ok or auth_source is None:
                return INTERFERENCE
            context.auth_source = auth_source
            context.auth_expires = auth_source.expires_at or context.pal_exp
            return None

        auth_source = context.repository.ensure_gaa_authorization()
        auth_ok, _ = context.repository.authorization_valid(
            SOURCE_TYPE_GAA, auth_source.source_id
        )
        if not auth_ok:
            return INTERFERENCE
        context.auth_source = auth_source
        context.auth_expires = None
        return None


class ProtectionRule:
    def validate(self, context: GrantContext) -> int | None:
        assert context.low is not None and context.high is not None
        assert context.max_eirp is not None
        latitude, longitude = context.location
        if latitude is not None and longitude is not None:
            if context.profile.protection.exclusion_zones and (
                context.repository.hits_exclusion_zone(
                    latitude, longitude, context.low, context.high
                )
            ):
                return INTERFERENCE
            if context.profile.protection.fss_gwbl and (
                context.repository.blocked_by_fss_gwbl(
                    latitude, longitude, context.low, context.high
                )
            ):
                return INTERFERENCE

        if context.installation and context.profile.protection.border_protection:
            if context.repository.violates_border_pfd(
                context.installation,
                context.max_eirp,
                context.low,
                context.high,
            ):
                return INTERFERENCE
        return None


class ConflictRule:
    def validate(self, context: GrantContext) -> int | None:
        assert context.cbsd is not None
        assert context.low is not None and context.high is not None
        existing = context.repository.active_grants(context.cbsd.cbsd_id)
        if has_frequency_conflict(
            existing,
            context.low,
            context.high,
            also_pending=context.pending_ranges,
        ):
            return GRANT_CONFLICT
        if context.repository.peer_has_grant(context.cbsd):
            return GRANT_CONFLICT
        return None


class AuthorizationCeilingRule:
    def validate(self, context: GrantContext) -> int | None:
        expire = grant_expire_time(context.auth_expires, context.profile)
        if context.auth_expires is not None and expire <= utc_now():
            return INTERFERENCE
        return None


def default_grant_validator() -> GrantValidator:
    """Build the default ordered Chain of Responsibility for Grant."""
    return GrantValidator(
        (
            CbsdIdentityRule(),
            BlacklistRule(),
            MeasurementReportRule(),
            OperationParamRule(),
            FrequencyRangeRule(),
            EirpCapabilityRule(),
            ChannelAuthorizationRule(),
            ProtectionRule(),
            ConflictRule(),
            AuthorizationCeilingRule(),
        )
    )
