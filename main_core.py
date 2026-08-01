"""
Lightweight SAS core entrypoint — single RSA mTLS listener.

Compared to ``main.py`` (monolithic), this surface:
- mounts only CBSD + admin routers (no SAS–SAS);
- binds a single HTTPS port (9000), no ECC thread;
- exposes ``GET /ready`` for cold-start polling.

Usage:
  python main_core.py
"""

from __future__ import annotations

import ssl
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from database import init_db
from routes.admin_routes import router as admin_router
from routes.cbsd_routes import router as cbsd_router
from services.error_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from services.mtls_auth import (
    RSA_CIPHERS,
    create_mtls_ssl_context,
    patch_uvicorn_for_client_cert,
)
from services.profile_manager import initialize_active_profile

HARNESS_CERTS = ROOT.parent / "src" / "harness" / "certs"
SSL_CERTFILE = HARNESS_CERTS / "server.cert"
SSL_KEYFILE = HARNESS_CERTS / "server.key"
SSL_CA_CERTS = HARNESS_CERTS / "ca.cert"
SSL_CRL_DIR = HARNESS_CERTS / "crl"

RSA_PORT = 9000

patch_uvicorn_for_client_cert()

app = FastAPI(title="SAS Agent Core", version="0.1.0")
app.add_exception_handler(RequestValidationError, request_validation_exception_handler)
app.add_exception_handler(StarletteHTTPException, http_exception_handler)
app.include_router(admin_router)
app.include_router(cbsd_router)


@app.on_event("startup")
def on_startup() -> None:
    initialize_active_profile()
    init_db()


@app.get("/ready")
def ready() -> dict[str, str]:
    """Readiness probe for cold-start / orchestration benchmarks."""
    return {"status": "ok"}


def _rsa_ssl_context_factory(config, default_factory):
    del config, default_factory
    return create_mtls_ssl_context(
        certfile=SSL_CERTFILE,
        keyfile=SSL_KEYFILE,
        ca_certs=SSL_CA_CERTS,
        crl_dir=SSL_CRL_DIR,
        ciphers=RSA_CIPHERS,
    )


def main() -> None:
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

    import uvicorn

    print(f"Core RSA mTLS listener starting on https://0.0.0.0:{RSA_PORT}")
    uvicorn.run(
        "main_core:app",
        host="0.0.0.0",
        port=RSA_PORT,
        ssl_certfile=str(SSL_CERTFILE),
        ssl_keyfile=str(SSL_KEYFILE),
        ssl_ca_certs=str(SSL_CA_CERTS),
        ssl_cert_reqs=ssl.CERT_REQUIRED,
        ssl_context_factory=_rsa_ssl_context_factory,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
