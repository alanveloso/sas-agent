# Benchmarks — `sas-agent`

## Fronteiras

| Pasta | Papel |
|---|---|
| `benchmarks/load/` | Implementação dos experimentos (pode evoluir) |
| `benchmarks/{lifecycle,mtls,persistence,cold_start}/` | Entradas estáveis do **artigo** (wrappers) |
| `benchmarks/benchkit/` | Schema + I/O de campanha + reexport de helpers |
| `artifacts/raw/` | Dados brutos imutáveis por `campaign_id` |
| `analysis/` | Só lê raw → tabelas/figuras (nunca sobe o SAS) |

## Escopo congelado do artigo (4 experimentos)

1. **Lifecycle decomposition** — REG/SIQ/GRA/HBT/RLQ/DRG + `X-SAS-Timing`
2. **mTLS** — matriz de certificados + overhead TLS vs mTLS
3. **Persistência** — SQLite commit / batch / memory (+ Postgres opcional)
4. **Cold start instrumentado** — marcos internos + polls

Não repetir agora: WInnForum (salvo regressão), concorrência antiga, fatorial de faixa, boundary (salvo mudança de lógica).

## Campanha oficial

```bash
export HARNESS_CERTS_DIR=/path/to/Spectrum-Access-System/src/harness/certs
bash benchmarks/load/build_images.sh   # ou scripts/build_images.sh

# Smoke (sujo ok)
.venv/bin/python benchmarks/run_article_campaign.py --smoke

# Oficial: tree limpa recomendada
.venv/bin/python benchmarks/run_article_campaign.py
.venv/bin/python benchmarks/run_article_campaign.py --allow-dirty   # override
```

Saídas:

```text
artifacts/raw/<campaign_id>/lifecycle_decomposition/
artifacts/raw/<campaign_id>/mtls_correctness_overhead/
artifacts/raw/<campaign_id>/persistence_decomposition/
artifacts/raw/<campaign_id>/instrumented_cold_start/
artifacts/manifests/<campaign_id>.json
artifacts/tables/<campaign_id>/tables.md
```

## Executores individuais

```bash
.venv/bin/python benchmarks/lifecycle/run.py --campaign-id 2026-08-01_article-eval
.venv/bin/python benchmarks/mtls/run.py --campaign-id ...
.venv/bin/python benchmarks/persistence/run.py --campaign-id ...
.venv/bin/python benchmarks/cold_start/run.py --campaign-id ...
.venv/bin/python analysis/generate_tables.py --campaign-id ...
```

## `load/` (só o que a campanha usa)

`lifecycle_decomp_bench`, `cert_matrix_bench`, `tls_mtls_overhead_bench`,
`persistence_modes_bench`, `unified_coldstart`, mais `common.py` e
`build_images.sh`.

Instrumentação de latência: `SAS_TIMING=1` (desligada por padrão na app).
