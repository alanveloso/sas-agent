# Canonical experimental corpus — submission 2026-12135

Single analysis source for figures, tables, and statistics of the Major Revision.

## Reproduce analyses (no new benchmarks)

```bash
# Optional staging for existing tools (local; artifacts/raw is gitignored)
ln -sfn "$(pwd)/evidence/2026-12135/analysis_stage" artifacts/raw/2026-12135

.venv/bin/python analysis/validate_campaign.py --campaign-id 2026-12135
.venv/bin/python analysis/generate_tables.py --campaign-id 2026-12135
.venv/bin/python analysis/uncertainty_report.py --campaign-id 2026-12135 \
  --out /tmp/uncertainty_2026-12135.json
# figures (if desired):
# .venv/bin/python analysis/generate_figures.py --campaign-id 2026-12135
```

See `manifest.json` for per-experiment provenance (commit, image, units, thermal notes).

## Controlled campaign script

`scripts/run_controlled_campaign.sh` (checksum in `scripts/run_controlled_campaign.sha256`) reproduced isolation, cert matrix, lifecycle, cold start, TLS/mTLS, persistence, factorial, and process concurrency **with thermal gates enabled**.

WInnForum, boundary, robustness, container concurrency / K-selection, and sustained load come from earlier validated campaigns listed in the manifest — not from that script.

## Security

Private harness keys are **not** included. Generate laboratory PKI separately for new runs.

## Layout note

`analysis_stage/` is a **symlink farm** into the semantic directories (`lifecycle/`, `coldstart/`, …) so existing tools under `artifacts/raw/<id>/` work without duplicating CSVs. Do not replace those symlinks with copies.
