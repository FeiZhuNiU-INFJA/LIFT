# Agent runtimes (`agent-runtimes/`)

Each subdirectory owns **one agent runtime's** Docker image, plugins, and container config.

| Directory | Runtime | Image |
|-----------|---------|-------|
| [`openclaw/`](openclaw/) | OpenClaw gateway + plugins | `evolve-eval-openclaw-with-evolve:latest`（默认，带进化插件）/ `evolve-eval-openclaw-base:latest`（`INSTALL_SELF_EVOLVING=false` 构建，不带进化插件） |

Host-side orchestration lives in `src/lift/adapters/<runtime>/`.
