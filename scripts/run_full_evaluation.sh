#!/usr/bin/env bash
# Full Major Revision evaluation campaign for TNSM-2026-12135.
#
# PREPARATION / MANUAL EXECUTION ONLY — researcher starts each stage (or `all`).
# Does NOT overwrite evidence/2026-12135/. Does NOT git-checkout old SHAs.
# Does NOT blindly wrap scripts/run_controlled_campaign.sh (that script pins
# main@d3aaa3d and uses absolute cert paths / broad docker rm).
#
# Usage:
#   OUT_ROOT=evidence/tnsm-2026-12135-full-<UTC> ./scripts/run_full_evaluation.sh preflight
#   ./scripts/run_full_evaluation.sh freeze
#   ./scripts/run_full_evaluation.sh <stage>
#   ./scripts/run_full_evaluation.sh status|validate|checksums|dry-run|inventory
#   SMOKE=1 ./scripts/run_full_evaluation.sh coldstart   # reduced n (not definitive)
#
# Env: OUT_ROOT IMAGE_TAG CERTS_DIR HARNESS_CERTS_DIR HOST_COMMIT PY SEED SMOKE DRY_RUN
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PY="${PY:-$REPO/.venv/bin/python}"
IMAGE_TAG="${IMAGE_TAG:-sas-agent:article}"
HOST_COMMIT="${HOST_COMMIT:-$(git rev-parse HEAD)}"
SEED="${SEED:-42}"
SMOKE="${SMOKE:-0}"
DRY_RUN="${DRY_RUN:-0}"
STAMP="${STAMP:-$(date -u +%Y%m%dT%H%M%SZ)}"
OUT_ROOT="${OUT_ROOT:-$REPO/evidence/tnsm-2026-12135-full-${STAMP}}"
# Always absolute so staging symlinks under artifacts/raw/ resolve correctly.
OUT_ROOT="$(cd "$REPO" && realpath -m "$OUT_ROOT")"
CERTS_DIR="${CERTS_DIR:-$REPO/evidence/tnsm-2026-12135-controlled-20261111T205011Z/meta/harness_certs}"
HARNESS_CERTS_DIR="${HARNESS_CERTS_DIR:-$CERTS_DIR}"
CERTS_DIR="$(cd "$REPO" && realpath -m "$CERTS_DIR")"
HARNESS_CERTS_DIR="$(cd "$REPO" && realpath -m "$HARNESS_CERTS_DIR")"

# Complementary / selection (limits frozen; do not retune after observing results)
# Env overrides allowed for campaign wiring; P4 numeric limits stay illustrative-frozen.
K5_SEED="${K5_SEED:-84242}"
K5_REPEATS="${K5_REPEATS:-5}"
# Independent K=3 validation seeds: disjoint from calib SEED and K5_SEED.
K3_SEED="${K3_SEED:-94242}"
K3_REPEATS="${K3_REPEATS:-5}"
COLOCATED_PAIRS="${COLOCATED_PAIRS:-5}"
COLOCATED_CLIENTS="${COLOCATED_CLIENTS:-5}"
COLOCATED_DPC="${COLOCATED_DPC:-300}"
COLOCATED_SEC="${COLOCATED_SEC:-600}"
# Match lifecycle_decomp sustained defaults (Registration+Grant / Relinquish mix).
COLOCATED_ARRIVAL_SEC="${COLOCATED_ARRIVAL_SEC:-10}"
COLOCATED_RELINQUISH_SEC="${COLOCATED_RELINQUISH_SEC:-30}"
RECOVERY_CYCLES="${RECOVERY_CYCLES:-30}"
P4_STARTUP="${P4_STARTUP:-60}"
P4_PROV="${P4_PROV:-1.5}"
P4_GRANT_P95="${P4_GRANT_P95:-1000}"
P4_MEM="${P4_MEM:-2048}"

STAGE="${1:-}"
shift || true

usage() {
  cat <<EOF
Stages (run independently; resume via STAGE_*_DONE):
  preflight freeze inventory
  unittest security_tests
  isolation cert_matrix boundary robustness winnforum
  coldstart lifecycle tls persistence factorial
  process_concurrency container_concurrency container_per_agent
  sustained colocated recovery
  k5_validation k3_validation offline_selection
  analyze validate checksums
  status dry-run all

Canonical corpus evidence/2026-12135/ is read-only.
EOF
}

die() { echo "ERROR: $*" >&2; exit 1; }
log() { echo "[$(date -u +%H:%M:%SZ)] $*"; }

refuse_canonical() {
  local canon="$REPO/evidence/2026-12135"
  [[ "$(realpath -m "$OUT_ROOT")" != "$(realpath -m "$canon")" ]] \
    || die "OUT_ROOT must not be the canonical corpus"
  case "$OUT_ROOT" in
    *tnsm-2026-12135-final*|*tnsm-2026-12135-rev1*|*sustained-txfix*|*r18-colocated*)
      die "OUT_ROOT looks like a historical/pilot tree: $OUT_ROOT" ;;
  esac
}

image_id() { docker image inspect --format '{{.Id}}' "$IMAGE_TAG"; }

require_freeze() {
  [[ -f "$OUT_ROOT/meta/protocol_frozen.json" ]] || die "run freeze first"
  local frozen_commit frozen_img
  frozen_commit="$("$PY" -c "import json;print(json.load(open('$OUT_ROOT/meta/protocol_frozen.json'))['host_commit'])")"
  frozen_img="$("$PY" -c "import json;print(json.load(open('$OUT_ROOT/meta/protocol_frozen.json'))['image_id'])")"
  local now_commit now_img
  now_commit="$(git rev-parse HEAD)"
  now_img="$(image_id)"
  [[ "$now_commit" == "$frozen_commit" ]] || die "HOST_COMMIT diverged: freeze=$frozen_commit now=$now_commit"
  [[ "$now_img" == "$frozen_img" ]] || die "IMAGE_ID diverged: freeze=$frozen_img now=$now_img"
}

mark_done() {
  local s="$1"
  mkdir -p "$OUT_ROOT/meta/stages"
  date -u +%Y-%m-%dT%H:%M:%SZ >"$OUT_ROOT/meta/stages/STAGE_${s}_DONE"
  # Callers must only invoke after: exit 0 + required artifacts + integrity checks.
  log "STAGE $s DONE"
}

