"""Generic authorization source lifecycle for Grants (PAL, GAA, …)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from domain.band_profile import BandProfile
from models.models import AuthorizationSource, Grant
from services.profile_manager import resolve_optional

SOURCE_TYPE_PAL = "PAL"
SOURCE_TYPE_GAA = "GAA"
GAA_SOURCE_ID = "CBRS"

STATUS_ACTIVE = "ACTIVE"
STATUS_REVOKED = "REVOKED"
STATUS_EXPIRED = "EXPIRED"

TERM_AUTH_REVOKED = "AUTHORIZATION_REVOKED"
TERM_AUTH_EXPIRED = "AUTHORIZATION_EXPIRED"
TERM_AUTH_MISSING = "AUTHORIZATION_MISSING"
TERM_AUTH_INVALID = "AUTHORIZATION_INVALID"

ACTIVE_STATUSES = frozenset({STATUS_ACTIVE})
AUTH_TERMINATION_REASONS = frozenset(
    {
        TERM_AUTH_REVOKED,
        TERM_AUTH_EXPIRED,
        TERM_AUTH_MISSING,
        TERM_AUTH_INVALID,
    }
)


def parse_utc(value: Any) -> datetime | None:
    """Parse WINNF-style UTC timestamps; returns naive UTC datetime or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None, microsecond=0)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def utc_now() -> datetime:
    return datetime.utcnow().replace(microsecond=0)


def ensure_gaa_authorization(
    db: Session, *, profile: BandProfile | None = None
) -> AuthorizationSource:
    """Ensure the global GAA policy source exists and is ACTIVE."""
    active = resolve_optional(profile)
    policy_id = active.gaa_policy_id or f"{active.regulatory_rule_id}_GAA"
    row = (
        db.query(AuthorizationSource)
        .filter_by(source_type=SOURCE_TYPE_GAA, source_id=GAA_SOURCE_ID)
        .first()
    )
    if row is None:
        row = AuthorizationSource(
            source_type=SOURCE_TYPE_GAA,
            source_id=GAA_SOURCE_ID,
            status=STATUS_ACTIVE,
            expires_at=None,
            record_json=json.dumps({"policy": policy_id}),
        )
        db.add(row)
        db.flush()
        return row
    row.status = STATUS_ACTIVE
    row.expires_at = None
    row.updated_at = utc_now()
    return row


def upsert_authorization_source(
    db: Session,
    *,
    source_type: str,
    source_id: str,
    status: str,
    expires_at: datetime | None,
    record: dict[str, Any] | None = None,
) -> AuthorizationSource:
    """Insert or update an authorization source keyed by (type, id)."""
    source_type = source_type.upper()
    status = status.upper()
    row = (
        db.query(AuthorizationSource)
        .filter_by(source_type=source_type, source_id=source_id)
        .first()
    )
    if row is None:
        row = AuthorizationSource(source_type=source_type, source_id=source_id)
        db.add(row)

    row.status = status
    row.expires_at = expires_at
    if record is not None:
        row.record_json = json.dumps(record)
    row.updated_at = utc_now()
    db.flush()
    return row


def get_authorization_source(
    db: Session, source_type: str | None, source_id: str | None
) -> AuthorizationSource | None:
    if not source_type or not source_id:
        return None
    return (
        db.query(AuthorizationSource)
        .filter_by(source_type=source_type.upper(), source_id=source_id)
        .first()
    )


def evaluate_authorization(
    source: AuthorizationSource | None,
    *,
    now: datetime | None = None,
) -> tuple[bool, str | None]:
    """
    Return (is_valid, termination_reason).

    Fail-closed: missing / revoked / expired / unknown status are invalid.
    """
    now = now or utc_now()
    if source is None:
        return False, TERM_AUTH_MISSING

    status = (source.status or "").upper()
    if status == STATUS_REVOKED:
        return False, TERM_AUTH_REVOKED
    if status == STATUS_EXPIRED:
        return False, TERM_AUTH_EXPIRED
    if status not in ACTIVE_STATUSES:
        return False, TERM_AUTH_INVALID

    if source.expires_at is not None:
        exp = source.expires_at.replace(microsecond=0)
        if exp <= now:
            return False, TERM_AUTH_EXPIRED

    return True, None


