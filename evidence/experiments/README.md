# SAS Agent experimental results

Single published collection of laboratory measurements for the SAS Agent.
Organized by technical experiment category under `evidence/experiments/`.

## Layout

| Path | Contents |
|------|----------|
| `functional/` | Isolation, certificate matrix, boundary, robustness, WInnForum logs |
| `lifecycle/` | Per-profile lifecycle decomposition deployments |
| `coldstart/` | Instrumented cold-start runs |
| `security/` | TLS/mTLS overhead and related unit-test logs |
| `persistence/` | Persistence mode comparisons |
| `factorial/` | Factorial provisioning contrast |
| `concurrency/` | Process concurrency, container calibration, per-agent scaling |
| `provisioning/` | Calibration, independent K validation, illustrative sensitivity |
| `sustained/` | Single-agent sustained load; colocated mixed-operation pairs |
| `recovery/` | HTTP/mTLS SQLite recovery cycles |
| `analysis/` | Regenerated tables and figures |
| `meta/` | Campaign provenance, selection log, host telemetry |
| `manifest.json` | Experiment index and provenance |
| `checksums.sha256` | File digests for the collection |
| `analysis_stage/` | Symlink farm for analysis tools (no duplicated CSVs) |

## Environment

- Python 3.12+ with the repository virtualenv (`.venv`)
- Docker image used for Agent runs is recorded per experiment in `manifest.json`
- Private harness keys are **not** stored here

## Entrypoint

```bash
./scripts/evaluate.sh help
./scripts/evaluate.sh validate
./scripts/evaluate.sh tables
./scripts/evaluate.sh figures
./scripts/evaluate.sh selection
# verify digests
(cd evidence/experiments && sha256sum -c checksums.sha256 | tail)
```

Campaign runs (new measurements) also go through the same entrypoint:

```bash
OUT_ROOT=evidence/runs/<stamp> CERTS_DIR=/path/to/harness/certs \
  ./scripts/evaluate.sh preflight
./scripts/evaluate.sh freeze
./scripts/evaluate.sh all   # or individual stages
```

Outputs land in `evidence/experiments/analysis/`.

## Provisioning selection (illustrative limits)

Calibration and independent validation are **separate** trees. Do not pool their
rows as repeats of one campaign. Illustrative limits (startup 60 s,
provisioning 1.5 s, grant batch p95 1000 ms, memory 2048 MB) are not SLOs and
must not be retuned after observing validation.

```bash
./scripts/evaluate.sh selection
```

## Reproduce experiments

Long-running load and harness campaigns require:

- Built Agent image matching the recorded Image ID (or a rebuilt image with
  matching Agent sources)
- Laboratory mTLS certificates (generate locally; do not commit private keys)
- Host thermal/load gates as used by the load runners under `benchmarks/load/`
- Optional WInnForum harness image (`sas-harness`) and free ports 9000/9001

See `benchmarks/load/` and `run_cbrs_tests.sh` for runner entry points. Exact
seeds, repeats, and BandProfiles are in each experiment’s manifests and in
`meta/campaigns/`.

## Integrity notes

- Multiple Agent git commits contributed to this collection; each experiment
  records its own provenance. Do not claim a single commit produced all rows.
- Colocated sustained results are the mixed-operation campaign (periodic
  Registration/Grant arrivals and Relinquishments). Earlier no-mix pilots are
  excluded (see `meta/selection_log.json`).
- Failed WInnForum attempt with HTTP 403 on admin reset is retained under
  `functional/winnforum_failed_admin403/` for provenance only.
