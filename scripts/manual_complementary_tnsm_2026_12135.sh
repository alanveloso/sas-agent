#!/usr/bin/env bash
# =============================================================================
# TNSM-2026-12135 — Execuções complementares (manual, copy-paste)
# =============================================================================
# NÃO sobrescreve evidence/tnsm-2026-12135-full-20261112T200619Z/
# NÃO sobrescreve evidence/2026-12135/
# NÃO declara certificação WInnForum.
#
# Uso:
#   1) Leia as seções 0–1 e cumpra o gate de host.
#   2) Faça commit de quaisquer patches locais pendentes (planned_* / compose).
#   3) Execute as seções em ordem, uma por vez, no shell (não rode este
#      arquivo inteiro de uma vez se quiser controle passo a passo).
#
# Alternativa segura: copie cada bloco ## RUN: ... para o terminal.
# =============================================================================
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

# ---- Parâmetros congelados (não retunar) ----
IMAGE_TAG="${IMAGE_TAG:-sas-agent:article}"
CERTS_DIR="${CERTS_DIR:-$REPO/evidence/tnsm-2026-12135-controlled-20261111T205011Z/meta/harness_certs}"
PY="${PY:-$REPO/.venv/bin/python}"
P4_STARTUP=60
P4_PROV=1.5
P4_GRANT_P95=1000
P4_MEM=2048
K3_SEED=94242
K3_REPEATS=5
# Seed R1.8 disjunta da campanha integral (SEED=42 → pares 1042..5042)
R18_SEED=74242
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_ROOT="${OUT_ROOT:-$REPO/evidence/tnsm-2026-12135-complementary-${STAMP}}"

echo "REPO=$REPO"
echo "OUT_ROOT=$OUT_ROOT"
echo "HEAD=$(git rev-parse HEAD)"
echo "IMAGE_TAG=$IMAGE_TAG"
echo "IMAGE_ID=$(docker image inspect --format '{{.Id}}' "$IMAGE_TAG")"

# =============================================================================
# 0) GATE DE HOST — obrigatório antes de qualquer ensaio definitivo
# =============================================================================
# Serviços de update ativos invalidam o preflight_article. Pare-os (temporário):
#
#   sudo systemctl stop unattended-upgrades.service packagekit.service \
#     snapd.service fwupd.service
#   # opcional: sudo systemctl stop snapd.socket
#
# Confirme AC, load e Docker:
cat <<'EOF'

## RUN: gate host (cole após parar os serviços de update)
cd /home/alanveloso/Código/sas-agent
REQUIRE_AC_POWER=1 REQUIRE_CLEAN_GIT=1 MAX_LOAD_1M=1.0 \
  ./scripts/preflight_article.sh --image sas-agent:article
# Só continue se o exit code for 0 (Failures: 0).

EOF

# =============================================================================
# 1) Abrir OUT_ROOT + freeze (após commit local pendente, se houver)
# =============================================================================
cat <<EOF

## RUN: abrir campanha complementar + freeze
cd /home/alanveloso/Código/sas-agent
export OUT_ROOT='$OUT_ROOT'
export IMAGE_TAG=sas-agent:article
export CERTS_DIR='$CERTS_DIR'
export SEED=42
# R1.8 usa R18_SEED via env no estágio colocated (abaixo)
./scripts/run_full_evaluation.sh preflight
./scripts/run_full_evaluation.sh freeze
./scripts/run_full_evaluation.sh status
test -f "\$OUT_ROOT/meta/protocol_frozen.json"
cat "\$OUT_ROOT/meta/protocol_frozen.json" | head -40

EOF

# =============================================================================
# 2) K=3 — validação independente (P4 congelado; seeds 95242..99242)
# =============================================================================
# Calibração integral: device_seed 1042..5042 (base 42)
# K5 sensibilidade:     85242..89242 (base 84242)
# K3 independente:      95242..99242 (base 94242) — disjuntos
cat <<EOF

## RUN: K=3 validação independente (definitivo)
cd /home/alanveloso/Código/sas-agent
export OUT_ROOT='$OUT_ROOT'
export IMAGE_TAG=sas-agent:article
export CERTS_DIR='$CERTS_DIR'
./scripts/run_full_evaluation.sh k3_validation
# Artefatos: \$OUT_ROOT/12_provisioning/k3_validation/
# Checagem rápida:
python3 - <<'PY'
import csv
from pathlib import Path
import os
root=Path(os.environ['OUT_ROOT'])/'12_provisioning/k3_validation'
rows=list(csv.DictReader((root/'container_concurrency_runs.csv').open()))
assert all(r['k_containers']=='3' for r in rows)
seeds=sorted({int(r['device_seed']) for r in rows})
print('n',len(rows),'device_seeds',seeds)
assert seeds==[95242,96242,97242,98242,99242], seeds
assert all(r['exit_code'] in ('0','0.0') for r in rows)
print('K3 OK')
PY

EOF

# =============================================================================
# 3) R1.8 — colocated definitivo com mistura contínua
# =============================================================================
cat <<EOF

