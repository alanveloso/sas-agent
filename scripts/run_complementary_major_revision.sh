#!/usr/bin/env bash
# Coordinated complementary campaign for TNSM-2026-12135 Major Revision.
# Does NOT overwrite evidence/2026-12135/. Does NOT push/commit.
# Stages are independently resumable via STAGE_DONE markers under OUT_ROOT.
#
# Failure policy (colocated / concurrency runners):
#   infrastructure|harness → may invalidate a run (recorded in CSVs).
#   measured_sas_or_transport / app failures under load → retained; never dropped.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

HOST_COMMIT="${HOST_COMMIT:-$(git rev-parse HEAD)}"
IMAGE_TAG="${IMAGE_TAG:-sas-agent:article}"
IMAGE_ID="$(docker image inspect --format '{{.Id}}' "$IMAGE_TAG")"
CERTS_DIR="${CERTS_DIR:-$REPO/evidence/tnsm-2026-12135-controlled-20261111T205011Z/meta/harness_certs}"
PY="${PY:-$REPO/.venv/bin/python}"
STAMP="${STAMP:-$(date -u +%Y%m%dT%H%M%SZ)}"
OUT_ROOT="${OUT_ROOT:-$REPO/evidence/tnsm-2026-12135-complementary-${STAMP}}"
STAGE="${1:-all}"

# Frozen seeds / limits (declared before collection; do not edit mid-run).
COLOCATED_SEED=42
COLOCATED_PAIRS=5
COLOCATED_CLIENTS=5
COLOCATED_DEVICES_PER_CLIENT=300
COLOCATED_SEC=600
K5_SEED=84242   # disjoint from calib(42) and historical val(4242)
K5_REPEATS=5
RECOVERY_CYCLES=30

# P0 = original illustrative defaults (provisioning_selection.ILLUSTRATIVE_LIMITS).
LIMIT_STARTUP=60
LIMIT_PROV=60
LIMIT_GRANT_P95=1000
LIMIT_MEM=2048

# P4 = illustrative sensitivity with prior knowledge of calibration frontiers.
# Anticipated calibration choice: K=5. NOT a prospective independent SLA/SLO.
# Frozen before independent K=5 validation; do not edit after seeing validation.
P4_STARTUP=60
P4_PROV=1.5
P4_GRANT_P95=1000
P4_MEM=2048

usage() {
  cat <<EOF
Usage: $0 <stage|all|status>
  stages: freeze | k5_validation | colocated | recovery | offline_selection | checksums | status
Env: OUT_ROOT STAMP IMAGE_TAG CERTS_DIR HOST_COMMIT PY
EOF
}

mark_done() {
  local s="$1"
  mkdir -p "$OUT_ROOT/meta"
  date -u +%Y-%m-%dT%H:%M:%SZ >"$OUT_ROOT/meta/STAGE_${s}_DONE"
  echo "STAGE $s DONE -> $OUT_ROOT"
}

stage_done() {
  [[ -f "$OUT_ROOT/meta/STAGE_${1}_DONE" ]]
}

require_certs() {
  [[ -f "$CERTS_DIR/ca.cert" && -f "$CERTS_DIR/domain_proxy.cert" ]] || {
    echo "FAIL certs at $CERTS_DIR" >&2
    exit 1
  }
  # Private keys must exist for mTLS clients but must never be copied into OUT_ROOT.
  [[ -f "$CERTS_DIR/domain_proxy.key" ]] || {
    echo "FAIL domain_proxy.key missing (runtime only; not archived)" >&2
    exit 1
  }
}

refuse_canonical_overwrite() {
  local canon="$REPO/evidence/2026-12135"
  if [[ "$(realpath -m "$OUT_ROOT")" == "$(realpath -m "$canon")" ]]; then
    echo "FAIL refusing OUT_ROOT=$OUT_ROOT (canonical corpus)" >&2
    exit 1
  fi
}

