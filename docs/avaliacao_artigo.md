# Avaliação do artigo — campanha congelada

Quatro experimentos:

1. Lifecycle decomposition (estágios exclusivos; `stage_handler_ms` = corpo do serviço)
2. mTLS correctness + overhead (`/v1.3/status` → HTTP 200; latências individuais)
3. Persistência SQLite commit/batch/memory (Postgres opcional)
4. Cold start: estágios por start/end (≥0) + `runtime_and_observation_gap_ms`

```bash
export HARNESS_CERTS_DIR=/path/to/harness/certs
DOCKER_NO_CACHE=1 bash benchmarks/load/build_images.sh
.venv/bin/python benchmarks/run_article_campaign.py --smoke
# Oficial: tree limpa (git_dirty=false)
.venv/bin/python benchmarks/run_article_campaign.py
```

Saídas: `artifacts/raw/<campaign_id>/` · `analysis/validate_campaign.py`.
