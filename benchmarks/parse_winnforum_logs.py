"""Parse WInnForum harness ``unittest -v`` logs into a validation CSV.

Consumes ``benchmarks/data/winnforum_<suite>.log`` files produced by
``run_cbrs_tests.sh`` and emits one row per test case with the result and a
pointer (file + line) back to the raw evidence, so report conclusions can be
audited against the source logs.
"""

from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path
from typing import Any

# "test_WINNF_FT_S_REG_1 (testcases.WINNF_FT_S_REG_testcase.RegistrationTestcase) ... ok"
_TEST_START = re.compile(r"^(?P<name>test\w+)\s+\((?P<cls>[\w\.]+)\)(?P<rest>.*)$")
_RESULT_TOKENS = (
    ("... ok", "pass"),
    ("... FAIL", "fail"),
    ("... ERROR", "error"),
    ("... skipped", "skip"),
    ("... expected failure", "expected_failure"),
)
_BARE_RESULTS = {
    "ok": "pass",
    "FAIL": "fail",
    "ERROR": "error",
}
_SUMMARY_RAN = re.compile(r"^Ran (?P<count>\d+) tests? in (?P<secs>[\d\.]+)s")


def _classify(fragment: str) -> str | None:
    for token, result in _RESULT_TOKENS:
        if token in fragment:
            return result
    return None


def parse_log(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    pending: dict[str, Any] | None = None
    suite = path.stem.replace("winnforum_", "")

    for lineno, line in enumerate(
        path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
    ):
        m = _TEST_START.match(line.strip())
        if m:
            result = _classify(m.group("rest"))
            row = {
                "suite": suite,
                "test_case": m.group("name"),
                "test_class": m.group("cls"),
                "result": result,
                "log_file": path.name,
                "log_line": lineno,
            }
            if result is None:
                pending = row
            else:
                rows.append(row)
                pending = None
            continue

        if pending is not None:
            stripped = line.strip()
            result = _BARE_RESULTS.get(stripped) or (
                "skip"
                if stripped.startswith("skipped")
                else "expected_failure"
                if stripped.startswith("expected failure")
                else _classify(stripped)
            )
            if result:
                pending["result"] = result
                pending["log_line"] = lineno
                rows.append(pending)
                pending = None

    if pending is not None:
        pending["result"] = "unknown"
        rows.append(pending)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logs-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    logs = sorted(args.logs_dir.glob("winnforum_*.log"))
    if not logs:
        print(f"No winnforum_*.log files found in {args.logs_dir}")
        return 1

    all_rows: list[dict[str, Any]] = []
    for log in logs:
        rows = parse_log(log)
        all_rows.extend(rows)
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["result"]] = counts.get(r["result"], 0) + 1
        print(f"{log.name}: {len(rows)} cases {counts}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    fields = ["suite", "test_case", "test_class", "result", "log_file", "log_line"]
    with args.out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(all_rows)
    print(f"Wrote {args.out} ({len(all_rows)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
