#!/usr/bin/env python3
"""Dimension C: coverage and pass/fail of the local unit suite."""

from __future__ import annotations

import csv
import json
import re
import subprocess
import sys
from pathlib import Path

SANDBOX = Path(__file__).resolve().parent
ROOT = SANDBOX.parent
DATA = SANDBOX / "data"
LOGS = SANDBOX / "logs"


def _parse_pytest_summary(stdout: str, stderr: str) -> dict:
    passed = failed = skipped = errors = 0
    text = (stdout or "") + "\n" + (stderr or "")
    for line in text.splitlines()[::-1]:
        if not any(k in line for k in ("passed", "failed", "error", "skipped")):
            continue
        for kind, bucket in (
            ("passed", "passed"),
            ("failed", "failed"),
            ("skipped", "skipped"),
            ("errors", "errors"),
            ("error", "errors"),
        ):
            m = re.search(rf"(\d+)\s+{kind}", line)
            if m:
                val = int(m.group(1))
                if bucket == "passed":
                    passed = val
                elif bucket == "failed":
                    failed = val
                elif bucket == "skipped":
                    skipped = val
                else:
                    errors = val
        break
    return {
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "errors": errors,
    }


def _json_to_rows(coverage_json: Path) -> list[dict]:
    data = json.loads(coverage_json.read_text(encoding="utf-8"))
    files = data.get("files") or {}
    rows: list[dict] = []
    for name, meta in files.items():
        summary = meta.get("summary") or {}
        stmts = int(summary.get("num_statements", 0))
        covered = int(summary.get("covered_lines", 0))
        miss = int(summary.get("missing_lines", stmts - covered))
        pct = float(summary.get("percent_covered", 0.0))
        try:
            rel = Path(name).resolve().relative_to(ROOT).as_posix()
        except Exception:  # noqa: BLE001
            rel = name.replace("\\", "/")
        rows.append(
            {
                "file": rel,
                "coverage_percent": round(pct, 2),
                "statements": stmts,
                "miss": miss,
            }
        )
    totals = data.get("totals") or {}
    if totals:
        rows.append(
            {
                "file": "TOTAL",
                "coverage_percent": round(float(totals.get("percent_covered", 0.0)), 2),
                "statements": int(totals.get("num_statements", 0)),
                "miss": int(totals.get("missing_lines", 0)),
            }
        )
    rows.sort(key=lambda r: (r["file"] == "TOTAL", r["file"]))
    return rows


def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    coverage_json = DATA / "coverage_raw.json"
    coverage_raw_csv = DATA / "coverage_raw.csv"
    pytest_log = LOGS / "pytest_output.txt"
    passfail_path = DATA / "dimension_c_passfail.json"
    out_csv = DATA / "dimension_c_coverage.csv"

    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "tests/",
        "-v",
        "--cov=domain",
        "--cov=services",
        "--cov=models",
        "--cov=routes",
        "--cov=schemas",
        "--cov=database",
        "--cov-report=term-missing",
        f"--cov-report=json:{coverage_json}",
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    pytest_log.write_text(
        f"RC={proc.returncode}\nCMD={' '.join(cmd)}\n"
        f"--- STDOUT ---\n{proc.stdout}\n--- STDERR ---\n{proc.stderr}\n",
        encoding="utf-8",
    )

    summary = _parse_pytest_summary(proc.stdout or "", proc.stderr or "")
    passfail = {
        "exit_code": proc.returncode,
        **summary,
        "ok": proc.returncode == 0,
    }
    passfail_path.write_text(json.dumps(passfail, indent=2), encoding="utf-8")

    rows: list[dict] = []
    if coverage_json.is_file():
        rows = _json_to_rows(coverage_json)
        with coverage_raw_csv.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=["Name", "Stmts", "Miss", "Cover"])
            writer.writeheader()
            for r in rows:
                writer.writerow(
                    {
                        "Name": r["file"],
                        "Stmts": r["statements"],
                        "Miss": r["miss"],
                        "Cover": r["coverage_percent"],
                    }
                )
    else:
        # Fallback: coverage.py + unittest
        subprocess.run(
            [
                sys.executable,
                "-m",
                "coverage",
                "run",
                "--source=domain,services,models,routes,schemas,database",
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
            ],
            cwd=str(ROOT),
            check=False,
        )
        subprocess.run(
            [sys.executable, "-m", "coverage", "json", "-o", str(coverage_json)],
            cwd=str(ROOT),
            check=False,
        )
        if coverage_json.is_file():
            rows = _json_to_rows(coverage_json)

    with out_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["file", "coverage_percent", "statements", "miss"],
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {out_csv} ({len(rows)} files)")
    print(f"Wrote {passfail_path}: {passfail}")
    print(f"pytest rc={proc.returncode}")
    return 0 if proc.returncode == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
