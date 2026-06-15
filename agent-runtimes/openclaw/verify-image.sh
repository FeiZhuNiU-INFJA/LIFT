#!/usr/bin/env bash
# Quick check that LIFT OpenClaw image has required plugins.
set -euo pipefail

IMAGE="${1:-evolve-eval-openclaw-with-evolve:latest}"

echo "==> Verifying image: ${IMAGE}"
docker run --rm "${IMAGE}" openclaw --version || true
echo ""
echo "==> Plugins:"
docker run --rm "${IMAGE}" openclaw plugins list || true
echo ""
echo "==> Extensions directory:"
docker run --rm "${IMAGE}" ls -la /root/.openclaw/extensions || true
echo ""
echo "==> Evolution runtime (self-evolving):"
docker run --rm "${IMAGE}" ls -la /root/.openclaw/evolution-runtime 2>/dev/null || \
  echo "(evolution-runtime may be created on first gateway start)"
