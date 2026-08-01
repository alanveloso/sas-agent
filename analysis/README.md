# Analysis (read-only)

Consumes `artifacts/raw/<campaign_id>/` and writes:

- `artifacts/tables/<campaign_id>/`
- `artifacts/figures/<campaign_id>/` (stubs until plotting is filled)

Never starts Docker, the SAS agent, or benchmarks.
