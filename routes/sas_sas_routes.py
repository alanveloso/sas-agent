"""SAS↔SAS (v1.3) routes: Full Activity Dump + ESC sensor."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import unquote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from database import get_db
from models.models import EscSensor
from services.fad_service import (
    SAS_SAS_VERSION,
    get_dump_file_by_path,
    get_latest_ready_dump,
)
from services.mtls_auth import require_peer_sas

router = APIRouter(prefix=f"/{SAS_SAS_VERSION}", tags=["sas-sas"])


@router.get("/status")
def peer_status(_peer_hash: str = Depends(require_peer_sas)):
    """Deterministic HTTP 200 after successful SAS↔SAS authorization.

    Used by the certificate matrix positive case (handshake + auth + app).
    """
    return {"status": "ok", "peerCertificateHash": _peer_hash}


@router.get("/dump")
def get_full_activity_dump(
    db: Session = Depends(get_db),
    _peer_hash: str = Depends(require_peer_sas),
):
    dump = get_latest_ready_dump(db)
    if dump is None:
        raise HTTPException(status_code=404, detail="No Full Activity Dump available")
    return Response(
        content=dump.manifest_json,
        media_type="application/json",
        status_code=200,
    )


@router.get("/cbsd/{filename}")
def download_cbsd_dump_file(
    filename: str,
    db: Session = Depends(get_db),
    _peer_hash: str = Depends(require_peer_sas),
):
    return _download_dump_file("cbsd", filename, db)


@router.get("/zone/{filename}")
def download_zone_dump_file(
    filename: str,
    db: Session = Depends(get_db),
    _peer_hash: str = Depends(require_peer_sas),
):
    return _download_dump_file("zone", filename, db)


@router.get("/coordination/{filename}")
def download_coordination_dump_file(
    filename: str,
    db: Session = Depends(get_db),
    _peer_hash: str = Depends(require_peer_sas),
):
    return _download_dump_file("coordination", filename, db)


@router.get("/esc_sensor/{path:path}")
def get_esc_sensor_or_dump_file(
    path: str,
    db: Session = Depends(get_db),
    _peer_hash: str = Depends(require_peer_sas),
):
    """
    Serves either:
    - dump file: /v1.3/esc_sensor/activity_dump_file_esc_sensor0.json
    - record:    /v1.3/esc_sensor/{recordId}  (recordId may contain '/')
    """
    decoded = unquote(path)

    if decoded.endswith(".json"):
        return _download_dump_file("esc_sensor", decoded.rsplit("/", 1)[-1], db)

    # Record lookup — accept full id or suffix after esc_sensor/
    record_id = decoded
    row = db.query(EscSensor).filter_by(record_id=record_id).first()
    if row is None and not record_id.startswith("esc_sensor/"):
        row = db.query(EscSensor).filter_by(record_id=f"esc_sensor/{record_id}").first()
    if row is None:
        return JSONResponse({})
    try:
        data: dict[str, Any] = json.loads(row.data_json or "{}")
    except json.JSONDecodeError:
        return JSONResponse({})
    return JSONResponse(data)


def _download_dump_file(
    record_type: str,
    filename: str,
    db: Session,
) -> Response:
    url_path = f"/{SAS_SAS_VERSION}/{record_type}/{filename}"
    fad_file = get_dump_file_by_path(db, url_path)
    if fad_file is None:
        raise HTTPException(status_code=404, detail="Dump file not found")
    return Response(
        content=fad_file.content_json,
        media_type="application/json",
        status_code=200,
    )
