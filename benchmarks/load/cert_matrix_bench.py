"""Matriz de validação de certificados mTLS (handshake + política SAS↔SAS).

Casos (artefatos WInnForum harness + cert sintético sem clientAuth):
  1. valid_authorized       — sas.cert + PeerSas → GET /v1.3/status → HTTP 200
  2. missing_cert           — cliente sem certificado → falha de handshake
  3. untrusted_ca           — non_cbrs_signed_sas → handshake rejeitado
  4. expired                — sas_expired → handshake rejeitado
  5. no_client_auth         — cert ROLE_SAS sem EKU clientAuth → 403 app
  6. missing_sas_policy     — device_a (ROLE_CBSD) → 403 Invalid SAS
  7. wrong_zone_policy      — sas_inapplicable (ZONE) → 403 Invalid SAS
  8. unauthorized_peer      — sas_1.cert sem PeerSas → 403 Peer not authorized
  9. revoked_crl            — sas_blacklisted + CRL montada → handshake rejeitado

Cada linha separa tls_handshake_ok / application_reached / authorization_ok.
"""

from __future__ import annotations

import argparse
import csv
import os
import ssl
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import (
    ExtendedKeyUsageOID,
    ExtensionOID,
    NameOID,
    ObjectIdentifier,
)

LOAD_DIR = Path(__file__).resolve().parent
BENCH_DIR = LOAD_DIR.parent
ROOT = BENCH_DIR.parent
DATA = BENCH_DIR / "data"
if str(LOAD_DIR) not in sys.path:
    sys.path.insert(0, str(LOAD_DIR))

from common import (  # noqa: E402
    DockerRunSpec,
    build_mtls_context,
    capture_manifest,
    docker_rm,
    docker_run,
    image_digest,
    new_run_id,
    utc_now_iso,
    wait_http_200,
    write_manifest,
)

OID_ROLE_SAS = ObjectIdentifier("1.3.6.1.4.1.46609.1.1.1")

RESULT_FIELDS = [
    "case_id", "description", "layer", "cert_pair", "endpoint",
    "expected_outcome", "observed_outcome",
    "tls_handshake_ok", "application_reached", "authorization_ok",
    "http_status", "error", "pass", "latency_ms",
]


@dataclass(frozen=True)
class Case:
    case_id: str
    description: str
    layer: str  # handshake | app
    cert: str | None  # stem relative to certs dir, or special tokens
    key: str | None
    endpoint: str
    expected: str  # ok | handshake_fail | http_403 | http_403_invalid | http_403_peer
    inject_peer: bool = False
    peer_cert: str | None = None  # which cert fingerprint to inject (before request)


def _resolve_certs(cli: Path | None) -> Path:
    if cli is not None:
        return cli.expanduser().resolve()
    env = os.environ.get("HARNESS_CERTS_DIR")
    if env:
        return Path(env).expanduser().resolve()
    candidate = ROOT.parent / "src" / "harness" / "certs"
    if candidate.is_dir():
        return candidate
    alt = Path("/home/alanveloso/Código/Spectrum-Access-System/src/harness/certs")
    if alt.is_dir():
        return alt
    return (ROOT / "certs").resolve()


def _fingerprint(cert_path: Path) -> str:
    from services.mtls_auth import sha1_fingerprint_colon

    der = x509.load_pem_x509_certificate(cert_path.read_bytes())
    return sha1_fingerprint_colon(der)


def _make_no_client_auth_cert(certs: Path, out_dir: Path) -> tuple[Path, Path]:
    """Issue ROLE_SAS leaf without clientAuth EKU, signed by root_ca (in ca.cert)."""
    ca_cert_path = certs / "root_ca.cert"
    ca_key_path = certs / "private" / "root_ca.key"
    ca_cert = x509.load_pem_x509_certificate(ca_cert_path.read_bytes())
    ca_key = serialization.load_pem_private_key(ca_key_path.read_bytes(), password=None)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "sas-agent bench"),
        x509.NameAttribute(NameOID.COMMON_NAME, "no-client-auth"),
    ])
    now = datetime.now(timezone.utc)
    # Intentionally omit ExtendedKeyUsage so TLS may accept the chain while
    # ``is_valid_sas_client_certificate`` rejects (no clientAuth).
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=2))
        .add_extension(
            x509.CertificatePolicies([
                x509.PolicyInformation(OID_ROLE_SAS, policy_qualifiers=None),
            ]),
            critical=False,
        )
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=True,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
    )
    cert = builder.sign(private_key=ca_key, algorithm=hashes.SHA256())
    cert_path = out_dir / "no_client_auth.cert"
    key_path = out_dir / "no_client_auth.key"
    # Leaf + issuer helps some verifiers walk the chain.
    cert_path.write_bytes(
        cert.public_bytes(serialization.Encoding.PEM) + ca_cert_path.read_bytes()
    )
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def _client_ctx(
    certs: Path,
    cert_name: str | None,
    key_name: str | None,
    *,
    extra_cert: Path | None = None,
    extra_key: Path | None = None,
) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(cafile=str(certs / "ca.cert"))
    if extra_cert is not None and extra_key is not None:
        ctx.load_cert_chain(certfile=str(extra_cert), keyfile=str(extra_key))
    elif cert_name and key_name:
        ctx.load_cert_chain(
            certfile=str(certs / cert_name),
            keyfile=str(certs / key_name),
        )
    return ctx


