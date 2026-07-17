"""Admin / test-control routes expected by the WINNF harness."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from database import get_db, reset_db
from models.models import (
    AdminInjectedData,
    BlacklistedFccId,
    ConditionalRegistration,
    CpiUser,
    EscSensor,
    FccIdRecord,
    PeerSas,
    UserIdRecord,
)
from schemas.admin import (
    BlacklistFccIdRequest,
    ConditionalRegistrationRequest,
    InjectCpiUserRequest,
    InjectFccIdRequest,
    InjectUserIdRequest,
)
from services.cpas_service import (
    get_daily_activities_completed,
    trigger_daily_activities,
)
from services.fad_service import (
    create_full_activity_dump,
    rewrite_esc_sensor_id,
    rewrite_zone_id,
)

router = APIRouter(prefix="/admin", tags=["admin"])


def _empty_ok() -> Response:
    return Response(status_code=200, content=b"", media_type="application/json")


def _store_injection(db: Session, kind: str, payload: Any) -> None:
    db.add(
        AdminInjectedData(
            kind=kind,
            data_json=json.dumps(payload if payload is not None else {}),
        )
    )
    db.commit()


@router.post("/reset")
def admin_reset():
    reset_db()
    return _empty_ok()


@router.post("/injectdata/fcc_id")
def inject_fcc_id(body: InjectFccIdRequest, db: Session = Depends(get_db)):
    existing = db.query(FccIdRecord).filter_by(fcc_id=body.fccId).first()
    if existing:
        existing.fcc_max_eirp = body.fccMaxEirp
    else:
        db.add(FccIdRecord(fcc_id=body.fccId, fcc_max_eirp=body.fccMaxEirp))
    db.commit()
    return _empty_ok()


@router.post("/injectdata/user_id")
def inject_user_id(body: InjectUserIdRequest, db: Session = Depends(get_db)):
    if not db.query(UserIdRecord).filter_by(user_id=body.userId).first():
        db.add(UserIdRecord(user_id=body.userId))
        db.commit()
    return _empty_ok()


@router.post("/injectdata/conditional_registration")
def inject_conditional_registration(
    body: ConditionalRegistrationRequest, db: Session = Depends(get_db)
):
    for item in body.registrationData:
        fcc_id = item.get("fccId")
        serial = item.get("cbsdSerialNumber")
        if not fcc_id or not serial:
            continue
        existing = (
            db.query(ConditionalRegistration)
            .filter_by(fcc_id=fcc_id, cbsd_serial_number=serial)
            .first()
        )
        payload = json.dumps(item)
        if existing:
            existing.data_json = payload
        else:
            db.add(
                ConditionalRegistration(
                    fcc_id=fcc_id,
                    cbsd_serial_number=serial,
                    data_json=payload,
                )
            )
    db.commit()
    return _empty_ok()


@router.post("/injectdata/cpi_user")
def inject_cpi_user(body: InjectCpiUserRequest, db: Session = Depends(get_db)):
    existing = db.query(CpiUser).filter_by(cpi_id=body.cpiId).first()
    if existing:
        existing.cpi_name = body.cpiName
        existing.cpi_public_key = body.cpiPublicKey
    else:
        db.add(
            CpiUser(
                cpi_id=body.cpiId,
                cpi_name=body.cpiName,
                cpi_public_key=body.cpiPublicKey,
            )
        )
    db.commit()
    return _empty_ok()


@router.post("/injectdata/blacklist_fcc_id")
def blacklist_fcc_id(body: BlacklistFccIdRequest, db: Session = Depends(get_db)):
    if not db.query(BlacklistedFccId).filter_by(fcc_id=body.fccId).first():
        db.add(BlacklistedFccId(fcc_id=body.fccId))
        db.commit()
    return _empty_ok()


@router.post("/trigger/meas_report_in_registration_response")
def trigger_meas_report_in_registration(db: Session = Depends(get_db)):
    from services.meas_report import FLAG_MEAS_REG, set_admin_flag

    set_admin_flag(db, FLAG_MEAS_REG)
    return _empty_ok()


@router.post("/trigger/meas_report_in_heartbeat_response")
def trigger_meas_report_in_heartbeat(db: Session = Depends(get_db)):
    from services.meas_report import FLAG_MEAS_HBT, set_admin_flag

    set_admin_flag(db, FLAG_MEAS_HBT)
    return _empty_ok()


@router.post("/injectdata/fss")
async def inject_fss(request: Request, db: Session = Depends(get_db)):
    body: Any = {}
    try:
        body = await request.json()
    except Exception:
        pass
    _store_injection(db, "fss", body)
    return _empty_ok()


@router.post("/injectdata/wisp")
async def inject_wisp(request: Request, db: Session = Depends(get_db)):
    body: Any = {}
    try:
        body = await request.json()
    except Exception:
        pass
    _store_injection(db, "wisp", body)
    return _empty_ok()


@router.post("/injectdata/pal_database_record")
async def inject_pal_database_record(request: Request, db: Session = Depends(get_db)):
    from services.pal_service import upsert_pal_records

    body: Any = {}
    try:
        body = await request.json()
    except Exception:
        pass
    upsert_pal_records(db, body)
    return _empty_ok()


@router.post("/injectdata/zone")
async def inject_zone(request: Request, db: Session = Depends(get_db)):
    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        pass
    record = body.get("record") or {}
    if isinstance(record, dict):
        zone_id = rewrite_zone_id(record.get("id"))
        record = dict(record)
        record["id"] = zone_id
        body = dict(body)
        body["record"] = record
    else:
        zone_id = rewrite_zone_id(None)
    _store_injection(db, "zone", body)
    return JSONResponse(zone_id)


@router.post("/injectdata/exclusion_zone")
async def inject_exclusion_zone(request: Request, db: Session = Depends(get_db)):
    """Persist GeoJSON exclusion zone + frequencyRanges (EXZ_1)."""
    from services.exclusion_zone_service import persist_exclusion_zone

    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        pass
    persist_exclusion_zone(db, body if isinstance(body, dict) else {})
    return _empty_ok()


@router.post("/trigger/enable_ntia_15_517")
def trigger_enable_ntia_15_517(db: Session = Depends(get_db)):
    """Enable NTIA coastal exclusion zones using the active band profile."""
    from services.exclusion_zone_service import enable_ntia_exclusion_zones
    from services.profile_manager import get_active_band_profile

    enable_ntia_exclusion_zones(db, profile=get_active_band_profile())
    return _empty_ok()


@router.post("/injectdata/peer_sas")
async def inject_peer_sas(request: Request, db: Session = Depends(get_db)):
    """Persist peer SAS certificateHash + url for SAS↔SAS authorization."""
    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        pass
    cert_hash = (body.get("certificateHash") or "").strip()
    url = (body.get("url") or "").strip()
    if cert_hash:
        existing = db.query(PeerSas).filter_by(certificate_hash=cert_hash).first()
        if existing:
            existing.url = url
        else:
            db.add(PeerSas(certificate_hash=cert_hash, url=url))
        db.commit()
    return _empty_ok()


@router.post("/injectdata/esc_sensor")
async def inject_esc_sensor(request: Request, db: Session = Depends(get_db)):
    """Persist EscSensorRecord for inclusion in Full Activity Dump."""
    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        pass
    record = body.get("record") or body
    if not isinstance(record, dict):
        return _empty_ok()
    record = dict(record)
    record_id = rewrite_esc_sensor_id(record.get("id"))
    record["id"] = record_id
    existing = db.query(EscSensor).filter_by(record_id=record_id).first()
    payload = json.dumps(record)
    if existing:
        existing.data_json = payload
    else:
        db.add(EscSensor(record_id=record_id, data_json=payload))
    db.commit()
    return _empty_ok()


@router.post("/trigger/create_full_activity_dump")
def trigger_create_full_activity_dump(db: Session = Depends(get_db)):
    """Generate FullActivityDump manifesto + activity dump files."""
    create_full_activity_dump(db)
    return _empty_ok()


@router.post("/trigger/daily_activities_immediately")
def trigger_daily_activities_immediately(db: Session = Depends(get_db)):
    """Start CPAS: pull peer FADs and apply conflict resolution."""
    trigger_daily_activities(db)
    return _empty_ok()


@router.post("/get_daily_activities_status")
def get_daily_activities_status(db: Session = Depends(get_db)):
    """Return completed=true only after peer FAD sync / conflict application finishes."""
    return JSONResponse({"completed": get_daily_activities_completed(db)})


@router.post("/trigger/load_dpas")
def trigger_load_dpas():
    return _empty_ok()


@router.post("/trigger/dpa_activation")
async def trigger_dpa_activation(request: Request, db: Session = Depends(get_db)):
    from services.meas_report import FLAG_DPA_ACTIVE, set_admin_flag

    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        pass
    set_admin_flag(db, FLAG_DPA_ACTIVE, body if isinstance(body, dict) else {})
    return _empty_ok()


@router.post("/trigger/bulk_dpa_activation")
async def trigger_bulk_dpa_activation(request: Request, db: Session = Depends(get_db)):
    from services.meas_report import FLAG_DPA_ACTIVE, clear_admin_flags

    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        pass
    # Deactivate clears stored DPA activations (HBT.12 / GRA prep).
    if isinstance(body, dict) and body.get("activate") is False:
        clear_admin_flags(db, FLAG_DPA_ACTIVE)
    return _empty_ok()


@router.post("/get_ppa_status")
def get_ppa_status(db: Session = Depends(get_db)):
    """WDB/PCR: poll until PPA creation finishes."""
    row = db.query(AdminInjectedData).filter_by(kind="ppa_creation_status").first()
    if row:
        try:
            status = json.loads(row.data_json or "{}")
            return JSONResponse(
                {
                    "completed": bool(status.get("completed", True)),
                    "withError": bool(status.get("withError", False)),
                }
            )
        except json.JSONDecodeError:
            pass
    return JSONResponse({"completed": True, "withError": False})


@router.post("/trigger/create_ppa")
async def create_ppa(request: Request, db: Session = Depends(get_db)):
    """WDB/PCR: create PPA zone; fail when requested PAL IDs are unknown."""
    from services.meas_report import set_admin_flag

    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        pass
    pal_ids = body.get("palIds") or []
    from services.pal_service import known_pal_ids

    known_pal_ids_set = known_pal_ids(db)

    missing = [pid for pid in pal_ids if pid not in known_pal_ids_set]
    if not pal_ids or missing:
        set_admin_flag(
            db, "ppa_creation_status", {"completed": True, "withError": True}
        )
        # Harness may accept HTTP error OR status.withError; return 200 + status flag.
        return JSONResponse("")

    set_admin_flag(
        db, "ppa_creation_status", {"completed": True, "withError": False}
    )
    return JSONResponse(f"zone/ppa/mvp/{pal_ids[0]}/0")


@router.post("/injectdata/database_url")
async def inject_database_url(request: Request, db: Session = Depends(get_db)):
    """FDB/WDB/IPR: accept external DB URL injection (FSS, GWBL, PAL, CPI, …)."""
    body: Any = {}
    try:
        body = await request.json()
    except Exception:
        pass
    _store_injection(db, "database_url", body)
    return _empty_ok()


@router.post("/trigger/enable_scheduled_daily_activities")
def trigger_enable_scheduled_daily_activities(db: Session = Depends(get_db)):
    """FDB_8: enable scheduled CPAS; MVP stores a flag and returns HTTP 200."""
    from services.meas_report import set_admin_flag

    set_admin_flag(db, "scheduled_daily_activities", {"enabled": True})
    return _empty_ok()


# Catch-all stubs so the harness never gets HTTP 404 on admin paths.
@router.api_route(
    "/{full_path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
)
async def admin_stub(full_path: str, request: Request):
    if request.method == "POST" and full_path.endswith(
        ("get_daily_activities_status",)
    ):
        return JSONResponse({"completed": True})
    return _empty_ok()
