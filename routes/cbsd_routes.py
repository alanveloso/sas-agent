"""CBSD-to-SAS v1.2 routes."""

from __future__ import annotations

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
from services.spectrum_inquiry_service import process_spectrum_inquiry

router = APIRouter(prefix="/v1.2", tags=["cbsd-sas"])


def _client_cert_hash(request: Request) -> str | None:
    cert = load_client_certificate(request)
    if cert is None:
        return None
    return sha1_fingerprint_colon(cert)


@router.post("/registration")
def registration(
    request: Request,
    body: RegistrationBatchRequest,
    db: Session = Depends(get_db),
    profile: BandProfile = Depends(get_active_band_profile),
):
    responses = process_registration(
        db,
        body.registrationRequest,
        certificate_hash=_client_cert_hash(request),
        profile=profile,
    )
    return JSONResponse({"registrationResponse": responses})


@router.post("/grant")
def grant(
    request: Request,
    body: GrantBatchRequest,
    db: Session = Depends(get_db),
    profile: BandProfile = Depends(get_active_band_profile),
):
    responses = process_grant(
        db,
        body.grantRequest,
        certificate_hash=_client_cert_hash(request),
        profile=profile,
    )
    return JSONResponse({"grantResponse": responses})


@router.post("/heartbeat")
def heartbeat(
    body: HeartbeatBatchRequest,
    db: Session = Depends(get_db),
    profile: BandProfile = Depends(get_active_band_profile),
):
    responses = process_heartbeat(db, body.heartbeatRequest, profile=profile)
    return JSONResponse({"heartbeatResponse": responses})


@router.post("/spectrumInquiry")
def spectrum_inquiry(
    request: Request,
    body: SpectrumInquiryBatchRequest,
    db: Session = Depends(get_db),
    profile: BandProfile = Depends(get_active_band_profile),
):
    responses = process_spectrum_inquiry(
        db,
        body.spectrumInquiryRequest,
        certificate_hash=_client_cert_hash(request),
        profile=profile,
    )
    return JSONResponse({"spectrumInquiryResponse": responses})


@router.post("/relinquishment")
def relinquishment(
    body: RelinquishmentBatchRequest, db: Session = Depends(get_db)
):
    responses = process_relinquishment(db, body.relinquishmentRequest)
    return JSONResponse({"relinquishmentResponse": responses})


@router.post("/deregistration")
def deregistration(
    body: DeregistrationBatchRequest, db: Session = Depends(get_db)
):
    responses = process_deregistration(db, body.deregistrationRequest)
    return JSONResponse({"deregistrationResponse": responses})
