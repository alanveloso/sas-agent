#!/usr/bin/env bash
# CBRS regression: Docker sas-agent + WINNF SIQ/GRA harness suites.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HARNESS_DIR="$REPO_ROOT/src/harness"
CERTS_DIR="$HARNESS_DIR/certs"

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

if [[ ! -f "$CERTS_DIR/generate_fake_certs.sh" ]]; then
  fail "Harness não encontrado em $HARNESS_DIR"
  exit 1
fi

if [[ ! -f "$CERTS_DIR/server.cert" ]]; then
  log "Gerando certificados mTLS do harness..."
  if [[ ! -f "$REPO_ROOT/cert/openssl.cnf" ]]; then
    fail "Arquivo $REPO_ROOT/cert/openssl.cnf não encontrado."
    exit 1
  fi
  (cd "$CERTS_DIR" && bash generate_fake_certs.sh)
fi

PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
  for candidate in \
    "$REPO_ROOT/.venv/bin/python" \
    "$SCRIPT_DIR/.venv/bin/python" \
    python3 \
    python
  do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c "import six" 2>/dev/null; then
      PYTHON="$candidate"
      break
    fi
  done
fi

if [[ -z "$PYTHON" ]]; then
  fail "Python com dependência 'six' não encontrado. Crie um venv e instale: pip install six"
  exit 1
fi

COMPOSE=(docker compose -f "$SCRIPT_DIR/docker-compose.yml" -f "$SCRIPT_DIR/docker-compose.host.yml")

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

write_temp_sas_cfg() {
  SAS_CFG_BACKUP=""
  if [[ -f "$HARNESS_DIR/sas.cfg" ]]; then
    SAS_CFG_BACKUP="$(mktemp)"
    cp "$HARNESS_DIR/sas.cfg" "$SAS_CFG_BACKUP"
  fi
  cat >"$HARNESS_DIR/sas.cfg" <<EOF
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

restore_sas_cfg() {
  if [[ -n "${SAS_CFG_BACKUP:-}" && -f "$SAS_CFG_BACKUP" ]]; then
    mv -f "$SAS_CFG_BACKUP" "$HARNESS_DIR/sas.cfg"
  fi
}

cleanup() {
  log "Encerrando contêiner sas-agent..."
  restore_sas_cfg
  "${COMPOSE[@]}" down --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

pick_host_ports
write_temp_sas_cfg

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

log "Executando suítes de regressão CBRS..."
cd "$HARNESS_DIR"

FAILED=0
SUITES=(
  testcases.WINNF_FT_S_SIQ_testcase
  testcases.WINNF_FT_S_GRA_testcase
)

for suite in "${SUITES[@]}"; do
  log ""
  log "==> $suite"
  if "$PYTHON" -m unittest "$suite" -v; then
    pass "OK: $suite"
  else
    fail "ERRO: $suite"
    FAILED=1
  fi
done

log ""
if [[ "$FAILED" -eq 0 ]]; then
  pass "SUCCESS: todas as suítes CBRS passaram."
  exit 0
fi

fail "FAILED: uma ou mais suítes CBRS falharam."
exit 1
