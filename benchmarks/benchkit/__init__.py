"""Article campaign helpers + re-export of load/common utilities."""

from __future__ import annotations

import sys
from pathlib import Path

_LOAD = Path(__file__).resolve().parent.parent / "load"
if str(_LOAD) not in sys.path:
    sys.path.insert(0, str(_LOAD))

# Import load helpers under a private alias to avoid shadowing this package.
import importlib

_load_common = importlib.import_module("common")  # benchmarks/load/common.py

from .campaign_io import (  # noqa: E402
    ARTIFACTS,
    BENCH,
    FIGURES,
    LOAD,
    MANIFESTS,
    PROCESSED,
    RAW,
    REPO_ROOT,
    TABLES,
    campaign_manifest_path,
    campaign_raw_dir,
    new_campaign_id,
    write_json,
)
from .schema import (  # noqa: E402
    ARTICLE_EXPERIMENTS,
    COMMON_ROW_FIELDS,
    EXPERIMENT_SCRIPTS,
)

# Re-export selected load helpers
capture_manifest = _load_common.capture_manifest
git_snapshot = _load_common.git_snapshot
DEFAULT_SEED = _load_common.DEFAULT_SEED
utc_now_iso = _load_common.utc_now_iso
build_mtls_context = _load_common.build_mtls_context
DockerRunSpec = _load_common.DockerRunSpec
percentile = _load_common.percentile

__all__ = [
    "ARTIFACTS",
    "BENCH",
    "FIGURES",
    "LOAD",
    "MANIFESTS",
    "PROCESSED",
    "RAW",
    "REPO_ROOT",
    "TABLES",
    "ARTICLE_EXPERIMENTS",
    "COMMON_ROW_FIELDS",
    "EXPERIMENT_SCRIPTS",
    "campaign_manifest_path",
    "campaign_raw_dir",
    "new_campaign_id",
    "write_json",
    "capture_manifest",
    "git_snapshot",
    "DEFAULT_SEED",
    "utc_now_iso",
    "build_mtls_context",
    "DockerRunSpec",
    "percentile",
]
