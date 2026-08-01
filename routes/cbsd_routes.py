"""CBSD-to-SAS v1.2 routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from database import get_db
from domain.band_profile import BandProfile
from schemas.deregistration import DeregistrationBatchRequest
from schemas.grant import GrantBatchRequest
from schemas.heartbeat import HeartbeatBatchRequest
from schemas.registration import RegistrationBatchRequest
from schemas.relinquishment import RelinquishmentBatchRequest
from schemas.spectrum_inquiry import SpectrumInquiryBatchRequest
from services.deregistration_service import process_deregistration
from services.grant_service import process_grant
from services.heartbeat_service import process_heartbeat
from services.mtls_auth import load_client_certificate, sha1_fingerprint_colon
from services.profile_manager import get_active_band_profile
from services.registration_service import process_registration
from services.relinquishment_service import process_relinquishment
from services.request_timing import clear_timing, start_timing, timed_span, timing_enabled
from services.spectrum_inquiry_service import process_spectrum_inquiry

router = APIRouter(prefix="/v1.2", tags=["cbsd-sas"])


def _client_cert_hash(request: Request) -> str | None:
    cert = load_client_certificate(request)
    if cert is None:
        return None
    return sha1_fingerprint_colon(cert)


def _run_op(operation: str, fn, request: Request | None = None) -> JSONResponse:
    bag = start_timing(operation)
    try:
        if bag is not None and request is not None:
            cl = request.headers.get("content-length")
            if cl and cl.isdigit():
                bag.extra["request_bytes"] = int(cl)
        with timed_span("decision"):
            payload = fn()
        with timed_span("serialization"):
            resp = JSONResponse(payload)
        if bag is not None and timing_enabled():
            try:
                bag.extra["response_bytes"] = len(resp.body or b"")
            except Exception:  # noqa: BLE001
                pass
            resp.headers["X-SAS-Timing"] = bag.header_value()
        return resp
    finally:
        clear_timing()


@router.post("/registration")
def registration(
    request: Request,
    body: RegistrationBatchRequest,
    db: Session = Depends(get_db),
    profile: BandProfile = Depends(get_active_band_profile),
):
    def _body() -> dict[str, Any]:
        responses = process_registration(
            db,
            body.registrationRequest,
            certificate_hash=_client_cert_hash(request),
            profile=profile,
        )
        return {"registrationResponse": responses}

    return _run_op("registration", _body, request)


@router.post("/grant")
def grant(
    request: Request,
    body: GrantBatchRequest,
    db: Session = Depends(get_db),
    profile: BandProfile = Depends(get_active_band_profile),
):
    def _body() -> dict[str, Any]:
        responses = process_grant(
            db,
            body.grantRequest,
            certificate_hash=_client_cert_hash(request),
            profile=profile,
        )
        return {"grantResponse": responses}

    return _run_op("grant", _body, request)


@router.post("/heartbeat")
def heartbeat(
    request: Request,
    body: HeartbeatBatchRequest,
    db: Session = Depends(get_db),
    profile: BandProfile = Depends(get_active_band_profile),
):
    def _body() -> dict[str, Any]:
        responses = process_heartbeat(db, body.heartbeatRequest, profile=profile)
        return {"heartbeatResponse": responses}

    return _run_op("heartbeat", _body, request)


@router.post("/spectrumInquiry")
def spectrum_inquiry(
    request: Request,
    body: SpectrumInquiryBatchRequest,
    db: Session = Depends(get_db),
    profile: BandProfile = Depends(get_active_band_profile),
):
    def _body() -> dict[str, Any]:
        responses = process_spectrum_inquiry(
            db,
            body.spectrumInquiryRequest,
            certificate_hash=_client_cert_hash(request),
            profile=profile,
        )
        return {"spectrumInquiryResponse": responses}

    return _run_op("spectrumInquiry", _body, request)


@router.post("/relinquishment")
def relinquishment(
    body: RelinquishmentBatchRequest, db: Session = Depends(get_db)
):
    def _body() -> dict[str, Any]:
        responses = process_relinquishment(db, body.relinquishmentRequest)
        return {"relinquishmentResponse": responses}

    return _run_op("relinquishment", _body)


@router.post("/deregistration")
def deregistration(
    body: DeregistrationBatchRequest, db: Session = Depends(get_db)
):
    def _body() -> dict[str, Any]:
        responses = process_deregistration(db, body.deregistrationRequest)
        return {"deregistrationResponse": responses}

    return _run_op("deregistration", _body)
