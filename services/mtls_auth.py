"""mTLS helpers for SAS↔SAS (v1.3) authorization."""

from __future__ import annotations

import hashlib
import logging
import ssl
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import ExtendedKeyUsageOID, ExtensionOID, ObjectIdentifier
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from database import get_db
from models.models import PeerSas

logger = logging.getLogger(__name__)

# WInnForum certificate policy OIDs (cert/openssl.cnf)
OID_ROLE_SAS = ObjectIdentifier("1.3.6.1.4.1.46609.1.1.1")
OID_ZONE = ObjectIdentifier("1.3.6.1.4.1.46609.1.2")

# Ciphers allowed on the SAS↔SAS / CBSD interface (mirrors Fake SAS).
# ECDHE-RSA-AES256-GCM-SHA384 is intentionally excluded (SSS_14).
RSA_CIPHERS = [
    "AES128-GCM-SHA256",
    "AES256-GCM-SHA384",
    "ECDHE-RSA-AES128-GCM-SHA256",
]
ECC_CIPHERS = [
    "ECDHE-ECDSA-AES128-GCM-SHA256",
    "ECDHE-ECDSA-AES256-GCM-SHA384",
]
ALLOWED_CIPHERS = RSA_CIPHERS + ECC_CIPHERS


def sha1_fingerprint_colon(cert: x509.Certificate) -> str:
    """SHA-1 fingerprint in OpenSSL digest format: ``AA:BB:CC:...`` (uppercase)."""
    der = cert.public_bytes(Encoding.DER)
    digest = hashlib.sha1(der).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def load_client_certificate(request: Request) -> Optional[x509.Certificate]:
    """Extract the peer (client) X.509 certificate from the TLS connection."""
    transport = request.scope.get("transport")
    if transport is None:
        return None
    try:
        ssl_object = transport.get_extra_info("ssl_object")
    except Exception:
        return None
    if ssl_object is None:
        return None
    try:
        der = ssl_object.getpeercert(binary_form=True)
    except Exception:
        return None
    if not der:
        return None
    try:
        return x509.load_der_x509_certificate(der)
    except Exception:
        logger.debug("Failed to parse client certificate", exc_info=True)
        return None


def _policy_oids(cert: x509.Certificate) -> set[ObjectIdentifier]:
    try:
        ext = cert.extensions.get_extension_for_oid(ExtensionOID.CERTIFICATE_POLICIES)
    except x509.ExtensionNotFound:
        return set()
    oids: set[ObjectIdentifier] = set()
    for policy in ext.value:
        oids.add(policy.policy_identifier)
    return oids


def _has_client_auth(cert: x509.Certificate) -> bool:
    try:
        ext = cert.extensions.get_extension_for_oid(ExtensionOID.EXTENDED_KEY_USAGE)
        return ExtendedKeyUsageOID.CLIENT_AUTH in ext.value
    except x509.ExtensionNotFound:
        return False


def is_valid_sas_client_certificate(cert: x509.Certificate) -> bool:
    """
    Application-level SAS client cert checks for SSS_10 / SSS_15.

    - Must carry ROLE_SAS policy
    - Must not carry inapplicable ZONE policy
    - Must allow clientAuth
    - Must not be expired (defense in depth; TLS usually rejects first)
    """
    now = datetime.now(timezone.utc)
    try:
        not_before = cert.not_valid_before_utc
        not_after = cert.not_valid_after_utc
    except AttributeError:
        not_before = cert.not_valid_before.replace(tzinfo=timezone.utc)
        not_after = cert.not_valid_after.replace(tzinfo=timezone.utc)
    if now < not_before or now > not_after:
        return False

    policies = _policy_oids(cert)
    if OID_ROLE_SAS not in policies:
        return False
    if OID_ZONE in policies:
        return False
    if not _has_client_auth(cert):
        return False
    return True