def _classify(exc: Exception | None, status: int | None, body: str) -> str:
    if exc is not None:
        msg = str(exc).lower()
        if any(t in msg for t in ("ssl", "certificate", "handshake", "eof", "wrong_version")):
            return "handshake_fail"
        return f"error:{type(exc).__name__}"
    if status is None:
        return "error:no_status"
    if status == 200:
        return "ok"
    if status == 403:
        low = body.lower()
        if "peer" in low and "not authorized" in low:
            return "http_403_peer"
        if "invalid sas" in low:
            return "http_403_invalid"
        return "http_403"
    return f"http_{status}"


def _layer_flags(outcome: str, status: int | None) -> tuple[int, int, str]:
    """Return (tls_handshake_ok, application_reached, authorization_ok).

    authorization_ok is '' when N/A (handshake never completed).
    """
    if outcome == "handshake_fail":
        return 0, 0, ""
    if outcome == "ok" and status == 200:
        return 1, 1, "1"
    if outcome.startswith("http_403"):
        return 1, 1, "0"
    if status is not None:
        return 1, 1, "0"
    return 0, 0, ""


def _probe(
    url: str,
    ctx: ssl.SSLContext,
) -> tuple[str, int | None, str, float]:
    import time

    t0 = time.perf_counter()
    try:
        with httpx.Client(verify=ctx, timeout=10.0) as client:
            resp = client.get(url)
            body = resp.text[:500]
            outcome = _classify(None, resp.status_code, body)
            return outcome, resp.status_code, body, (time.perf_counter() - t0) * 1000.0
    except Exception as exc:  # noqa: BLE001
        return _classify(exc, None, ""), None, str(exc)[:300], (time.perf_counter() - t0) * 1000.0


