"""Campaign directories under ``artifacts/`` and shared path helpers."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = REPO_ROOT / "artifacts"
RAW = ARTIFACTS / "raw"
PROCESSED = ARTIFACTS / "processed"
FIGURES = ARTIFACTS / "figures"
TABLES = ARTIFACTS / "tables"
MANIFESTS = ARTIFACTS / "manifests"
BENCH = REPO_ROOT / "benchmarks"
LOAD = BENCH / "load"


def utc_stamp_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def slug(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", text.strip().lower())
    return s.strip("-") or "run"


def new_campaign_id(label: str = "article") -> str:
    """e.g. 2026-08-01_article-eval"""
    return f"{utc_stamp_day()}_{slug(label)}"


def campaign_raw_dir(campaign_id: str, experiment_id: str) -> Path:
    path = RAW / campaign_id / slug(experiment_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def campaign_manifest_path(campaign_id: str) -> Path:
    MANIFESTS.mkdir(parents=True, exist_ok=True)
    return MANIFESTS / f"{campaign_id}.json"


def write_json(path: Path, payload: MappingLike) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


MappingLike = dict[str, Any]
