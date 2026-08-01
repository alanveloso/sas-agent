"""Grant orchestration with injectable persistence and validation."""

from __future__ import annotations

import json
import uuid
from typing import Any, Protocol

from sqlalchemy.orm import Session

from domain.band_profile import BandProfile
from models.models import AuthorizationSource, BlacklistedFccId, Cbsd, FccIdRecord, Grant
from services.authorization_service import (
    ensure_gaa_authorization,
    is_authorization_valid,
    sync_grant_authorization_snapshot,
)
from services.grant_validation import (
    SUCCESS,
    GrantContext,
    GrantRepository,
    GrantValidator,
    default_grant_validator,
    grant_expire_time,
)
from services.profile_manager import resolve_optional


class GrantStore(GrantRepository, Protocol):
    """Persistence boundary consumed by the grant use case."""

    def save_grant(self, context: GrantContext) -> dict[str, Any]: ...

    def ensure_default_gaa(self) -> None: ...

    def commit(self) -> None: ...


class SqlAlchemyGrantStore:
    """SQLAlchemy adapter kept outside the grant decision logic."""

    def __init__(self, session: Session, profile: BandProfile) -> None:
        self._session = session
        self._profile = profile

    def find_cbsd(self, cbsd_id: str) -> Cbsd | None:
        return self._session.query(Cbsd).filter_by(cbsd_id=cbsd_id).first()

    def is_device_blacklisted(self, device_id: str) -> bool:
        return (
            self._session.query(BlacklistedFccId)
            .filter_by(fcc_id=device_id)
            .first()
            is not None
        )

    def device_max_eirp(self, device_id: str) -> float | None:
        record = (
            self._session.query(FccIdRecord).filter_by(fcc_id=device_id).first()
        )
        return float(record.fcc_max_eirp) if record else None

    def measurements_required(self) -> bool:
        from services.meas_report import FLAG_MEAS_REG, admin_flag_set

        return admin_flag_set(self._session, FLAG_MEAS_REG)

    def active_grants(self, cbsd_id: str) -> list[Grant]:
        return (
            self._session.query(Grant)
            .filter_by(cbsd_id=cbsd_id, terminated=False)
            .all()
        )

    def load_pal_records(self) -> list[dict[str, Any]]:
        from services.spectrum_inquiry_service import _load_injected

        return _load_injected(self._session, "pal")

    def load_zone_records(self) -> list[dict[str, Any]]:
        from services.spectrum_inquiry_service import _load_injected

        return _load_injected(self._session, "zone")

    def ensure_gaa_authorization(self) -> AuthorizationSource:
        return ensure_gaa_authorization(self._session, profile=self._profile)

    def authorization_valid(
        self, source_type: str, source_id: str
    ) -> tuple[bool, AuthorizationSource | None]:
        ok, _reason, source = is_authorization_valid(
            self._session, source_type, source_id
        )
        return ok, source

    def hits_exclusion_zone(
        self, lat: float, lon: float, low: int, high: int
    ) -> bool:
        from services.exclusion_zone_service import point_hits_exclusion_zone

        return point_hits_exclusion_zone(
            self._session, lat, lon, low, high, profile=self._profile
        )

    def blocked_by_fss_gwbl(
        self, lat: float, lon: float, low: int, high: int
    ) -> bool:
        from services.federal_db_service import grant_blocked_by_fss_gwbl

        return grant_blocked_by_fss_gwbl(
            self._session, lat, lon, low, high, profile=self._profile
        )

    def violates_border_pfd(
        self,
        installation: dict[str, Any],
        max_eirp: float,
        low: int,
        high: int,
    ) -> bool:
        from services.border_protection import violates_border_pfd

        return violates_border_pfd(
            installation, max_eirp, low, high, profile=self._profile
        )

    def peer_has_grant(self, cbsd: Cbsd) -> bool:
        from services.cpas_service import peer_has_grant_for_cbsd

        return peer_has_grant_for_cbsd(self._session, cbsd)

    def certificate_mismatch(
        self, cbsd: Cbsd, certificate_hash: str | None
    ) -> bool:
        from services.spectrum_inquiry_service import _cert_mismatch

        return _cert_mismatch(cbsd, certificate_hash)

    def validate_meas_report(self, meas_report: Any | None) -> int | None:
        from services.meas_report import validate_meas_report

        return validate_meas_report(
            meas_report, require_full_cbrs=True, profile=self._profile
        )

    def grant_sync_stamp(self) -> dict[str, int]:
        from services.federal_db_service import grant_sync_stamp

        return grant_sync_stamp(self._session)

    def ensure_default_gaa(self) -> None:
        ensure_gaa_authorization(self._session, profile=self._profile)

    def save_grant(self, context: GrantContext) -> dict[str, Any]:
        assert context.cbsd is not None
        assert context.low is not None and context.high is not None
        assert context.max_eirp is not None
        assert context.channel_type is not None
        assert context.auth_source is not None

        grant_id = f"grant/{uuid.uuid4().hex}"
        expire = grant_expire_time(context.auth_expires, context.profile)
        stamp = self.grant_sync_stamp()
        grant_payload = dict(context.request)
        grant_payload["fss_gen"] = stamp.get("fss", 0)
        grant_payload["gwbl_gen"] = stamp.get("gwbl", 0)
        grant_payload["exz_gen"] = stamp.get("exz", 0)
        grant_payload["dpa_gen"] = stamp.get("dpa", 0)

        grant = Grant(
            grant_id=grant_id,
            cbsd_pk=context.cbsd.id,
            cbsd_id=context.cbsd.cbsd_id,
            channel_type=context.channel_type,
            low_frequency=context.low,
            high_frequency=context.high,
            max_eirp=context.max_eirp,
            grant_expire_time=expire,
            heartbeat_interval=context.profile.heartbeat_interval_sec,
            grant_json=json.dumps(grant_payload),
        )
        sync_grant_authorization_snapshot(grant, context.auth_source)
        if grant.authorization_expires_at is None and context.auth_expires is not None:
            grant.authorization_expires_at = context.auth_expires
        self._session.add(grant)

        return {
            "cbsdId": context.cbsd.cbsd_id,
            "grantId": grant_id,
            "grantExpireTime": expire.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "heartbeatInterval": context.profile.heartbeat_interval_sec,
            "channelType": context.channel_type,
            "response": {"responseCode": SUCCESS},
        }

    def commit(self) -> None:
        self._session.commit()


