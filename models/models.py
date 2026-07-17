"""SQLAlchemy models for the SAS MVP."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


class FccIdRecord(Base):
    __tablename__ = "fcc_ids"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fcc_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    fcc_max_eirp: Mapped[float] = mapped_column(Float, default=47.0)


class UserIdRecord(Base):
    __tablename__ = "user_ids"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(256), unique=True, index=True)


class BlacklistedFccId(Base):
    __tablename__ = "blacklisted_fcc_ids"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fcc_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)


class ConditionalRegistration(Base):
    __tablename__ = "conditional_registrations"
    __table_args__ = (
        UniqueConstraint("fcc_id", "cbsd_serial_number", name="uq_cond_fcc_serial"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fcc_id: Mapped[str] = mapped_column(String(64), index=True)
    cbsd_serial_number: Mapped[str] = mapped_column(String(128), index=True)
    data_json: Mapped[str] = mapped_column(Text)


class CpiUser(Base):
    __tablename__ = "cpi_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cpi_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    cpi_name: Mapped[str] = mapped_column(String(256), default="")
    cpi_public_key: Mapped[str] = mapped_column(Text, default="")


class Cbsd(Base):
    __tablename__ = "cbsds"
    __table_args__ = (
        UniqueConstraint("fcc_id", "cbsd_serial_number", name="uq_cbsd_fcc_serial"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cbsd_id: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    fcc_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(256))
    cbsd_serial_number: Mapped[str] = mapped_column(String(128))
    cbsd_category: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # SHA-1 fingerprint of the registering client cert (``AA:BB:...``), for mTLS binding.
    certificate_hash: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    registration_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    grants: Mapped[list[Grant]] = relationship(
        "Grant", back_populates="cbsd", cascade="all, delete-orphan"
    )


class Grant(Base):
    __tablename__ = "grants"
    __table_args__ = (
        Index(
            "ix_grants_auth_source",
            "authorization_source_type",
            "authorization_source_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    grant_id: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    cbsd_pk: Mapped[int] = mapped_column(ForeignKey("cbsds.id"), index=True)
    cbsd_id: Mapped[str] = mapped_column(String(256), index=True)
    channel_type: Mapped[str] = mapped_column(String(16), default="GAA")
    low_frequency: Mapped[int] = mapped_column(Integer, default=0)
    high_frequency: Mapped[int] = mapped_column(Integer, default=0)
    max_eirp: Mapped[float | None] = mapped_column(Float, nullable=True)
    grant_expire_time: Mapped[datetime] = mapped_column(DateTime)
    heartbeat_interval: Mapped[int] = mapped_column(Integer, default=60)
    transmit_expire_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    authorized: Mapped[bool] = mapped_column(Boolean, default=False)
    meas_report_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    terminated: Mapped[bool] = mapped_column(Boolean, default=False)
    # Generic authorization origin (e.g. PAL palId or GAA policy id).
    authorization_source_id: Mapped[str | None] = mapped_column(
        String(256), nullable=True, index=True
    )
    authorization_source_type: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )
    authorization_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    # Distinguishes auth eviction (Heartbeat 500) from relinquishment (103).
    termination_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    grant_json: Mapped[str] = mapped_column(Text, default="{}")
    cbsd: Mapped[Cbsd] = relationship("Cbsd", back_populates="grants")


class AuthorizationSource(Base):
    """Generic authorization origin that Grants bind to (PAL, GAA policy, …)."""

    __tablename__ = "authorization_sources"
    __table_args__ = (
        UniqueConstraint(
            "source_type", "source_id", name="uq_auth_source_type_id"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_type: Mapped[str] = mapped_column(String(32), index=True)
    source_id: Mapped[str] = mapped_column(String(256), index=True)
    # ACTIVE | REVOKED | EXPIRED
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE", index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    record_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class PalRecord(Base):
    """Priority Access License injected by the marketplace via /admin/injectdata/pal_database_record."""

    __tablename__ = "pal_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pal_id: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    user_id: Mapped[str] = mapped_column(String(256), index=True)
    low_frequency: Mapped[int] = mapped_column(Integer)
    high_frequency: Mapped[int] = mapped_column(Integer)
    license_status: Mapped[str] = mapped_column(String(16), default="VALID")
    license_expiration: Mapped[str | None] = mapped_column(String(32), nullable=True)
    record_json: Mapped[str] = mapped_column(Text, default="{}")


class AdminInjectedData(Base):
    """Stores admin-injected incumbents / PAL / PPA for Spectrum Inquiry simulation."""

    __tablename__ = "admin_injected_data"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)  # fss|wisp|pal|zone
    data_json: Mapped[str] = mapped_column(Text)


class PeerSas(Base):
    """Peer SAS authorized for SAS↔SAS (FAD) access."""

    __tablename__ = "peer_sas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    certificate_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    url: Mapped[str] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class EscSensor(Base):
    """ESC sensor records injected via admin API for FAD export."""

    __tablename__ = "esc_sensors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    record_id: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    data_json: Mapped[str] = mapped_column(Text)


class PeerFadRecord(Base):
    """Records imported from a peer SAS Full Activity Dump (UUT as client)."""

    __tablename__ = "peer_fad_records"
    __table_args__ = (
        UniqueConstraint(
            "peer_sas_id", "record_type", "record_id", name="uq_peer_fad_record"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    peer_sas_id: Mapped[int] = mapped_column(ForeignKey("peer_sas.id"), index=True)
    record_type: Mapped[str] = mapped_column(String(32), index=True)
    record_id: Mapped[str] = mapped_column(String(256), index=True)
    data_json: Mapped[str] = mapped_column(Text)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class FadDump(Base):
    """Local Full Activity Dump generation (UUT as SAS↔SAS server)."""

    __tablename__ = "fad_dumps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    generation_datetime: Mapped[str] = mapped_column(String(32), index=True)
    description: Mapped[str] = mapped_column(String(256), default="Full activity dump files")
    manifest_json: Mapped[str] = mapped_column(Text, default="{}")
    ready: Mapped[bool] = mapped_column(Boolean, default=False)
    files: Mapped[list[FadFile]] = relationship(
        "FadFile", back_populates="dump", cascade="all, delete-orphan"
    )


class FadFile(Base):
    """Activity dump file content belonging to a FadDump."""

    __tablename__ = "fad_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dump_id: Mapped[int] = mapped_column(ForeignKey("fad_dumps.id"), index=True)
    record_type: Mapped[str] = mapped_column(String(32), index=True)
    url_path: Mapped[str] = mapped_column(String(512), index=True)
    checksum: Mapped[str] = mapped_column(String(40))
    size: Mapped[int] = mapped_column(Integer, default=0)
    content_json: Mapped[str] = mapped_column(Text)
    dump: Mapped[FadDump] = relationship("FadDump", back_populates="files")