unmark_done() {
  local s="$1"
  rm -f "$OUT_ROOT/meta/stages/STAGE_${s}_DONE"
}

stage_done() { [[ -f "$OUT_ROOT/meta/stages/STAGE_${1}_DONE" ]]; }

require_files() {
  local label="$1"; shift
  local f
  for f in "$@"; do
    [[ -e "$f" ]] || die "stage $label missing required artifact: $f"
  done
}

record_attempt() {
  local stage="$1" status="$2" note="${3:-}"
  mkdir -p "$OUT_ROOT/meta"
  printf '%s\t%s\t%s\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$stage" "$status" "$note" \
    >>"$OUT_ROOT/meta/attempts.tsv"
}

wait_gate() {
  local label="$1"
  [[ "$DRY_RUN" == "1" ]] && { log "(dry-run) wait_gate $label"; return 0; }
  log "thermal gate: $label"
  "$PY" - <<PY
import sys
from pathlib import Path
sys.path.insert(0, "benchmarks/load")
from common import wait_thermal_gate
out = Path("$OUT_ROOT") / "host_telemetry" / f"thermal_gate_${label}.json"
out.parent.mkdir(parents=True, exist_ok=True)
rec = wait_thermal_gate(
    max_tctl_c=65.0, max_load1=1.0, max_cpu_pct=10.0,
    timeout_sec=900.0, out_path=out, label="$label",
)
print({"passed": rec.get("passed"), "wait_sec": rec.get("wait_sec")})
raise SystemExit(0 if rec.get("passed") else 1)
PY
}

run_cmd() {
  local name="$1"; shift
  log "RUN $name: $*"
  record_attempt "$name" "start" "$*"
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "(dry-run) $*"
    record_attempt "$name" "dry-run" ""
    return 0
  fi
  local t0=$SECONDS ec=0
  set +e
  "$@"
  ec=$?
  set -e
  record_attempt "$name" "exit:$ec" "wall=$((SECONDS - t0))s"
  return "$ec"
}

# Cleanup only containers belonging to known campaign prefixes (not all sas-*).
safe_rm_campaign_containers() {
  local id
  for id in $(docker ps -aq \
      --filter "name=sas-http-sqlite-recovery" \
      --filter "name=sas-colocated" \
      2>/dev/null || true); do
    docker rm -f "$id" >/dev/null 2>&1 || true
  done
}

# ---------- Stages ----------

stage_preflight() {
  refuse_canonical
  mkdir -p "$OUT_ROOT"/{meta,host_telemetry}
  command -v docker >/dev/null || die "docker missing"
  [[ -x "$PY" ]] || die "venv python missing: $PY"
  docker image inspect "$IMAGE_TAG" >/dev/null || die "image missing: $IMAGE_TAG"
  [[ -f "$CERTS_DIR/ca.cert" ]] || die "certs missing: $CERTS_DIR"
  [[ -f "$CERTS_DIR/domain_proxy.cert" ]] || die "domain_proxy.cert missing"
  local free_g
  free_g="$(df -BG --output=avail "$REPO" | tail -1 | tr -dc '0-9')"
  [[ "${free_g:-0}" -ge 20 ]] || die "disk <20GiB ($free_g)"

  # Read-only host snapshot (no permanent Ubuntu changes).
  "$PY" - <<PY >"$OUT_ROOT/meta/preflight_snapshot.json"
import json, os, platform, shutil, subprocess, sys
from pathlib import Path
sys.path.insert(0, "benchmarks/load")
from common import host_load_snapshot, cpu_freq_snapshot, thermal_throttling_hints
s = host_load_snapshot()
s["cpu_freq"] = cpu_freq_snapshot()
s["throttling_hints"] = thermal_throttling_hints()
s["python"] = sys.version
s["platform"] = platform.platform()
s["docker"] = subprocess.check_output(["docker","version","--format","{{.Server.Version}}"], text=True).strip()
s["image_id"] = subprocess.check_output(["docker","image","inspect","--format","{{.Id}}","$IMAGE_TAG"], text=True).strip()
s["host_commit"] = "$HOST_COMMIT"
s["ac_power"] = None
try:
    online = Path("/sys/class/power_supply")
    for p in online.glob("*/online"):
        if p.read_text().strip() == "1":
            s["ac_power"] = True
            break
        s["ac_power"] = False
except Exception as exc:
    s["ac_power_error"] = str(exc)
s["disk_free_gb"] = shutil.disk_usage(".").free // (1<<30)
s["gate_admit"] = (
    s.get("temperature_tctl_c") is not None and s["temperature_tctl_c"] <= 65
    and s.get("loadavg_1m") is not None and s["loadavg_1m"] <= 1.0
    and s.get("cpu_percent_prior") is not None and s["cpu_percent_prior"] <= 10
)
print(json.dumps(s, indent=2))
raise SystemExit(0 if s["gate_admit"] else 1)
PY
  # Optional: article preflight (read-only); do not fail hard on AC if unavailable.
  if [[ -x "$REPO/scripts/preflight_article.sh" ]]; then
    REQUIRE_AC_POWER=0 REQUIRE_CLEAN_GIT=0 MAX_LOAD_1M=1.0 \
      "$REPO/scripts/preflight_article.sh" --image "$IMAGE_TAG" \
      >"$OUT_ROOT/meta/preflight_article.txt" 2>&1 || \
      log "WARN preflight_article non-zero (see meta/preflight_article.txt)"
  fi
  echo "$HOST_COMMIT" >"$OUT_ROOT/meta/agent_sha.txt"
  image_id >"$OUT_ROOT/meta/image_id.txt"
  mark_done preflight
}

