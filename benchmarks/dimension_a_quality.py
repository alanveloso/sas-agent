#!/usr/bin/env python3
"""Dimension A: cyclomatic complexity (Radon) + CBRS-term coupling."""

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
INGESTION_ALLOWLIST = {
    "domain/band_profile.py",
    "services/profile_manager.py",
}
TERM_PATTERNS = {
    "fcc": re.compile(r"\bfcc(?:_id|Id)?\b", re.IGNORECASE),
    "pal": re.compile(r"\bpal\b", re.IGNORECASE),
    "gaa": re.compile(r"\bgaa\b", re.IGNORECASE),
    "cpi": re.compile(r"\bcpi\b", re.IGNORECASE),
}


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _python_files() -> list[Path]:
    files: list[Path] = []
    for folder in (ROOT / "services", ROOT / "domain"):
        if not folder.is_dir():
            continue
        files.extend(sorted(p for p in folder.rglob("*.py") if p.is_file()))
    return files


def _run_radon_json(args: list[str], out_path: Path) -> dict:
    cmd = [sys.executable, "-m", "radon", *args, "-j"]
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    text = (proc.stdout or "").strip()
    out_path.write_text(
        text + (("\n" + proc.stderr) if proc.stderr else ""),
        encoding="utf-8",
    )
    if not text:
        raise RuntimeError(f"radon failed ({proc.returncode}): {proc.stderr}")
    return json.loads(text)


def _normalize_key(key: str) -> str:
    key = key.replace("\\", "/")
    root_s = str(ROOT).replace("\\", "/")
    if key.startswith(root_s):
        return Path(key).relative_to(ROOT).as_posix()
    return key.lstrip("./")


def _count_terms(path: Path) -> dict[str, int]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {k: 0 for k in TERM_PATTERNS}
    return {name: len(pat.findall(text)) for name, pat in TERM_PATTERNS.items()}


def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    targets = ["services", "domain"]

    cc_data = _run_radon_json(["cc", "-s", "-a", *targets], LOGS / "radon_cc.txt")
    raw_data = _run_radon_json(["raw", "-s", *targets], LOGS / "radon_raw.txt")

    cc_by_file: dict[str, dict[str, float]] = {}
    for key, entries in cc_data.items():
        rel = _normalize_key(key)
        if not isinstance(entries, list) or not entries:
            cc_by_file[rel] = {
                "cyclomatic_complexity": 0.0,
                "cc_max": 0.0,
                "cc_n_functions": 0.0,
            }
            continue
        scores = [float(e.get("complexity", 0)) for e in entries]
        cc_by_file[rel] = {
            "cyclomatic_complexity": round(sum(scores) / len(scores), 4),
            "cc_max": max(scores),
            "cc_n_functions": float(len(scores)),
        }

    loc_by_file: dict[str, int] = {}
    for key, metrics in raw_data.items():
        rel = _normalize_key(key)
        if isinstance(metrics, dict):
            loc_by_file[rel] = int(metrics.get("loc", 0))

    quality_rows: list[dict] = []
    term_rows: list[dict] = []

    for path in _python_files():
        rel = _rel(path)
        cc_info = cc_by_file.get(
            rel,
            {"cyclomatic_complexity": 0.0, "cc_max": 0.0, "cc_n_functions": 0.0},
        )
        loc = loc_by_file.get(rel)
        if loc is None:
            loc = sum(
                1
                for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()
                if line.strip()
            )

        counts = _count_terms(path)
        total_terms = sum(counts.values())
        outside = rel not in INGESTION_ALLOWLIST

        quality_rows.append(
            {
                "file": rel,
                "cyclomatic_complexity": cc_info["cyclomatic_complexity"],
                "lines_of_code": loc,
                "cbrs_term_occurrences": total_terms,
            }
        )
        term_rows.append(
            {
                "file": rel,
                "outside_ingestion": str(outside).lower(),
                "fcc": counts["fcc"],
                "pal": counts["pal"],
                "gaa": counts["gaa"],
                "cpi": counts["cpi"],
                "total": total_terms,
                "cc_max": cc_info["cc_max"],
                "cc_n_functions": int(cc_info["cc_n_functions"]),
            }
        )

    quality_rows.sort(key=lambda r: r["file"])
    term_rows.sort(key=lambda r: r["file"])

    out_quality = DATA / "dimension_a_quality.csv"
    with out_quality.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "file",
                "cyclomatic_complexity",
                "lines_of_code",
                "cbrs_term_occurrences",
            ],
        )
        writer.writeheader()
        writer.writerows(quality_rows)

    out_terms = DATA / "dimension_a_terms_raw.csv"
    with out_terms.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "file",
                "outside_ingestion",
                "fcc",
                "pal",
                "gaa",
                "cpi",
                "total",
                "cc_max",
                "cc_n_functions",
            ],
        )
        writer.writeheader()
        writer.writerows(term_rows)

    print(f"Wrote {out_quality}")
    print(f"Wrote {out_terms}")
    leak = sum(
        int(r["total"])
        for r in term_rows
        if r["outside_ingestion"] == "true" and int(r["total"]) > 0
    )
    print(f"Term occurrences outside ingestion allowlist: {leak}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
