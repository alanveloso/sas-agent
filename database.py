"""SQLite database setup for the SAS MVP."""

from __future__ import annotations

import os
import threading
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, sessionmaker

_default_db = Path(__file__).resolve().parent / "sas_mvp.db"
_db_override = os.environ.get("SAS_DATABASE_PATH", "").strip()
DB_PATH = Path(_db_override) if _db_override else _default_db
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

_init_lock = threading.Lock()


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _table_columns(table: str) -> set[str]:
    insp = inspect(engine)
    if not insp.has_table(table):
        return set()
    return {col["name"] for col in insp.get_columns(table)}


def _ensure_column(table: str, column: str, ddl_type: str) -> None:
    if column in _table_columns(table):
        return
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))


def _ensure_index(name: str, ddl: str) -> None:
    with engine.begin() as conn:
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {ddl}"))


def migrate_schema() -> None:
    """Idempotent SQLite schema upgrades for authorization lifecycle fields."""
    from models import models  # noqa: F401

    # create_all adds missing tables (authorization_sources) but not columns.
    Base.metadata.create_all(bind=engine)

    if "grants" in inspect(engine).get_table_names():
        _ensure_column("grants", "authorization_source_id", "VARCHAR(256)")
        _ensure_column("grants", "authorization_source_type", "VARCHAR(32)")
        _ensure_column("grants", "authorization_expires_at", "DATETIME")
        _ensure_column("grants", "termination_reason", "VARCHAR(64)")
        _ensure_index(
            "ix_grants_auth_source",
            "grants (authorization_source_type, authorization_source_id)",
        )
        _ensure_index(
            "ix_grants_authorization_source_id",
            "grants (authorization_source_id)",
        )
        _ensure_index(
            "ix_grants_authorization_source_type",
            "grants (authorization_source_type)",
        )

    _backfill_authorization_bindings()


def _backfill_authorization_bindings() -> None:
    """Bind or fail-closed legacy grants after schema upgrade."""
    from services.authorization_service import (
        SOURCE_TYPE_GAA,
        SOURCE_TYPE_PAL,
        STATUS_ACTIVE,
        STATUS_EXPIRED,
        STATUS_REVOKED,
        TERM_AUTH_MISSING,
        ensure_gaa_authorization,
        parse_utc,
        upsert_authorization_source,
        utc_now,
    )
    from models.models import Grant, PalRecord
    from services.pal_service import ACTIVE_LICENSE_STATUSES, pal_record_to_dict

    db = SessionLocal()
    try:
        gaa = ensure_gaa_authorization(db)

        # Mirror existing PAL rows into AuthorizationSource so Grants can revalidate.
        now = utc_now()
        for row in db.query(PalRecord).all():
            rec = pal_record_to_dict(row)
            exp = parse_utc(row.license_expiration) or parse_utc(
                (rec.get("license") or {}).get("licenseExpiration")
            )
            status = (row.license_status or "VALID").upper()
            if status not in ACTIVE_LICENSE_STATUSES:
                auth_status = STATUS_REVOKED
            elif exp is not None and exp <= now:
                auth_status = STATUS_EXPIRED
            else:
                auth_status = STATUS_ACTIVE
            upsert_authorization_source(
                db,
                source_type=SOURCE_TYPE_PAL,
                source_id=row.pal_id,
                status=auth_status,
                expires_at=exp,
                record=rec,
            )

        # Legacy GAA grants without an explicit source bind to the active policy.
        legacy_gaa = (
            db.query(Grant)
            .filter(
                Grant.terminated.is_(False),
                Grant.channel_type == SOURCE_TYPE_GAA,
                Grant.authorization_source_id.is_(None),
            )
            .all()
        )
        for grant in legacy_gaa:
            grant.authorization_source_type = SOURCE_TYPE_GAA
            grant.authorization_source_id = gaa.source_id
            grant.authorization_expires_at = None

        # Legacy PAL grants cannot recover palId → fail-closed terminate.
        legacy_pal = (
            db.query(Grant)
            .filter(
                Grant.terminated.is_(False),
                Grant.channel_type == SOURCE_TYPE_PAL,
                Grant.authorization_source_id.is_(None),
            )
            .all()
        )
        for grant in legacy_pal:
            grant.terminated = True
            grant.termination_reason = TERM_AUTH_MISSING
            grant.authorized = False

        db.commit()
    finally:
        db.close()


def init_db() -> None:
    from models import models  # noqa: F401

    # RSA + ECDSA uvicorn processes share this module; serialize schema creation.
    with _init_lock:
        try:
            migrate_schema()
        except OperationalError as exc:
            # Concurrent create_all can race on SQLite ("table already exists").
            if "already exists" not in str(exc).lower():
                raise


def reset_db() -> None:
    """Drop and recreate all tables (admin/reset)."""
    from models import models  # noqa: F401

    with _init_lock:
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        # Seed the default GAA policy source after a full reset.
        from services.authorization_service import ensure_gaa_authorization

        db = SessionLocal()
        try:
            ensure_gaa_authorization(db)
            db.commit()
        finally:
            db.close()
