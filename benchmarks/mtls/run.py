#!/usr/bin/env python3
"""mTLS correctness + overhead (article experiment B).

Runs certificate matrix then TLS vs mTLS overhead into the same campaign folder.
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
    p.add_argument("--skip-correctness", action="store_true")
    p.add_argument("--skip-overhead", action="store_true")
    args = p.parse_args()
    campaign = args.campaign_id or new_campaign_id("article")
    out = campaign_raw_dir(campaign, "mtls_correctness_overhead")
    rc = 0
    if not args.smoke:
        sys.path.insert(0, str(LOAD))
        from common import wait_thermal_gate  # noqa: E402

        wait_thermal_gate(
            out_path=out / "thermal_gate_mtls_campaign.json",
            label="mtls_campaign",
        )
    if not args.skip_correctness:
        cmd = [
            sys.executable,
            str(LOAD / "cert_matrix_bench.py"),
            "--out-dir",
            str(out),
        ]
        print(" ".join(cmd), flush=True)
        rc |= subprocess.call(cmd, cwd=str(ROOT))
    if not args.skip_overhead:
        cmd = [
            sys.executable,
            str(LOAD / "tls_mtls_overhead_bench.py"),
            "--out-dir",
            str(out),
        ]
        if args.smoke:
            cmd += [
                "--blocks", "2", "--requests-per-block", "10",
                "--warmup", "2", "--cooldown-sec", "0.2", "--skip-thermal-gate",
            ]
        else:
            cmd += ["--blocks", "30", "--requests-per-block", "100", "--warmup", "5"]
            # Persistent path uses the same N; for new connections N=100 is enough.
            # Override persistent density via extra args if desired.
        print(" ".join(cmd), flush=True)
        rc |= subprocess.call(cmd, cwd=str(ROOT))
    print(f"→ {out}", flush=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
