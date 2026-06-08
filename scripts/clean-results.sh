#!/usr/bin/env bash
# Reclaim and remove results/ (fixes root-owned files from OpenClaw Docker workspaces).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RESULTS="${ROOT}/results"

if [[ ! -d "${RESULTS}" ]]; then
  echo "Nothing to clean: ${RESULTS} does not exist."
  exit 0
fi

if find "${RESULTS}" -user root -print -quit 2>/dev/null | grep -q .; then
  echo "Reclaiming root-owned files under ${RESULTS} (sudo)..."
  sudo chown -R "$(id -u):$(id -g)" "${RESULTS}"
fi

rm -rf "${RESULTS:?}"/*
echo "Cleaned ${RESULTS}"
