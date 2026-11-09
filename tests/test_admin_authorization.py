"""/admin authorization: allowlisted TLS client certificate only, fail-closed."""

from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if "database" not in sys.modules:
    os.environ.setdefault(
        "SAS_DATABASE_PATH", str(Path(tempfile.mkdtemp(prefix="sas_admin_")) / "sas.db")
    )

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import routes.admin_routes as admin_routes  # noqa: E402
from services.mtls_auth import (  # noqa: E402
    admin_fingerprints,
    sha1_fingerprint_colon,
    sha256_fingerprint_colon,
)


def _self_signed(cn: str) -> x509.Certificate:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = dt.datetime.now(dt.timezone.utc)
    return (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )


class _FakeSSLObject:
    def __init__(self, cert: x509.Certificate | None):
        self._der = cert.public_bytes(serialization.Encoding.DER) if cert else None

    def getpeercert(self, binary_form: bool = False):
        return self._der


class _FakeTransport:
    def __init__(self, cert: x509.Certificate | None):
        self._ssl = _FakeSSLObject(cert)

    def get_extra_info(self, name: str):
        return self._ssl if name == "ssl_object" else None


def _app_with_tls_peer(cert: x509.Certificate | None):
    """Mirror patch_uvicorn_for_client_cert(): expose the TLS transport in scope."""
    app = FastAPI()
    app.include_router(admin_routes.router)

    async def asgi(scope, receive, send):
        if scope["type"] == "http" and cert is not None:
            scope["transport"] = _FakeTransport(cert)
        await app(scope, receive, send)

    return asgi


class AdminAuthorizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin = _self_signed("admin")
        cls.cbsd = _self_signed("cbsd")
        cls.admin_fp = sha256_fingerprint_colon(cls.admin)

    def setUp(self):
        self.reset = mock.patch.object(admin_routes, "reset_db").start()
        self.addCleanup(mock.patch.stopall)

    def _post(self, cert, path="/admin/reset", *, allow=None, headers=None):
        env = {} if allow is None else {"SAS_ADMIN_CERT_SHA256": allow}
        with mock.patch.dict(os.environ, env, clear=False):
            if allow is None:
                os.environ.pop("SAS_ADMIN_CERT_SHA256", None)
            client = TestClient(_app_with_tls_peer(cert))
            return client.post(path, headers=headers or {})

    def test_no_client_certificate_is_rejected(self):
        r = self._post(None, allow=self.admin_fp)
        self.assertEqual(r.status_code, 403)
        self.reset.assert_not_called()

    def test_unset_allowlist_is_fail_closed(self):
        r = self._post(self.admin, allow=None)
        self.assertEqual(r.status_code, 403)
        self.reset.assert_not_called()

    def test_certificate_not_in_allowlist_is_rejected(self):
        r = self._post(self.cbsd, allow=self.admin_fp)
        self.assertEqual(r.status_code, 403)
        self.reset.assert_not_called()

    def test_allowlisted_certificate_can_reset(self):
        allow = f"{self.admin_fp.lower().replace(':', '')}, {sha256_fingerprint_colon(_self_signed('x'))}"
        r = self._post(self.admin, allow=allow)
        self.assertEqual(r.status_code, 200, r.text)
        self.reset.assert_called_once()

    def test_sha1_fingerprint_is_not_accepted(self):
        r = self._post(self.admin, allow=sha1_fingerprint_colon(self.admin))
        self.assertEqual(r.status_code, 403)
        self.reset.assert_not_called()

    def test_catch_all_admin_paths_are_protected(self):
        self.assertEqual(self._post(self.cbsd, "/admin/any/path", allow=self.admin_fp).status_code, 403)
        self.assertEqual(self._post(self.admin, "/admin/any/path", allow=self.admin_fp).status_code, 200)

    def test_http_headers_are_not_client_identity(self):
        spoof = {
            "X-SSL-Client-Cert": "spoofed",
            "X-Client-Cert-SHA256": self.admin_fp,
            "X-Forwarded-Client-Cert": f"Hash={self.admin_fp}",
            "SSL_CLIENT_S_DN": "CN=admin",
        }
        for cert in (None, self.cbsd):
            with self.subTest(tls_cert="none" if cert is None else "cbsd"):
                r = self._post(cert, allow=self.admin_fp, headers=spoof)
                self.assertEqual(r.status_code, 403)
        self.reset.assert_not_called()

    def test_fingerprint_parsing(self):
        fp = self.admin_fp
        self.assertEqual(
            admin_fingerprints(f" {fp.lower()} ;{fp.replace(':', '')}, bogus,"),
            frozenset({fp.replace(":", "")}),
        )
        self.assertEqual(admin_fingerprints(""), frozenset())


class DockerRunAdminEnvTests(unittest.TestCase):
    def setUp(self):
        load = str(ROOT / "benchmarks" / "load")
        if load not in sys.path:
            sys.path.insert(0, load)
        import common

        self.common = common
        self.tmp = Path(tempfile.mkdtemp(prefix="sas_admin_certs_"))
        cert = _self_signed("domain_proxy")
        (self.tmp / "domain_proxy.cert").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        self.fp = sha256_fingerprint_colon(cert)

    def _cmd(self, **spec_kw):
        spec = self.common.DockerRunSpec(
            image="img", name="n", host_port=1, certs_dir=self.tmp, **spec_kw
        )
        ok = mock.Mock(returncode=0, stdout="id", stderr="")
        with mock.patch.object(self.common.subprocess, "run", return_value=ok) as run:
            self.common.docker_run(spec)
        return run.call_args_list[-1].args[0]

    def test_bench_client_cert_is_authorized_by_default(self):
        self.assertEqual(self.common.cert_sha256_fingerprint(self.tmp / "domain_proxy.cert"), self.fp)
        self.assertIn(f"SAS_ADMIN_CERT_SHA256={self.fp}", self._cmd())

    def test_explicit_admin_env_is_respected(self):
        cmd = self._cmd(extra_env={"SAS_ADMIN_CERT_SHA256": ""})
        self.assertIn("SAS_ADMIN_CERT_SHA256=", cmd)
        self.assertNotIn(f"SAS_ADMIN_CERT_SHA256={self.fp}", cmd)


if __name__ == "__main__":
    unittest.main()