stage_freeze() {
  refuse_canonical
  stage_done preflight || die "run preflight first"
  # Idempotent: never overwrite a freeze after definitive experiments began.
  if stage_done freeze && [[ -f "$OUT_ROOT/meta/protocol_frozen.json" ]]; then
    log "freeze already recorded — refusing overwrite"
    return 0
  fi
  if [[ -f "$OUT_ROOT/meta/protocol_frozen.json" ]]; then
    die "protocol_frozen.json exists without STAGE_freeze_DONE — refuse overwrite"
  fi
  # Block freeze-after-measurement reordering (coldstart before formal freeze).
  local early
  for early in coldstart lifecycle tls persistence factorial \
               process_concurrency container_concurrency container_per_agent \
               sustained colocated recovery k5_validation k3_validation; do
    if stage_done "$early"; then
      die "cannot freeze after stage $early already DONE — freeze must precede definitive experiments"
    fi
  done
  mkdir -p "$OUT_ROOT/meta"
  local img
  img="$(image_id)"
  "$PY" - <<PY
import hashlib, json, subprocess, sys
from pathlib import Path
repo = Path("$REPO")
out = Path("$OUT_ROOT/meta")
files = [
  "main.py","database.py","services/heartbeat_service.py","services/mtls_auth.py",
  "services/authorization_service.py","services/profile_manager.py","requirements.txt",
  "benchmarks/load/common.py",
  "benchmarks/load/colocated_sustained_bench.py",
  "benchmarks/load/container_concurrency_bench.py",
  "benchmarks/load/lifecycle_decomp_bench.py",
  "benchmarks/load/unified_coldstart.py",
  "benchmarks/load/http_mtls_sqlite_recovery_campaign.py",
  "benchmarks/load/factorial_provisioning_bench.py",
  "benchmarks/load/persistence_modes_bench.py",
  "benchmarks/load/tls_mtls_overhead_bench.py",
  "analysis/provisioning_selection.py","analysis/uncertainty_report.py",
  "scripts/run_full_evaluation.sh","scripts/run_complementary_major_revision.sh",
]
hashes = {rel: hashlib.sha256((repo/rel).read_bytes()).hexdigest() if (repo/rel).is_file() else None for rel in files}
# Agent vs image match for core files
matches = {}
for rel in ["main.py","database.py","services/heartbeat_service.py","services/mtls_auth.py"]:
    host = hashes[rel]
    proc = subprocess.run(["docker","run","--rm","--entrypoint","sha256sum","$IMAGE_TAG",f"/app/sas-agent/{rel}"],
                          capture_output=True, text=True, timeout=60)
    img_h = proc.stdout.split()[0] if proc.returncode == 0 else None
    matches[rel] = {"host": host, "image": img_h, "match": host == img_h}
if not all(v["match"] for v in matches.values()):
    print("AGENT_IMAGE_MISMATCH", matches, file=sys.stderr)
    raise SystemExit(2)
proto = {
  "campaign_id": "tnsm-2026-12135-full",
  "out_root": str(Path("$OUT_ROOT")),
  "frozen_at_utc": subprocess.check_output(["date","-u","+%Y-%m-%dT%H:%M:%SZ"], text=True).strip(),
  "host_commit": "$HOST_COMMIT",
  "branch": subprocess.check_output(["git","branch","--show-current"], text=True).strip(),
  "image_tag": "$IMAGE_TAG",
  "image_id": "$img",
  "agent_image_file_match": matches,
  "script_sha256": hashes,
  "seed": $SEED,
  "smoke": $SMOKE,
  "thermal_gate": {"tctl_c_max": 65.0, "load1_max": 1.0, "cpu_pct_prior_max": 10.0},
  "certs_dir": "$CERTS_DIR",
  "certs_policy": "runtime mount only; never archive *.key into OUT_ROOT",
  "canonical_corpus_readonly": "evidence/2026-12135",
  "failure_policy": {
    "retain": ["measured_sas", "functional failures under load"],
    "may_invalidate": ["infrastructure", "harness", "thermal_gate_fail"],
    "no_selective_deletion": True,
    "no_STAGE_DONE_before_artifact_check": True
  },
  "stages_planned": [
    "unittest","security_tests","isolation","cert_matrix","boundary","robustness","winnforum",
    "coldstart","lifecycle","tls","persistence","factorial",
    "process_concurrency","container_concurrency","container_per_agent",
    "sustained","colocated","recovery","k5_validation","k3_validation","offline_selection","analyze"
  ],
  "params": {
    "lifecycle": {"profiles": 8, "reps": 5 if $SMOKE == 0 else 1, "n_cycles": 30 if $SMOKE == 0 else 3},
    "coldstart": {"n_per_profile": 30 if $SMOKE == 0 else 2, "profiles": 8},
    "tls": {"blocks": 30 if $SMOKE == 0 else 2, "requests_per_block": 100},
    "persistence": {"repeats": 20 if $SMOKE == 0 else 2, "modes": ["sqlite_commit","sqlite_batch","sqlite_memory"], "populations": [133,1000]},
    "factorial": {"repeats": 20 if $SMOKE == 0 else 2, "probe_n": 10},
    "process_concurrency": {"k": [1,2,3,5,10,15], "devices": 1500, "repeats": 5},
    "container_concurrency": {"k": [1,2,3,5,10,15], "devices": 1500, "repeats": 5, "scenarios": ["A","B"]},
    "container_per_agent": {"devices_per_agent": 300, "k": [1,2,3,5,10,15], "repeats": 5},
    "sustained": {"populations": [300,1500], "seeds": [1,2,3], "sec": 600, "schedule": "respect_transmit_expire"},
    "colocated": {
      "pairs": $COLOCATED_PAIRS, "clients": $COLOCATED_CLIENTS, "dpc": $COLOCATED_DPC,
      "sec": $COLOCATED_SEC, "seed": $SEED,
      "arrival_interval_sec": $COLOCATED_ARRIVAL_SEC,
      "relinquish_interval_sec": $COLOCATED_RELINQUISH_SEC
    },
    "recovery": {"cycles": $RECOVERY_CYCLES},
    "k5_validation": {"seed": $K5_SEED, "repeats": $K5_REPEATS, "evaluation_package": "P4_sensitivity_not_k_star"},
    "k3_validation": {"seed": $K3_SEED, "repeats": $K3_REPEATS, "evaluation_package": "P4_selected_k_independent",
                      "note": "disjoint seeds; validates selected K under frozen P4 limits"},
    "P4": {"startup_sec": $P4_STARTUP, "provisioning_sec": $P4_PROV, "grant_batch_p95_ms": $P4_GRANT_P95, "memory_mb": $P4_MEM,
           "kind": "illustrative_sensitivity_prior_calibration", "not_prospective_slo": True}
  },
  "do_not_use": ["scripts/run_controlled_campaign.sh without reviewing pin to d3aaa3d", "benchmarks/run_campaign.py as primary corpus protocol", "pilot evidence/2026-12135-r18-*"]
}
(out / "protocol_frozen.json").write_text(json.dumps(proto, indent=2) + "\n")
print(out / "protocol_frozen.json")
PY
  mark_done freeze
}

