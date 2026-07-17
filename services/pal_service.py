"""PAL (Priority Access License) persistence and lookup for marketplace integration."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from models.models import AdminInjectedData, PalRecord
from services.authorization_service import (
    SOURCE_TYPE_PAL,
    STATUS_ACTIVE,
    STATUS_EXPIRED,
    STATUS_REVOKED,
    apply_authorization_cascade,
    get_authorization_source,
    parse_utc,
    upsert_authorization_source,
    utc_now,
)

ACTIVE_LICENSE_STATUSES = frozenset({"VALID"})


def _pal_frequencies(record: dict[str, Any]) -> tuple[int, int] | None:
    """Extract low/high Hz from channelAssignment or legacy palBlock."""
    assignment = (record.get("channelAssignment") or {}).get("primaryAssignment") or {}
    low = assignment.get("lowFrequency")
    high = assignment.get("highFrequency")
    if low is None or high is None:
        block = record.get("palBlock") or {}
        low = block.get("lowFrequency")
        high = block.get("highFrequency")
    if low is None or high is None:
        return None
    return int(low), int(high)


def _license_status_to_auth_status(license_status: str, expires_at) -> str:
    status = (license_status or "VALID").upper()
    if status not in ACTIVE_LICENSE_STATUSES:
        return STATUS_REVOKED
    if expires_at is not None and expires_at.replace(microsecond=0) <= utc_now():
        return STATUS_EXPIRED
    return STATUS_ACTIVE


def _is_active_pal(record: dict[str, Any], *, now=None) -> bool:
    """True when licenseStatus is VALID and licenseExpiration is in the future."""
    now = now or utc_now()
    status = (record.get("licenseStatus") or "VALID").upper()
    if status not in ACTIVE_LICENSE_STATUSES:
        return False
    exp_raw = (record.get("license") or {}).get("licenseExpiration")
    if exp_raw:
        exp = parse_utc(exp_raw)
        if exp is None:
            # Malformed expiration → fail-closed for availability/issuance.
            return False
        if exp <= now:
            return False
    return True


def pal_record_to_dict(row: PalRecord) -> dict[str, Any]:
    """Deserialize a stored PAL row to the WINNF JSON shape."""
    try:
        data = json.loads(row.record_json or "{}")
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("palId", row.pal_id)
    data.setdefault("userId", row.user_id)
    if "channelAssignment" not in data:
        data["channelAssignment"] = {
            "primaryAssignment": {
                "lowFrequency": row.low_frequency,
                "highFrequency": row.high_frequency,
            }
        }
    data.setdefault("licenseStatus", row.license_status)
    if row.license_expiration and "license" not in data:
        data["license"] = {"licenseExpiration": row.license_expiration}
    elif row.license_expiration:
        lic = data.get("license")
        if isinstance(lic, dict):
            lic.setdefault("licenseExpiration", row.license_expiration)
    return data


def upsert_pal_record(db: Session, record: dict[str, Any]) -> PalRecord | None:
    """Insert or update a PAL record keyed by palId; cascade revoke derived grants."""
    pal_id = record.get("palId")
    if not pal_id:
        return None

    freqs = _pal_frequencies(record)
    if freqs is None:
        return None

    user_id = str(record.get("userId") or "")
    license_status = str(record.get("licenseStatus") or "VALID").upper()
    license_exp_raw = (record.get("license") or {}).get("licenseExpiration")
    expires_at = parse_utc(license_exp_raw)

    existing_auth = get_authorization_source(db, SOURCE_TYPE_PAL, pal_id)
    previous_status = existing_auth.status if existing_auth else None
    previous_expires = existing_auth.expires_at if existing_auth else None

    row = db.query(PalRecord).filter_by(pal_id=pal_id).first()
    if row is None:
        row = PalRecord(pal_id=pal_id)
        db.add(row)

    row.user_id = user_id
    row.low_frequency = freqs[0]
    row.high_frequency = freqs[1]
    row.license_status = license_status
    row.license_expiration = license_exp_raw if isinstance(license_exp_raw, str) else (
        expires_at.strftime("%Y-%m-%dT%H:%M:%SZ") if expires_at else None
    )
    row.record_json = json.dumps(record)

    auth_status = _license_status_to_auth_status(license_status, expires_at)
    upsert_authorization_source(
        db,
        source_type=SOURCE_TYPE_PAL,
        source_id=str(pal_id),
        status=auth_status,
        expires_at=expires_at,
        record=record,
    )
    apply_authorization_cascade(
        db,
        source_type=SOURCE_TYPE_PAL,
        source_id=str(pal_id),
        previous_status=previous_status,
        previous_expires_at=previous_expires,
        new_status=auth_status,
        new_expires_at=expires_at,
    )
    return row


def upsert_pal_records(db: Session, payload: Any) -> int:
    """Persist one or many PAL records; returns count of upserted rows."""
    if payload is None:
        return 0
    records = payload if isinstance(payload, list) else [payload]
    count = 0
    for rec in records:
        if not isinstance(rec, dict):
            continue
        if upsert_pal_record(db, rec) is not None:
            count += 1
    if count:
        db.commit()
    return count


def load_pal_records(db: Session, *, active_only: bool = False) -> list[dict[str, Any]]:
    """Load PAL records from the dedicated table, with legacy fallback."""
    pals: list[dict[str, Any]] = []
    seen: set[str] = set()
    now = utc_now()

    for row in db.query(PalRecord).all():
        rec = pal_record_to_dict(row)
        pal_id = rec.get("palId")
        if not pal_id or (active_only and not _is_active_pal(rec, now=now)):
            continue
        pals.append(rec)
        seen.add(pal_id)

    # Legacy rows injected before PalRecord existed.
    for inj in db.query(AdminInjectedData).filter_by(kind="pal").all():
        try:
            rec = json.loads(inj.data_json or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        pal_id = rec.get("palId")
        if not pal_id or pal_id in seen:
            continue
        if active_only and not _is_active_pal(rec, now=now):
            continue
        pals.append(rec)

    return pals


def known_pal_ids(db: Session) -> set[str]:
    """Return all palId values known to the SAS."""
    return {p.get("palId") for p in load_pal_records(db) if p.get("palId")}


def active_pals_for_user(db: Session, user_id: str) -> list[dict[str, Any]]:
    """Return VALID PAL licenses owned by the given userId."""
    return [
        p
        for p in load_pal_records(db, active_only=True)
        if p.get("userId") == user_id
    ]
