# Hermes agent (`agent-runtimes/hermes`)

LIFT 评测用的 [Hermes](https://hermes-agent.nousresearch.com) 镜像。与 OpenClaw /
GenericAgent 一样，LIFT（`src`）在每个 phase 的 work 容器里通过 **`docker exec`** 驱动 agent；
judge agent 运行在同镜像、同 workspace、同 load_state 的 sibling 容器中。
Hermes 的驱动入口是容器内常驻的 **`hermes_runner.py`**（stdin/stdout sentinel 协议），
而不是 `gateway run`。

## 目录结构（自包含构建上下文）

```
agent-runtimes/hermes/
├── .dockerignore
├── Dockerfile
├── build-image.sh
├── scripts/
│   ├── install-heavy.sh          # 构建期 L2：pip / uv / nvm / npm / git clone / warmup（重量层）
│   └── install-config.sh         # 构建期 L4：langfuse overlay / run_agent.py patch / plugins enable（轻量层）
├── hermes-bootstrap.sh           # 构建期：初始化 $HERMES_HOME 状态根 + 同步 bundled skills
├── hermes-entrypoint.sh         # 启动期：patch config.yaml + enable 插件 + 容器空转
├── patch_hermes_config.py       # 从 env 生成 config.yaml 的 model 块（不 bake secret）
├── hermes-helper/               # Hermes runner 与协议说明
│   ├── hermes_runner.py         # 容器内长生命周期 runner（Hermes chat 唯一执行通道）
│   └── README_hermes_runner.md
└── langfuse-hermes/             # LIFT 版 Hermes Langfuse 插件（覆盖 Hermes 自带插件）
    ├── __init__.py
    ├── plugin.yaml
    └── README.md
```

> 自包含约定：构建 Hermes 镜像所需的 runner 与 langfuse 插件资产都维护在本目录内，
> Dockerfile 只 `COPY` 本地文件，不依赖仓库外的旧入口或宿主机路径。

## 构建镜像

在**仓库根目录**执行：

```bash
bash agent-runtimes/hermes/build-image.sh
```

默认产出 `lift-hermes:latest`，基于上游 `nousresearch/hermes-agent:v2026.5.16`。

### 带 OpenSpace MCP 插件的变体

```bash
bash agent-runtimes/hermes/build-image.sh --with-openspace
# 产出 lift-hermes-with-openspace:latest
```

OpenSpace（基于 MCP 的 quality-first skill hub）在构建期由 `scripts/install-heavy.sh` git clone
到 `/opt/OpenSpace`（sparse-checkout 跳过 `assets/`），装进独立 Python 3.12 venv（`/opt/openspace-venv`；
Hermes 自带 venv 无 pip 且很可能 <3.12，不能复用），软链 `openspace-mcp` 到 `/usr/local/bin`，
并把 `delegate-task` / `skill-discovery` 两个 host skill 拷进 `/opt/hermes-state/skills`。
`mcp_servers.openspace` 的注册在容器启动时由 `patch_hermes_config.py` upsert 进 `config.yaml`
（由 `ENV OPENSPACE_ENABLED=true` 触发，走 config patch 而非构建期 `hermes mcp add`）。
源可用 env `OPENSPACE_GIT_URL` / `OPENSPACE_GIT_REF` 覆盖。对应 LIFT `-r hermes_with_openspace`
（常量 `HERMES_WITH_OPENSPACE_DOCKER_IMAGE`）。

### 带 agentmemory memory provider plugin 的变体

```bash
bash agent-runtimes/hermes/build-image.sh --with-agentmemory
# 产出 lift-hermes-with-agentmemory:latest；对应 LIFT -r hermes_with_agentmemory
```

agentmemory（跨会话持久记忆，README「Option 2: Memory provider plugin」深度集成）采用**纯本地**模式：
`all-MiniLM-L6-v2` 嵌入 + BM25 + 知识图，**零 API Key、离线**（构建期预热 iii-engine 与嵌入模型进镜像）。
构建期由 `scripts/install-heavy.sh` 装 Node ≥20 + `@agentmemory/agentmemory`，`scripts/install-config.sh` 把 `integrations/hermes`
拷进 `/opt/hermes-state/plugins/agentmemory`；容器启动时 `patch_hermes_config.py` 把
`memory.provider: agentmemory` upsert 进 `config.yaml`，`hermes-entrypoint.sh` 后台拉起 agentmemory
server（`:3111`）。chat 走 `docker exec hermes_runner.py`（同容器同网络命名空间），runner 直连的
`AIAgent`（`skip_memory=False`）通过 `localhost:3111` 访问 server；runner 会在 `AGENTMEMORY_ENABLED=true`
时向 stderr 打印实际挂载的 memory provider 名以便核验。work 容器中的记忆落在 `/root/.agentmemory`，
随 `docker commit` 进 delta 镜像；judge sibling 容器只负责验收，不参与 commit。与 `--with-openspace` **互斥**。源可用 env
`AGENTMEMORY_GIT_URL` / `AGENTMEMORY_GIT_REF` 覆盖，npm registry 用 `NPM_CONFIG_REGISTRY`。

> ⚠️ **端口与网络**：agentmemory server 每容器绑定 `:3111`。该变体在 adapter 层**强制 bridge 网络**
> （`force_bridge_network=True`），忽略全局 `CONTAINER_NETWORK_MODE`（若设为 `host` 会打 WARNING 并
> 回退 bridge），避免同一宿主并发容器抢同一端口冲突。对应常量 `HERMES_WITH_AGENTMEMORY_DOCKER_IMAGE`。

### 基础镜像 tag / 源切换

| 变量 | 默认 | 说明 |
|------|------|------|
| `HERMES_IMAGE` | `lift-hermes:latest` | 产物 tag |
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

不同 Hermes 镜像布局不一致，`scripts/install-heavy.sh` 在构建期**自动探测**并写入
`/opt/lift/hermes-paths.env`（`scripts/install-config.sh` 直接 source 复用）：

- `HERMES_VENV_PY` — Hermes 自带 venv 的 python（用于跑 runner / 装 langfuse）
- `HERMES_SRC_DIR` — Hermes 源码目录（runner `--hermes-agent-dir`）
- `HERMES_PLUGINS_DIR` — 插件根目录（langfuse 覆盖到 `observability/langfuse`）

校验：

```bash
docker run --rm lift-hermes:latest cat /opt/lift/hermes-paths.env
docker run --rm lift-hermes:latest sh -lc \
  '. /opt/lift/hermes-paths.env; "$HERMES_VENV_PY" -m pip show langfuse'
```

## 环境变量（运行期）

`docker run` 时由 LIFT 注入；`config.yaml` 在容器启动时由 `patch_hermes_config.py`
生成，**不在镜像层 bake secret**：

- `MODEL_NAME` — `custom/model_id`（provider 前缀恒为 `custom`）；Hermes `config.yaml`
  中 `model.default` 取 `/` 后缀，`model.provider` 固定 `custom`
- `WORK_OPENAI_BASE_URL` — work LLM base_url
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
  default: <MODEL_NAME 的 / 后缀>
  provider: custom
  base_url: <WORK_OPENAI_BASE_URL>
  api_key: <WORK_OPENAI_API_KEY>
  api_mode: chat_completions
```

## Workspace / 状态布局

| 容器内路径 | 后端 | 是否被 `docker commit` 持久化 | 用途 |
|---|---|---|---|
| `/opt/hermes-state/` | 镜像 FS（非 VOLUME） | ✅ 是 | Hermes 状态根（config / sessions / memories / skills）。warmup 期 review 写入的记忆随 delta 镜像带走。 |
| `/opt/data/` | 上游继承 VOLUME | ❌ 否 | 上游默认状态根；因是 VOLUME，写入不进 `docker commit`，故 LIFT 弃用改挂 `/opt/hermes-state`。 |
| `/workspace/task/` | 宿主机 bind mount | ❌ 否 | 每题 IO：materials 进、`result/` 出，供宿主机判分。 |

> 与官方文档一致：**绝不**让两个运行中的 Hermes 容器共享同一宿主机数据目录。
> 本 runtime 不挂 Hermes 数据 volume，状态根 `/opt/hermes-state` 留在各容器镜像 FS 内，天然隔离。
> 状态根在镜像 build 期由 `hermes-bootstrap.sh` 一次性初始化（含 bundled skills 同步），
> 运行期 entrypoint 不再 seeding，保证 commit 后的 delta 镜像重启时不被覆盖。

## 默认进化机制

Hermes 的进化触发方式与其它 runtime 都不同 —— 它是**每题主动 + suite 收尾被动**:

| 钩子 | Hermes 行为 |
|---|---|
| `evolve_after_task` | **每道 warmup 题结束后硬保证**触发 background review:work runner 收到 `task_end` 后跑 review,阻塞到写完 `/opt/hermes-state/memories`、`/opt/hermes-state/skills` 才 `end_session` 返回 `True`。**review 未干净落盘则 raise**,`_run_evolve_after_task_with_retry` 会重试 3 次,仍失败则整个 `produce_delta` 中止,绝不放行未 review 的 delta 到 holdout。见 [`HermesAdapter.evolve_after_task`](../../src/lift/adapters/hermes/adapter.py) L130-184。 |
| `evolve_after_warmup` | **no-op** —— 所有演化已在每题 review 里完成。 |
| `materialize_delta` | 基类默认 `docker commit`,把 `/opt/hermes-state/` 整个目录冻结进 delta 镜像。 |

因此 Hermes 与 GA / EvoScientist 的"被动隐式"完全不同:后者 warmup 期完全放任 agent
自主写入,只在 warmup 结束后 commit;而 Hermes 是**每题都有一次同步的、必须完成的
review 步骤**,再 commit。这也是为什么 `-r hermes` 只有一个 runtime(没有 `_active_evolve`
变体) —— 主动 evolve 已经内建在 baseline 的 `evolve_after_task` 里了。

### 进化产物路径

| 容器内路径 | 内容 | 是否在 `evolve_paths` 白名单 | 说明 |
|---|---|---|---|
| `/opt/hermes-state/memories/` | `MEMORY.md` / `USER.md` 等长期记忆 | ✅ 是 | background review 蒸馏产物,跨任务共享 |
| `/opt/hermes-state/skills/` | 蒸馏出的技能包(每个含 `SKILL.md`) | ✅ 是 | 同上,跨任务复用 |
| `/opt/hermes-state/sessions/` | 每题会话流水 | ❌ 否 | 会随 commit 进 delta,但不作为"进化证据" |
| `/opt/hermes-state/logs/` | 运行时日志 | ❌ 否 | 同上 |
| `/opt/hermes-state/config.yaml` | 容器启动时 patch 生成 | ✅ 属镜像 FS | 每次容器重启都会被 entrypoint 重写,不影响 delta |

`evolve_paths` 声明见 [`HermesAdapter.evolve_paths`](../../src/lift/adapters/hermes/adapter.py) L45-48;
仅作 delta preflight 负向判定,`docker commit` 实际会捕获整个容器 FS。

### 跨 session / 跨任务共享 & 竞态提示

Warmup 多任务共享同一容器,`/opt/hermes-state/memories/` 与 `.../skills/` 就是跨任务
共享的知识库。但正因 review 是**每题一次的写操作**,一旦 warmup 用 `parallel_single`
(默认)让多题几乎同时结束,多个 review 进程会并发写同一 memory 存储 → **竞态**。

因此 Hermes **推荐显式加 `--warmup-container-policy serial_single`**(单容器逐题串行,
review 也串行),详见下面 *warmup 并发策略* 章节。这是 Hermes 集成时唯一必须偏离
LIFT 默认的地方。

## 与 LIFT 集成（`src`）

```bash
bash agent-runtimes/hermes/build-image.sh
python -m src.cli.lift_main -r hermes --benchmark_dir assets/benchmarks_demo \
    --suite hello.json --run-id hermes-smoke
```

默认镜像常量 `HERMES_DOCKER_IMAGE`（`lift-hermes:latest`）定义于
[`src/paths.py`](../../src/paths.py)。

### warmup 并发策略（重要）

Hermes 的演化是"每题 work session 结束触发 background review，写入共享
`/opt/hermes-state`"。框架默认 `--warmup-container-policy parallel_single`（同一个 work 容器内多题
并发，另有 sibling judge 容器评分），此时多个 review 进程会**并发写同一 memory 存储，存在竞态**。

**推荐 Hermes warmup 显式用 `serial_single`**（同一个 work 容器逐题串行，review 也串行），
与 Hermes suite 内串行评测语义一致；跨 suite/repeat 的并发仍由 `--max-parallel-suites`
提供：

```bash
python -m src.cli.lift_main -r hermes --benchmark_dir assets/benchmarks_demo \
    --suite hello.json --run-id hermes-run \
    --warmup-container-policy serial_single
```

> 若未加该参数、仍用 `parallel_single`，`HermesAdapter` 会在启动时打一条
> LOGGER.warning 提示竞态风险，但不阻断运行。holdout 阶段每题每 phase 独立 work+judge 容器对、无 review，
> 不受此影响，与 OpenClaw 完全一致。

## 本机 smoke/debug 注意

推荐在 WSL/Linux 服务器运行完整评测；本机只做最小 smoke。若为调试直接在宿主机跑
Hermes（非容器主路径），会改动 `~/.hermes/config.yaml`，与容器内 `/opt/hermes-state/config.yaml`
互不影响，但要注意别把宿主机凭据混入评测。

## Langfuse 关联

每轮 chat 产生两条 trace：框架 `emit_pre_chat_state` 的 `work_agent` / `judge_agent`
span（宿主机侧），与容器内 Hermes 插件的 `Hermes turn`。后处理
（`agent_source="hermes"`）通过 tags 里的 work/judge session_id 配对，走
[`_stitch_by_tags`](../../src/report/langfuse_trace_stitch.py)。

<a id="token-5-fields"></a>
### Token 5 字段落库状态

全 5 字段（`input_fresh` / `cache_write` / `cache_read` / `output` / `reasoning`）齐，
**依赖 LIFT 覆盖版 `langfuse-hermes` 插件**（构建期由 `scripts/install-config.sh`
overlay 到 Hermes venv 的 `observability/langfuse`）。要点：

- 上游 `agent.usage_pricing.normalize_usage` 在 **OpenAI-compatible / Ark** 路径下会
  丢弃 `prompt_tokens_details.cached_tokens` 与 `completion_tokens_details.reasoning_tokens`，
  canonical 里对应字段为 0。
- 覆盖版插件里的 `_fallback_extract_from_raw_usage()` 直接从 raw usage 兜底
  提取,兼容 OpenAI/Ark、Anthropic (`cache_read_input_tokens` /
  `cache_creation_input_tokens`)、DeepSeek (`prompt_cache_hit_tokens`) 命名,
  按 Anthropic-style key 写入 `usageDetails`。
- 结果：跑 LIFT 镜像时 5 字段齐;直接跑上游 Hermes 时 Ark 路径下 `cache_read` /
  `reasoning` 仍会为 0。

统一口径 / 排障 / 断层图见
[skill/lift-integrate-agent-runtime/docs/token-observability.md](../../skill/lift-integrate-agent-runtime/docs/token-observability.md)。
