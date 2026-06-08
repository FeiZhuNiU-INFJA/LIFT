# OpenClaw agent (`agents/openclaw`)

Gateway image with **self-evolving-plugin-pro** and **langfuse-tracer** pre-installed. HACE (`src_new`) runs agents via **`docker exec … openclaw`** inside ephemeral per-task containers.

## Layout

```
agents/openclaw/
├── .dockerignore          # build context excludes (context = this directory)
├── Dockerfile
├── build-image.sh
├── container_defaults.yaml
├── config/                # openclaw.json fragments
├── plugins/
│   ├── langfuse-tracer/
│   └── self-evolving-plugin-pro-2026.4.23.zip
└── scripts/
    └── openclaw-instance.sh
```

## Build (recommended)

From the **repository root**:

```bash
bash agents/openclaw/build-image.sh
```

Produces `evolve-eval-openclaw:latest`.

Verify:

```bash
bash agents/openclaw/verify-image.sh evolve-eval-openclaw:latest
```

Optional entrypoint-based image (ephemeral instances):

```bash
docker build -f agents/openclaw/Dockerfile.entrypoint -t evolve-eval-openclaw:entrypoint agents/openclaw
```

## Environment

Copy [`.env.docker.example`](.env.docker.example) into the repo root `.env`:

- `ARK_API_KEY` — **required for image build**; injected into `config/models.fragment.json`
- `MODEL_NAME` — host/src_new agent model id
- `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` — runtime
- `LANGFUSE_BASE_URL` — use `http://host.docker.internal:3000` inside containers
- HACE `ContainerSession` adds `--add-host=host.docker.internal:host-gateway` (required on Linux for langfuse-tracer ingestion)

## HACE integration (`src_new`)

```bash
bash agents/openclaw/build-image.sh
python -m src_new.cli.hace_main --runtime openclaw --suite hello.json --warmup-only
python -m src_new.cli.hace_main --runtime openclaw --suite hello.json
```

Default image: `evolve-eval-openclaw:latest` (see `container_defaults.yaml`).

## Instance lifecycle (manual debugging)

```bash
./agents/openclaw/scripts/openclaw-instance.sh create --id run-a
eval "$(./agents/openclaw/scripts/openclaw-instance.sh env run-a)"
./agents/openclaw/scripts/openclaw-instance.sh destroy run-a
```

Commit warmup container to delta image:

```bash
./agents/openclaw/scripts/openclaw-instance.sh commit run-a --tag evolve-eval-delta:my-run-r0-suite
```

## Compose (optional)

```bash
docker compose -f agents/openclaw/compose.openclaw.yml up -d --build
```
