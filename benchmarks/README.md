# `sas-agent` Benchmarks

Self-contained, reproducible measurement harness for the `sas-agent` SAS
(Spectrum Access System). The scripts here produce **raw and aggregated data
only** (CSV + JSON manifests); they do not generate prose reports.

Everything runs against the project source **read-only**: no file under the
main application or the official WInnForum tests is modified. Each measurement
runs in a fresh subprocess with a throwaway SQLite database and an explicit
band profile, so runs never contaminate each other.

## Dimensions

| Script | What it measures | Main outputs (`data/`) |
|---|---|---|
| `dimension_a_quality.py` | Cyclomatic complexity (Radon) and CBRS-term coupling in `services/` and `domain/` | `dimension_a_quality.csv`, `dimension_a_terms_raw.csv` |
| `dimension_b_efficiency.py` | A/B micro-benchmarks: monolithic vs. core, and profile parameterization weight | `dimension_b_runs_raw.csv`, `dimension_b_efficiency.csv` |
| `dimension_c_coverage.py` | Unit-suite pass/fail and code coverage | `dimension_c_coverage.csv`, `dimension_c_passfail.json`, `coverage_raw.json` |
| `performance_focus.py` | Performance-only slice: cold start + hot grant, monolithic vs. core, profile-agnostic check | `performance_focus_*.csv/json` |
| `agnosticism_benchmark.py` | Architecture isolation + 8-profile agnosticism (cold start + hot grant) | `agnosticism_*.csv/json` |
| `lifecycle_benchmark.py` | **Full CBSD–SAS lifecycle** (registration → spectrum inquiry → grant → heartbeat → relinquishment → deregistration), per endpoint and end-to-end, monolithic vs. core and across 8 profiles | `lifecycle_*.csv/json` |

Workers (invoked as subprocesses, not run directly):

- `phase_worker.py` — single phase (cold start / hot grant / CPAS / profile resolution).
- `lifecycle_worker.py` — one full lifecycle, timing every endpoint.

`run_all.py` orchestrates dimensions A, B and C and writes `data/execution_manifest.json`.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r ../requirements.txt      # main app deps
.venv/bin/pip install -r requirements.txt         # benchmark-only deps
```

## Running

```bash
# From the benchmarks/ directory, using the venv interpreter:
.venv/bin/python dimension_a_quality.py
.venv/bin/python dimension_b_efficiency.py
.venv/bin/python dimension_c_coverage.py
.venv/bin/python performance_focus.py
.venv/bin/python agnosticism_benchmark.py
.venv/bin/python lifecycle_benchmark.py
.venv/bin/python run_all.py            # dimensions A + B + C + manifest
```

All datasets land in `data/`. Per-run logs land in `logs/` (git-ignored).

## Reproducibility

- Fixed RNG seed (`SEED = 42`) and shuffled schedule.
- `N` valid samples per scenario plus `WARMUP` discarded warmups.
- Every profile is loaded through the same path (`BAND_PROFILE_FILE`, JSON).
- Each manifest captures CPU, RAM, OS, Python version, seed and parameters.

## Band profiles (`profiles/`)

| Profile | Experimental role |
|---|---|
| `cbrs_winnforum` | Rich regulatory package (protections on) |
| `br_anatel_privativa` | Lean national package |
| `lab_custom_3p8` | Lab band 3.8–3.9 GHz |
| `synth_narrow_5mhz` | Narrow raster (5 MHz) |
| `synth_wide_20mhz` | Wide raster (20 MHz) |
| `synth_protections_all_on` | All protection flags enabled |
| `synth_protections_all_off` | All protection flags disabled |
| `synth_short_timers` | Short grant/heartbeat timers |
