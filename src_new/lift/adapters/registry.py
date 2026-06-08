from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from src_new.lift.pipeline.run_options import RunOptions

if TYPE_CHECKING:
    from src_new.lift.adapters.base import RuntimeAdapter

SUPPORTED_RUNTIMES = ("openclaw",)

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _yaml_docker_image(path: Path) -> str | None:
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("docker_image:"):
            return line.split(":", 1)[1].strip().strip('"').strip("'")
    return None


def default_docker_image(runtime: str) -> str:
    normalized = runtime.strip().lower()
    if normalized == "openclaw":
        from_yaml = _yaml_docker_image(_REPO_ROOT / "agents" / "openclaw" / "container_defaults.yaml")
        return from_yaml or "evolve-eval-openclaw:latest"
    raise ValueError(f"No default docker image for runtime {runtime!r}")


def create_adapter(runtime: str, options: RunOptions) -> RuntimeAdapter:
    normalized = runtime.strip().lower()
    if normalized == "openclaw":
        from src_new.lift.adapters.openclaw.adapter import OpenClawAdapter

        return OpenClawAdapter(options)
    supported = ", ".join(SUPPORTED_RUNTIMES)
    raise ValueError(f"Unknown runtime {runtime!r}; supported: {supported}")
