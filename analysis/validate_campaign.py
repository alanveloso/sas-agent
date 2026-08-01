"""Validate temporal identities and required artifacts for a campaign.

Writes ``artifacts/raw/<id>/validation_report.json`` and exits non-zero on failure.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
RAW = REPO / "artifacts" / "raw"

# Exclusive lifecycle stack (article bars)
LIFE_STAGES = (
    "stage_handler_ms",
    "stage_database_ms",
    "stage_serialization_ms",
    "stage_other_ms",
)
# Cold-start internal additive stack (excludes diag_bind_to_ready_ms)
COLD_INTERNAL = (
    "python_bootstrap_ms",
    "profile_load_ms",
    "database_init_ms",
    "tls_init_ms",
    "internal_other_ms",
)


@dataclass
class Check:
    rule: str
    status: str  # pass | fail
    detail: str = ""
    file: str = ""
    row: int | None = None


@dataclass
class Report:
    campaign_id: str
    status: str
    checks_passed: int = 0
    checks_failed: int = 0
    checks: list[dict[str, Any]] = field(default_factory=list)
    generated_at_utc: str = ""


def _f(v: Any) -> float:
    if v in ("", None):
        return 0.0
    return float(v)


def _add(checks: list[Check], rule: str, ok: bool, detail: str = "", file: str = "", row: int | None = None) -> None:
    checks.append(
        Check(
            rule=rule,
            status="pass" if ok else "fail",
            detail=detail,
            file=file,
            row=row,
        )
    )


def validate_lifecycle(raw_csv: Path, checks: list[Check], *, tol_ms: float = 0.5) -> None:
    rel = str(raw_csv.relative_to(REPO)) if raw_csv.is_relative_to(REPO) else str(raw_csv)
    if not raw_csv.is_file():
        _add(checks, "lifecycle.file_exists", False, "missing raw csv", rel)
        return
    _add(checks, "lifecycle.file_exists", True, file=rel)
    with raw_csv.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    measured = [
        (i, r) for i, r in enumerate(rows)
        if str(r.get("warmup")) in ("0", "False")
        and r.get("exit_code") in ("0", 0)
        and r.get("operation") in (
            "registration",
            "spectrumInquiry",
            "grant",
            "heartbeat",
            "relinquishment",
            "deregistration",
        )
    ]
    _add(checks, "lifecycle.has_measured_rows", bool(measured), f"n={len(measured)}", rel)
    for i, r in measured:
        client = _f(r.get("client_ms", ""))
        app = _f(r.get("application_total_ms", ""))
        ext = _f(r.get("external_ms", ""))
        non_db = _f(r.get("application_non_db_ms", ""))
        db = _f(r.get("database_ms", ""))
        stages = sum(_f(r.get(f, "")) for f in LIFE_STAGES)
        _add(
            checks,
            "lifecycle.client_eq_app_plus_external",
            abs((app + ext) - client) <= tol_ms,
            f"client={client} app+ext={app + ext}",
            rel,
            i,
        )
        _add(
            checks,
            "lifecycle.app_eq_non_db_plus_db",
            abs((non_db + db) - app) <= tol_ms,
            f"app={app} non_db+db={non_db + db}",
            rel,
            i,
        )
        _add(
            checks,
            "lifecycle.app_eq_sum_exclusive_stages",
            abs(stages - app) <= tol_ms,
            f"sum(stages)={stages} app={app} fields={LIFE_STAGES}",
            rel,
            i,
        )
        # Stop flooding: keep first few fails only counted fully; still record all passes lightly
        if sum(1 for c in checks if c.status == "fail" and c.rule.startswith("lifecycle.")) > 30:
            break


def validate_mtls(root: Path, checks: list[Check]) -> None:
    cert = root / "mtls_correctness_overhead" / "cert_matrix_results.csv"
    rel = str(cert.relative_to(REPO)) if cert.is_file() else str(cert)
    if not cert.is_file():
        _add(checks, "mtls.cert_matrix_exists", False, "missing", rel)
        return
    _add(checks, "mtls.cert_matrix_exists", True, file=rel)
    with cert.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    pos = [r for r in rows if r.get("case_id") == "valid_authorized"]
    _add(checks, "mtls.valid_authorized_present", bool(pos), file=rel)
    if pos:
        r = pos[0]
        _add(
            checks,
            "mtls.valid_authorized_http_200",
            str(r.get("http_status")) == "200",
            f"http_status={r.get('http_status')}",
            rel,
        )
        _add(
            checks,
            "mtls.valid_authorized_pass",
            str(r.get("pass")) == "1",
            f"pass={r.get('pass')}",
            rel,
        )
    req = root / "mtls_correctness_overhead" / "tls_mtls_requests.csv"
    rel_r = str(req.relative_to(REPO)) if req.is_file() else str(req)
    if not req.is_file():
        _add(checks, "mtls.per_request_exists", False, "missing tls_mtls_requests.csv", rel_r)
    else:
        with req.open(encoding="utf-8") as fh:
            n = sum(1 for _ in fh) - 1
        _add(checks, "mtls.per_request_exists", True, f"n_rows={n}", rel_r)
        _add(checks, "mtls.per_request_nonempty", n >= 1, f"n_rows={n}", rel_r)


def validate_cold(root: Path, checks: list[Check], *, tol_ms: float = 5.0) -> None:
    cold = root / "instrumented_cold_start" / "unified_coldstart_runs.csv"
    rel = str(cold.relative_to(REPO)) if cold.is_file() else str(cold)
    if not cold.is_file():
        _add(checks, "cold.file_exists", False, "missing", rel)
        return
    _add(checks, "cold.file_exists", True, file=rel)
    with cold.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    _add(checks, "cold.has_rows", bool(rows), f"n={len(rows)}", rel)
    if not rows:
        return
    for need in (
        "cold_start_total_ms",
        "docker_run_to_container_started_ms",
        "internal_total_ms",
        "runtime_and_observation_gap_ms",
        "internal_other_ms",
        "diag_bind_to_ready_ms",
        *COLD_INTERNAL,
    ):
        _add(checks, f"cold.column.{need}", need in rows[0], file=rel)

    fail_budget = 0
    for i, r in enumerate(rows):
        if str(r.get("exit_code")) not in ("0", "0.0"):
            continue
        for k in (*COLD_INTERNAL, "runtime_and_observation_gap_ms", "diag_bind_to_ready_ms"):
            v = r.get(k, "")
            if v in ("", None):
                continue
            ok = float(v) >= 0
            if not ok or fail_budget < 5:
                _add(
                    checks,
                    "cold.non_negative_additive",
                    ok,
                    f"{k}={v}",
                    rel,
                    i,
                )
                if not ok:
                    fail_budget += 1
        total = _f(r.get("cold_start_total_ms"))
        docker = _f(r.get("docker_run_to_container_started_ms"))
        internal = _f(r.get("internal_total_ms"))
        gap = _f(r.get("runtime_and_observation_gap_ms"))
        ok_ext = abs((docker + internal + gap) - total) <= tol_ms
        _add(
            checks,
            "cold.external_identity",
            ok_ext,
            f"docker+internal+gap={docker + internal + gap} total={total}",
            rel,
            i,
        )
        attributed = sum(_f(r.get(k)) for k in COLD_INTERNAL)
        ok_int = abs(attributed - internal) <= tol_ms
        _add(
            checks,
            "cold.internal_identity",
            ok_int,
            f"sum(additive)={attributed} internal={internal}",
            rel,
            i,
        )
        if fail_budget > 20:
            break


def validate_manifest(root: Path, checks: list[Check]) -> None:
    man = root / "campaign_manifest.json"
    rel = str(man.relative_to(REPO)) if man.is_file() else str(man)
    if not man.is_file():
        _add(checks, "manifest.exists", False, "missing campaign_manifest.json", rel)
        return
    _add(checks, "manifest.exists", True, file=rel)
    data = json.loads(man.read_text(encoding="utf-8"))
    _add(checks, "manifest.has_experiments", "experiments" in data, file=rel)
    if data.get("campaign_type") == "article-evaluation" and data.get("git_dirty"):
        _add(
            checks,
            "manifest.official_requires_clean_git",
            False,
            "git_dirty=true on article-evaluation",
            rel,
        )
    else:
        _add(checks, "manifest.official_requires_clean_git", True, "ok_or_smoke", rel)


def validate_campaign(campaign_id: str) -> int:
    root = RAW / campaign_id
    checks: list[Check] = []
    if not root.is_dir():
        print(f"missing campaign dir {root}", file=sys.stderr)
        return 1

    validate_lifecycle(root / "lifecycle_decomposition" / "lifecycle_decomp_raw.csv", checks)
    validate_mtls(root, checks)
    validate_cold(root, checks)
    validate_manifest(root, checks)

    # Deduplicate flood: collapse identical passing rules counted many times is fine;
    # for report, keep all but summarize.
    passed = sum(1 for c in checks if c.status == "pass")
    failed = sum(1 for c in checks if c.status == "fail")
    # Unique fail rules for manifesto clarity
    fail_details = [c for c in checks if c.status == "fail"]

    report = Report(
        campaign_id=campaign_id,
        status="success" if failed == 0 else "failed",
        checks_passed=passed,
        checks_failed=failed,
        checks=[asdict(c) for c in checks],
        generated_at_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    out = root / "validation_report.json"
    out.write_text(json.dumps(asdict(report), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {out}")

    if failed:
        print("VALIDATION FAILED:", file=sys.stderr)
        shown = set()
        for c in fail_details[:25]:
            key = (c.rule, c.detail, c.file, c.row)
            if key in shown:
                continue
            shown.add(key)
            loc = c.file
            if c.row is not None:
                loc = f"{loc}:row={c.row}"
            print(f"  - [{c.rule}] {c.detail} ({loc})", file=sys.stderr)
        return 1
    print(f"VALIDATION OK — {campaign_id} ({passed} checks)")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--campaign-id", required=True)
    args = p.parse_args()
    return validate_campaign(args.campaign_id)


if __name__ == "__main__":
    raise SystemExit(main())