# Best-effort cleanup restricted to this campaign's known name prefixes only.
safe_cleanup_campaign_containers() {
  local id
  for id in $(docker ps -aq --filter "name=sas-http-sqlite-recovery" 2>/dev/null || true); do
    docker rm -f "$id" >/dev/null 2>&1 || true
  done
  # Colocated / concurrency benches create/remove their own names; do not sweep unrelated containers.
}

write_effective_params() {
  local stage="$1"
  mkdir -p "$OUT_ROOT/meta"
  cat >"$OUT_ROOT/meta/effective_params_${stage}.json" <<EOF
{
  "stage": "$stage",
  "recorded_at_utc": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "host_commit": "$HOST_COMMIT",
  "image_tag": "$IMAGE_TAG",
  "image_id": "$IMAGE_ID",
  "out_root": "$OUT_ROOT",
  "certs_dir": "$CERTS_DIR",
  "certs_archived": false
}
EOF
}

stage_freeze() {
  refuse_canonical_overwrite
  mkdir -p "$OUT_ROOT/meta"
  require_certs
  local digest
  digest="$(docker image inspect --format '{{json .RepoDigests}}' "$IMAGE_TAG")"
  "$PY" - <<PY
import hashlib, json, subprocess
from pathlib import Path
repo = Path("$REPO")
out = Path("$OUT_ROOT/meta")
files = [
    "benchmarks/load/colocated_sustained_bench.py",
    "benchmarks/load/container_concurrency_bench.py",
    "benchmarks/load/http_mtls_sqlite_recovery_campaign.py",
    "benchmarks/load/common.py",
    "analysis/provisioning_selection.py",
    "analysis/uncertainty_report.py",
    "analysis/statistics.py",
    "tests/test_http_mtls_sqlite_recovery.py",
    "scripts/run_complementary_major_revision.sh",
]
hashes = {}
for rel in files:
    p = repo / rel
    hashes[rel] = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
proto = {
  "campaign_id": "tnsm-2026-12135-complementary",
  "out_root": str(Path("$OUT_ROOT")),
  "frozen_at_utc": subprocess.check_output(["date","-u","+%Y-%m-%dT%H:%M:%SZ"], text=True).strip(),
  "host_commit": "$HOST_COMMIT",
  "branch": subprocess.check_output(["git","-C",str(repo),"branch","--show-current"], text=True).strip(),
  "image_tag": "$IMAGE_TAG",
  "image_id": "$IMAGE_ID",
  "image_repo_digests_json": json.loads("""$digest"""),
  "agent_core_match_note": "verify host Agent file sha256 vs image at freeze; tag is not identity",
  "certs_dir": "$CERTS_DIR",
  "certs_policy": "mounted read-only at runtime; never copied into OUT_ROOT; *.key excluded from checksums",
  "sqlite_defaults": {"journal_mode": "delete", "synchronous": 2, "persistence": "sqlite_file_full"},
  "band_profile": "cbrs_winnforum",
  "script_sha256": hashes,
  "failure_policy": {
    "retain_under_load": ["measured_sas", "measured_sas_or_transport", "app failures with exit_code=1"],
    "may_invalidate": ["infrastructure", "harness", "thermal_gate_fail"],
    "no_selective_deletion": True
  },
  "stages": {
    "colocated": {
      "pairs": $COLOCATED_PAIRS,
      "n_clients": $COLOCATED_CLIENTS,
      "devices_per_client": $COLOCATED_DEVICES_PER_CLIENT,
      "total_devices": $((COLOCATED_CLIENTS * COLOCATED_DEVICES_PER_CLIENT)),
      "sustained_sec": $COLOCATED_SEC,
      "seed": $COLOCATED_SEED,
      "order": "per-pair shuffle of [A,B] with Random(seed)",
      "thermal_gate": True,
      "pilot_exclusion": "evidence/2026-12135-r18-colocated-* must not be merged"
    },
    "k5_validation": {
      "k_values": [5],
      "devices": 1500,
      "population_mode": "fixed_total",
      "persistence": "sqlite_file_full",
      "repeats": $K5_REPEATS,
      "seed": $K5_SEED,
      "seed_disjoint_from": {"calibration": 42, "historical_validation": 4242},
      "preserve_existing_validation_k": [1, 15],
      "evaluation_package": "P4_k5_illustrative_sensitivity",
      "limits_illustrative": {
        "startup_sec": $P4_STARTUP,
        "provisioning_sec": $P4_PROV,
        "grant_batch_p95_ms": $P4_GRANT_P95,
        "memory_mb": $P4_MEM,
        "external_sla_foundation": None,
        "note": "Same P4 limits apply to K=5 and historical K=15 under offline_selection; do not retune after validation"
      },
      "rule": "smallest calibrated K with every repeat meeting every limit; validation never adjusts limits"
    },
    "recovery": {
      "cycles": $RECOVERY_CYCLES,
      "endpoint_proof": "HTTPS heartbeat responseCode=0 after restart",
      "not_process_heartbeat": True
    },
    "offline_selection_packages": {
      "P0_original_illustrative": {
        "startup_sec": 60, "provisioning_sec": 60,
        "grant_batch_p95_ms": 1000, "memory_mb": 2048,
        "kind": "original_illustrative_defaults"
      },
      "P1_tight_grant_p95": {
        "startup_sec": 60, "provisioning_sec": 60,
        "grant_batch_p95_ms": 230, "memory_mb": 2048,
        "kind": "sensitivity"
      },
      "P2_tight_memory": {
        "startup_sec": 60, "provisioning_sec": 60,
        "grant_batch_p95_ms": 1000, "memory_mb": 120,
        "kind": "sensitivity"
      },
      "P3_infeasible_stress": {
        "startup_sec": 0.5, "provisioning_sec": 1.0,
        "grant_batch_p95_ms": 100, "memory_mb": 50,
        "kind": "sensitivity_infeasible"
      },
      "P4_k5_illustrative_sensitivity": {
        "startup_sec": $P4_STARTUP,
        "provisioning_sec": $P4_PROV,
        "grant_batch_p95_ms": $P4_GRANT_P95,
        "memory_mb": $P4_MEM,
        "persistence": "sqlite_file_full",
        "kind": "illustrative_sensitivity_with_prior_calibration_knowledge",
        "anticipated_calibration_choice": 5,
        "not_prospective_independent_slo": True,
        "frozen_before_k5_validation": True
      }
    },
    "memory_semantics": {
      "interval_sec": 0.5,
      "ram_peak_mb_sum": "sampled max of sum(docker stats MemUsage); NOT absolute peak; NOT PSS",
      "ram_after_load_mb_sum": "single post-load sample",
      "process_rss_pss": "not collected in container benches; do not invent"
    }
  },
  "canonical_corpus": "evidence/2026-12135 (read-only; never overwrite)",
  "hardware_expected": "ThinkPad P14s Gen5 AMD Ryzen 7 PRO 8840HS 16 threads ~28GiB Ubuntu 24.04"
}
(out / "protocol_frozen.json").write_text(json.dumps(proto, indent=2) + "\n")
print(out / "protocol_frozen.json")
PY
  write_effective_params freeze
  mark_done freeze
}

