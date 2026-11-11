#!/usr/bin/env bash
# Campanha experimental CONTROLADA (TNSM) — verificação + execução.
#
# Uso (no terminal preparado):
#   cd /home/alanveloso/Código/sas-agent
#   bash scripts/run_controlled_campaign.sh              # campanha completa
#   bash scripts/run_controlled_campaign.sh --smoke      # fumaça curta
#   bash scripts/run_controlled_campaign.sh --dry-run    # só preflight + imprime comandos
#   bash scripts/run_controlled_campaign.sh --phases coldstart,tls
#
# Políticas:
#   - NÃO usa --skip-thermal-gate (gates ligados onde o runner oferece).
#   - NÃO sobrescreve evidence/ históricas (rev1/final/txfix/audit).
#   - NÃO faz commit/push/rebuild destrutivo.
#   - Escreve apenas sob evidence/tnsm-2026-12135-controlled-<UTC>/ (ou $EVIDENCE_ROOT).
#
# Pré-requisitos: main @ d3aaa3d…, Docker, imagem sas-agent:final-d3842d8,
# PKI spectrum-access-system/certs, SAS_ADMIN_CERT_SHA256 (ou deriva de admin.cert).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${PY:-$ROOT/.venv/bin/python}"
IMAGE="${IMAGE:-sas-agent:final-d3842d8}"
CERTS="${CERTS:-/home/alanveloso/Código/spectrum-access-system/certs}"
EXPECTED_SHA_PREFIX="${EXPECTED_SHA_PREFIX:-d3aaa3d}"
SEED="${SEED:-42}"

SMOKE=0
DRY_RUN=0
ALLOW_DIRTY=1
PHASES_DEFAULT="isolation,cert_matrix,coldstart,lifecycle,tls,persistence,factorial,process,analyze"
PHASES="$PHASES_DEFAULT"

usage() {
  sed -n '2,25p' "$0"
  echo "Fases: $PHASES_DEFAULT"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --smoke) SMOKE=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --phases) PHASES="$2"; shift 2 ;;
    --image) IMAGE="$2"; shift 2 ;;
    --certs) CERTS="$2"; shift 2 ;;
    --require-clean) ALLOW_DIRTY=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "arg desconhecido: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ -z "${EVIDENCE_ROOT:-}" ]]; then
  EVIDENCE_ROOT="evidence/tnsm-2026-12135-controlled-$(date -u +%Y%m%dT%H%M%SZ)"