stage_inventory() {
  refuse_canonical
  mkdir -p "$OUT_ROOT/meta"
  "$PY" - <<'PY' >"$OUT_ROOT/meta/inventory.json"
import json
from pathlib import Path
repo = Path(".")
tests = sorted(p.name for p in (repo/"tests").glob("test_*.py"))
benches = sorted(p.name for p in (repo/"benchmarks/load").glob("*.py") if p.name != "__init__.py")
print(json.dumps({
  "tests": tests,
  "benchmarks_load": benches,
  "analysis": sorted(p.name for p in (repo/"analysis").glob("*.py")),
  "scripts": sorted(p.name for p in (repo/"scripts").glob("*.sh")),
  "historical_corpus_dirs": sorted(p.name for p in (repo/"evidence/2026-12135").iterdir() if p.is_dir()),
}, indent=2))
PY
  mark_done inventory
}

stage_unittest() {
  require_freeze
  stage_done unittest && { log "unittest already done"; return 0; }
  mkdir -p "$OUT_ROOT/01_tests"
  local ec=0
  if [[ "$DRY_RUN" == "1" ]]; then
    log "(dry-run) unittest discover"
  else
    set +e
    "$PY" -m unittest discover -s tests -v \
      >"$OUT_ROOT/01_tests/unittest.log" 2>&1
    ec=$?
    set -e
  fi
  record_attempt unittest "exit:$ec" ""
  [[ $ec -eq 0 ]] || { echo "unittest FAIL" | tee -a "$OUT_ROOT/meta/failures.log"; return $ec; }
  [[ -s "$OUT_ROOT/01_tests/unittest.log" || "$DRY_RUN" == "1" ]] || return 1
  mark_done unittest
}

stage_security_tests() {
  require_freeze
  stage_done security_tests && return 0
  mkdir -p "$OUT_ROOT/01_tests"
  run_cmd security_tests "$PY" -m unittest \
    tests.test_admin_authorization \
    tests.test_tls_persistent_revocation \
    tests.test_authorization_lifecycle \
    tests.test_http_mtls_sqlite_recovery \
    tests.test_sqlite_crash_recovery -v \
    >"$OUT_ROOT/01_tests/security_tests.log" 2>&1
  mark_done security_tests
}

stage_isolation() {
  require_freeze
  stage_done isolation && return 0
  mkdir -p "$OUT_ROOT/02_functional/isolation"
  wait_gate isolation
  run_cmd isolation "$PY" benchmarks/load/agent_isolation_bench.py \
    --seed "$SEED" --out-dir "$OUT_ROOT/02_functional/isolation"
  [[ -f "$OUT_ROOT/02_functional/isolation/agent_isolation_results.csv" || "$DRY_RUN" == "1" ]] || return 1
  mark_done isolation
}

stage_cert_matrix() {
  require_freeze
  stage_done cert_matrix && return 0
  mkdir -p "$OUT_ROOT/02_functional/cert_matrix"
  [[ -f "$HARNESS_CERTS_DIR/ca.cert" ]] || die "HARNESS_CERTS_DIR incomplete: $HARNESS_CERTS_DIR"
  echo "certs_from=$HARNESS_CERTS_DIR keys_not_archived=true" \
    >"$OUT_ROOT/02_functional/cert_matrix/certs_note.txt"
  wait_gate cert_matrix
  run_cmd cert_matrix "$PY" benchmarks/load/cert_matrix_bench.py \
    --image "$IMAGE_TAG" --certs-dir "$HARNESS_CERTS_DIR" --port 19360 \
    --out-dir "$OUT_ROOT/02_functional/cert_matrix"
  mark_done cert_matrix
}

stage_boundary() {
  require_freeze
  stage_done boundary && return 0
  mkdir -p "$OUT_ROOT/02_functional/boundary"
  run_cmd boundary "$PY" benchmarks/load/boundary_enforcement_bench.py \
    --profiles-dir benchmarks/profiles \
    --out-dir "$OUT_ROOT/02_functional/boundary"
  mark_done boundary
}

stage_robustness() {
  require_freeze
  stage_done robustness && return 0
  mkdir -p "$OUT_ROOT/02_functional/robustness"
  run_cmd robustness "$PY" -m unittest tests.test_band_profile_robustness -v \
    >"$OUT_ROOT/02_functional/robustness/unittest.log" 2>&1
  # Emit a one-line CSV provenance note (full CSV historically from campaign runner).
  echo "source,test_band_profile_robustness,see_unittest.log" \
    >"$OUT_ROOT/02_functional/robustness/band_profile_robustness_note.csv"
  mark_done robustness
}

stage_winnforum() {
  require_freeze
  stage_done winnforum && return 0
  mkdir -p "$OUT_ROOT/02_functional/winnforum"
  # Prefer re-execution under frozen image; parse-only is historical reuse, not a new run.
  if [[ -x "$REPO/run_cbrs_tests.sh" ]] && docker image inspect sas-harness >/dev/null 2>&1; then
    wait_gate winnforum
    run_cmd winnforum_harness env EVIDENCE_DIR="$OUT_ROOT/02_functional/winnforum" \
      bash "$REPO/run_cbrs_tests.sh"
    require_files winnforum "$OUT_ROOT/02_functional/winnforum/lifecycle_validation.csv"
    echo "mode=harness_reexecution" >"$OUT_ROOT/02_functional/winnforum/PROVENANCE.txt"
    mark_done winnforum
    return 0
  fi
  if [[ -d "$REPO/benchmarks/data" ]] && compgen -G "$REPO/benchmarks/data/winnforum_*.log" >/dev/null; then
    run_cmd winnforum_parse "$PY" benchmarks/parse_winnforum_logs.py \
      --logs-dir "$REPO/benchmarks/data" \
      --out "$OUT_ROOT/02_functional/winnforum/lifecycle_validation.csv"
    require_files winnforum "$OUT_ROOT/02_functional/winnforum/lifecycle_validation.csv"
    cat >"$OUT_ROOT/02_functional/winnforum/PROVENANCE.txt" <<EOF
mode=parse_only_historical_logs
logs_dir=benchmarks/data
note=NOT a fresh WInnForum harness execution under the frozen campaign image.
Do not declare certification. Historical corpus: evidence/2026-12135/functional/winnforum/
Re-run with sas-harness image for definitive re-execution of all 65 cases.
EOF
    # Parse-only satisfies artifact presence but is not certification; keep DONE for resume,
    # with explicit provenance that execution was not repeated.
    mark_done winnforum
    return 0
  fi
  cat >"$OUT_ROOT/02_functional/winnforum/EXTERNAL_PREP_REQUIRED.txt" <<EOF
WInnForum 65-case harness not runnable in this environment snapshot.
Requires: sas-harness image + docker compose + free ports 9000/9001,
or logs under benchmarks/data/winnforum_*.log for parse-only mode.
Historical evidence remains at evidence/2026-12135/functional/winnforum/ (n=65).
This stage is NOT marked DONE — re-run after external prep.
EOF
  record_attempt winnforum "external_prep_required" ""
  log "WARN winnforum requires external prep — stage NOT done"
  return 0
}

