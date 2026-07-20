#!/usr/bin/env bash
# CBRS regression: Docker sas-agent + WINNF SIQ/GRA/HBT harness suites.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS_IMAGE="sas-harness"
TEST_DIR=""
IMAGE_CONTAINER=""
COMPOSE=(docker compose -f "$SCRIPT_DIR/docker-compose.yml" -f "$SCRIPT_DIR/docker-compose.host.yml")

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

log() { printf '%s\n' "$*"; }
fail() { printf "${RED}%s${NC}\n" "$*"; }
pass() { printf "${GREEN}%s${NC}\n" "$*"; }

if ! command -v docker >/dev/null 2>&1; then
  fail "Docker não encontrado. Instale o Docker Engine para executar este script."
  exit 1
fi

cleanup() {
  log "Encerrando contêiner sas-agent..."
  "${COMPOSE[@]}" down --remove-orphans >/dev/null 2>&1 || true
  if [[ -n "$IMAGE_CONTAINER" ]]; then
    docker rm "$IMAGE_CONTAINER" >/dev/null 2>&1 || true
  fi
  if [[ -n "$TEST_DIR" ]]; then
    rm -rf "$TEST_DIR"
  fi
}
trap cleanup EXIT

log "Construindo imagem do harness ($HARNESS_IMAGE)..."
docker build -f "$SCRIPT_DIR/Dockerfile.harness" -t "$HARNESS_IMAGE" "$SCRIPT_DIR"

TEST_DIR="$(mktemp -d)"
SAS_CFG_FILE="$TEST_DIR/sas.cfg"
export HARNESS_CERTS_DIR="$TEST_DIR/certs"
mkdir -p "$HARNESS_CERTS_DIR"

log "Extraindo certificados de teste da imagem..."
IMAGE_CONTAINER="$(docker create "$HARNESS_IMAGE")"
docker cp "$IMAGE_CONTAINER:/harness/src/harness/certs/." "$HARNESS_CERTS_DIR"
docker rm "$IMAGE_CONTAINER" >/dev/null
IMAGE_CONTAINER=""

HARNESS_RUN=(docker run --rm --network host -e HOME=/tmp
  -v "$SAS_CFG_FILE:/harness/src/harness/sas.cfg:ro"
  "$HARNESS_IMAGE")

pick_host_ports() {
  HOST_RSA_PORT=9000
  HOST_ECC_PORT=9001
  if command -v ss >/dev/null 2>&1; then
    if ss -tln | awk '{print $4}' | grep -qE ':9000$'; then
      fail "Porta 9000 ocupada. Pare o SAS local antes de rodar os testes Docker (network_mode: host)."
      exit 1
    fi
  fi
  export HOST_RSA_PORT HOST_ECC_PORT
}

write_sas_cfg() {
  cat >"$SAS_CFG_FILE" <<EOF
[SasConfig]
AdminApiBaseUrl: localhost:${HOST_RSA_PORT}
CbsdSasRsaBaseUrl: localhost:${HOST_RSA_PORT}
CbsdSasEcBaseUrl: localhost:${HOST_ECC_PORT}
SasSasRsaBaseUrl: localhost:${HOST_RSA_PORT}
SasSasEcBaseUrl: localhost:${HOST_ECC_PORT}
CbsdSasVersion: v1.2
SasSasVersion: v1.3
AdminId: sas_admin_id
MaximumBatchSize: 100
EOF
}

pick_host_ports
write_sas_cfg

log "Subindo sas-agent (faixa CBRS 3550–3700 MHz)..."
"${COMPOSE[@]}" up -d --build

log "Aguardando listeners HTTPS (9000/9001)..."
ready=0
for _ in $(seq 1 60); do
  if "${COMPOSE[@]}" exec -T sas-agent \
    python -c "import socket; s=socket.socket(); s.settimeout(1); s.connect(('127.0.0.1', 9000)); s.close()" \
    2>/dev/null; then
    ready=1
    break
  fi
  sleep 1
done

if [[ "$ready" -ne 1 ]]; then
  fail "FAILED: sas-agent não ficou pronto a tempo."
  "${COMPOSE[@]}" logs --tail=50 sas-agent || true
  exit 1
fi

log "Executando suítes de regressão CBRS (no container do harness)..."

# Evidence logs for the evaluation report (Fase 1 do plano de avaliação).
EVIDENCE_DIR="$SCRIPT_DIR/benchmarks/data"
mkdir -p "$EVIDENCE_DIR"

FAILED=0
# Full CBSD lifecycle order: REG -> SIQ -> GRA -> HBT -> RLQ -> DRG.
SUITES=(
  testcases.WINNF_FT_S_REG_testcase
  testcases.WINNF_FT_S_SIQ_testcase
  testcases.WINNF_FT_S_GRA_testcase
  testcases.WINNF_FT_S_HBT_testcase
  testcases.WINNF_FT_S_RLQ_testcase
  testcases.WINNF_FT_S_DRG_testcase
)

for suite in "${SUITES[@]}"; do
  log ""
  log "==> $suite"
  suite_log="$EVIDENCE_DIR/winnforum_${suite##*.}.log"
  if "${HARNESS_RUN[@]}" python -m unittest "$suite" -v 2>&1 | tee "$suite_log"; then
    pass "OK: $suite"
  else
    fail "ERRO: $suite"
    FAILED=1
  fi
done

log ""
log "Consolidando evidências em CSV..."
if python3 "$SCRIPT_DIR/benchmarks/parse_winnforum_logs.py" \
  --logs-dir "$EVIDENCE_DIR" \
  --out "$EVIDENCE_DIR/lifecycle_validation.csv"; then
  pass "Evidências: $EVIDENCE_DIR/lifecycle_validation.csv"
else
  fail "Falha ao consolidar evidências (logs preservados em $EVIDENCE_DIR)."
fi

log ""
if [[ "$FAILED" -eq 0 ]]; then
  pass "SUCCESS: todas as suítes CBRS passaram."
  exit 0
fi

fail "FAILED: uma ou mais suítes CBRS falharam."
exit 1
