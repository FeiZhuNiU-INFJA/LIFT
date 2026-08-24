#!/usr/bin/env bash
# Build all LIFT runtime images.
#
# SSoT for build commands: docs/build-images.md
#
# Usage:
#   bash scripts/build-all-images.sh                     # build everything
#   bash scripts/build-all-images.sh --only NAME[,NAME]  # build only listed targets
#   bash scripts/build-all-images.sh --skip NAME[,NAME]  # build everything except listed
#   bash scripts/build-all-images.sh --list              # list all target names and exit
#   bash scripts/build-all-images.sh -h | --help
#
# --only and --skip are mutually exclusive.
#
# Env passthrough: all env vars are inherited by each build-image.sh child.
# Common ones:
#   APT_MIRROR / PIP_INDEX_URL          intranet mirrors (auto-detected on byted intranet)
#   LIFT_INTRANET_AUTODETECT=0          force public mirrors
#   DOCKER_BUILD_NETWORK=host           bridge network too slow for large tarballs
#     (openhuman targets always force host network internally)
#
# Failures do not abort the batch; a summary is printed at the end.

set -uo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

# ----------------------------------------------------------------------------
# Target registry — mirrors docs/build-images.md table.
# Each entry: "name|command"
# Names are stable; docs and scripts refer to them by these keys.
# ----------------------------------------------------------------------------
readonly TARGETS=(
  "openclaw-base|bash agent-runtimes/openclaw/build-image.sh"
  "openclaw-with-evolve|bash agent-runtimes/openclaw/build-image.sh --with-evolve"
  "openclaw-with-openspace|bash agent-runtimes/openclaw/build-image.sh --with-openspace"
  "openclaw-with-agentmemory|bash agent-runtimes/openclaw/build-image.sh --with-agentmemory"
  "hermes|bash agent-runtimes/hermes/build-image.sh"
  "hermes-with-openspace|bash agent-runtimes/hermes/build-image.sh --with-openspace"
  "hermes-with-agentmemory|bash agent-runtimes/hermes/build-image.sh --with-agentmemory"
  "openhuman|DOCKER_BUILD_NETWORK=host bash agent-runtimes/openhuman/build-image.sh"
  "openhuman-with-agentmemory|DOCKER_BUILD_NETWORK=host bash agent-runtimes/openhuman/build-image.sh --with-agentmemory"
  "genericagent|bash agent-runtimes/genericagent/build-image.sh"
  "evoscientist|bash agent-runtimes/evoscientist/build-image.sh"
  "prime-agent|bash agent-runtimes/prime_agent/build-image.sh"
)

usage() {
  cat <<EOF
Build all LIFT runtime images (SSoT: docs/build-images.md).

Usage:
  $(basename "$0")                     Build everything
  $(basename "$0") --only NAME[,NAME]  Build only listed targets
  $(basename "$0") --skip NAME[,NAME]  Build everything except listed
  $(basename "$0") --list              List all target names and exit
  $(basename "$0") -h | --help

Targets:
$(for entry in "${TARGETS[@]}"; do printf "  %s\n" "${entry%%|*}"; done)
EOF
}

MODE=""      # "only" or "skip"
FILTER=""    # comma-separated names

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --list)
      for entry in "${TARGETS[@]}"; do printf "%s\n" "${entry%%|*}"; done
      exit 0
      ;;
    --only)
      [[ -n "${MODE}" ]] && { echo "ERROR: --only and --skip are mutually exclusive" >&2; exit 2; }
      MODE="only"; FILTER="${2:-}"; shift 2
      [[ -z "${FILTER}" ]] && { echo "ERROR: --only requires a comma-separated target list" >&2; exit 2; }
      ;;
    --skip)
      [[ -n "${MODE}" ]] && { echo "ERROR: --only and --skip are mutually exclusive" >&2; exit 2; }
      MODE="skip"; FILTER="${2:-}"; shift 2
      [[ -z "${FILTER}" ]] && { echo "ERROR: --skip requires a comma-separated target list" >&2; exit 2; }
      ;;
    *) echo "ERROR: unknown arg: $1" >&2; usage; exit 2 ;;
  esac
done

_should_run() {
  local name="$1"
  if [[ "${MODE}" == "only" ]]; then
    [[ ",${FILTER}," == *",${name},"* ]]
  elif [[ "${MODE}" == "skip" ]]; then
    [[ ",${FILTER}," != *",${name},"* ]]
  else
    return 0
  fi
}

# Validate filter names up front.
if [[ -n "${MODE}" ]]; then
  IFS=',' read -ra _requested <<< "${FILTER}"
  _all_names=""
  for entry in "${TARGETS[@]}"; do _all_names+=",${entry%%|*},"; done
  for n in "${_requested[@]}"; do
    [[ "${_all_names}" == *",${n},"* ]] || { echo "ERROR: unknown target: ${n}" >&2; echo "Run --list to see valid names." >&2; exit 2; }
  done
fi

declare -a RESULTS=()
_total_start=$(date +%s)

for entry in "${TARGETS[@]}"; do
  name="${entry%%|*}"
  cmd="${entry#*|}"

  if ! _should_run "${name}"; then
    RESULTS+=("SKIP  | ${name}")
    continue
  fi

  echo ""
  echo "================================================================"
  echo "[$(date +%H:%M:%S)] BUILD  ${name}"
  echo "                   $ ${cmd}"
  echo "================================================================"

  _t0=$(date +%s)
  bash -c "${cmd}"
  _rc=$?
  _dt=$(( $(date +%s) - _t0 ))

  if [[ "${_rc}" -eq 0 ]]; then
    RESULTS+=("OK    | ${name} (${_dt}s)")
  else
    RESULTS+=("FAIL  | ${name} (exit=${_rc}, ${_dt}s)")
  fi
done

_total_dt=$(( $(date +%s) - _total_start ))

echo ""
echo "================================================================"
echo "SUMMARY (total ${_total_dt}s)"
echo "================================================================"
for line in "${RESULTS[@]}"; do echo "${line}"; done

# Exit non-zero if any FAIL.
for line in "${RESULTS[@]}"; do
  [[ "${line}" == FAIL* ]] && exit 1
done
exit 0