def is_authorization_valid(
    db: Session,
    source_type: str | None,
    source_id: str | None,
    *,
    now: datetime | None = None,
) -> tuple[bool, str | None, AuthorizationSource | None]:
    source = get_authorization_source(db, source_type, source_id)
    ok, reason = evaluate_authorization(source, now=now)
    return ok, reason, source


def terminate_grants_for_source(
    db: Session,
    *,
    source_type: str,
    source_id: str,
    reason: str,
    also_clamp_expires_at: datetime | None = None,
) -> int:
    """
    Mark all active grants derived from a source as terminated.

    Returns the number of grants affected. Caller owns the transaction commit.
    """
    source_type = source_type.upper()
    grants = (
        db.query(Grant)
        .filter_by(
            authorization_source_type=source_type,
            authorization_source_id=source_id,
            terminated=False,
        )
        .all()
    )
    count = 0
    for grant in grants:
        grant.terminated = True
        grant.termination_reason = reason
        grant.authorized = False
        if also_clamp_expires_at is not None:
            grant.authorization_expires_at = also_clamp_expires_at
        count += 1
    return count


def sync_grant_authorization_snapshot(
    grant: Grant, source: AuthorizationSource
) -> None:
    """Copy authoritative source identity/expiration onto a Grant row."""
    grant.authorization_source_type = source.source_type
    grant.authorization_source_id = source.source_id
    grant.authorization_expires_at = source.expires_at


def clamp_datetime(value: datetime, ceiling: datetime | None) -> datetime:
    """Return min(value, ceiling) when ceiling is set."""
    value = value.replace(microsecond=0)
    if ceiling is None:
        return value
    ceiling = ceiling.replace(microsecond=0)
    return min(value, ceiling)


def apply_authorization_cascade(
    db: Session,
    *,
    source_type: str,
    source_id: str,
    previous_status: str | None,
    previous_expires_at: datetime | None,
    new_status: str,
    new_expires_at: datetime | None,
) -> int:
    """
    Invalidate derived grants when a source becomes invalid or shrinks.

    Triggers:
    - status leaves ACTIVE (e.g. REVOKED / EXPIRED)
    - expires_at moves into the past
    - expires_at is reduced (grants keep the new ceiling via snapshot update;
      already-past grants terminate)
    """
    new_status = new_status.upper()
    now = utc_now()
    ok, reason = evaluate_authorization(
        AuthorizationSource(
            source_type=source_type,
            source_id=source_id,
            status=new_status,
            expires_at=new_expires_at,
        ),
        now=now,
    )
    if not ok:
        return terminate_grants_for_source(
            db,
            source_type=source_type,
            source_id=source_id,
            reason=reason or TERM_AUTH_INVALID,
            also_clamp_expires_at=new_expires_at,
        )

    # Still active: if expiration shrank, clamp snapshots and terminate grants
    # whose transmit window can no longer be renewed (already past new ceiling).
    if new_expires_at is None:
        return 0

    shrunk = previous_expires_at is None or new_expires_at < previous_expires_at.replace(
        microsecond=0
    )
    if not shrunk and (previous_status or STATUS_ACTIVE).upper() in ACTIVE_STATUSES:
        # Refresh snapshots to the authoritative ceiling even when unchanged.
        for grant in (
            db.query(Grant)
            .filter_by(
                authorization_source_type=source_type.upper(),
                authorization_source_id=source_id,
                terminated=False,
            )
            .all()
        ):
            grant.authorization_expires_at = new_expires_at
        return 0

    count = 0
    for grant in (
        db.query(Grant)
        .filter_by(
            authorization_source_type=source_type.upper(),
            authorization_source_id=source_id,
            terminated=False,
        )
        .all()
    ):
        grant.authorization_expires_at = new_expires_at
        if new_expires_at.replace(microsecond=0) <= now:
            grant.terminated = True
            grant.termination_reason = TERM_AUTH_EXPIRED
            grant.authorized = False
            count += 1
    return count
