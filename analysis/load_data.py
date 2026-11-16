"""Load raw campaign CSVs from ``artifacts/raw/<campaign_id>/``."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
RAW = REPO / "artifacts" / "raw"
EXPERIMENTS = REPO / "evidence" / "experiments"
DEFAULT_CAMPAIGN = "experiments"


def ensure_experiments_staged() -> None:
    """Link ``artifacts/raw/experiments`` → ``evidence/experiments/analysis_stage``."""
    if not (EXPERIMENTS / "analysis_stage").is_dir():
        return
    from analysis.stage_experiments import stage_experiments

    stage_experiments()


def list_campaigns() -> list[str]:
    ensure_experiments_staged()
    if not RAW.is_dir():
        return []
    return sorted(p.name for p in RAW.iterdir() if p.is_dir() and not p.name.startswith("."))


def latest_campaign() -> str | None:
    ensure_experiments_staged()
    if (RAW / DEFAULT_CAMPAIGN).is_dir() or (RAW / DEFAULT_CAMPAIGN).is_symlink():
        return DEFAULT_CAMPAIGN
    cams = list_campaigns()
    return cams[-1] if cams else None


def campaign_dir(campaign_id: str) -> Path:
    if campaign_id == DEFAULT_CAMPAIGN:
        ensure_experiments_staged()
    return RAW / campaign_id


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def find_csvs(campaign_id: str, name_substr: str) -> list[Path]:
    root = campaign_dir(campaign_id)
    if not root.is_dir():
        return []
    # pathlib.Path.rglob may not descend into symlink children of a symlink farm
    # (analysis_stage). Walk with followlinks=True instead.
    import os

    found: list[Path] = []
    root_res = root.resolve()
    for dirpath, _dirnames, filenames in os.walk(root_res, followlinks=True):
        for name in filenames:
            if name_substr in name and name.endswith(".csv"):
                found.append(Path(dirpath) / name)
    return sorted(found)


def load_lifecycle_summary(campaign_id: str) -> list[dict[str, str]]:
    paths = find_csvs(campaign_id, "lifecycle_decomp_summary")
    rows: list[dict[str, str]] = []
    for path in paths:
        rows.extend(read_csv(path))
    return rows


def load_persistence_summary(campaign_id: str) -> list[dict[str, str]]:
    paths = find_csvs(campaign_id, "persistence_modes_summary")
    rows: list[dict[str, str]] = []
    for path in paths:
        rows.extend(read_csv(path))
    return rows


def load_mtls_overhead_summary(campaign_id: str) -> list[dict[str, str]]:
    paths = find_csvs(campaign_id, "tls_mtls_overhead_summary")
    rows: list[dict[str, str]] = []
    for path in paths:
        rows.extend(read_csv(path))
    return rows


def load_coldstart_summary(campaign_id: str) -> list[dict[str, str]]:
    paths = find_csvs(campaign_id, "unified_coldstart_summary")
    rows: list[dict[str, str]] = []
    for path in paths:
        rows.extend(read_csv(path))
    return rows


def load_cert_matrix_summary(campaign_id: str) -> list[dict[str, Any]]:
    paths = find_csvs(campaign_id, "cert_matrix_summary")
    rows: list[dict[str, Any]] = []
    for path in paths:
        rows.extend(read_csv(path))
    return rows