fi
# paths relativos → absolutos sob ROOT
[[ "$EVIDENCE_ROOT" = /* ]] || EVIDENCE_ROOT="$ROOT/$EVIDENCE_ROOT"
mkdir -p "$EVIDENCE_ROOT"/{meta,01_functional/isolation,01_functional/cert_matrix,02_lifecycle_multiband,03_coldstart_multiband,04_tls_mtls,05_persistence,06_factorial,07_process_concurrency,08_analysis,host_telemetry}

LOG="$EVIDENCE_ROOT/meta/campaign_runner.log"
exec > >(tee -a "$LOG") 2>&1

echo "=== controlled campaign $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
echo "ROOT=$ROOT"
echo "EVIDENCE_ROOT=$EVIDENCE_ROOT"
echo "IMAGE=$IMAGE CERTS=$CERTS SMOKE=$SMOKE DRY_RUN=$DRY_RUN PHASES=$PHASES"

want_phase() {
  [[ ",$PHASES," == *",$1,"* ]]
}

die() { echo "ERROR: $*" >&2; exit 1; }

# ---------- Preflight ----------
preflight() {
  echo "######## PREFLIGHT ########"
  command -v docker >/dev/null || die "docker não encontrado"
  command -v openssl >/dev/null || die "openssl não encontrado"
  [[ -x "$PY" ]] || die "Python venv ausente: $PY"
  [[ -d "$CERTS" ]] || die "CERTS inexistente: $CERTS"
  [[ -f "$CERTS/admin.cert" ]] || die "faltando $CERTS/admin.cert"
  [[ -f "$CERTS/ca.cert" ]] || die "faltando $CERTS/ca.cert"

  local branch head
  branch="$(git branch --show-current)"
  head="$(git rev-parse HEAD)"
  echo "branch=$branch HEAD=$head"
  [[ "$branch" == "main" ]] || die "checkout deve ser main (atual: $branch)"
  [[ "$head" == ${EXPECTED_SHA_PREFIX}* ]] || die "HEAD esperado prefixo $EXPECTED_SHA_PREFIX… (atual $head)"

  local dirty
  dirty="$(git status --porcelain)"
  if [[ -n "$dirty" ]]; then
    echo "git dirty files:"
    echo "$dirty"
    [[ "$ALLOW_DIRTY" == "1" ]] || die "árvore dirty; use sem --require-clean apenas se for conscientemente"
    echo "WARN: prosseguindo com working tree dirty (ALLOW_DIRTY=1)"
  fi

  if [[ -z "${SAS_ADMIN_CERT_SHA256:-}" ]]; then
    export SAS_ADMIN_CERT_SHA256
    SAS_ADMIN_CERT_SHA256="$(openssl x509 -noout -fingerprint -sha256 -in "$CERTS/admin.cert" | cut -d= -f2)"
  fi
  [[ ${#SAS_ADMIN_CERT_SHA256} -ge 64 ]] || die "SAS_ADMIN_CERT_SHA256 inválido"

  docker image inspect "$IMAGE" >/dev/null || die "imagem Docker ausente: $IMAGE"
  systemctl is-active docker >/dev/null || die "docker.service inativo"

  # histórico intacto (existência)
  for d in \
    evidence/tnsm-2026-12135-final-20261111T182327Z \
    evidence/tnsm-2026-12135-rev1-20261110T124131Z \
    evidence/tnsm-2026-12135-sustained-txfix-20261111T005908Z
  do
    [[ -d "$ROOT/$d" ]] || echo "WARN: histórico ausente $d"
  done
  case "$EVIDENCE_ROOT" in
    *tnsm-2026-12135-final*|*tnsm-2026-12135-rev1*|*sustained-txfix*|*audit-p2-13*)
      die "EVIDENCE_ROOT aponta para pasta histórica — recuse overwrite"
      ;;
  esac

  local free_g
  free_g="$(df -BG --output=avail "$ROOT" | tail -1 | tr -dc '0-9')"
  [[ "${free_g:-0}" -ge 20 ]] || die "espaço livre <20GiB ($free_g)"

  # Gate térmico (mesmo critério dos benches)
  "$PY" - <<'PY' || die "thermal/load gate FAILED — aguarde resfriar / reduza load e rode de novo"
import sys
sys.path.insert(0, "benchmarks/load")
from common import host_load_snapshot
s = host_load_snapshot()
t, l, c = s.get("temperature_tctl_c"), s.get("loadavg_1m"), s.get("cpu_percent_prior")
ok = t is not None and t <= 65 and l is not None and l <= 1.0 and c is not None and c <= 10
print({"tctl": t, "load1": l, "cpu_pct": c, "PASS_DEFAULT_GATE": ok})
raise SystemExit(0 if ok else 1)
PY

  # Metadados
  {
    echo "campaign_id=$(basename "$EVIDENCE_ROOT")"
    echo "started_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "git_commit=$head"
    echo "git_branch=$branch"
    echo "image=$IMAGE"
    echo "certs=$CERTS"
    echo "smoke=$SMOKE"
    echo "phases=$PHASES"
    echo "skip_thermal_gate=false"
  } > "$EVIDENCE_ROOT/meta/campaign_id.txt"
  git rev-parse HEAD > "$EVIDENCE_ROOT/meta/agent_sha.txt"
  docker image inspect --format '{{.Id}}' "$IMAGE" > "$EVIDENCE_ROOT/meta/image_id.txt"
  echo "$SAS_ADMIN_CERT_SHA256" > "$EVIDENCE_ROOT/meta/admin_sha256.txt"
  "$PY" - <<PY > "$EVIDENCE_ROOT/meta/preflight_gate.json"
import json, sys
sys.path.insert(0, "benchmarks/load")
from common import host_load_snapshot, cpu_freq_snapshot, thermal_throttling_hints
s = host_load_snapshot()
s["cpu_freq"] = cpu_freq_snapshot()
s["throttling_hints"] = thermal_throttling_hints()
print(json.dumps(s, indent=2))
PY

  echo "PREFLIGHT OK"
}

wait_gate() {
  local label="$1"
  echo "---- wait thermal gate before: $label ----"
  "$PY" - <<PY
import sys, time, json
from pathlib import Path
sys.path.insert(0, "benchmarks/load")
from common import wait_thermal_gate
out = Path("$EVIDENCE_ROOT") / "host_telemetry" / "thermal_gate_${label}.json"
rec = wait_thermal_gate(
    max_tctl_c=65.0,
    max_load1=1.0,
    max_cpu_pct=10.0,
    timeout_sec=900.0,
    out_path=out,
    label="$label",
)
print(json.dumps({"passed": rec.get("passed"), "wait_sec": rec.get("wait_sec"), "out": str(out)}, indent=2))
raise SystemExit(0 if rec.get("passed") else 1)
PY
}

ensure_harness_certs() {
  # cert_matrix precisa do pacote completo do harness (inclui chaves CA).
  local dest="$EVIDENCE_ROOT/meta/harness_certs"
  if [[ -f "$dest/root_ca.key" && -f "$dest/ca.cert" ]]; then
    HARNESS_CERTS="$dest"
    return 0
  fi
  echo "Extraindo certs do sas-harness:latest → $dest"
  mkdir -p "$dest"
  local cid
  cid="$(docker create sas-harness:latest)"
  # caminho real na imagem local: /harness/src/harness/certs
  if ! docker cp "$cid:/harness/src/harness/certs/." "$dest/"; then
    docker rm "$cid" >/dev/null
    die "não foi possível extrair /harness/src/harness/certs de sas-harness:latest"
  fi
  docker rm "$cid" >/dev/null
  # algumas chaves CA ficam em private/; espelhar na raiz se o bench espera root_ca.key
  if [[ -f "$dest/private/root_ca.key" && ! -f "$dest/root_ca.key" ]]; then
    cp -a "$dest/private/root_ca.key" "$dest/root_ca.key"
  fi
  [[ -f "$dest/ca.cert" ]] || die "extração harness incompleta (sem ca.cert)"
  HARNESS_CERTS="$dest"
  echo "HARNESS_CERTS=$HARNESS_CERTS"
}

run_cmd() {
  local label="$1"; shift
  echo ""
  echo "######## $label ########"
  echo "+ $*"
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "(dry-run: não executado)"
    return 0
  fi
  local t0=$SECONDS
  if "$@"; then
    echo "EXIT:0 wall=$((SECONDS - t0))s"
    return 0
  else
    local ec=$?
    echo "EXIT:$ec wall=$((SECONDS - t0))s" | tee -a "$EVIDENCE_ROOT/meta/failures.log"
    return "$ec"
  fi
}

# ---------- Phases ----------
phase_isolation() {
  wait_gate isolation
  # isolation é in-process; gate prévio documenta host
  run_cmd isolation \
    "$PY" benchmarks/load/agent_isolation_bench.py \
      --seed "$SEED" \
      --out-dir "$EVIDENCE_ROOT/01_functional/isolation"
}

phase_cert_matrix() {
  ensure_harness_certs
  wait_gate cert_matrix
  local n_note="$EVIDENCE_ROOT/01_functional/cert_matrix/certs_note.txt"
  echo "certs_from=$HARNESS_CERTS" > "$n_note"
  run_cmd cert_matrix \
    "$PY" benchmarks/load/cert_matrix_bench.py \
      --image "$IMAGE" \
      --certs-dir "$HARNESS_CERTS" \
      --port 19360 \
      --out-dir "$EVIDENCE_ROOT/01_functional/cert_matrix"
}

phase_coldstart() {
  local n=30 cooldown=3
  [[ "$SMOKE" == "1" ]] && n=2 && cooldown=1
  wait_gate coldstart
  run_cmd coldstart \
    "$PY" benchmarks/load/unified_coldstart.py \
      --n "$n" --seed "$SEED" \
      --image "$IMAGE" --certs-dir "$CERTS" \
      --profiles-dir benchmarks/profiles \
      --cooldown-sec "$cooldown" \
      --out-dir "$EVIDENCE_ROOT/03_coldstart_multiband"
}

phase_lifecycle() {
  local n=30 warmup=3
  [[ "$SMOKE" == "1" ]] && n=3 && warmup=1
  local profiles=()
  mapfile -t profiles < <(ls benchmarks/profiles/*.json | xargs -n1 basename -s .json)
  local port_base=20100 i=0
  echo "lifecycle profiles: ${profiles[*]}"
  for pid in "${profiles[@]}"; do
    local reps=(1 2 3 4 5)
    [[ "$SMOKE" == "1" ]] && reps=(1)
    for rep in "${reps[@]}"; do
      i=$((i + 1))
      local out="$EVIDENCE_ROOT/02_lifecycle_multiband/${pid}/rep${rep}"
      mkdir -p "$out"
      local port=$((port_base + i))
      wait_gate "lifecycle_${pid}_rep${rep}"
      echo "=== [$i] $pid rep$rep port=$port $(date -u +%H:%M:%SZ) ===" | tee "$out/runner.log"
      if [[ "$DRY_RUN" == "1" ]]; then
        echo "(dry-run) lifecycle $pid rep$rep"
        continue
      fi
      set +e
      "$PY" benchmarks/load/lifecycle_decomp_bench.py \
        --n "$n" --warmup "$warmup" --seed $((SEED + i)) \
        --profile "$pid" --profiles-dir benchmarks/profiles \
        --image "$IMAGE" --certs-dir "$CERTS" \
        --port "$port" \
        --out-dir "$out" \
        >> "$out/runner.log" 2>&1
      local ec=$?
      set -e
      echo "EXIT:$ec" | tee -a "$out/runner.log"
      docker ps -aq --filter name=sas- | xargs -r docker rm -f >/dev/null 2>&1 || true
      [[ $ec -eq 0 ]] || echo "FAIL $pid rep$rep ec=$ec" | tee -a "$EVIDENCE_ROOT/meta/failures.log"
    done
  done
}

phase_tls() {
  local blocks=30
  [[ "$SMOKE" == "1" ]] && blocks=2
  wait_gate tls_mtls
  run_cmd tls \
    "$PY" benchmarks/load/tls_mtls_overhead_bench.py \
      --blocks "$blocks" --requests-per-block 100 --warmup 5 --seed "$SEED" \
      --image "$IMAGE" --certs-dir "$CERTS" \
      --mtls-port 19460 --tls-port 19461 \
      --out-dir "$EVIDENCE_ROOT/04_tls_mtls"
}

phase_persistence() {
  local repeats=20
  [[ "$SMOKE" == "1" ]] && repeats=2
  wait_gate persistence
  run_cmd persistence \
    "$PY" benchmarks/load/persistence_modes_bench.py \
      --repeats "$repeats" --seed "$SEED" \
      --modes sqlite_commit sqlite_batch sqlite_memory \
      --populations 133 1000 \
      --out-dir "$EVIDENCE_ROOT/05_persistence"
}

phase_factorial() {
  # Runner SEM thermal gate interno → gate explícito aqui.
  local repeats=20
  [[ "$SMOKE" == "1" ]] && repeats=2
  wait_gate factorial
  run_cmd factorial \
    "$PY" benchmarks/load/factorial_provisioning_bench.py \
      --repeats "$repeats" --probe-n 10 --seed "$SEED" \
      --out-dir "$EVIDENCE_ROOT/06_factorial"
}

phase_process() {
  local repeats=5
  local kvals=(1 2 3 5 10 15)
  local devices=1500
  if [[ "$SMOKE" == "1" ]]; then
    repeats=1
    kvals=(1 3)
    devices=60
  fi
  # concurrent_slicing já faz gate por repeat internamente
  run_cmd process \
    "$PY" benchmarks/metrics_sandbox/concurrent_slicing_bench.py \
      --v2-prefix \
      --k-values "${kvals[@]}" \
      --devices "$devices" \
      --repeats "$repeats" \
      --seed "$SEED" \
      --out-dir "$EVIDENCE_ROOT/07_process_concurrency"
}

phase_analyze() {
  # Staging symlinks para analysis/* (não sobrescreve históricos)
  local cid
  cid="$(basename "$EVIDENCE_ROOT")"
  local stage="$ROOT/artifacts/raw/$cid"
  mkdir -p "$stage/lifecycle_decomposition"
  ln -sfn "$EVIDENCE_ROOT/03_coldstart_multiband" "$stage/instrumented_cold_start"
  ln -sfn "$EVIDENCE_ROOT/04_tls_mtls" "$stage/mtls_correctness_overhead"
  ln -sfn "$EVIDENCE_ROOT/05_persistence" "$stage/persistence_decomposition"
  ln -sfn "$EVIDENCE_ROOT/06_factorial" "$stage/factorial_provisioning"
  ln -sfn "$EVIDENCE_ROOT/07_process_concurrency" "$stage/concurrent_slicing_v2"
  ln -sfn "$EVIDENCE_ROOT/01_functional/cert_matrix" "$stage/cert_matrix"
  # lifecycle multi-deployment
  if [[ -d "$EVIDENCE_ROOT/02_lifecycle_multiband" ]]; then
    for pdir in "$EVIDENCE_ROOT/02_lifecycle_multiband"/*; do
      [[ -d "$pdir" ]] || continue
      local pname; pname="$(basename "$pdir")"
      for rdir in "$pdir"/rep*; do
        [[ -d "$rdir" ]] || continue
        local rname; rname="$(basename "$rdir")"
        mkdir -p "$stage/lifecycle_decomposition/$pname/$rname"
        for f in lifecycle_decomp_raw.csv lifecycle_decomp_summary.csv lifecycle_decomp_manifest.json; do
          [[ -f "$rdir/$f" ]] || continue
          ln -sfn "$rdir/$f" "$stage/lifecycle_decomposition/$pname/$rname/$f"
        done
      done
    done
  fi
  # manifesto mínimo
  "$PY" - <<PY
import json
from pathlib import Path
from datetime import datetime, timezone
cid = Path("$EVIDENCE_ROOT").name
stage = Path("artifacts/raw") / cid
man = {
  "campaign_id": cid,
  "campaign_type": "evaluation",
  "git_dirty": False,
  "sas_agent_sha": Path("$EVIDENCE_ROOT/meta/agent_sha.txt").read_text().strip(),
  "image_id": Path("$EVIDENCE_ROOT/meta/image_id.txt").read_text().strip(),
  "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
  "experiments": {
    "lifecycle": {"dir": "lifecycle_decomposition", "layout": "multi_deployment"},
    "mtls": {"dir": "mtls_correctness_overhead", "cert_matrix": "cert_matrix/cert_matrix_results.csv"},
    "cold_start": {"dir": "instrumented_cold_start"},
    "persistence": {"dir": "persistence_decomposition"},
    "factorial": {"dir": "factorial_provisioning"},
    "concurrent_process": {"dir": "concurrent_slicing_v2"},
  },
  "phases": "$PHASES".split(","),
}
(stage / "campaign_manifest.json").write_text(json.dumps(man, indent=2) + "\n")
print("wrote", stage / "campaign_manifest.json")
PY
  run_cmd validate \
    "$PY" analysis/validate_campaign.py --campaign-id "$(basename "$EVIDENCE_ROOT")" || true
  run_cmd tables \
    "$PY" analysis/generate_tables.py --campaign-id "$(basename "$EVIDENCE_ROOT")" || true
  local unc="$EVIDENCE_ROOT/08_analysis/uncertainty_catalog.json"
  if [[ ! -f "$unc" ]]; then
    run_cmd uncertainty \
      "$PY" analysis/uncertainty_report.py \
        --campaign-id "$(basename "$EVIDENCE_ROOT")" \
        --out "$unc" || true
  fi
}

# ---------- Main ----------
preflight

ec=0
run_phase() {
  local name="$1" fn="$2"
  want_phase "$name" || return 0
  if ! "$fn"; then
    echo "PHASE_FAIL $name" | tee -a "$EVIDENCE_ROOT/meta/failures.log"
    ec=1
    # continua demais fases para preservar o que for possível
  fi
}

run_phase isolation phase_isolation
run_phase cert_matrix phase_cert_matrix
run_phase coldstart phase_coldstart
run_phase lifecycle phase_lifecycle
run_phase tls phase_tls
run_phase persistence phase_persistence
run_phase factorial phase_factorial
run_phase process phase_process
run_phase analyze phase_analyze

echo "completed_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$EVIDENCE_ROOT/meta/campaign_id.txt"
if [[ -f "$EVIDENCE_ROOT/meta/failures.log" ]]; then
  echo "Houve falhas — veja $EVIDENCE_ROOT/meta/failures.log"
  ec=1
fi
echo "=== DONE EVIDENCE_ROOT=$EVIDENCE_ROOT (exit=$ec) ==="
exit "$ec"
