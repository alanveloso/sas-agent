"""
SAS MVP entrypoint — HTTPS + mTLS for the WINNF harness.

- RSA endpoint:  https://0.0.0.0:9000  (server.cert)
- ECDSA endpoint: https://0.0.0.0:9001  (server-ecc.cert) — SSS_3 / SSS_4

Usage (from sas_mvp_core/):
  .venv/bin/python main.py
"""

from __future__ import annotations

import ssl
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

# Allow `python main.py` and `uvicorn main:app` from sas_mvp_core/
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from database import init_db
from routes.admin_routes import router as admin_router
from routes.cbsd_routes import router as cbsd_router
from routes.sas_sas_routes import router as sas_sas_router
from services.error_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from services.mtls_auth import (
    ECC_CIPHERS,
    RSA_CIPHERS,
    create_mtls_ssl_context,
    patch_uvicorn_for_client_cert,
)
from services.profile_manager import initialize_active_profile

HARNESS_CERTS = ROOT.parent / "src" / "harness" / "certs"
SSL_CERTFILE = HARNESS_CERTS / "server.cert"
SSL_KEYFILE = HARNESS_CERTS / "server.key"
SSL_ECC_CERTFILE = HARNESS_CERTS / "server-ecc.cert"
SSL_ECC_KEYFILE = HARNESS_CERTS / "server-ecc.key"
SSL_CA_CERTS = HARNESS_CERTS / "ca.cert"
SSL_CRL_DIR = HARNESS_CERTS / "crl"

RSA_PORT = 9000
ECC_PORT = 9001

# Must run before uvicorn binds so RequestResponseCycle exposes the TLS transport.
patch_uvicorn_for_client_cert()

app = FastAPI(title="SAS MVP Core", version="0.1.0")
app.add_exception_handler(RequestValidationError, request_validation_exception_handler)
app.add_exception_handler(StarletteHTTPException, http_exception_handler)
app.include_router(admin_router)
app.include_router(cbsd_router)
app.include_router(sas_sas_router)


@app.on_event("startup")
def on_startup():
    initialize_active_profile()
    init_db()


@app.post("/{version}/registration")
async def registration_unsupported_version(version: str, request: Request):
    """REG_10 / REG_13: unsupported CBSD-SAS protocol version → responseCode 100."""
    if version == "v1.2":
        # Concrete /v1.2/registration route should handle this; reject accidental hits.
        return JSONResponse({"detail": "Use /v1.2/registration"}, status_code=500)
    body = await request.json()
    requests = body.get("registrationRequest") or []
    responses = [{"response": {"responseCode": 100}} for _ in requests]
    return JSONResponse({"registrationResponse": responses})


@app.post("/{version}/heartbeat")
async def heartbeat_unsupported_version(version: str, request: Request):
    """HBT_3: unsupported CBSD-SAS protocol version → responseCode 100."""
    if version == "v1.2":
        return JSONResponse({"detail": "Use /v1.2/heartbeat"}, status_code=500)
    body = await request.json()
    past_s = (datetime.utcnow() - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    responses = []
    for req in body.get("heartbeatRequest") or []:
        responses.append(
            {
                "cbsdId": req.get("cbsdId"),
                "grantId": req.get("grantId"),
                "transmitExpireTime": past_s,
                "response": {"responseCode": 100},
            }
        )
    return JSONResponse({"heartbeatResponse": responses})


def _rsa_ssl_context_factory(config, default_factory):
    del config, default_factory
    return create_mtls_ssl_context(
        certfile=SSL_CERTFILE,
        keyfile=SSL_KEYFILE,
        ca_certs=SSL_CA_CERTS,
        crl_dir=SSL_CRL_DIR,
        ciphers=RSA_CIPHERS,
    )


def _ecc_ssl_context_factory(config, default_factory):
    del config, default_factory
    return create_mtls_ssl_context(
        certfile=SSL_ECC_CERTFILE,
        keyfile=SSL_ECC_KEYFILE,
        ca_certs=SSL_CA_CERTS,
        crl_dir=SSL_CRL_DIR,
        ciphers=ECC_CIPHERS,
    )


def _run_uvicorn(port: int, certfile: Path, keyfile: Path, ssl_factory) -> None:
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        ssl_certfile=str(certfile),
        ssl_keyfile=str(keyfile),
        ssl_ca_certs=str(SSL_CA_CERTS),
        ssl_cert_reqs=ssl.CERT_REQUIRED,
        ssl_context_factory=ssl_factory,
        reload=False,
        log_level="info",
    )


def main():
    missing = [
        p.name
        for p in (SSL_CERTFILE, SSL_KEYFILE, SSL_CA_CERTS)
        if not p.exists()
    ]
    if missing:
        raise SystemExit(
            f"Certificados TLS não encontrados em {HARNESS_CERTS}: {missing}. "
            "Execute: cd src/harness/certs && bash generate_fake_certs.sh"
        )

    if SSL_ECC_CERTFILE.exists() and SSL_ECC_KEYFILE.exists():
        ecc_thread = threading.Thread(
            target=_run_uvicorn,
            kwargs={
                "port": ECC_PORT,
                "certfile": SSL_ECC_CERTFILE,
                "keyfile": SSL_ECC_KEYFILE,
                "ssl_factory": _ecc_ssl_context_factory,
            },
            name="uvicorn-ecc",
            daemon=True,
        )
        ecc_thread.start()
        print(f"ECDSA mTLS listener starting on https://0.0.0.0:{ECC_PORT}")
    else:
        print(
            f"Aviso: {SSL_ECC_CERTFILE.name}/{SSL_ECC_KEYFILE.name} ausentes — "
            "SSS_3/SSS_4 (ECDSA) não estarão disponíveis."
        )

    print(f"RSA mTLS listener starting on https://0.0.0.0:{RSA_PORT}")
    _run_uvicorn(
        port=RSA_PORT,
        certfile=SSL_CERTFILE,
        keyfile=SSL_KEYFILE,
        ssl_factory=_rsa_ssl_context_factory,
    )


if __name__ == "__main__":
    main()
