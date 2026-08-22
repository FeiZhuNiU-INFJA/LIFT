# Prime Agent（`agent-runtimes/prime_agent`）

[PrimeIntellect-ai/prime-agent](https://github.com/PrimeIntellect-ai/prime-agent) 的 LIFT 评测镜像。Prime Agent 是 Node/TS CLI（发布包 `prime-agent`，bin `prime-agent`），运行时会拉起一个**常驻 IPython 内核**执行工具调用，因此镜像同时需要 Node + Python。自进化能力来自它的 **Continual Harness** + **`/refine`** 命令。

LIFT（`src`）通过 `docker exec` 在一次性 work 容器里单发 `prime-agent --mode json ...` 驱动 agent；judge 运行在同镜像、同 workspace、同 load_state 的 sibling 容器中，二者文件系统记忆隔离。

对应两个 CLI runtime：

- `-r prime_agent` —— baseline / **被动进化**（`evolve_after_warmup` 为 no-op）
- `-r prime_agent_active_evolve` —— **主动进化**（逐题触发 global `/refine`）；与 baseline 共用同一镜像

## 目录结构

```
agent-runtimes/prime_agent/
├── Dockerfile
├── build-image.sh
├── scripts/
│   ├── install-heavy.sh    # L2 重量层：从官方 R2 下载+校验+全局装 prime-agent
│   └── install-config.sh   # L4 轻量层：渲染 models.json / settings.json + Langfuse env
├── skills/
│   └── firecrawl/          # Firecrawl 远程 MCP skill（路线一，静态 bearer token）
└── workspace_seed/         # LIFT 特定 project context（可选；当前为空）
```

## 构建镜像

在**仓库根目录**执行：

```bash
bash agent-runtimes/prime_agent/build-image.sh
# → lift-prime-agent:latest（常量 PRIME_AGENT_DOCKER_IMAGE）
```

`prime_agent` 与 `prime_agent_active_evolve` **共用同一个镜像**，无需分别构建；差异只在 host adapter 层的 evolve 钩子。

**分发方式说明**：prime-agent 不在公共 npm registry，而是以 Prime Intellect 官方 R2 上的发布 tarball 分发（package.json 的 3 个 `@earendil-works/*` 依赖也指向同源 R2 tarball URL）。故 L2 层用 download → verify → `npm install -g <tarball>`（对齐官方 `install.sh`），而非 `npm i -g prime-agent`。

### 各环境镜像源切换

| 环境 | 命令 |
|------|------|
| 公网（默认） | `bash agent-runtimes/prime_agent/build-image.sh` |
| 字节内网 | **脚本自动探测**（命中 `mirrors.byted.org` 即切 `APT_MIRROR` / `PIP_INDEX_URL`）；`LIFT_INTRANET_AUTODETECT=0` 可关闭 |
| 其它内网/自建源 | 显式设 `APT_MIRROR`（布局与官方一致的 Debian 镜像）+ `PIP_INDEX_URL`（PEP 503 simple 索引）+ `PRIME_AGENT_DOWNLOAD_BASE_URL`（指向已同步 R2 tarball 的内网镜像） |

内网构建时 npm/curl 需走公司代理访问公网 R2 / GitHub，`build-image.sh` 内建宿主代理透传（检测宿主 `http_proxy` → 拼进 `--build-arg`）。

## 环境变量

将仓库根 `.env` 备好后 `build-image.sh` 会把以下 build-arg bake 进镜像：

- `WORK_OPENAI_API_KEY` / `WORK_OPENAI_BASE_URL` —— **构建期必填**；渲染进 Prime Agent 的 `models.json`（OpenAI 兼容后端，默认火山方舟 `https://ark.cn-beijing.volces.com/api/v3`）
- `MODEL_NAME` —— **构建期必填**，`custom/model_id` 格式（provider 前缀恒为 `custom`）；斜杠后的 `model_id` 是实际请求的模型
- `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` —— 运行时 host 侧 push trace 需要
- `LANGFUSE_HOST` —— 镜像内固定 `http://host.docker.internal:3000`；LIFT `start_prime_agent_container` 的 `env_vars` 还会把 `.env` 里的 loopback host 改写为 `host.docker.internal`（`-e` 优先级高于 `env_file`）
- `REASONING_EFFORT` —— 默认 `high`
- `FIRECRAWL_API_KEY` —— 可选联网能力（见下文）；置空则 firecrawl skill 仍加载，但调用抛 `NotEnabled`

运行时由 LIFT 注入的关键 env：

- `PRIME_AGENT_CODING_AGENT_DIR=/root/.prime/agent` —— **钉死状态根目录**（见下文进化机制）
- `PRIME_AGENT_KERNEL_PYTHON=/usr/bin/python3` —— 复用系统 python3（已装 ipykernel + rlm），跳过运行时 kernel-venv bootstrap
- `LIFT_EVAL_RUN_TAG` —— 由 LIFT 设为 `run_id`，作为 Langfuse plugin trace 的 tag
- `LIFT_PRIME_AGENT_SESSION_ID` —— 每题的 LIFT/Langfuse session id（**注意**：这只用于观测关联，不等于 Prime Agent 自己的 conversation session）

<a id="token-5-fields"></a>
### Token 5 字段落库状态（host 侧 push）

⚠️ **Prime Agent 镜像里没有容器内 Langfuse 插件 / overlay**——它是 headless CLI 单发（`docker exec ... --mode json`），没有常驻进程、也没有 plugin 机制可挂 emitter。因此 5 字段 token 走 **host 侧 push**：

1. `chat_agent.py` 的 `chat()` 拿到 `prime-agent --mode json` 的 stdout JSON 事件流后，解析其中的 usage 字段；
2. 调 [`langfuse_usage.py::push_prime_agent_plugin_trace_safe`](../../src/lift/adapters/prime_agent/langfuse_usage.py) 在 host 侧补写一条 `name="prime-agent-plugin"` 的 trace（root span + 每个 assistant round-trip 一个 GENERATION observation，挂 `usage_details`），`session_id` 传 LIFT 侧 session（失败仅 warning，不阻断评测）；
3. `src/models.py` 的 `LANGFUSE_PLUGIN_TRACE_NAMES` 已含 `"prime-agent-plugin"`，后处理 `trace_backfill` 据此拼回 5 字段。

跨 runtime 的 emitter locus 判定与 host 侧 push 通法见 [skill/lift-integrate-agent-runtime/docs/token-observability.md](../../skill/lift-integrate-agent-runtime/docs/token-observability.md) §2 断点 0 + §3.B。

### Firecrawl 联网能力（路线一：官方远程 MCP + 静态 bearer token）

LIFT 评测容器**无浏览器、无其它联网搜索工具**，Prime Agent 的联网能力靠 Firecrawl 官方 hosted MCP server（从 IPython 内核直连）：

- skill 落在 `${PRIME_AGENT_STATE_DIR}/skills/firecrawl`（prime-agent 扫描 skills 位置并提取到 system prompt）；
- 因 `PRIME_AGENT_KERNEL_PYTHON` 已钉死，prime-agent 不会自动把 Python skill 装进 kernel，故 Dockerfile 手动 `pip install -e` 进系统 python3（skill 发现与 kernel import 共用同一份源）；
- 用**静态 bearer token**（`FIRECRAWL_API_KEY`），不是 OAuth——`settings.json` 的 `mcpServers.firecrawl` 用 `bearerTokenEnvVar` 引用 env，不写进 JSON 明文；
- 工具集由 server 定义，skill 内约定"**discover before you call**"（先 `firecrawl.list_tools()` 再调）。

`FIRECRAWL_API_KEY` 置空时 skill 仍加载，调用抛 `NotEnabled`，agent 可回退到 kernel + `requests`。

## 状态目录布局（镜像 FS vs bind mount）

| 容器内路径 | 后端 | 是否被 `docker commit` 持久化 | 用途 |
|---|---|---|---|
| `/root/.prime/agent/harness/` | 镜像 FS | ✅ 是 | **global** Continual Harness（`harness_state.json`）——进化产物核心落点 |
| `/root/.prime/agent/skills/` | 镜像 FS | ✅ 是 | agent 自写的 skills + baked 的 firecrawl skill |
| `/root/.prime/agent/sessions/` | 镜像 FS | ✅ 是 | 会话轨迹（`/refine` 的 `-c` 续接读这里） |
| `/root/.prime/agent/supervisor-owners/` 等 | 镜像 FS | ⚠️ 被 commit 但**须启动前清理**（见下文 daemon 状态） | daemon 运行时瞬态锁 |
| `/workspace/task/` | 宿主机 bind mount → `results/{run_id}/outcome/.../{phase}/...` | ❌ 否（mount） | 每题 IO 通道：素材送入、产物写出供宿主判分 |

状态根由 env `PRIME_AGENT_CODING_AGENT_DIR` 钉死到 `/root/.prime/agent`（= `src/paths.py` 的 `PRIME_AGENT_STATE_DIR`），避免 XDG 漂移导致 commit 抓不到。

## 默认进化机制

| Runtime | 触发方式 | `evolve_after_warmup` / `evolve_after_task` | 进化产物 |
|---|---|---|---|
| `-r prime_agent`（baseline） | **被动隐式** | 均 no-op | warmup 期 Prime Agent 自然写入的 harness / skills（`PRIME_AGENT_STATE_DIR` 下），由 `docker commit` 捕获 |
| `-r prime_agent_active_evolve` | **主动显式** | `evolve_after_task` 逐题触发 global `/refine`；`evolve_after_warmup` 显式 no-op | 同 baseline + 每题 `/refine --global` 蒸馏进 global harness |

两相共用 `evolve_paths = (PRIME_AGENT_STATE_DIR,)`，供 delta preflight 输出 evolve-only 摘要、负向判定 warmup 是否真产出进化产物。

**跨 session / 跨任务共享**：warmup 每题是**独立 conversation session**（每次 chat 新建自己的 session）。Prime Agent 的 Continual Harness 默认 **session-local**——普通任务执行只写 local harness，绑定当次 session id；而 LIFT holdout 每题是全新容器 + 全新 session，**读不到 local harness**。只有落到 **global** harness（`~/.prime/agent/harness/`）的产物才能被 `docker commit` 带入 delta 并被 holdout 新 session 读到。

### ⚠️ baseline 的定位（重要，勿把 ≈0 增益误读为缺陷）

由于上面的 session-local 语义，`prime_agent` baseline 的 evolved 相对 baseline **预期增益 ≈ 0**——warmup 自然写入的多是 local harness，被 holdout 新 session 丢弃。因此**不要**把 `prime_agent` 当作"测 Prime Agent 开箱自进化能力"的主指标；它的真正意义是 `prime_agent_active_evolve` 的**科学对照组（负向对照 / ablation）**。有了这个 ≈0 的被动基线，active 变体的增益才能被归因到"显式 refine 机制"而非噪声。评"Prime Agent 是否真能自进化",看 **active 变体相对本 baseline 的差值**。

### active_evolve 为什么逐题触发 global `/refine`

`/refine` 只复盘**当前/最近一次会话**的轨迹（upstream `rlm-runtime.md`）。若只在 `evolve_after_warmup` 触发一次，`-c`（`--continue`）续接的"最近会话"仅是最后一道 warmup 题，前 N-1 题的教训丢失。改为逐题触发（挂在 `evolve_after_task`）：每题一结束就 `prime-agent -c -p -- "/refine --global"`，此刻"最近会话"恰是刚结束那题，`-c` 天然对齐，每题证据都被提升到 global 累积层。`--global` 把作用域从 session-local 提升到持久层。实现见 [`prime_agent_active_evolve/refine.py`](../../src/lift/adapters/prime_agent_active_evolve/refine.py)。

## 两个 Prime-Agent 特有约束

### 1. warmup 必须串行

Continual Harness 是**共享可变状态**，global harness 落成**单个无锁、非原子的文件**（`harness_state.json`）。

- **baseline**（`prime_agent`）：`parallel_single` 下多题并发 CRUD 同一 harness 会竞态，构造时打 WARNING 但放行，推荐 `--warmup-container-policy serial_single`。
- **active_evolve**（`prime_agent_active_evolve`）：`__init__` 里**强制 coerce 到 `SERIAL_SINGLE`**（不放行）。因为逐题 `-c` 续接"最近会话"只在串行时才准确对齐刚结束那题；并发会让 refine 复盘错轨迹 + 竞态写 harness。

### 2. evolved 容器启动前必须清 daemon 瞬态（EXDEV）

Prime Agent 后台 daemon supervisor 启动时要"抢所有权"——把 `supervisor-owners/<uuid>.owner` `renameSync`。`docker commit` 会把 warmup 容器里这些**运行时瞬态锁**一并固化进 delta 镜像；evolved 容器从该镜像启动时，跨设备 rename（overlayfs upperdir ↔ bind mount）触发 `EXDEV: cross-device link not permitted`，daemon 起不来 → 首轮 chat 秒崩。baseline 从干净基线镜像起不受影响，症状是"baseline 正常、evolved 独崩"。

修法：[`session.py::_clear_stale_daemon_state`](../../src/lift/adapters/prime_agent/session.py#L121-L148) 作为 `post_start_hook`（容器启动后、首轮 chat 前）`rm -rf` 掉 supervisor-owners / daemon-workers / session-leases 等瞬态目录——**只清瞬态,不动 `harness/`（进化产物）**；daemon 起来会自建这些锁，清理是安全的。

## 与 LIFT 集成（`src`）

```bash
bash agent-runtimes/prime_agent/build-image.sh

# baseline（对照组）
python -m src.cli.lift_main -r prime_agent \
  --benchmark_dir assets/benchmarks_demo --suite hello.json \
  --warmup-container-policy serial_single --run-id pa-smoke --warmup-only

# active evolve（会强制 serial_single）
python -m src.cli.lift_main -r prime_agent_active_evolve \
  --benchmark_dir assets/benchmarks_demo --suite integration_check.json \
  --run-id pa-evolve
```

默认镜像：`-r prime_agent` 与 `-r prime_agent_active_evolve` 均 → `lift-prime-agent:latest`（常量 `PRIME_AGENT_DOCKER_IMAGE`，定义于 `src/paths.py`）。