stage_k5_validation() {
  refuse_canonical_overwrite
  stage_done freeze || { echo "run freeze first"; exit 1; }
  if stage_done k5_validation; then echo "k5_validation already done"; return 0; fi
  require_certs
  local dest="$OUT_ROOT/k5_validation"
  mkdir -p "$dest"
  write_effective_params k5_validation
  # Collect measurements only; P4 limits are applied offline and must not be edited after this stage.
  cat >"$dest/EVALUATION_PACKAGE.txt" <<EOF
evaluation_package=P4_k5_illustrative_sensitivity
limits_frozen_before_this_stage=true
startup_sec=$P4_STARTUP
provisioning_sec=$P4_PROV
grant_batch_p95_ms=$P4_GRANT_P95
memory_mb=$P4_MEM
persistence=sqlite_file_full
do_not_retune_limits_after_observing_results=true
EOF
  "$PY" "$REPO/benchmarks/load/container_concurrency_bench.py" \
    --k-values 5 \
    --devices 1500 \
    --repeats "$K5_REPEATS" \
    --seed "$K5_SEED" \
    --scenarios A \
    --image "$IMAGE_TAG" \
    --certs-dir "$CERTS_DIR" \
    --out-dir "$dest"
  mark_done k5_validation
}

stage_colocated() {
  refuse_canonical_overwrite
  stage_done freeze || { echo "run freeze first"; exit 1; }
  if stage_done colocated; then echo "colocated already done"; return 0; fi
  require_certs
  local dest="$OUT_ROOT/colocated"
  mkdir -p "$dest"
  write_effective_params colocated
  "$PY" "$REPO/benchmarks/load/colocated_sustained_bench.py" \
    --mode paired \
    --pairs "$COLOCATED_PAIRS" \
    --n-clients "$COLOCATED_CLIENTS" \
    --devices-per-client "$COLOCATED_DEVICES_PER_CLIENT" \
    --sustained-sec "$COLOCATED_SEC" \
    --seed "$COLOCATED_SEED" \
    --heartbeat-schedule-mode respect_transmit_expire \
    --image "$IMAGE_TAG" \
    --certs-dir "$CERTS_DIR" \
    --out-dir "$dest"
  mark_done colocated
}