def require_peer_sas(
    request: Request,
    db: Session = Depends(get_db),
) -> str:
    """
    FastAPI dependency: authorize SAS↔SAS access via mTLS peer fingerprint.

    Returns the authorized certificate hash, or raises HTTP 403.
    """
    cert = load_client_certificate(request)
    if cert is None:
        raise HTTPException(status_code=403, detail="Client certificate required")

    if not is_valid_sas_client_certificate(cert):
        raise HTTPException(status_code=403, detail="Invalid SAS client certificate")

    cert_hash = sha1_fingerprint_colon(cert)
    peer = db.query(PeerSas).filter_by(certificate_hash=cert_hash).first()
    if peer is None:
        # Harness may store lower-case; compare case-insensitively.
        peer = (
            db.query(PeerSas)
            .filter(PeerSas.certificate_hash.ilike(cert_hash))
            .first()
        )
    if peer is None:
        raise HTTPException(status_code=403, detail="Peer SAS not authorized")
    return cert_hash


def _patch_request_response_cycle(module_path: str, transport_arg: str) -> bool:
    """Inject TLS transport into ASGI scope so load_client_certificate() works."""
    try:
        import importlib

        mod = importlib.import_module(module_path)
        cycle_cls = getattr(mod, "RequestResponseCycle")
    except Exception:
        return False

    if getattr(cycle_cls.__init__, "_sas_mtls_patched", False):
        return True

    original_init = cycle_cls.__init__

    if transport_arg == "second_positional":
        # httptools: (scope, transport, flow, ...)
        def patched_init(self, scope, transport, *args, **kwargs):
            if isinstance(scope, dict):
                scope["transport"] = transport
            return original_init(self, scope, transport, *args, **kwargs)
    else:
        # h11: (scope, conn, transport, flow, ...)
        def patched_init(self, scope, conn, transport, *args, **kwargs):
            if isinstance(scope, dict):
                scope["transport"] = transport
            return original_init(self, scope, conn, transport, *args, **kwargs)

    patched_init._sas_mtls_patched = True  # type: ignore[attr-defined]
    cycle_cls.__init__ = patched_init  # type: ignore[method-assign]
    return True


def patch_uvicorn_for_client_cert() -> None:
    """Expose the asyncio transport on the ASGI scope (uvicorn does not by default)."""
    patched = [
        _patch_request_response_cycle(
            "uvicorn.protocols.http.h11_impl", "third_positional"
        ),
        _patch_request_response_cycle(
            "uvicorn.protocols.http.httptools_impl", "second_positional"
        ),
    ]
    if not any(patched):
        logger.warning("Could not patch uvicorn HTTP protocol for client cert access")
    else:
        logger.info("Patched uvicorn HTTP protocol(s) for mTLS client cert access")


def _load_crl_pems(crl_dir: Path, ctx: ssl.SSLContext) -> None:
    """Load PEM-encoded CRLs so revoked / blacklisted certs fail the handshake."""
    if not crl_dir.is_dir():
        return
    loaded = 0
    for pem in sorted(crl_dir.glob("*.crl.pem")):
        try:
            ctx.load_verify_locations(cafile=str(pem))
            loaded += 1
        except ssl.SSLError as exc:
            logger.warning("Skipping CRL %s: %s", pem.name, exc)
    if loaded:
        ctx.verify_flags |= ssl.VERIFY_CRL_CHECK_CHAIN
        logger.info("Loaded %d CRL PEM file(s) from %s", loaded, crl_dir)


def create_mtls_ssl_context(
    *,
    certfile: Path,
    keyfile: Path,
    ca_certs: Path,
    crl_dir: Path | None = None,
    ciphers: list[str] | None = None,
) -> ssl.SSLContext:
    """
    Build a TLS 1.2+ server context with client-certificate verification (mTLS).

    Mirrors Fake SAS: CERT_REQUIRED, WInnForum CA, restricted cipher list.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(cafile=str(ca_certs))
    if crl_dir is not None:
        _load_crl_pems(crl_dir, ctx)
    ctx.load_cert_chain(certfile=str(certfile), keyfile=str(keyfile))
    ctx.set_ciphers(":".join(ciphers or RSA_CIPHERS))
    return ctx
