"""Registration orchestration with injectable persistence and validation."""

from __future__ import annotations

import json
from typing import Any, Protocol

from sqlalchemy.orm import Session

from domain.band_profile import BandProfile
from models.models import (
    BlacklistedFccId,
    Cbsd,
    ConditionalRegistration,
    CpiUser,
    FccIdRecord,
    Grant,
    UserIdRecord,
)
from services.profile_manager import resolve_optional
from services.registration_validation import (
    SUCCESS,
    RegistrationContext,
    RegistrationRepository,
    RegistrationValidator,
    default_registration_validator,
    merge_registration,
)


class RegistrationStore(RegistrationRepository, Protocol):
    """Persistence boundary consumed by the registration use case."""

    def conditionals(self, device_id: str, serial_number: str) -> dict[str, Any]: ...

    def save_registration(
        self,
        context: RegistrationContext,
        certificate_hash: str | None,
    ) -> str: ...

    def registration_measurements_enabled(self) -> bool: ...

    def commit(self) -> None: ...


class SqlAlchemyRegistrationStore:
    """SQLAlchemy adapter kept outside the registration decision logic."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def is_device_blacklisted(self, device_id: str) -> bool:
        return (
            self._session.query(BlacklistedFccId)
            .filter_by(fcc_id=device_id)
            .first()
            is not None
        )

    def device_authorization(self, device_id: str) -> tuple[bool, float | None]:
        record = (
            self._session.query(FccIdRecord).filter_by(fcc_id=device_id).first()
        )
        if record is None:
            return False, None
        return True, float(record.fcc_max_eirp)

    def user_is_authorized(self, user_id: str) -> bool:
        return (
            self._session.query(UserIdRecord)
            .filter_by(user_id=user_id)
            .first()
            is not None
        )

    def installer_is_authorized(self, installer_id: str) -> bool:
        return (
            self._session.query(CpiUser)
            .filter_by(cpi_id=installer_id)
            .first()
            is not None
        )

    def conditionals(
        self, device_id: str, serial_number: str
    ) -> dict[str, Any]:
        record = (
            self._session.query(ConditionalRegistration)
            .filter_by(
                fcc_id=device_id,
                cbsd_serial_number=serial_number,
            )
            .first()
        )
        return json.loads(record.data_json) if record else {}

    def save_registration(
        self,
        context: RegistrationContext,
        certificate_hash: str | None,
    ) -> str:
        cbsd_id = f"{context.device_id}/{context.serial_number}"
        existing = self._session.query(Cbsd).filter_by(cbsd_id=cbsd_id).first()
        if existing is None:
            self._session.add(
                Cbsd(
                    cbsd_id=cbsd_id,
                    fcc_id=context.device_id,
                    user_id=context.user_id,
                    cbsd_serial_number=context.serial_number,
                    cbsd_category=context.category,
                    certificate_hash=certificate_hash,
                    registration_json=json.dumps(context.merged),
                )
            )
            return cbsd_id

        existing.user_id = context.user_id
        existing.cbsd_category = context.category
        existing.registration_json = json.dumps(context.merged)
        if certificate_hash is not None:
            existing.certificate_hash = certificate_hash
        self._terminate_grants(cbsd_id)
        return cbsd_id

    def _terminate_grants(self, cbsd_id: str) -> None:
        grants = (
            self._session.query(Grant)
            .filter_by(cbsd_id=cbsd_id, terminated=False)
            .all()
        )
        for grant in grants:
            grant.terminated = True

    def registration_measurements_enabled(self) -> bool:
        from services.meas_report import FLAG_MEAS_REG, admin_flag_set

        return admin_flag_set(self._session, FLAG_MEAS_REG)

    def commit(self) -> None:
        self._session.commit()


class RegistrationService:
    """Register batches using an injected profile, store and rule pipeline."""

    def __init__(
        self,
        profile: BandProfile,
        store: RegistrationStore,
        validator: RegistrationValidator | None = None,
    ) -> None:
        self._profile = profile
        self._store = store
        self._validator = validator or self._default_validator()

    @staticmethod
    def _default_validator() -> RegistrationValidator:
        from services.quiet_zone_service import registration_blocked_by_quiet_zone

        return default_registration_validator(registration_blocked_by_quiet_zone)

    def process(
        self,
        requests: list[dict[str, Any]],
        *,
        certificate_hash: str | None = None,
    ) -> list[dict[str, Any]]:
        responses = [
            self._register(dict(request), certificate_hash) for request in requests
        ]
        self._add_measurement_requests(requests, responses)
        self._store.commit()
        return responses

    def _register(
        self, request: dict[str, Any], certificate_hash: str | None
    ) -> dict[str, Any]:
        context = self._context(request)
        response_code = self._validator.validate(context)
        if response_code is not None:
            return {"response": {"responseCode": response_code}}
        cbsd_id = self._store.save_registration(context, certificate_hash)
        return {"cbsdId": cbsd_id, "response": {"responseCode": SUCCESS}}

    def _context(self, request: dict[str, Any]) -> RegistrationContext:
        required = ("userId", "fccId", "cbsdSerialNumber")
        has_identity = all(request.get(field) for field in required)
        conditionals = (
            self._store.conditionals(
                str(request["fccId"]), str(request["cbsdSerialNumber"])
            )
            if has_identity
            else {}
        )
        return RegistrationContext(
            request=request,
            merged=merge_registration(request, conditionals),
            profile=self._profile,
            repository=self._store,
        )

    def _add_measurement_requests(
        self,
        requests: list[dict[str, Any]],
        responses: list[dict[str, Any]],
    ) -> None:
        if not self._store.registration_measurements_enabled():
            return
        from services.meas_report import MEAS_WITHOUT_GRANT

        for request, response in zip(requests, responses):
            successful = response.get("response", {}).get("responseCode") == SUCCESS
            supports_measurement = MEAS_WITHOUT_GRANT in (
                request.get("measCapability") or []
            )
            if successful and supports_measurement:
                response["measReportConfig"] = [MEAS_WITHOUT_GRANT]


def process_registration(
    db: Session,
    registration_requests: list[dict[str, Any]],
    *,
    certificate_hash: str | None = None,
    profile: BandProfile | None = None,
    store: RegistrationStore | None = None,
    validator: RegistrationValidator | None = None,
) -> list[dict[str, Any]]:
    """Compatibility wrapper for the existing route and benchmark callers."""
    service = RegistrationService(
        resolve_optional(profile),
        store or SqlAlchemyRegistrationStore(db),
        validator,
    )
    return service.process(
        registration_requests,
        certificate_hash=certificate_hash,
    )
