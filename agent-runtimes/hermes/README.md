# Hermes agent (`agent-runtimes/hermes`)

LIFT 评测用的 [Hermes](https://hermes-agent.nousresearch.com) 镜像。与 OpenClaw /
GenericAgent 一样，LIFT（`src`）在每题独立容器里通过 **`docker exec`** 驱动 agent；
Hermes 的驱动入口是容器内常驻的 **`hermes_runner.py`**（stdin/stdout sentinel 协议），
而不是 `gateway run`。

## 目录结构（自包含构建上下文）

```
agent-runtimes/hermes/
├── .dockerignore
├── Dockerfile
├── build-image.sh
├── install-in-image.sh          # 构建期：装 langfuse 进 Hermes venv + 覆盖插件 + 记录发现的路径
├── hermes-entrypoint.sh         # 启动期：patch config.yaml + enable 插件 + 容器空转
├── patch_hermes_config.py       # 从 env 生成 config.yaml 的 model 块（不 bake secret）
├── hermes-helper/               # 从 legacy/hermes-helper 整目录拷贝
│   ├── hermes_runner.py         # 容器内长生命周期 runner（Hermes chat 唯一执行通道）
│   └── README_hermes_runner.md
└── langfuse-hermes/             # 从 legacy/langfuse-hermes 整目录拷贝（覆盖 Hermes 自带 langfuse 插件）
    ├── __init__.py
    ├── plugin.yaml
    └── README.md
```

> 自包含约定：构建 Hermes 镜像所需、原本只在 `legacy/` 的资产（runner、langfuse 插件）
> 已整目录拷贝进本目录，Dockerfile 只 `COPY` 本地文件，不跨目录引用 `../../legacy/`。
> 更新 legacy 版本后需重新拷贝同步。

## 构建镜像

在**仓库根目录**执行：

```bash
bash agent-runtimes/hermes/build-image.sh
```

默认产出 `evolve-eval-hermes:latest`，基于上游 `nousresearch/hermes-agent:v2026.5.16`
（对齐 legacy 使用的 Hermes 版本）。

### 基础镜像 tag / 源切换

| 变量 | 默认 | 说明 |
|------|------|------|
| `HERMES_IMAGE` | `evolve-eval-hermes:latest` | 产物 tag |
| `HERMES_BASE_IMAGE_REPO` | `nousresearch/hermes-agent` | 上游仓库 |
| `HERMES_BASE_IMAGE_TAG` | `v2026.5.16` | 上游 tag（`.env` 可覆盖） |
| `HERMES_BASE_IMAGE` | （未设） | 直接指定完整上游镜像，优先于 REPO:TAG |
| `PIP_INDEX_URL` | 公网 | 内网构建时切 PyPI 源 |

```bash
# 换上游 tag
HERMES_BASE_IMAGE_TAG=latest bash agent-runtimes/hermes/build-image.sh
# 内网 PyPI
PIP_INDEX_URL=https://bytedpypi.byted.org/simple/ bash agent-runtimes/hermes/build-image.sh
```

## 构建期自动发现的路径

不同 Hermes 镜像布局不一致，`install-in-image.sh` 在构建期**自动探测**并写入
`/opt/evolve-eval/hermes-paths.env`：

- `HERMES_VENV_PY` — Hermes 自带 venv 的 python（用于跑 runner / 装 langfuse）
- `HERMES_SRC_DIR` — Hermes 源码目录（runner `--hermes-agent-dir`）
- `HERMES_PLUGINS_DIR` — 插件根目录（langfuse 覆盖到 `observability/langfuse`）

校验：

```bash
docker run --rm evolve-eval-hermes:latest cat /opt/evolve-eval/hermes-paths.env
docker run --rm evolve-eval-hermes:latest sh -lc \
  '. /opt/evolve-eval/hermes-paths.env; "$HERMES_VENV_PY" -m pip show langfuse'
```

## 环境变量（运行期）

`docker run` 时由 LIFT 注入；`config.yaml` 在容器启动时由 `patch_hermes_config.py`
生成，**不在镜像层 bake secret**：

- `MODEL_NAME` — `provider/model_id`；Hermes `config.yaml` 中 `model.default` 取
  `/` 后缀，`model.provider` 固定 `custom`
- `HERMES_MODEL_NAME` —（可选）显式覆盖 `model.default`
- `WORK_OPENAI_BASE_URL` / `HERMES_API_URL` — work LLM base_url（后者优先）
- `WORK_OPENAI_API_KEY` — work LLM api_key
- `MAX_TOKENS` — runner `--max-tokens`（默认 51200）
- `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_BASE_URL` — 入口脚本
  会映射为 Hermes 插件所需的 `HERMES_LANGFUSE_*`
- `FIRECRAWL_API_KEY` — 可选。**非空时**构建期会烧入镜像 ENV 并执行
  `npx -y firecrawl-cli init --all`；运行期 adapter 也会注入该 key。为空则整段跳过。
- `LIFT_EVAL_RUN_TAG` — LIFT 设为 `run_id`

生成的 `config.yaml` model 块：

```yaml
model:
  default: <MODEL_NAME 的 / 后缀，或 HERMES_MODEL_NAME>
  provider: custom
  base_url: <HERMES_API_URL 或 WORK_OPENAI_BASE_URL>
  api_key: <WORK_OPENAI_API_KEY>
  api_mode: chat_completions
```

## Workspace / 状态布局

| 容器内路径 | 后端 | 是否被 `docker commit` 持久化 | 用途 |
|---|---|---|---|
| `/opt/data/` | 镜像 FS | ✅ 是 | Hermes 状态根（config / sessions / memories / skills）。warmup 期 review 写入的记忆随 delta 镜像带走。 |
| `/workspace/task/` | 宿主机 bind mount | ❌ 否 | 每题 IO：materials 进、`result/` 出，供宿主机判分。 |

> 与官方文档一致：**绝不**让两个运行中的 Hermes 容器共享同一宿主机数据目录。
> 本 runtime 不挂 Hermes 数据 volume，`/opt/data` 留在各容器镜像 FS 内，天然隔离。

## 与 LIFT 集成（`src`）

```bash
bash agent-runtimes/hermes/build-image.sh
python -m src.cli.lift_main -r hermes --benchmark_dir assets/benchmarks_demo \
    --suite hello.json --run_id hermes-smoke
```

默认镜像常量 `HERMES_DOCKER_IMAGE`（`evolve-eval-hermes:latest`）定义于
[`src/paths.py`](../../src/paths.py)。

### warmup 并发策略（重要）

Hermes 的演化是"每题 work session 结束触发 background review，写入共享
`/opt/data`"。框架默认 `--warmup-container-policy parallel_single`（单容器内多题
并发），此时多个 review 进程会**并发写同一 memory 存储，存在竞态**。

**推荐 Hermes warmup 显式用 `serial_single`**（单容器逐题串行，review 也串行），
与 legacy"suite 内串行"语义一致；跨 suite/repeat 的并发仍由 `--max-parallel-suites`
提供：

```bash
python -m src.cli.lift_main -r hermes --benchmark_dir assets/benchmarks_demo \
    --suite hello.json --run_id hermes-run \
    --warmup-container-policy serial_single
```

> 若未加该参数、仍用 `parallel_single`，`HermesAdapter` 会在启动时打一条
> LOGGER.warning 提示竞态风险，但不阻断运行。holdout 阶段每题独立容器、无 review，
> 不受此影响，与 OpenClaw 完全一致。

## 本机 smoke/debug 注意

推荐在 WSL/Linux 服务器运行完整评测；本机只做最小 smoke。若为调试直接在宿主机跑
Hermes（非容器主路径），会改动 `~/.hermes/config.yaml`，与容器内 `/opt/data/config.yaml`
互不影响，但要注意别把宿主机凭据混入评测。

## Langfuse 关联

每轮 chat 产生两条 trace：框架 `emit_pre_chat_state` 的 `work_agent` / `judge_agent`
span（宿主机侧），与容器内 Hermes 插件的 `Hermes turn`。后处理
（`agent_source="hermes"`）通过 tags 里的 work/judge session_id 配对，走
[`_stitch_hermes`](../../src/report/langfuse_trace_stitch.py)。
