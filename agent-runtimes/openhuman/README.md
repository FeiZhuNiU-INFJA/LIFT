# OpenHuman runtime image (`agent-runtimes/openhuman`)

LIFT 评测用的 [OpenHuman](https://github.com/tinyhumansai/openhuman) 镜像。
`openhuman-core` 是 Rust 实现的 HTTP JSON-RPC server；LIFT 通过
**`POST http://127.0.0.1:{host_port}/rpc` (`method: "openhuman.agent_chat"`)** 完成
一轮对话。

## Layout

```
agent-runtimes/openhuman/
├── .dockerignore
├── Dockerfile
├── build-image.sh
├── install-in-image.sh        # build 期渲染 ~/.openhuman/config.toml
├── config.toml.template       # OpenAI-兼容直连模板；占位符由 install-in-image.sh sed 渲染
├── workspace_seed/            # （占位）holdout 容器启动前 copy 进 /workspace/task
└── README.md
```

## Build (recommended)

从仓库根：

```bash
bash agent-runtimes/openhuman/build-image.sh
```

默认产出 `evolve-eval-openhuman:latest`，对应 LIFT `-r openhuman`。

流程：

1. 从 [GitHub Releases](https://github.com/tinyhumansai/openhuman/releases/latest)
   拉取 latest `.deb` 安装包（`amd64` 默认；`OPENHUMAN_ARCH=arm64` 切 arm64）
2. `apt-get install ./OpenHuman_*.deb` 装 `openhuman-core` 二进制
3. `install-in-image.sh` 把 `.env` 中的 `ARK_API_KEY` / `MODEL_NAME` /
   `ARK_BASE_URL` 通过 sed 渲染到 `/root/.openhuman/config.toml`，配置 OpenHuman
   的 OpenAI-兼容直连模式（`inference_url` + `api_key` + `default_model` 三字段
   配套，绕开 OpenHuman backend；见 upstream `Config.inference_url` 字段注释）

### 内网/外网构建（APT 镜像）

```bash
# 公网（默认）
bash agent-runtimes/openhuman/build-image.sh

# 内网
APT_MIRROR=http://mirrors.byted.org bash agent-runtimes/openhuman/build-image.sh
```

`.deb` 直接从 GitHub Release 拉；如公网不稳定可通过 `OPENHUMAN_DEB_URL` 指向
内网镜像。

### Override

| 变量 | 默认 | 说明 |
|------|------|------|
| `OPENHUMAN_IMAGE` | `evolve-eval-openhuman:latest` | 构建产物 tag |
| `OPENHUMAN_VERSION` | *(latest)* | 指定 upstream release 版本（如 `0.58.12`） |
| `OPENHUMAN_ARCH` | `amd64` | .deb 架构（arm64 host 需显式设 `arm64`） |
| `OPENHUMAN_DEB_URL` | — | 显式指向 .deb 下载 URL（离线构建 / 内网镜像） |
| `OPENHUMAN_MODEL_NAME` | — | 覆盖 `.env` 里的共享 `MODEL_NAME`（避免污染其他镜像） |

## Environment

镜像要求宿主仓库根 `.env` 提供：

- `ARK_API_KEY` — **build-time required**；烧入 `~/.openhuman/config.toml` 的 `api_key`
- `MODEL_NAME` / `OPENHUMAN_MODEL_NAME` — **build-time required**；作为
  `default_model` 烧入。LIFT 约定所有 agent 用同一 seed 模型
  （`doubao-seed-2-0-pro-260215`）。若拿到 `provider/model` 复合形式，脚本会
  自动剥去 provider 前缀。
- `ARK_BASE_URL` — 默认 `https://ark.cn-beijing.volces.com/api/v3`
- `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL` —
  运行期由 LIFT adapter 通过 `docker run --env-file` 注入，容器内 `localhost` /
  `127.0.0.1` 会自动改写为 `host.docker.internal`

## Runtime CMD & port

容器 `CMD` 是：

```
openhuman-core run --host 0.0.0.0 --port 7788
```

`--host 0.0.0.0` 是必需的：`openhuman-core` 默认 bind `127.0.0.1`，容器外无法
访问；LIFT 通过 `docker run -p 0:7788` 让 Docker 分配宿主端口，再 curl
`/health`（fallback: `/` / `/rpc`）探活，然后走 JSON-RPC 对话。

## Evolve delta

OpenHuman 没有独立的 `evolve` 命令。warmup 阶段 agent 自然写入的：

- `/root/.openhuman/workspace/memory_tree/` — 结构化长期记忆树
- `/root/.openhuman/workspace/wiki/` — Obsidian 风格的自建知识库

两条路径被 [`OpenHumanAdapter.evolve_paths`](../../src/lift/adapters/openhuman/adapter.py)
声明为白名单，`docker commit` 把它们随容器 FS 层带入 delta 镜像；delta.py 的
"仅演化路径" diff 用来校验真进化 vs 系统噪声。

## Langfuse

OpenHuman 的 Langfuse 集成走自家私有的 `/telemetry/langfuse/ingestion` proxy
路径（源见 upstream `src/observability/agent_tracing/langfuse.rs`）。**第一版
镜像不改 Langfuse 传输层**：`push_spans` / `push_observations` 的失败会被
caller swallow 成 warning，不影响 chat 主干。跑通 chat 后再迭代 Langfuse 联通。

## LIFT integration

```bash
bash agent-runtimes/openhuman/build-image.sh
python -m src.cli.lift_main -r openhuman \
    --benchmark_dir assets/benchmarks_demo --suite hello.json --warmup-only
python -m src.cli.lift_main -r openhuman \
    --benchmark_dir assets/benchmarks_demo --suite hello.json
```

默认镜像：

- `-r openhuman` → `evolve-eval-openhuman:latest`（常量 `OPENHUMAN_DOCKER_IMAGE`，
  定义于 [`src/paths.py`](../../src/paths.py)）

## Manual sanity check

```bash
docker run --rm evolve-eval-openhuman:latest /usr/local/bin/openhuman-core help
docker run --rm -p 7788:7788 evolve-eval-openhuman:latest &
curl -s http://127.0.0.1:7788/health || curl -s http://127.0.0.1:7788/
curl -s http://127.0.0.1:7788/rpc \
    -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","id":1,"method":"openhuman.agent_chat","params":{"message":"hi","thread_id":"t1"}}'
```