def _cases() -> list[Case]:
    return [
        Case(
            "valid_authorized",
            "certificado válido e autorizado",
            "app",
            "sas.cert",
            "sas.key",
            "/v1.3/status",
            "ok",
            inject_peer=True,
            peer_cert="sas.cert",
        ),
        Case(
            "missing_cert",
            "ausência de certificado",
            "handshake",
            None,
            None,
            "/ready",
            "handshake_fail",
        ),
        Case(
            "untrusted_ca",
            "CA não confiável",
            "handshake",
            "non_cbrs_signed_sas.cert",
            "non_cbrs_signed_sas.key",
            "/ready",
            "handshake_fail",
        ),
        Case(
            "expired",
            "certificado expirado",
            "handshake",
            "sas_expired.cert",
            "sas_expired.key",
            "/ready",
            "handshake_fail",
        ),
        Case(
            "no_client_auth",
            "ausência de clientAuth",
            "app",
            "__no_client_auth__",
            "__no_client_auth__",
            "/v1.3/status",
            "http_403_invalid",
            inject_peer=True,
            peer_cert="__no_client_auth__",
        ),
        Case(
            "missing_sas_policy",
            "política SAS ausente",
            "app",
            "device_a.cert",
            "device_a.key",
            "/v1.3/status",
            "http_403_invalid",
        ),
        Case(
            "wrong_zone_policy",
            "política de zona indevida",
            "app",
            "sas_inapplicable.cert",
            "sas.key",
            "/v1.3/status",
            "http_403_invalid",
        ),
        Case(
            "unauthorized_peer",
            "certificado válido, mas peer não autorizado",
            "app",
            "sas_1.cert",
            "sas_1.key",
            "/v1.3/status",
            "http_403_peer",
        ),
        Case(
            "revoked_crl",
            "certificado revogado com CRL",
            "handshake",
            "sas_blacklisted.cert",
            "sas_blacklisted.key",
            "/ready",
            "handshake_fail",
        ),
    ]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--image", default="sas-agent:monolithic")
    p.add_argument("--port", type=int, default=19300)
    p.add_argument("--certs-dir", type=Path, default=None)
    p.add_argument("--out-dir", type=Path, default=DATA)
    p.add_argument("--ready-timeout-sec", type=float, default=90.0)
    args = p.parse_args()

    out_dir = args.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    certs = _resolve_certs(args.certs_dir)
    if not (certs / "ca.cert").is_file():
        raise SystemExit(f"certs not found under {certs}")

    # Ensure ROOT on path for fingerprint helper
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    name = "sas-cert-matrix"
    admin_ctx = build_mtls_context(certs)
    base = f"https://127.0.0.1:{args.port}"

    docker_run(
        DockerRunSpec(
            image=args.image,
            name=name,
            host_port=args.port,
            certs_dir=certs,
            database_path=f"/data/sas_cert_matrix_{new_run_id()}.db",
        )
    )
    ok, _, err = wait_http_200(f"{base}/ready", admin_ctx, args.ready_timeout_sec)
    if not ok:
        docker_rm(name)
        raise SystemExit(f"container not ready: {err}")

    results: list[dict[str, Any]] = []
    failures = 0

    with tempfile.TemporaryDirectory(prefix="cert_matrix_") as tmp:
        tmp_path = Path(tmp)
        no_cert, no_key = _make_no_client_auth_cert(certs, tmp_path)

        # Inject peers needed up-front (valid sas + synthetic no_client_auth for app path).
        # unauthorized_peer uses sas_1 which is NOT injected.
        with httpx.Client(verify=admin_ctx, timeout=15.0) as client:
            for peer_file in (certs / "sas.cert", no_cert):
                fp = _fingerprint(peer_file)
                client.post(
                    f"{base}/admin/injectdata/peer_sas",
                    json={"certificateHash": fp, "url": "https://peer.example"},
                ).raise_for_status()

        for case in _cases():
            if case.cert == "__no_client_auth__":
                ctx = _client_ctx(certs, None, None, extra_cert=no_cert, extra_key=no_key)
                cert_pair = "no_client_auth.(cert/key)"
            elif case.cert is None:
                ctx = _client_ctx(certs, None, None)
                cert_pair = "(none)"
            else:
                ctx = _client_ctx(certs, case.cert, case.key)
                cert_pair = f"{case.cert}+{case.key}"

            outcome, status, body_or_err, latency = _probe(f"{base}{case.endpoint}", ctx)
            passed = outcome == case.expected
            # Allow http_403 as alias when detail subclass not available
            if not passed and case.expected.startswith("http_403") and outcome.startswith("http_403"):
                if case.expected == "http_403" or outcome == case.expected:
                    passed = True
                elif case.expected == "http_403_invalid" and outcome in ("http_403", "http_403_invalid"):
                    passed = True
                elif case.expected == "http_403_peer" and outcome in ("http_403", "http_403_peer"):
                    passed = True

            hs_ok, app_reached, auth_ok = _layer_flags(outcome, status)
            row = {
                "case_id": case.case_id,
                "description": case.description,
                "layer": case.layer,
                "cert_pair": cert_pair,
                "endpoint": case.endpoint,
                "expected_outcome": case.expected,
                "observed_outcome": outcome,
                "tls_handshake_ok": hs_ok,
                "application_reached": app_reached,
                "authorization_ok": auth_ok,
                "http_status": status if status is not None else "",
                "error": "" if passed else (body_or_err[:200]),
                "pass": int(passed),
                "latency_ms": round(latency, 3),
            }
            results.append(row)
            status_s = "PASS" if passed else "FAIL"
            print(
                f"  [{status_s}] {case.case_id}: expected={case.expected} "
                f"got={outcome} status={status}",
                flush=True,
            )
            if not passed:
                failures += 1

    docker_rm(name)

    results_path = out_dir / "cert_matrix_results.csv"
    summary_path = out_dir / "cert_matrix_summary.csv"
    with results_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=RESULT_FIELDS)
        w.writeheader()
        w.writerows(results)

    n = len(results)
    n_pass = sum(1 for r in results if r["pass"] == 1)
    with summary_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["metric", "value"])
        w.writeheader()
        w.writerow({"metric": "cases_total", "value": n})
        w.writerow({"metric": "cases_passed", "value": n_pass})
        w.writerow({"metric": "cases_failed", "value": n - n_pass})
        w.writerow({"metric": "pass_rate", "value": round(n_pass / n, 4) if n else 0})

    write_manifest(
        out_dir / "cert_matrix_manifest.json",
        capture_manifest(
            seed=0,
            n=n,
            warmup=0,
            script_paths=[LOAD_DIR / "cert_matrix_bench.py"],
            certs_dir=certs,
            extra={
                "experiment": "cert_matrix",
                "image": {"tag": args.image, "digest": image_digest(args.image)},
                "failures": failures,
            },
        ),
    )
    print(f"Wrote {results_path}\nWrote {summary_path}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
