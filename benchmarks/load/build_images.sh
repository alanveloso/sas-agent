#!/usr/bin/env bash
# Build comparative Docker images from the monorepo root (parent of sas-agent/).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SAS_AGENT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
MONOREPO_ROOT="$(cd "${SAS_AGENT_DIR}/.." && pwd)"

cd "${MONOREPO_ROOT}"

echo "Building sas-agent:monolithic from ${MONOREPO_ROOT} ..."
docker build -f sas-agent/Dockerfile -t sas-agent:monolithic .

echo "Building sas-agent:core from ${MONOREPO_ROOT} ..."
docker build -f sas-agent/Dockerfile.core -t sas-agent:core .

echo "Done."
docker images 'sas-agent:*' --format 'table {{.Repository}}\t{{.Tag}}\t{{.ID}}\t{{.Size}}'