stage_coldstart() {
  require_freeze
  stage_done coldstart && return 0
  local n=30 cooldown=3
  [[ "$SMOKE" == "1" ]] && n=2 && cooldown=1
  mkdir -p "$OUT_ROOT/03_coldstart"
  wait_gate coldstart
  run_cmd coldstart "$PY" benchmarks/load/unified_coldstart.py \
    --n "$n" --seed "$SEED" --image "$IMAGE_TAG" --certs-dir "$CERTS_DIR" \
    --profiles-dir benchmarks/profiles --cooldown-sec "$cooldown" \
    --out-dir "$OUT_ROOT/03_coldstart"
  mark_done coldstart
}

stage_lifecycle() {
  require_freeze
  stage_done lifecycle && return 0
  local n=30 warmup=3
  [[ "$SMOKE" == "1" ]] && n=3 && warmup=1
  local profiles=()
  mapfile -t profiles < <(ls benchmarks/profiles/*.json | xargs -n1 basename -s .json)
  local port_base=20100 i=0
  for pid in "${profiles[@]}"; do
    local reps=(1 2 3 4 5)
    [[ "$SMOKE" == "1" ]] && reps=(1)
    for rep in "${reps[@]}"; do
      i=$((i + 1))
      local out="$OUT_ROOT/04_lifecycle/${pid}/rep${rep}"
      if [[ -f "$out/lifecycle_decomp_summary.csv" ]]; then
        log "skip lifecycle $pid rep$rep (artifacts present)"
        continue
      fi
      mkdir -p "$out"
      local port=$((port_base + i))
      wait_gate "lifecycle_${pid}_rep${rep}"
      run_cmd "lifecycle_${pid}_rep${rep}" \
        "$PY" benchmarks/load/lifecycle_decomp_bench.py \
          --n "$n" --warmup "$warmup" --seed $((SEED + i)) \
          --profile "$pid" --profiles-dir benchmarks/profiles \
          --image "$IMAGE_TAG" --certs-dir "$CERTS_DIR" \
          --port "$port" --out-dir "$out" \
        || echo "FAIL lifecycle $pid rep$rep" | tee -a "$OUT_ROOT/meta/failures.log"
      # Narrow cleanup: containers from this port/name pattern only if still up.
      docker ps -aq --filter "publish=$port" | xargs -r docker rm -f >/dev/null 2>&1 || true
    done
  done
  mark_done lifecycle
}

stage_tls() {
  require_freeze
  stage_done tls && return 0
  local blocks=30
  [[ "$SMOKE" == "1" ]] && blocks=2
  mkdir -p "$OUT_ROOT/05_tls"
  wait_gate tls
  run_cmd tls "$PY" benchmarks/load/tls_mtls_overhead_bench.py \
    --blocks "$blocks" --requests-per-block 100 --warmup 5 --seed "$SEED" \
    --image "$IMAGE_TAG" --certs-dir "$CERTS_DIR" \
    --mtls-port 19460 --tls-port 19461 \
    --out-dir "$OUT_ROOT/05_tls"
  mark_done tls
}

stage_persistence() {
  require_freeze
  stage_done persistence && return 0
  local repeats=20
  [[ "$SMOKE" == "1" ]] && repeats=2
  mkdir -p "$OUT_ROOT/06_persistence"
  wait_gate persistence
  run_cmd persistence "$PY" benchmarks/load/persistence_modes_bench.py \
    --repeats "$repeats" --seed "$SEED" \
    --modes sqlite_commit sqlite_batch sqlite_memory \
    --populations 133 1000 \
    --out-dir "$OUT_ROOT/06_persistence"
  mark_done persistence
}

stage_factorial() {
  require_freeze
  stage_done factorial && return 0
  local repeats=20
  [[ "$SMOKE" == "1" ]] && repeats=2
  mkdir -p "$OUT_ROOT/07_factorial"
  wait_gate factorial
  run_cmd factorial "$PY" benchmarks/load/factorial_provisioning_bench.py \
    --repeats "$repeats" --probe-n 10 --seed "$SEED" \
    --out-dir "$OUT_ROOT/07_factorial"
  mark_done factorial
}

stage_process_concurrency() {
  require_freeze
  stage_done process_concurrency && return 0
  local repeats=5 devices=1500
  local kvals=(1 2 3 5 10 15)
  if [[ "$SMOKE" == "1" ]]; then repeats=1; kvals=(1 3); devices=60; fi
  mkdir -p "$OUT_ROOT/08_concurrency/processes"
  run_cmd process_concurrency "$PY" benchmarks/metrics_sandbox/concurrent_slicing_bench.py \
    --v2-prefix --k-values "${kvals[@]}" --devices "$devices" \
    --repeats "$repeats" --seed "$SEED" \
    --out-dir "$OUT_ROOT/08_concurrency/processes"
  mark_done process_concurrency
}

stage_container_concurrency() {
  require_freeze
  stage_done container_concurrency && return 0
  local repeats=5
  local kvals=(1 2 3 5 10 15)
  [[ "$SMOKE" == "1" ]] && repeats=1 && kvals=(1 5)
  mkdir -p "$OUT_ROOT/08_concurrency/containers"
  wait_gate container_concurrency
  run_cmd container_concurrency "$PY" benchmarks/load/container_concurrency_bench.py \
    --k-values "${kvals[@]}" --devices 1500 --repeats "$repeats" --seed "$SEED" \
    --scenarios A B --image "$IMAGE_TAG" --certs-dir "$CERTS_DIR" \
    --out-dir "$OUT_ROOT/08_concurrency/containers"
  mark_done container_concurrency
}

stage_container_per_agent() {
  require_freeze
  stage_done container_per_agent && return 0
  local repeats=5
  local kvals=(1 2 3 5 10 15)
  [[ "$SMOKE" == "1" ]] && repeats=1 && kvals=(1 5)
  mkdir -p "$OUT_ROOT/08_concurrency/per_agent"
  wait_gate container_per_agent
  run_cmd container_per_agent "$PY" benchmarks/load/container_concurrency_bench.py \
    --k-values "${kvals[@]}" --devices-per-agent 300 --repeats "$repeats" \
    --seed $((SEED + 17)) --scenarios A \
    --image "$IMAGE_TAG" --certs-dir "$CERTS_DIR" \
    --out-dir "$OUT_ROOT/08_concurrency/per_agent"
  mark_done container_per_agent
}

stage_sustained() {
  require_freeze
  stage_done sustained && return 0
  local sec=600
  [[ "$SMOKE" == "1" ]] && sec=15
  local pops=(300 1500)
  local seeds=(1 2 3)
  local port=20500
  for n in "${pops[@]}"; do
    for s in "${seeds[@]}"; do
      local out="$OUT_ROOT/09_sustained/n${n}_s${s}"
      if [[ -f "$out/lifecycle_sustained_summary.csv" ]]; then
        log "skip sustained n=$n seed=$s"
        continue
      fi
      mkdir -p "$out"
      wait_gate "sustained_n${n}_s${s}"
      port=$((port + 1))
      run_cmd "sustained_n${n}_s${s}" \
        "$PY" benchmarks/load/lifecycle_decomp_bench.py \
          --profile cbrs_winnforum --profiles-dir benchmarks/profiles \
          --image "$IMAGE_TAG" --certs-dir "$CERTS_DIR" --port "$port" \
          --seed "$s" --sustained-sec "$sec" --sustained-devices "$n" \
          --heartbeat-schedule-mode respect_transmit_expire \
          --out-dir "$out" \
        || echo "FAIL sustained n=$n s=$s" | tee -a "$OUT_ROOT/meta/failures.log"
      docker ps -aq --filter "publish=$port" | xargs -r docker rm -f >/dev/null 2>&1 || true
    done
  done
  mark_done sustained
}

stage_colocated() {
  require_freeze
  stage_done colocated && return 0
  local sec=$COLOCATED_SEC pairs=$COLOCATED_PAIRS
  [[ "$SMOKE" == "1" ]] && sec=15 && pairs=1
  mkdir -p "$OUT_ROOT/10_colocated"
  run_cmd colocated "$PY" benchmarks/load/colocated_sustained_bench.py \
    --mode paired --pairs "$pairs" --n-clients "$COLOCATED_CLIENTS" \
    --devices-per-client "$COLOCATED_DPC" --sustained-sec "$sec" \
    --arrival-interval-sec "$COLOCATED_ARRIVAL_SEC" \
    --relinquish-interval-sec "$COLOCATED_RELINQUISH_SEC" \
    --seed "$SEED" --heartbeat-schedule-mode respect_transmit_expire \
    --image "$IMAGE_TAG" --certs-dir "$CERTS_DIR" \
    --out-dir "$OUT_ROOT/10_colocated"
  require_files colocated \
    "$OUT_ROOT/10_colocated/colocated_sustained_runs.csv" \
    "$OUT_ROOT/10_colocated/colocated_sustained_raw.csv"
  mark_done colocated
}

stage_recovery() {
  require_freeze
  stage_done recovery && return 0
  local cycles=$RECOVERY_CYCLES
  [[ "$SMOKE" == "1" ]] && cycles=1
  mkdir -p "$OUT_ROOT/11_recovery"
  safe_rm_campaign_containers
  run_cmd recovery "$PY" benchmarks/load/http_mtls_sqlite_recovery_campaign.py \
    --cycles "$cycles" --out-dir "$OUT_ROOT/11_recovery" \
    --image "$IMAGE_TAG" --image-id "$(image_id)" \
    --certs-dir "$CERTS_DIR" --host-commit "$HOST_COMMIT"
  mark_done recovery
}

stage_k5_validation() {
  require_freeze
  stage_done k5_validation && return 0
  mkdir -p "$OUT_ROOT/12_provisioning/k5_validation"
  cat >"$OUT_ROOT/12_provisioning/k5_validation/EVALUATION_PACKAGE.txt" <<EOF
evaluation_package=P4_k5_illustrative_sensitivity
role=additional_sensitivity_evidence_NOT_validation_of_selected_k
startup_sec=$P4_STARTUP
provisioning_sec=$P4_PROV
grant_batch_p95_ms=$P4_GRANT_P95
memory_mb=$P4_MEM
do_not_retune_after_results=true
EOF
  local repeats=$K5_REPEATS
  [[ "$SMOKE" == "1" ]] && repeats=1
  wait_gate k5_validation
  run_cmd k5_validation "$PY" benchmarks/load/container_concurrency_bench.py \
    --k-values 5 --devices 1500 --repeats "$repeats" --seed "$K5_SEED" \
    --scenarios A --image "$IMAGE_TAG" --certs-dir "$CERTS_DIR" \
    --out-dir "$OUT_ROOT/12_provisioning/k5_validation"
  require_files k5_validation \
    "$OUT_ROOT/12_provisioning/k5_validation/container_concurrency_runs.csv"
  mark_done k5_validation
}

stage_k3_validation() {
  # Independent validation of P4-selected K=3. Does NOT retune P4 limits.
  # Seeds disjoint from calibration (SEED) and K5_SEED. Do not reuse calib rows.
  require_freeze
  stage_done k3_validation && return 0
  mkdir -p "$OUT_ROOT/12_provisioning/k3_validation"
  cat >"$OUT_ROOT/12_provisioning/k3_validation/EVALUATION_PACKAGE.txt" <<EOF
evaluation_package=P4_selected_k_independent_validation
selected_k=3
startup_sec=$P4_STARTUP
provisioning_sec=$P4_PROV
grant_batch_p95_ms=$P4_GRANT_P95
memory_mb=$P4_MEM
seed=$K3_SEED
repeats=$K3_REPEATS
devices=1500
scenario=A
persistence=sqlite_default_as_container_concurrency_bench
thermal_gate=required
disjoint_from_calib_seed=$SEED
disjoint_from_k5_seed=$K5_SEED
do_not_retune_limits=true
do_not_reuse_calibration_observations=true
k5_validation_is_additional_sensitivity_only=true
EOF
  if [[ "${PREPARE_ONLY:-0}" == "1" ]]; then
    log "PREPARE_ONLY: wrote k3_validation protocol; not executing"
    record_attempt k3_validation "prepared_only" "seed=$K3_SEED"
    return 0
  fi
  local repeats=$K3_REPEATS
  [[ "$SMOKE" == "1" ]] && repeats=1
  wait_gate k3_validation
  run_cmd k3_validation "$PY" benchmarks/load/container_concurrency_bench.py \
    --k-values 3 --devices 1500 --repeats "$repeats" --seed "$K3_SEED" \
    --scenarios A --image "$IMAGE_TAG" --certs-dir "$CERTS_DIR" \
    --out-dir "$OUT_ROOT/12_provisioning/k3_validation"
  require_files k3_validation \
    "$OUT_ROOT/12_provisioning/k3_validation/container_concurrency_runs.csv"
  mark_done k3_validation
}

stage_offline_selection() {
  require_freeze
  stage_done offline_selection && return 0
  mkdir -p "$OUT_ROOT/12_provisioning/offline_selection"
  local calib="$REPO/evidence/2026-12135/concurrency/containers/calibration/container_concurrency_runs.csv"
  local val_hist="$REPO/evidence/2026-12135/concurrency/containers/validation/container_concurrency_runs.csv"
  local val_args=()
  [[ -f "$val_hist" ]] && val_args+=(--validation "$val_hist")
  local val_k5="$OUT_ROOT/12_provisioning/k5_validation/container_concurrency_runs.csv"
  [[ -f "$val_k5" ]] && val_args+=(--validation "$val_k5")
  local val_k3="$OUT_ROOT/12_provisioning/k3_validation/container_concurrency_runs.csv"
  [[ -f "$val_k3" ]] && val_args+=(--validation "$val_k3")
  # Prefer new calibration if present
  local cal_new="$OUT_ROOT/08_concurrency/containers/container_concurrency_runs.csv"
  [[ -f "$cal_new" ]] && calib="$cal_new"

  run_sel() {
    local name="$1"; shift
    run_cmd "sel_$name" "$PY" analysis/provisioning_selection.py \
      --calibration "$calib" ${val_args[@]+"${val_args[@]}"} "$@" \
      --out "$OUT_ROOT/12_provisioning/offline_selection/${name}.json"
  }
  run_sel P0_original_illustrative \
    --limit-startup-sec 60 --limit-provisioning-sec 60 \
    --limit-grant-batch-p95-ms 1000 --limit-memory-mb 2048
  run_sel P1_tight_grant_p95 \
    --limit-startup-sec 60 --limit-provisioning-sec 60 \
    --limit-grant-batch-p95-ms 230 --limit-memory-mb 2048
  run_sel P2_tight_memory \
    --limit-startup-sec 60 --limit-provisioning-sec 60 \
    --limit-grant-batch-p95-ms 1000 --limit-memory-mb 120
  run_sel P3_infeasible_stress \
    --limit-startup-sec 0.5 --limit-provisioning-sec 1.0 \
    --limit-grant-batch-p95-ms 100 --limit-memory-mb 50
  run_sel P4_k5_illustrative_sensitivity \
    --limit-startup-sec "$P4_STARTUP" --limit-provisioning-sec "$P4_PROV" \
    --limit-grant-batch-p95-ms "$P4_GRANT_P95" --limit-memory-mb "$P4_MEM"
  cat >"$OUT_ROOT/12_provisioning/offline_selection/README_LIMITS.txt" <<EOF
P0–P3: illustrative sensitivity.
P4: frozen illustrative limits (startup=${P4_STARTUP}s, provisioning=${P4_PROV}s,
    grant_p95=${P4_GRANT_P95}ms, mem=${P4_MEM}MB). Do NOT retune after observing results.
New calibration under these limits may select K≠5; keep selection rule and limits.
Independent validation of selected K uses k3_validation (disjoint seeds).
k5_validation remains additional sensitivity evidence only.
EOF
  mark_done offline_selection
}

stage_analyze() {
  require_freeze
  # Allow re-run after staging/validator fixes: clear prior DONE only if present.
  # Never mark DONE unless validate exits 0.
  mkdir -p "$OUT_ROOT/13_analysis"
  local cid
  cid="$(basename "$OUT_ROOT")"
  local stage="$REPO/artifacts/raw/$cid"
  mkdir -p "$stage"
  # Symlink layout compatible with validate_campaign / analysis tools.
  # Do not move or rewrite raw CSVs under OUT_ROOT.
  [[ -d "$OUT_ROOT/03_coldstart" ]] && ln -sfn "$OUT_ROOT/03_coldstart" "$stage/instrumented_cold_start"
  [[ -d "$OUT_ROOT/04_lifecycle" ]] && ln -sfn "$OUT_ROOT/04_lifecycle" "$stage/lifecycle_decomposition"
  [[ -d "$OUT_ROOT/05_tls" ]] && ln -sfn "$OUT_ROOT/05_tls" "$stage/mtls_correctness_overhead"
  [[ -d "$OUT_ROOT/02_functional/cert_matrix" ]] && ln -sfn "$OUT_ROOT/02_functional/cert_matrix" "$stage/cert_matrix"
  [[ -d "$OUT_ROOT/06_persistence" ]] && ln -sfn "$OUT_ROOT/06_persistence" "$stage/persistence_decomposition"
  [[ -d "$OUT_ROOT/07_factorial" ]] && ln -sfn "$OUT_ROOT/07_factorial" "$stage/factorial_provisioning"
  [[ -d "$OUT_ROOT/08_concurrency/processes" ]] && ln -sfn "$OUT_ROOT/08_concurrency/processes" "$stage/concurrent_slicing_v2"
  [[ -d "$OUT_ROOT/08_concurrency/containers" ]] && ln -sfn "$OUT_ROOT/08_concurrency/containers" "$stage/container_concurrency"
  "$PY" - <<PY
import json
from pathlib import Path
from datetime import datetime, timezone
cid = Path("$OUT_ROOT").name
out_root = Path("$OUT_ROOT")
stage = Path("artifacts/raw") / cid
man = {
  "campaign_id": cid,
  "campaign_type": "full_evaluation",
  "sas_agent_sha": (out_root / "meta/agent_sha.txt").read_text().strip(),
  "image_id": (out_root / "meta/image_id.txt").read_text().strip(),
  "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
  "experiments": {
    "coldstart": "03_coldstart",
    "lifecycle": "04_lifecycle",
    "tls_mtls": "05_tls",
    "cert_matrix": "02_functional/cert_matrix",
    "persistence": "06_persistence",
    "factorial": "07_factorial",
    "process_concurrency": "08_concurrency/processes",
    "container_concurrency": "08_concurrency/containers",
    "sustained": "09_sustained",
    "colocated": "10_colocated",
    "recovery": "11_recovery",
    "k5_validation": "12_provisioning/k5_validation",
    "k3_validation": "12_provisioning/k3_validation",
    "offline_selection": "12_provisioning/offline_selection",
  },
  "phases": [
    "functional", "coldstart", "lifecycle", "tls", "persistence", "factorial",
    "concurrency", "sustained", "colocated", "recovery", "provisioning", "analysis"
  ],
  "evidence_root": str(out_root),
  "staging_note": "artifacts/raw/<id> holds symlinks into OUT_ROOT; raw CSVs are not moved",
}
(stage / "campaign_manifest.json").write_text(json.dumps(man, indent=2) + "\n")
print("wrote", stage / "campaign_manifest.json")
PY
  local vec=0
  set +e
  run_cmd validate "$PY" analysis/validate_campaign.py --campaign-id "$cid" \
    >"$OUT_ROOT/13_analysis/validate.log" 2>&1
  vec=$?
  set -e
  if [[ "$vec" -ne 0 ]]; then
    unmark_done analyze
    log "VALIDATE FAILED — STAGE analyze NOT marked DONE (see 13_analysis/validate.log)"
    record_attempt analyze "validate_failed" "exit=$vec"
    return "$vec"
  fi
  # Confirm report status is success (never mark DONE on artificial/partial PASS).
  "$PY" -c "
import json, sys
from pathlib import Path
rep = json.loads((Path('artifacts/raw') / '$cid' / 'validation_report.json').read_text())
sys.exit(0 if rep.get('status') == 'success' else 1)
" || { unmark_done analyze; die "validation_report status != success"; }
  run_cmd tables "$PY" analysis/generate_tables.py --campaign-id "$cid" \
    >"$OUT_ROOT/13_analysis/tables.log" 2>&1 || true
  run_cmd uncertainty "$PY" analysis/uncertainty_report.py --campaign-id "$cid" \
    --out "$OUT_ROOT/13_analysis/uncertainty_catalog.json" || true
  require_files analyze \
    "$REPO/artifacts/raw/$cid/validation_report.json" \
    "$OUT_ROOT/13_analysis/validate.log"
  mark_done analyze
}

stage_validate() { stage_analyze; }

stage_checksums() {
  refuse_canonical
  (
    cd "$OUT_ROOT"
    find . -type f \
      ! -name 'checksums.sha256' \
      ! -name '*.key' \
      ! -name '*.pem' \
      ! -path '*/harness_certs/*' \
      -print0 | sort -z | xargs -0 sha256sum >checksums.sha256
  )
  mark_done checksums
}

stage_status() {
  echo "OUT_ROOT=$OUT_ROOT"
  echo "HOST_COMMIT=$(git rev-parse HEAD)"
  echo "IMAGE_TAG=$IMAGE_TAG"
  echo "IMAGE_ID=$(image_id 2>/dev/null || echo missing)"
  echo "SMOKE=$SMOKE DRY_RUN=$DRY_RUN"
  local s
  for s in preflight freeze inventory unittest security_tests isolation cert_matrix \
           boundary robustness winnforum coldstart lifecycle tls persistence factorial \
           process_concurrency container_concurrency container_per_agent \
           sustained colocated recovery k5_validation k3_validation offline_selection \
           analyze checksums; do
    if stage_done "$s"; then echo "DONE $s"; else echo "TODO $s"; fi
  done
}

stage_dry_run() {
  DRY_RUN=1
  log "dry-run mode: printing planned commands for key stages"
  stage_freeze
  stage_isolation
  stage_coldstart
  stage_status
}

stage_all() {
  stage_preflight
  stage_freeze
  stage_inventory
  stage_unittest
  stage_security_tests
  stage_isolation
  stage_cert_matrix
  stage_boundary
  stage_robustness
  stage_winnforum
  stage_coldstart
  stage_lifecycle
  stage_tls
  stage_persistence
  stage_factorial
  stage_process_concurrency
  stage_container_concurrency
  stage_container_per_agent
  stage_sustained
  stage_colocated
  stage_recovery
  stage_k5_validation
  # k3_validation is opt-in after P4 selects K=3; not part of default `all`
  # (prepare with: PREPARE_ONLY=1 ... k3_validation). Run when selected K known.
  stage_offline_selection
  stage_analyze
  stage_checksums
  stage_status
}

case "$STAGE" in
  ""|-h|--help|help) usage ;;
  preflight) stage_preflight ;;
  freeze) stage_freeze ;;
  inventory) stage_inventory ;;
  unittest) stage_unittest ;;
  security_tests) stage_security_tests ;;
  isolation) stage_isolation ;;
  cert_matrix) stage_cert_matrix ;;
  boundary) stage_boundary ;;
  robustness) stage_robustness ;;
  winnforum) stage_winnforum ;;
  coldstart) stage_coldstart ;;
  lifecycle) stage_lifecycle ;;
  tls) stage_tls ;;
  persistence) stage_persistence ;;
  factorial) stage_factorial ;;
  process_concurrency) stage_process_concurrency ;;
  container_concurrency) stage_container_concurrency ;;
  container_per_agent) stage_container_per_agent ;;
  sustained) stage_sustained ;;
  colocated) stage_colocated ;;
  recovery) stage_recovery ;;
  k5_validation) stage_k5_validation ;;
  k3_validation) stage_k3_validation ;;
  offline_selection) stage_offline_selection ;;
  analyze|validate) stage_analyze ;;
  checksums) stage_checksums ;;
  status) stage_status ;;
  dry-run) stage_dry_run ;;
  all) stage_all ;;
  *) usage; die "unknown stage: $STAGE" ;;
esac
