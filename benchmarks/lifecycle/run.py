#!/usr/bin/env python3
"""Lifecycle latency decomposition (article experiment A).

Thin wrapper → ``benchmarks/load/lifecycle_decomp_bench.py``.
Writes under ``artifacts/raw/<campaign_id>/lifecycle_decomposition/``.
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
    p.add_argument("extra", nargs=argparse.REMAINDER, help="Args after -- forwarded to bench")
    args = p.parse_args()
    campaign = args.campaign_id or new_campaign_id("article")
    out = campaign_raw_dir(campaign, "lifecycle_decomposition")
    cmd = [sys.executable, str(LOAD / "lifecycle_decomp_bench.py"), "--out-dir", str(out)]
    if args.smoke:
        cmd += ["--n", "3", "--warmup", "1", "--profile", "cbrs_winnforum", "--skip-thermal-gate"]
    else:
        cmd += ["--n", "30", "--warmup", "3"]
    # strip leading -- from remainder
    extra = list(args.extra)
    if extra and extra[0] == "--":
        extra = extra[1:]
    cmd += extra
    print(" ".join(cmd), flush=True)
    print(f"→ {out}", flush=True)
    return subprocess.call(cmd, cwd=str(ROOT))


if __name__ == "__main__":
    raise SystemExit(main())
