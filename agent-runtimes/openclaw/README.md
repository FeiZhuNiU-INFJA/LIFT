# OpenClaw agent (`agent-runtimes/openclaw`)

Gateway image with **self-evolving-plugin-pro** and **langfuse-tracer** pre-installed. LIFT (`src`) runs agents via **`docker exec … openclaw`** inside ephemeral per-task containers.

## Layout

```
agent-runtimes/openclaw/
├── .dockerignore          # build context excludes (context = this directory)
├── Dockerfile
├── build-image.sh
├── container_defaults.yaml
├── config/                # openclaw.json fragments
├── workspace_seed/        # pre-filled IDENTITY/USER/SOUL (no BOOTSTRAP.md)
├── plugins/
│   ├── langfuse-tracer/
│   └── self-evolving-plugin-pro-2026.4.23.zip
└── scripts/
    └── openclaw-instance.sh
```

## Build (recommended)

From the **repository root**:

```bash
bash agent-runtimes/openclaw/build-image.sh
```

Produces `evolve-eval-openclaw:latest` (includes `workspace_seed` at `/opt/evolve-eval/workspace_seed`).

LIFT copies this seed into each task workspace before mount so agents skip first-run onboarding.

Verify:

```bash
bash agent-runtimes/openclaw/verify-image.sh evolve-eval-openclaw:latest
```

Optional entrypoint-based image (ephemeral instances):

```bash
docker build -f agent-runtimes/openclaw/Dockerfile.entrypoint -t evolve-eval-openclaw:entrypoint agent-runtimes/openclaw
```

## Environment

Copy [`.env.docker.example`](.env.docker.example) into the repo root `.env`:

- `ARK_API_KEY` — **required for image build**; injected into `config/models.fragment.json`
- `MODEL_NAME` — host/src agent model id
- `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` — runtime
- `LANGFUSE_BASE_URL` — use `http://host.docker.internal:3000` inside containers
- LIFT `ContainerSession` adds `--add-host=host.docker.internal:host-gateway` (required on Linux for langfuse-tracer ingestion)

## LIFT integration (`src`)

```bash
bash agent-runtimes/openclaw/build-image.sh
python -m src.cli.lift_main -r openclaw --suite hello.json --warmup-only
python -m src.cli.lift_main -r openclaw --suite hello.json
```

Default image: `evolve-eval-openclaw:latest` (see `container_defaults.yaml`).

## Instance lifecycle (manual debugging)

```bash
./agent-runtimes/openclaw/scripts/openclaw-instance.sh create --id run-a
eval "$(./agent-runtimes/openclaw/scripts/openclaw-instance.sh env run-a)"
./agent-runtimes/openclaw/scripts/openclaw-instance.sh destroy run-a
```

Commit warmup container to delta image:

```bash
./agent-runtimes/openclaw/scripts/openclaw-instance.sh commit run-a --tag evolve-eval-delta:my-run-r0-suite
```

## Compose (optional)

```bash
docker compose -f agent-runtimes/openclaw/compose.openclaw.yml up -d --build
```
