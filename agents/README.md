# Agent runtimes

Each subdirectory owns **one agent runtime's** Docker image, plugins, and container config.

| Directory | Runtime | Image |
|-----------|---------|-------|
| [`openclaw/`](openclaw/) | OpenClaw gateway + plugins | `evolve-eval-openclaw:latest` |

Host-side orchestration lives in `src/lift/adapters/<runtime>/`.
