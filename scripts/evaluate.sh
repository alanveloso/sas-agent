#!/usr/bin/env bash
# =============================================================================
# SAS Agent — single entrypoint for laboratory evaluation
# =============================================================================
# Run campaigns, validate the published experiments collection, and regenerate
# tables/figures. Internal helpers live under scripts/lib/.
#
# Usage:
#   ./scripts/evaluate.sh help
#   ./scripts/evaluate.sh host-preflight [--wait] [--image sas-agent:article]
#   OUT_ROOT=evidence/runs/<stamp> CERTS_DIR=... ./scripts/evaluate.sh preflight
#   ./scripts/evaluate.sh freeze
#   ./scripts/evaluate.sh <campaign-stage>   # see ./scripts/evaluate.sh stages
#   ./scripts/evaluate.sh all
#   ./scripts/evaluate.sh validate
#   ./scripts/evaluate.sh tables
#   ./scripts/evaluate.sh figures
#   ./scripts/evaluate.sh uncertainty
#   ./scripts/evaluate.sh selection
#   ./scripts/evaluate.sh publish-collection
#   ./scripts/evaluate.sh rebuild-manifest
#   ./scripts/evaluate.sh status
#   ./scripts/evaluate.sh winnforum
#   ./scripts/evaluate.sh checksums
#
# Env: OUT_ROOT IMAGE_TAG CERTS_DIR HARNESS_CERTS_DIR PY SEED SMOKE DRY_RUN
# =============================================================================
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PY="${PY:-$REPO/.venv/bin/python}"
IMAGE_TAG="${IMAGE_TAG:-sas-agent:article}"
CAMPAIGN="$REPO/scripts/lib/run_campaign.sh"
HOST_PREFLIGHT="$REPO/scripts/lib/preflight_host.sh"
EXPERIMENTS="$REPO/evidence/experiments"

die() { echo "ERROR: $*" >&2; exit 1; }
log() { echo "[$(date -u +%H:%M:%SZ)] $*"; }

usage() {
  cat <<EOF
SAS Agent evaluation entrypoint

Campaign stages (resume via STAGE_*_DONE under OUT_ROOT):
  preflight freeze inventory
  unittest security_tests
  isolation cert_matrix boundary robustness winnforum
  coldstart lifecycle tls persistence factorial
  process_concurrency container_concurrency container_per_agent
  sustained colocated recovery
  k5_validation k3_validation offline_selection
  analyze checksums status dry-run all

Collection / analysis:
  host-preflight     Host readiness checks (thermal, load, updates, git)
  validate           Validate published evidence/experiments
  tables             Regenerate analysis tables
  figures            Regenerate analysis figures
  uncertainty        Uncertainty catalog for the published collection
  selection          Illustrative provisioning selection (calib + K validation)
  publish-collection Rebuild evidence/experiments from campaign trees
  rebuild-manifest   Refresh manifest.json and checksums.sha256
  stages             List campaign stages

WInnForum harness:
  winnforum          Run 65-case harness against IMAGE_TAG (needs sas-harness)

Examples:
  ./scripts/evaluate.sh host-preflight --image \$IMAGE_TAG
  OUT_ROOT=evidence/runs/\$(date -u +%Y%m%dT%H%M%SZ) \\
    CERTS_DIR=/path/to/harness/certs ./scripts/evaluate.sh all
  ./scripts/evaluate.sh validate && ./scripts/evaluate.sh tables && ./scripts/evaluate.sh figures
EOF
}

cmd_validate() {
  [[ -d "$EXPERIMENTS" ]] || die "missing $EXPERIMENTS — run publish-collection first"
  "$PY" -c "from analysis.stage_experiments import stage_experiments; stage_experiments()"
  "$PY" analysis/validate_campaign.py --campaign-id experiments
}

cmd_tables() {
  "$PY" analysis/generate_tables.py --campaign-id experiments
}

cmd_figures() {
  "$PY" analysis/generate_figures.py --campaign-id experiments
}

cmd_uncertainty() {
  mkdir -p "$EXPERIMENTS/analysis"
  "$PY" analysis/uncertainty_report.py --campaign-id experiments \
    --out "$EXPERIMENTS/analysis/uncertainty_catalog.json"
}

cmd_selection() {
  local calib="$EXPERIMENTS/provisioning/calibration/container_concurrency_runs.csv"
  local val="$EXPERIMENTS/provisioning/validation/k3_independent/container_concurrency_runs.csv"
  [[ -f "$calib" ]] || die "missing calibration CSV: $calib"
  [[ -f "$val" ]] || die "missing validation CSV: $val"
  mkdir -p "$EXPERIMENTS/analysis"
  local out="$EXPERIMENTS/analysis/selection_illustrative_limits.json"
  # Deterministic rebuild of a derived analysis artifact (not raw measurements).
  rm -f "$out"
  "$PY" analysis/provisioning_selection.py \
    --calibration "$calib" \
    --validation "$val" \
    --limit-startup-sec "${P4_STARTUP:-60}" \
    --limit-provisioning-sec "${P4_PROV:-1.5}" \
    --limit-grant-batch-p95-ms "${P4_GRANT_P95:-1000}" \
    --limit-memory-mb "${P4_MEM:-2048}" \
    --out "$out"
}

cmd_publish() {
  "$PY" scripts/lib/publish_experiments.py
  "$PY" scripts/lib/rebuild_experiments_manifest.py
  log "published collection ready under evidence/experiments/"
}

cmd_rebuild_manifest() {
  "$PY" scripts/lib/rebuild_experiments_manifest.py
}

cmd_winnforum() {
  local out="${EVIDENCE_DIR:-$REPO/evidence/runs/winnforum-$(date -u +%Y%m%dT%H%M%SZ)}"
  mkdir -p "$out"
  log "WInnForum evidence → $out"
  SAS_IMAGE="${SAS_IMAGE:-$IMAGE_TAG}" SKIP_UUT_BUILD="${SKIP_UUT_BUILD:-1}" \
    EVIDENCE_DIR="$out" bash "$REPO/run_cbrs_tests.sh"
}

CMD="${1:-help}"
shift || true

case "$CMD" in
  help|-h|--help) usage ;;
  stages)
    usage
    ;;
  host-preflight)
    [[ -x "$HOST_PREFLIGHT" ]] || die "missing $HOST_PREFLIGHT"
    exec "$HOST_PREFLIGHT" --image "$IMAGE_TAG" "$@"
    ;;
  validate) cmd_validate ;;
  tables) cmd_tables ;;
  figures) cmd_figures ;;
  uncertainty) cmd_uncertainty ;;
  selection) cmd_selection ;;
  publish-collection|publish) cmd_publish ;;
  rebuild-manifest|manifest) cmd_rebuild_manifest ;;
  winnforum) cmd_winnforum ;;
  # Campaign runner stages (including in-campaign winnforum parse/harness stage)
  preflight|freeze|inventory|unittest|security_tests|isolation|cert_matrix|boundary|robustness|coldstart|lifecycle|tls|persistence|factorial|process_concurrency|container_concurrency|container_per_agent|sustained|colocated|recovery|k5_validation|k3_validation|offline_selection|analyze|checksums|status|dry-run|all|winnforum-stage)
    stage="$CMD"
    [[ "$CMD" == "winnforum-stage" ]] && stage="winnforum"
    exec bash "$CAMPAIGN" "$stage" "$@"
    ;;
  *)
    usage
    die "unknown command: $CMD"
    ;;
esac
