"""Common result schema and campaign I/O for article-facing benchmarks.

Host/environment detail lives in the campaign manifest. Per-row fields stay
minimal and stable so analysis scripts do not chase renaming.
"""

from __future__ import annotations

from typing import Final

# Present on every measurement row (when the writer supports enrichment).
COMMON_ROW_FIELDS: Final[tuple[str, ...]] = (
    "experiment_id",
    "run_id",
    "timestamp_utc",
    "profile_id",
    "seed",
    "warmup",
    "success",
    "error_type",
    "duration_ms",
)

# Units convention: *_ms, *_s, *_bytes, *_count, *_percent

ARTICLE_EXPERIMENTS: Final[tuple[str, ...]] = (
    "lifecycle_decomposition",
    "mtls_correctness_overhead",
    "persistence_decomposition",
    "instrumented_cold_start",
)

# Map experiment_id → implementing script under benchmarks/load/
EXPERIMENT_SCRIPTS: Final[dict[str, str]] = {
    "lifecycle_decomposition": "load/lifecycle_decomp_bench.py",
    "mtls_overhead": "load/tls_mtls_overhead_bench.py",
    "mtls_correctness": "load/cert_matrix_bench.py",
    "persistence_decomposition": "load/persistence_modes_bench.py",
    "instrumented_cold_start": "load/unified_coldstart.py",
}