class GrantService:
    """Issue grants using an injected profile, store and rule pipeline."""

    def __init__(
        self,
        profile: BandProfile,
        store: GrantStore,
        validator: GrantValidator | None = None,
    ) -> None:
        self._profile = profile
        self._store = store
        self._validator = validator or default_grant_validator()

    def process(
        self,
        requests: list[dict[str, Any]],
        *,
        certificate_hash: str | None = None,
    ) -> list[dict[str, Any]]:
        self._store.ensure_default_gaa()
        pending_by_cbsd: dict[str, list[tuple[int, int]]] = {}
        responses: list[dict[str, Any]] = []

        for raw in requests:
            request = dict(raw)
            cbsd_id = str(request["cbsdId"]) if request.get("cbsdId") else None
            pending = pending_by_cbsd.get(cbsd_id or "", [])
            response, approved = self._issue(
                request, certificate_hash, pending
            )
            responses.append(response)
            if approved is not None and cbsd_id is not None:
                pending_by_cbsd.setdefault(cbsd_id, []).append(approved)

        self._store.commit()
        return responses

    def _issue(
        self,
        request: dict[str, Any],
        certificate_hash: str | None,
        pending_ranges: list[tuple[int, int]],
    ) -> tuple[dict[str, Any], tuple[int, int] | None]:
        context = GrantContext(
            request=request,
            profile=self._profile,
            repository=self._store,
            certificate_hash=certificate_hash,
            pending_ranges=list(pending_ranges),
        )
        response_code = self._validator.validate(context)
        if response_code is not None:
            out: dict[str, Any] = {"response": {"responseCode": response_code}}
            if context.echo_cbsd_id and context.cbsd_id is not None:
                out["cbsdId"] = context.cbsd_id
            return out, None
        response = self._store.save_grant(context)
        assert context.low is not None and context.high is not None
        return response, (context.low, context.high)


def process_grant(
    db: Session,
    requests: list[dict[str, Any]],
    *,
    certificate_hash: str | None = None,
    profile: BandProfile | None = None,
    store: GrantStore | None = None,
    validator: GrantValidator | None = None,
) -> list[dict[str, Any]]:
    """Compatibility wrapper for the existing route and benchmark callers."""
    active = resolve_optional(profile)
    service = GrantService(
        active,
        store or SqlAlchemyGrantStore(db, active),
        validator,
    )
    return service.process(requests, certificate_hash=certificate_hash)


# Legacy module-level aliases used by older imports / diagnostics.
_boot = resolve_optional(None)
HEARTBEAT_INTERVAL_SEC = _boot.heartbeat_interval_sec
DEFAULT_GRANT_DURATION_SEC = _boot.grant_duration_sec
CAT_A_MAX_EIRP_10MHZ = _boot.category_a_max_eirp_dbm
CAT_B_MAX_EIRP_10MHZ = _boot.category_b_max_eirp_dbm
