#!/usr/bin/env python3
"""Persistence decomposition (article experiment C).

SQLite commit / batch / memory; optional Postgres via --with-postgres.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "benchmarks"))

from benchkit.campaign_io import LOAD, campaign_raw_dir, new_campaign_id  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--campaign-id", default=None)
    p.add_argument("--smoke", action="store_true")
    p.add_argument("--with-postgres", action="store_true")
    args = p.parse_args()
    campaign = args.campaign_id or new_campaign_id("article")
    out = campaign_raw_dir(campaign, "persistence_decomposition")
    modes = ["sqlite_commit", "sqlite_batch", "sqlite_memory"]
    if args.with_postgres:
        modes.append("postgres")
    cmd = [
        sys.executable,
        str(LOAD / "persistence_modes_bench.py"),
        "--out-dir",
        str(out),
        "--modes",
        *modes,
    ]
    if args.smoke:
        cmd += ["--repeats", "1", "--populations", "133", "--skip-thermal-gate"]
    else:
        cmd += ["--repeats", "20", "--populations", "133", "1000"]
    print(" ".join(cmd), flush=True)
    print(f"→ {out}", flush=True)
    return subprocess.call(cmd, cwd=str(ROOT))


if __name__ == "__main__":
    raise SystemExit(main())