## RUN: R1.8 colocated 5 pares × 600s (mistura arrival=10 / relinquish=30)
cd /home/alanveloso/Código/sas-agent
export OUT_ROOT='$OUT_ROOT'
export IMAGE_TAG=sas-agent:article
export CERTS_DIR='$CERTS_DIR'
export SEED=$R18_SEED
export COLOCATED_PAIRS=5
export COLOCATED_CLIENTS=5
export COLOCATED_DPC=300
export COLOCATED_SEC=600
export COLOCATED_ARRIVAL_SEC=10
export COLOCATED_RELINQUISH_SEC=30
./scripts/run_full_evaluation.sh colocated
# Artefatos: \$OUT_ROOT/10_colocated/
# Checagem rápida (mistura NÃO pode ser zero):
python3 - <<'PY'
import csv,json,os
from pathlib import Path
root=Path(os.environ['OUT_ROOT'])/'10_colocated'
sched=json.loads((root/'campaign_schedule.json').read_text())
assert sched['arrival_interval_sec']==10
assert sched['relinquish_interval_sec']==30
assert sched['mix_semantics']['scope']=='per_client'
rows=list(csv.DictReader((root/'colocated_sustained_runs.csv').open()))
assert len(rows)==10, len(rows)
for r in rows:
    assert int(r['arrivals_sum'])>0, r
    assert int(float(r.get('planned_arrivals_sum') or 0))>0
    assert r['exit_code'] in ('0','0.0')
print('R1.8 mix OK; sweeps', sorted({r['stats_sweeps'] for r in rows}))
PY

EOF

# =============================================================================
# 4) WInnForum — 65 casos sob imagem congelada (NÃO é certificação)
# =============================================================================
cat <<EOF

## RUN: WInnForum 65 casos (harness + UUT congelado)
cd /home/alanveloso/Código/sas-agent
export OUT_ROOT='$OUT_ROOT'
WF_OUT="\$OUT_ROOT/02_functional/winnforum_reexec"
mkdir -p "\$WF_OUT"
# Portas 9000/9001 livres; imagem sas-harness presente; UUT = sas-agent:article
SAS_IMAGE=sas-agent:article SKIP_UUT_BUILD=1 \
  EVIDENCE_DIR="\$WF_OUT" \
  bash ./run_cbrs_tests.sh
# Proveniência explícita:
cat > "\$WF_OUT/PROVENANCE.txt" <<PROV
mode=harness_reexecution
uut_image=\$(docker image inspect --format '{{.Id}}' sas-agent:article)
harness_image=\$(docker image inspect --format '{{.Id}}' sas-harness:latest)
host_commit=\$(git rev-parse HEAD)
note=Functional regression under frozen article image. NOT WInnForum certification.
historical_corpus=evidence/2026-12135/functional/winnforum/ (commit 0119147, n=65 pass)
PROV
wc -l "\$WF_OUT/lifecycle_validation.csv"
python3 - <<'PY'
import csv,os
from collections import Counter
from pathlib import Path
p=Path(os.environ['OUT_ROOT'])/'02_functional/winnforum_reexec/lifecycle_validation.csv'
rows=list(csv.DictReader(p.open()))
print('n',len(rows), Counter(r.get('result') for r in rows))
assert len(rows)==65
PY

EOF

# =============================================================================
# 5) Alias de apresentação P4 (não altera JSON da campanha integral)
# =============================================================================
cat <<EOF

## RUN: alias de apresentação P4 (opcional, no OUT_ROOT complementar)
cd /home/alanveloso/Código/sas-agent
export OUT_ROOT='$OUT_ROOT'
mkdir -p "\$OUT_ROOT/12_provisioning/offline_selection"
# Recomputa seleção com calibração da campanha integral (somente leitura) + validação K3
CALIB=evidence/tnsm-2026-12135-full-20261112T200619Z/08_concurrency/containers/container_concurrency_runs.csv
VAL_K3="\$OUT_ROOT/12_provisioning/k3_validation/container_concurrency_runs.csv"
\$PY analysis/provisioning_selection.py \\
  --calibration "\$CALIB" \\
  --validation "\$VAL_K3" \\
  --limit-startup-sec $P4_STARTUP --limit-provisioning-sec $P4_PROV \\
  --limit-grant-batch-p95-ms $P4_GRANT_P95 --limit-memory-mb $P4_MEM \\
  --out "\$OUT_ROOT/12_provisioning/offline_selection/P4_illustrative_selected_k.json"
# Nota de nomenclatura (arquivo legado P4_k5_* permanece na campanha integral):
cat > "\$OUT_ROOT/12_provisioning/offline_selection/README_P4_NAMING.txt" <<'TXT'
P4_k5_illustrative_sensitivity.json (campanha integral) = nome legado.
Conteúdo sob limites P4 congelados pode selecionar K≠5 (observado: K=3).
Neste OUT_ROOT complementar use P4_illustrative_selected_k.json como rótulo de apresentação.
Não retune limites. K5 validation = sensibilidade adicional, não validação do K*.
TXT

EOF

# =============================================================================
# 6) Checksums do OUT_ROOT complementar
# =============================================================================
cat <<EOF

## RUN: checksums
cd /home/alanveloso/Código/sas-agent
export OUT_ROOT='$OUT_ROOT'
./scripts/run_full_evaluation.sh checksums
ls -la "\$OUT_ROOT/checksums.sha256"

EOF

cat <<EOF

# =============================================================================
# Estimativas (ordem de grandeza)
# =============================================================================
# K=3:        ~5–15 min (gates + 5× create/load ~1.4 s wall load; create domina)
# R1.8:       ~2–2.5 h (5 pares × 2 × ~600 s + create/teardown/gates)
# WInnForum:  ~20–60 min (6 suítes REG..DRG; depende do harness)
# Disco:      ~1–5 GiB adicionais
# Não misturar pilotos/smokes com estes OUT_ROOT.
# Smoke R1.8 já existente (não definitivo):
#   evidence/tnsm-2026-12135-r18-smoke-20261113T134537Z/
# =============================================================================
EOF
