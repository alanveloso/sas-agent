#!/usr/bin/env python3
"""Instrumented cold start (article experiment D)."""

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
    p.add_argument("extra", nargs=argparse.REMAINDER)
    args = p.parse_args()
    campaign = args.campaign_id or new_campaign_id("article")
    out = campaign_raw_dir(campaign, "instrumented_cold_start")
    cmd = [sys.executable, str(LOAD / "unified_coldstart.py"), "--out-dir", str(out)]
    if args.smoke:
        cmd += ["--n", "2", "--cooldown-sec", "0.5", "--skip-thermal-gate"]
    else:
        cmd += ["--n", "30", "--cooldown-sec", "3"]
    extra = list(args.extra)
    if extra and extra[0] == "--":
        extra = extra[1:]
    cmd += extra
    print(" ".join(cmd), flush=True)
    print(f"→ {out}", flush=True)
    return subprocess.call(cmd, cwd=str(ROOT))


if __name__ == "__main__":
    raise SystemExit(main())
