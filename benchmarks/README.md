# Benchmarks — `sas-agent`

## Fronteiras

| Pasta | Papel |
|---|---|
| `benchmarks/load/` | Implementação dos experimentos |
| `benchmarks/{lifecycle,mtls,persistence,cold_start}/` | Wrappers por experimento |
| `benchmarks/benchkit/` | Schema + I/O de campanha |
| `artifacts/raw/` | Dados brutos por `campaign_id` |
| `analysis/` | Só lê raw → tabelas/figuras |

## Campanha

Um único runner: `benchmarks/run_campaign.py`. Cobre lifecycle, mTLS, persistência, cold start, fronteira, isolamento, fatorial, concorrência (processo e Docker) e robustez de BandProfile. WInnForum fica no harness à parte.

```bash
export HARNESS_CERTS_DIR=/path/to/Spectrum-Access-System/src/harness/certs
bash benchmarks/load/build_images.sh

.venv/bin/python benchmarks/run_campaign.py --smoke
.venv/bin/python benchmarks/run_campaign.py
```

Saídas em `artifacts/raw/<campaign_id>/` e `artifacts/manifests/<campaign_id>.json`.

Instrumentação de latência: `SAS_TIMING=1` (desligada por padrão na app).


> Prefer `./scripts/evaluate.sh` for the laboratory evaluation workflow. `run_campaign.py` remains a low-level benchkit runner writing under `artifacts/raw/`.