stage_recovery() {
  refuse_canonical_overwrite
  stage_done freeze || { echo "run freeze first"; exit 1; }
  if stage_done recovery; then echo "recovery already done"; return 0; fi
  require_certs
  local dest="$OUT_ROOT/sqlite_http_recovery"
  mkdir -p "$dest"
  write_effective_params recovery
  safe_cleanup_campaign_containers
  "$PY" "$REPO/benchmarks/load/http_mtls_sqlite_recovery_campaign.py" \
    --cycles "$RECOVERY_CYCLES" \
    --out-dir "$dest" \
    --image "$IMAGE_TAG" \
    --image-id "$IMAGE_ID" \
    --certs-dir "$CERTS_DIR" \
    --host-commit "$HOST_COMMIT"
  mark_done recovery
}

stage_offline_selection() {
  refuse_canonical_overwrite
  stage_done freeze || { echo "run freeze first"; exit 1; }
  if stage_done offline_selection; then echo "offline_selection already done"; return 0; fi
  local dest="$OUT_ROOT/offline_selection"
  mkdir -p "$dest"
  write_effective_params offline_selection
  local calib="$REPO/evidence/2026-12135/concurrency/containers/calibration/container_concurrency_runs.csv"
  local val_hist="$REPO/evidence/2026-12135/concurrency/containers/validation/container_concurrency_runs.csv"
  local val_k5=""
  if [[ -f "$OUT_ROOT/k5_validation/container_concurrency_runs.csv" ]]; then
    val_k5="$OUT_ROOT/k5_validation/container_concurrency_runs.csv"
  fi

  local val_args=()
  [[ -f "$val_hist" ]] && val_args+=(--validation "$val_hist")
  [[ -n "$val_k5" && -f "$val_k5" ]] && val_args+=(--validation "$val_k5")

  "$PY" "$REPO/analysis/provisioning_selection.py" \
    --calibration "$calib" \
    ${val_args[@]+"${val_args[@]}"} \
    --limit-startup-sec "$LIMIT_STARTUP" \
    --limit-provisioning-sec "$LIMIT_PROV" \
    --limit-grant-batch-p95-ms "$LIMIT_GRANT_P95" \
    --limit-memory-mb "$LIMIT_MEM" \
    --out "$dest/P0_original_illustrative.json"

  "$PY" "$REPO/analysis/provisioning_selection.py" \
    --calibration "$calib" \
    ${val_args[@]+"${val_args[@]}"} \
    --limit-startup-sec 60 --limit-provisioning-sec 60 \
    --limit-grant-batch-p95-ms 230 --limit-memory-mb 2048 \
    --out "$dest/P1_tight_grant_p95.json"

  "$PY" "$REPO/analysis/provisioning_selection.py" \
    --calibration "$calib" \
    ${val_args[@]+"${val_args[@]}"} \
    --limit-startup-sec 60 --limit-provisioning-sec 60 \
    --limit-grant-batch-p95-ms 1000 --limit-memory-mb 120 \
    --out "$dest/P2_tight_memory.json"

  "$PY" "$REPO/analysis/provisioning_selection.py" \
    --calibration "$calib" \
    ${val_args[@]+"${val_args[@]}"} \
    --limit-startup-sec 0.5 --limit-provisioning-sec 1.0 \
    --limit-grant-batch-p95-ms 100 --limit-memory-mb 50 \
    --out "$dest/P3_infeasible_stress.json"

  # P4: same limits for all K; anticipated calibration choice K=5.
  "$PY" "$REPO/analysis/provisioning_selection.py" \
    --calibration "$calib" \
    ${val_args[@]+"${val_args[@]}"} \
    --limit-startup-sec "$P4_STARTUP" \
    --limit-provisioning-sec "$P4_PROV" \
    --limit-grant-batch-p95-ms "$P4_GRANT_P95" \
    --limit-memory-mb "$P4_MEM" \
    --out "$dest/P4_k5_illustrative_sensitivity.json"

  cat >"$dest/README_LIMITS.txt" <<EOF
All limit packages are ILLUSTRATIVE (limits_are_illustrative=true).
No external SLA foundation. Packages were frozen in protocol_frozen.json before complementary collection.

P0 = original article example defaults.
P1–P3 = predeclared sensitivity; not tuned after seeing complementary outcomes.
P4 = illustrative sensitivity constructed with prior knowledge of calibration frontiers.
     Anticipated calibration choice: K=5.
     NOT a prospective independent SLO.
     Frozen before independent K=5 validation; do not modify after observing validation.
     Under P4, K=1 violates provisioning; K=5 and K=15 are compared under the SAME limits.

Historical validation K=1 and K=15 preserved under evidence/2026-12135/.../validation/.
EOF
  mark_done offline_selection
}

