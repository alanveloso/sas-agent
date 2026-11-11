"""Certificate revocation vs per-request authorization on persistent mTLS connections."""

from __future__ import annotations

import datetime as dt
import socket
import ssl
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
LOAD_DIR = ROOT / "benchmarks" / "load"
for p in (str(ROOT), str(LOAD_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

NOW = dt.datetime.now(dt.timezone.utc)
ROLE_SAS = x509.ObjectIdentifier("1.3.6.1.4.1.46609.1.1.1")


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _cert(subject_cn, key, issuer_cert=None, issuer_key=None, *, ca=False, eku=None, policy=None):
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject_cn)])
    b = (x509.CertificateBuilder()
         .subject_name(name)
         .issuer_name(issuer_cert.subject if issuer_cert else name)
         .public_key(key.public_key())
         .serial_number(x509.random_serial_number())
         .not_valid_before(NOW - dt.timedelta(hours=1))
         .not_valid_after(NOW + dt.timedelta(days=1))
         .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
    if eku:
        b = b.add_extension(x509.ExtendedKeyUsage(eku), critical=False)
    if policy:
        b = b.add_extension(
            x509.CertificatePolicies([x509.PolicyInformation(policy, None)]), critical=False)
    return b.sign(issuer_key or key, hashes.SHA256())


def _pem(obj) -> bytes:
    if isinstance(obj, rsa.RSAPrivateKey):
        return obj.private_bytes(serialization.Encoding.PEM,
                                 serialization.PrivateFormat.TraditionalOpenSSL,
                                 serialization.NoEncryption())
    return obj.public_bytes(serialization.Encoding.PEM)


class Pki:
    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="pki_"))
        self.ca_key = _key()
        self.ca = _cert("test-ca", self.ca_key, ca=True)
        srv_key, cli_key = _key(), _key()
        srv = _cert("localhost", srv_key, self.ca, self.ca_key,
                    eku=[ExtendedKeyUsageOID.SERVER_AUTH])
        self.client = _cert("peer-sas", cli_key, self.ca, self.ca_key,
                            eku=[ExtendedKeyUsageOID.CLIENT_AUTH], policy=ROLE_SAS)
        for name, obj in (("ca.cert", self.ca), ("server.cert", srv), ("server.key", srv_key),
                          ("client.cert", self.client), ("client.key", cli_key)):
            (self.dir / name).write_bytes(_pem(obj))
        self.crl_dir = self.dir / "crl"
        self.crl_dir.mkdir()
        self.write_crl(revoked=False)

    def write_crl(self, *, revoked: bool) -> None:
        b = (x509.CertificateRevocationListBuilder()
             .issuer_name(self.ca.subject)
             .last_update(NOW - dt.timedelta(minutes=5))
             .next_update(NOW + dt.timedelta(days=1)))
        if revoked:
            b = b.add_revoked_certificate(
                x509.RevokedCertificateBuilder()
                .serial_number(self.client.serial_number)
                .revocation_date(NOW - dt.timedelta(minutes=1)).build())
        crl = b.sign(self.ca_key, hashes.SHA256())
        (self.crl_dir / "ca.crl.pem").write_bytes(crl.public_bytes(serialization.Encoding.PEM))

    def server_ctx(self):
        from services.mtls_auth import create_mtls_ssl_context
        return create_mtls_ssl_context(
            certfile=self.dir / "server.cert", keyfile=self.dir / "server.key",
            ca_certs=self.dir / "ca.cert", crl_dir=self.crl_dir, client_auth="required")

    def client_ctx(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.minimum_version = ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        ctx.check_hostname = False
        ctx.load_verify_locations(cafile=str(self.dir / "ca.cert"))
        ctx.load_cert_chain(str(self.dir / "client.cert"), str(self.dir / "client.key"))
        return ctx


class EchoServer:
    """TLS echo server with a fixed SSLContext (as uvicorn: built once at startup)."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                raw, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(raw,), daemon=True).start()

    def _handle(self, raw):
        try:
            with self.ctx.wrap_socket(raw, server_side=True) as conn:
                while data := conn.recv(1024):
                    conn.sendall(data)
        except (ssl.SSLError, OSError):
            pass

    def connect(self, client_ctx):
        s = client_ctx.wrap_socket(socket.create_connection(("127.0.0.1", self.port), timeout=5))
        s.sendall(b"ping")
        assert s.recv(16) == b"ping"   # TLS 1.2: client cert is checked before this
        return s

    def close(self):
        self.sock.close()


class CrlSemanticsTests(unittest.TestCase):
    """Scenario A: CRL is enforced only at handshake, from the context's load time."""

    @classmethod
    def setUpClass(cls):
        cls.pki = Pki()

    def setUp(self):
        self.pki.write_crl(revoked=False)

    def test_crl_update_on_disk_affects_neither_open_connection_nor_running_context(self):
        server = EchoServer(self.pki.server_ctx())
        self.addCleanup(server.close)
        conn = server.connect(self.pki.client_ctx())
        self.addCleanup(conn.close)

        self.pki.write_crl(revoked=True)

        conn.sendall(b"again")                       # same TLS connection keeps working
        self.assertEqual(conn.recv(16), b"again")
        second = server.connect(self.pki.client_ctx())   # new handshake, same context
        second.close()

    def test_context_built_after_crl_update_rejects_revoked_certificate(self):
        self.pki.write_crl(revoked=True)
        server = EchoServer(self.pki.server_ctx())
        self.addCleanup(server.close)
        with self.assertRaisesRegex(ssl.SSLError, "CERTIFICATE_REVOKED"):
            server.connect(self.pki.client_ctx())


class PerRequestPeerAuthorizationTests(unittest.TestCase):
    """Scenario B: SAS↔SAS allowlist is re-read on every request of a connection."""

    def setUp(self):
        import services.mtls_auth as mtls
        from database import Base

        self.mtls = mtls
        self.pki = CrlSemanticsTests.pki if hasattr(CrlSemanticsTests, "pki") else Pki()
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        der = self.pki.client.public_bytes(serialization.Encoding.DER)
        ssl_obj = SimpleNamespace(getpeercert=lambda binary_form=False: der)
        transport = SimpleNamespace(get_extra_info=lambda n: ssl_obj if n == "ssl_object" else None)
        self.request = SimpleNamespace(scope={"transport": transport})   # one connection
        self.fp = mtls.sha1_fingerprint_colon(self.pki.client)

    def test_removed_peer_is_rejected_on_next_request_of_same_connection(self):
        PeerSas = self.mtls.PeerSas
        self.db.add(PeerSas(certificate_hash=self.fp, url="https://peer"))
        self.db.commit()
        self.assertEqual(self.mtls.require_peer_sas(self.request, db=self.db), self.fp)

        self.db.query(PeerSas).filter_by(certificate_hash=self.fp).delete()
        self.db.commit()
        with self.assertRaises(HTTPException) as cm:
            self.mtls.require_peer_sas(self.request, db=self.db)
        self.assertEqual(cm.exception.status_code, 403)

    def test_peer_added_later_is_accepted_on_same_connection(self):
        with self.assertRaises(HTTPException):
            self.mtls.require_peer_sas(self.request, db=self.db)
        self.db.add(self.mtls.PeerSas(certificate_hash=self.fp.lower(), url="https://peer"))
        self.db.commit()
        self.assertEqual(self.mtls.require_peer_sas(self.request, db=self.db), self.fp)


class BenchObservedFieldsTests(unittest.TestCase):
    def setUp(self):
        import tls_mtls_overhead_bench as bench
        self.bench = bench

    def _resp(self, cipher="AES128-GCM-SHA256", resumed=False):
        ssl_obj = SimpleNamespace(cipher=lambda: (cipher, "TLSv1.2", 128),
                                  version=lambda: "TLSv1.2", session_reused=resumed)
        stream = SimpleNamespace(get_extra_info=lambda n: ssl_obj if n == "ssl_object" else None)
        return SimpleNamespace(extensions={"network_stream": stream})

    def test_new_connection_spans(self):
        t = {"connection.connect_tcp.started": 1.000, "connection.connect_tcp.complete": 1.001,
             "connection.start_tls.started": 1.001, "connection.start_tls.complete": 1.004}
        f = self.bench._observed_fields(t, 6.0, self._resp())
        self.assertEqual(f["connection_reused_observed"], 0)
        self.assertAlmostEqual(f["tcp_connect_ms"], 1.0, places=3)
        self.assertAlmostEqual(f["tls_handshake_ms"], 3.0, places=3)
        self.assertAlmostEqual(f["request_exchange_ms"], 2.0, places=3)
        self.assertEqual((f["tls_version"], f["tls_cipher"], f["tls_session_reused"]),
                         ("TLSv1.2", "AES128-GCM-SHA256", 0))

    def test_reused_connection_has_no_connect_spans(self):
        f = self.bench._observed_fields({}, 0.8, self._resp())
        self.assertEqual(f["connection_reused_observed"], 1)
        self.assertEqual((f["tcp_connect_ms"], f["tls_handshake_ms"]), ("", ""))
        self.assertEqual(f["request_exchange_ms"], 0.8)

    def test_handshake_ssl_object_survives_closed_stream(self):
        tr = self.bench._Trace()
        captured = self._resp(cipher="ECDHE-RSA-AES128-GCM-SHA256").extensions["network_stream"]
        tr("connection.start_tls.complete", {"return_value": captured})
        closed = SimpleNamespace(extensions={"network_stream": SimpleNamespace(
            get_extra_info=lambda n: None)})
        f = self.bench._observed_fields(tr.t, 1.0, closed, tr.ssl_object)
        self.assertEqual(f["tls_cipher"], "ECDHE-RSA-AES128-GCM-SHA256")
        self.assertEqual(f["tls_session_reused"], 0)

    def test_trace_keeps_first_event_time(self):
        tr = self.bench._Trace()
        tr("connection.connect_tcp.started", {})
        first = tr.t["connection.connect_tcp.started"]
        tr("connection.connect_tcp.started", {})
        self.assertEqual(tr.t["connection.connect_tcp.started"], first)

    def test_request_fields_cover_observed_columns(self):
        f = self.bench._observed_fields({}, 1.0, self._resp())
        self.assertLessEqual(set(f), set(self.bench.REQ_FIELDS))


if __name__ == "__main__":
    unittest.main()
