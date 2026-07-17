"""Heartbeat business logic aligned with WINNF_FT_S_HBT / MES expectations."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from models.models import AdminInjectedData, BlacklistedFccId, Cbsd, Grant
from services.authorization_service import (
    AUTH_TERMINATION_REASONS,
    clamp_datetime,
    is_authorization_valid,
    utc_now,
)
from services.grant_service import DEFAULT_GRANT_DURATION_SEC, HEARTBEAT_INTERVAL_SEC
from services.meas_report import (
    FLAG_DPA_ACTIVE,
    FLAG_MEAS_HBT,
    MEAS_WITH_GRANT,
    admin_flag_set,
    cbsd_meas_capabilities,
    validate_meas_report,
)
from services.spectrum_inquiry_service import (
    _load_injected,
    _overlaps,
    _wisp_freq,
)

SUCCESS = 0
VERSION_UNSUPPORTED = 100
BLACKLISTED = 101
MISSING_PARAM = 102
INVALID_PARAM = 103
TERMINATED_GRANT = 500
SUSPENDED_GRANT = 501
UNSYNC_OP_PARAM = 502

# Prefer a short window so HBT.5 finishes quickly; must be ≤ 240 s (WINNF).
TRANSMIT_EXPIRE_SEC = 60


def _fmt(dt: datetime) -> str:
    return dt.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _past_tx() -> datetime:
    return utc_now() - timedelta(seconds=1)


def _future_tx(
    grant_expire: datetime, authorization_expires_at: datetime | None = None
) -> datetime:
    tx = utc_now() + timedelta(seconds=TRANSMIT_EXPIRE_SEC)
    # transmitExpireTime must be ≤ grantExpireTime and ≤ authorization ceiling.
    tx = clamp_datetime(tx, grant_expire)
    tx = clamp_datetime(tx, authorization_expires_at)
    return tx


def _base(
    code: int,
    *,
    cbsd_id: str | None = None,
    grant_id: str | None = None,
    tx: datetime | None = None,
    grant_expire: datetime | None = None,
    heartbeat_interval: int | None = None,
    meas_config: list[str] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "transmitExpireTime": _fmt(tx if tx is not None else _past_tx()),
        "response": {"responseCode": code},
    }
    if cbsd_id is not None:
        out["cbsdId"] = cbsd_id
    if grant_id is not None:
        out["grantId"] = grant_id
    if grant_expire is not None:
        out["grantExpireTime"] = _fmt(grant_expire)
    if heartbeat_interval is not None:
        out["heartbeatInterval"] = heartbeat_interval
    if meas_config is not None:
        out["measReportConfig"] = meas_config
    return out


def _grant_overlaps_wisp(db: Session, grant: Grant) -> bool:
    """True when grant frequency overlaps an injected WISP (post-CPAS simulation)."""
    for wisp in _load_injected(db, "wisp"):
        freq = _wisp_freq(wisp)
        if freq and _overlaps(grant.low_frequency, grant.high_frequency, freq[0], freq[1]):
            return True
    return False


def _grant_overlaps_active_dpa(db: Session, grant: Grant) -> bool:
    rows = db.query(AdminInjectedData).filter_by(kind=FLAG_DPA_ACTIVE).all()
    for row in rows:
        try:
            data = json.loads(row.data_json or "{}")
        except json.JSONDecodeError:
            continue
        fr = data.get("frequencyRange") or {}
        low = fr.get("lowFrequency")
        high = fr.get("highFrequency")
        if low is None or high is None:
            continue
        if _overlaps(grant.low_frequency, grant.high_frequency, int(low), int(high)):
            return True
    return False


def _terminate_for_authorization(
    grant: Grant, reason: str
) -> None:
    grant.terminated = True
    grant.termination_reason = reason
    grant.authorized = False


def _authorization_failure_response(
    grant: Grant, cbsd_id: str, grant_id: str, reason: str
) -> dict[str, Any]:
    _terminate_for_authorization(grant, reason)
    return _base(TERMINATED_GRANT, cbsd_id=cbsd_id, grant_id=grant_id)


def process_heartbeat(
    db: Session, requests: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    ask_meas = admin_flag_set(db, FLAG_MEAS_HBT)
    responses: list[dict[str, Any]] = []

    for req in requests:
        cbsd_id = req.get("cbsdId")
        grant_id = req.get("grantId")
        op_state = req.get("operationState")

        # Missing required fields → 102 with past transmitExpireTime.
        if not cbsd_id or not grant_id or not op_state:
            echo_grant = None
            if grant_id and cbsd_id:
                # Echo grantId when both ids present but operationState missing (HBT.4).
                if db.query(Grant).filter_by(grant_id=grant_id, cbsd_id=cbsd_id).first():
                    echo_grant = grant_id
            responses.append(
                _base(MISSING_PARAM, cbsd_id=cbsd_id, grant_id=echo_grant)
            )
            continue

        grant = (
            db.query(Grant)
            .filter_by(grant_id=grant_id, cbsd_id=cbsd_id)
            .first()
        )
        if not grant:
            # Invalid grantId → 103 without echoing grantId (HBT.7).
            responses.append(_base(INVALID_PARAM, cbsd_id=cbsd_id))
            continue

        cbsd = db.query(Cbsd).filter_by(cbsd_id=cbsd_id).first()
        if cbsd and db.query(BlacklistedFccId).filter_by(fcc_id=cbsd.fcc_id).first():
            responses.append(
                _base(BLACKLISTED, cbsd_id=cbsd_id, grant_id=grant_id)
            )
            continue

        # Fail-closed authorization revalidation before generic terminated handling.
        # Missing / revoked / expired sources → 500 TERMINATED_GRANT.
        now = utc_now()
        if (
            grant.termination_reason in AUTH_TERMINATION_REASONS
            and grant.terminated
        ):
            responses.append(
                _base(TERMINATED_GRANT, cbsd_id=cbsd_id, grant_id=grant_id)
            )
            continue

        auth_ok, auth_reason, auth_source = is_authorization_valid(
            db,
            grant.authorization_source_type,
            grant.authorization_source_id,
            now=now,
        )
        if not auth_ok:
            responses.append(
                _authorization_failure_response(
                    grant,
                    cbsd_id,
                    grant_id,
                    auth_reason or "AUTHORIZATION_INVALID",
                )
            )
            continue

        # Keep grant snapshot in sync with authoritative ceiling.
        if auth_source is not None:
            grant.authorization_expires_at = auth_source.expires_at

        auth_ceiling = grant.authorization_expires_at
        if auth_ceiling is not None and auth_ceiling.replace(microsecond=0) <= now:
            responses.append(
                _authorization_failure_response(
                    grant, cbsd_id, grant_id, "AUTHORIZATION_EXPIRED"
                )
            )
            continue

        # GRA_6: peer FAD reports an active grant for the same CBSD → terminate (500).
        from services.cpas_service import peer_has_grant_for_cbsd

        if cbsd and peer_has_grant_for_cbsd(db, cbsd):
            grant.terminated = True
            responses.append(
                _base(TERMINATED_GRANT, cbsd_id=cbsd_id, grant_id=grant_id)
            )
            continue

        if grant.terminated:
            # Relinquished / no longer valid grant → 103 (RLQ_2 requires 103;
            # RLQ_1 / HBT_5 also accept 500). Do not echo grantId.
            responses.append(_base(INVALID_PARAM, cbsd_id=cbsd_id))
            continue

        # DPA activation → suspend grant (501), do not terminate (GRA.1).
        # HBT.12 also accepts 501; transmitExpireTime stays in the past via _base.
        if _grant_overlaps_active_dpa(db, grant):
            responses.append(
                _base(
                    SUSPENDED_GRANT,
                    cbsd_id=cbsd_id,
                    grant_id=grant_id,
                    grant_expire=grant.grant_expire_time,
                    heartbeat_interval=grant.heartbeat_interval or HEARTBEAT_INTERVAL_SEC,
                )
            )
            continue

        if grant.grant_expire_time.replace(microsecond=0) <= now:
            responses.append(
                _base(INVALID_PARAM, cbsd_id=cbsd_id, grant_id=grant_id)
            )
            continue

        # Out-of-sync: AUTHORIZED before first successful GRANTED heartbeat.
        if op_state == "AUTHORIZED" and not grant.authorized:
            responses.append(
                _base(UNSYNC_OP_PARAM, cbsd_id=cbsd_id, grant_id=grant_id)
            )
            continue

        if _grant_overlaps_wisp(db, grant):
            grant.terminated = True
            responses.append(
                _base(TERMINATED_GRANT, cbsd_id=cbsd_id, grant_id=grant_id)
            )
            continue

        # Federal DB (EXZ / FSS / GWBL / DPA) — 500 for stale grants, 501 for current.
        from services.federal_db_service import heartbeat_federal_code

        federal_code = heartbeat_federal_code(db, cbsd, grant)
        if federal_code == TERMINATED_GRANT:
            grant.terminated = True
            responses.append(
                _base(TERMINATED_GRANT, cbsd_id=cbsd_id, grant_id=grant_id)
            )
            continue
        if federal_code == SUSPENDED_GRANT:
            responses.append(
                _base(
                    SUSPENDED_GRANT,
                    cbsd_id=cbsd_id,
                    grant_id=grant_id,
                    grant_expire=grant.grant_expire_time,
                    heartbeat_interval=grant.heartbeat_interval or HEARTBEAT_INTERVAL_SEC,
                )
            )
            continue

        capabilities = cbsd_meas_capabilities(cbsd.registration_json if cbsd else None)

        # After SAS asked for WITH_GRANT reports, validate subsequent heartbeats.
        if grant.meas_report_requested and MEAS_WITH_GRANT in capabilities:
            meas_err = validate_meas_report(
                req.get("measReport"), require_full_cbrs=False
            )
            if meas_err is not None:
                responses.append(
                    _base(meas_err, cbsd_id=cbsd_id, grant_id=grant_id)
                )
                continue

        if req.get("grantRenew") is True:
            renewed = utc_now() + timedelta(seconds=DEFAULT_GRANT_DURATION_SEC)
            grant.grant_expire_time = clamp_datetime(renewed, auth_ceiling)
            if grant.grant_expire_time <= now:
                responses.append(
                    _authorization_failure_response(
                        grant, cbsd_id, grant_id, "AUTHORIZATION_EXPIRED"
                    )
                )
                continue

        tx = _future_tx(grant.grant_expire_time, auth_ceiling)
        if tx <= now:
            responses.append(
                _authorization_failure_response(
                    grant, cbsd_id, grant_id, "AUTHORIZATION_EXPIRED"
                )
            )
            continue

        grant.transmit_expire_time = tx
        grant.authorized = True

        meas_config: list[str] | None = None
        if ask_meas and MEAS_WITH_GRANT in capabilities:
            meas_config = [MEAS_WITH_GRANT]
            grant.meas_report_requested = True

        responses.append(
            _base(
                SUCCESS,
                cbsd_id=cbsd_id,
                grant_id=grant_id,
                tx=tx,
                grant_expire=grant.grant_expire_time
                if req.get("grantRenew") is True
                else None,
                heartbeat_interval=grant.heartbeat_interval or HEARTBEAT_INTERVAL_SEC,
                meas_config=meas_config,
            )
        )

    db.commit()
    return responses