stage_checksums() {
  refuse_canonical_overwrite
  stage_done freeze || { echo "run freeze first"; exit 1; }
  (
    cd "$OUT_ROOT"
    # Exclude private keys and accidental cert material if ever dropped into the tree.
    find . -type f \
      ! -name 'checksums.sha256' \
      ! -name '*.key' \
      ! -name '*.pem' \
      ! -path '*/harness_certs/*' \
      -print0 | sort -z | xargs -0 sha256sum >checksums.sha256
  )
  write_effective_params checksums
  mark_done checksums
}

stage_status() {
  echo "OUT_ROOT=$OUT_ROOT"
  echo "HOST_COMMIT=$HOST_COMMIT"
  echo "IMAGE_TAG=$IMAGE_TAG"
  echo "IMAGE_ID=$IMAGE_ID"
  echo "P4_limits=startup:$P4_STARTUP prov:$P4_PROV grant_p95:$P4_GRANT_P95 mem:$P4_MEM"
  for s in freeze k5_validation colocated recovery offline_selection checksums; do
    if stage_done "$s"; then echo "DONE $s"; else echo "TODO $s"; fi
  done
}

case "$STAGE" in
  -h|--help|help) usage ;;
  freeze) stage_freeze ;;
  k5_validation) stage_k5_validation ;;
  colocated) stage_colocated ;;
  recovery) stage_recovery ;;
  offline_selection) stage_offline_selection ;;
  checksums) stage_checksums ;;
  status) stage_status ;;
  all)
    stage_freeze
    stage_k5_validation
    stage_colocated
    stage_recovery
    stage_offline_selection
    stage_checksums
    ;;
  *) usage; exit 2 ;;
esac
