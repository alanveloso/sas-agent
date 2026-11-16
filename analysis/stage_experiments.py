"""Ensure ``artifacts/raw/experiments`` points at the published collection staging.

Creates a symlink farm so existing validators and table/figure generators work
without duplicating CSVs. Safe to call repeatedly.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EXPERIMENTS = REPO / "evidence" / "experiments"
STAGE_SRC = EXPERIMENTS / "analysis_stage"
RAW = REPO / "artifacts" / "raw"
RAW_LINK = RAW / "experiments"


def stage_experiments() -> Path:
    if not STAGE_SRC.is_dir():
        raise FileNotFoundError(
            f"missing {STAGE_SRC.relative_to(REPO)}; run: ./scripts/evaluate.sh publish-collection"
        )
    RAW.mkdir(parents=True, exist_ok=True)
    if RAW_LINK.is_symlink() or RAW_LINK.exists():
        if RAW_LINK.is_symlink():
            RAW_LINK.unlink()
        elif RAW_LINK.is_dir():
            # refuse to delete a real directory of data
            raise RuntimeError(f"{RAW_LINK} exists and is not a symlink")
        else:
            RAW_LINK.unlink()
    # Prefer relative symlink for portability inside the clone.
    rel = Path("..") / ".." / "evidence" / "experiments" / "analysis_stage"
    RAW_LINK.symlink_to(rel)
    # Ensure campaign_manifest exists
    man = STAGE_SRC / "campaign_manifest.json"
    if not man.is_file():
        man.write_text(
            json.dumps(
                {
                    "campaign_id": "experiments",
                    "campaign_type": "published_experimental_collection",
                    "experiments": {},
                    "phases": ["functional", "lifecycle", "analysis"],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return RAW_LINK


if __name__ == "__main__":
    p = stage_experiments()
    print("staged", p)
