#!/usr/bin/env bash
# Build-time one-shot initialization of the Hermes state root (HERMES_HOME).
#
# Ported from the first-boot bootstrap in the upstream image entrypoint
# (/opt/hermes/docker/entrypoint.sh). Runs ONCE at image build (see Dockerfile),
# NOT at container start.
#
# Why build-time instead of entrypoint:
#   - The upstream /opt/hermes-state is an inherited Docker VOLUME, so runtime writes
#     there are never captured by `docker commit`. LIFT therefore relocates the
#     Hermes state root to /opt/hermes-state (a plain dir in the image layer) so
#     warmup review deltas survive the commit into the holdout delta image.
#   - Seeding must NOT run per-container: a holdout container restarted from a
#     committed delta image would otherwise re-seed and could clobber the very
#     evolved memory/skills we need to measure. Doing it once at build keeps the
#     committed state authoritative and the entrypoint side-effect-free.
set -euo pipefail

PATHS_ENV="/opt/lift/hermes-paths.env"
if [[ -f "$PATHS_ENV" ]]; then
  # shellcheck disable=SC1090
  source "$PATHS_ENV"
fi

export HERMES_HOME="${HERMES_HOME:-/opt/hermes-state}"
INSTALL_DIR="/opt/hermes"

# Profile directory structure (matches upstream; on-demand dirs like cache/ and
# platforms/ are created by the app itself at runtime).
mkdir -p "$HERMES_HOME"/{cron,sessions,logs,hooks,memories,skills,skins,plans,workspace,home}

# Seed .env / config.yaml / SOUL.md from image templates (first-boot only guard;
# harmless at build since the state root starts empty).
if [[ ! -f "$HERMES_HOME/.env" && -f "$INSTALL_DIR/.env.example" ]]; then
  cp "$INSTALL_DIR/.env.example" "$HERMES_HOME/.env"
fi
if [[ ! -f "$HERMES_HOME/config.yaml" && -f "$INSTALL_DIR/cli-config.yaml.example" ]]; then
  cp "$INSTALL_DIR/cli-config.yaml.example" "$HERMES_HOME/config.yaml"
fi
if [[ ! -f "$HERMES_HOME/SOUL.md" && -f "$INSTALL_DIR/docker/SOUL.md" ]]; then
  cp "$INSTALL_DIR/docker/SOUL.md" "$HERMES_HOME/SOUL.md"
fi

# Sync bundled skills. Manifest-based (tools/skills_sync.py): only ADDS bundled
# skills, never deletes user skills, and SKIPs any skill whose content diverges
# from the recorded origin hash. Safe to run over the firecrawl skills already
# present under $HERMES_HOME/skills.
if [[ -d "$INSTALL_DIR/skills" && -f "$INSTALL_DIR/tools/skills_sync.py" ]]; then
  SYNC_PY="${HERMES_VENV_PY:-}"
  if [[ -z "$SYNC_PY" || ! -x "$SYNC_PY" ]]; then
    SYNC_PY="$(command -v python3 || command -v python || true)"
  fi
  if [[ -n "$SYNC_PY" ]]; then
    echo "[hermes-bootstrap] syncing bundled skills via ${SYNC_PY} ..."
    if "$SYNC_PY" "$INSTALL_DIR/tools/skills_sync.py"; then
      echo "[hermes-bootstrap] bundled skills synced"
    else
      echo "[hermes-bootstrap] NOTE: skills_sync failed (non-fatal)"
    fi
  fi
fi

echo "[hermes-bootstrap] Hermes state root initialized at ${HERMES_HOME}"
