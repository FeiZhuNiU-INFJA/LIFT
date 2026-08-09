# OpenClaw agent（`agent-runtimes/openclaw`）

预装了 **self-evolving-plugin-pro** 与 **langfuse-tracer** 的 gateway 镜像。LIFT（`src`）通过在一次性的 work 容器里执行 **`docker exec … openclaw`** 来驱动 agent；judge agent 运行在同镜像、同 workspace、同 load_state 的 sibling 容器中，二者文件系统记忆隔离。

## 目录结构

```
agent-runtimes/openclaw/
├── .dockerignore          # build context 排除项（context = 本目录）
├── Dockerfile
├── build-image.sh
├── config/                # openclaw.json 各 fragment
├── workspace_seed/        # IDENTITY / USER / SOUL / AGENTS.md 等种子文件；构建期 baked 进 /root/.openclaw/workspace
├── plugins/
│   ├── langfuse-tracer/
│   └── self-evolving-plugin-pro-2026.4.23.zip
└── scripts/
    └── openclaw-instance.sh
```

## 构建镜像

在**仓库根目录**执行：

```bash
bash agent-runtimes/openclaw/build-image.sh
```

默认产出 `lift-openclaw-base:latest`（不带 self-evolving-plugin-pro 进化插件，对应 LIFT `-r openclaw`；`workspace_seed/` 在构建期已 baked 进镜像内的 `/root/.openclaw/workspace`）。

带进化插件的变体：

```bash
bash agent-runtimes/openclaw/build-image.sh --with-evolve
# 产出 lift-openclaw-with-evolve:latest
```

带 **OpenSpace**（基于 MCP 的 quality-first skill hub）插件的变体：

```bash
bash agent-runtimes/openclaw/build-image.sh --with-openspace
# 产出 lift-openclaw-with-openspace:latest
```

带 **agentmemory**（跨会话持久记忆，README「Option 2: OpenClaw memory plugin」深度集成）的变体：

```bash
bash agent-runtimes/openclaw/build-image.sh --with-agentmemory
# 产出 lift-openclaw-with-agentmemory:latest；对应 LIFT -r openclaw_with_agentmemory
```

> agentmemory 采用**纯本地**模式：`all-MiniLM-L6-v2` 嵌入 + BM25 + 知识图，**零 API Key、离线**
> （构建期预热 iii-engine 二进制与嵌入模型进镜像，运行期不需出网）。插件装进
> `/root/.openclaw/extensions/agentmemory` 并 claim `plugins.slots.memory = "agentmemory"`；容器启动时
> 由 `scripts/openclaw-agentmemory-prelaunch.sh` 在 gateway 前后台拉起 agentmemory server（`:3111`）。
> work 容器中的记忆落在 `/root/.agentmemory`，随 `docker commit` 进 delta 镜像；judge sibling 容器只负责验收，不参与 commit。
> 源可用 env `AGENTMEMORY_GIT_URL` / `AGENTMEMORY_GIT_REF` 覆盖，npm registry 用 `NPM_CONFIG_REGISTRY`。
>
> ⚠️ **端口与网络**：agentmemory server 每容器绑定 `:3111`（+3112/3113/49134）。该变体在
> adapter 层**强制 bridge 网络**（`force_bridge_network=True`），忽略全局 `CONTAINER_NETWORK_MODE`
> （若设为 `host` 会打 WARNING 并回退 bridge），避免同一宿主并发容器抢同一端口冲突。

> ⚠️ `--with-evolve` / `--with-openspace` / `--with-agentmemory` **三方互斥**，只能三选一。
> 同时传多个会报错退出；构建脚本与 Dockerfile 均有守卫。

OpenSpace 源默认 `git clone https://github.com/HKUDS/OpenSpace.git@main`（sparse-checkout 跳过 `assets/`），
可用 env `OPENSPACE_GIT_URL` / `OPENSPACE_GIT_REF` 覆盖（内网可指镜像 / 反代）。安装逻辑见
[install-heavy.sh](scripts/install-heavy.sh) 第 4 步：git clone 到 `/opt/OpenSpace`、
在独立 Python 3.12 venv（`/opt/openspace-venv`）里 `pip install -e`、软链 `openspace-mcp` 到
`/usr/local/bin`；[install-config.sh](scripts/install-config.sh) 再做 `openclaw mcp set openspace`（stdio，`toolTimeout=600`），并把 `delegate-task` /
`skill-discovery` 两个 host skill 拷进 `/root/.openclaw/skills`（随 `docker commit` 落 delta）。

种子文件直接位于**镜像内**的 `/root/.openclaw/workspace`，也是 work agent 真正的工作目录；因此 warmup 期间 work agent 生成的 SOUL / MEMORY / 日常记忆都会被 `docker commit` 一并打进 delta 镜像（详见下文 *Workspace 布局*）。judge 容器使用相同 seed 与 task workspace，但其状态不会被 commit。

### 字节内网构建（推荐复制即用）

公网默认走 `ghcr.io/openclaw/openclaw:latest` + `deb.debian.org` + `https://pypi.org/simple/`，字节内网拉取会很慢甚至超时。下面这套环境变量就是字节内网构建的固定写法，Dockerfile 与脚本本身**不需要改动**：

```bash
# 字节内网：base 镜像
OPENCLAW_BASE_IMAGE=ghcr.milu.moe/openclaw/openclaw:latest \
APT_MIRROR=http://mirrors.byted.org \
PIP_INDEX_URL=https://bytedpypi.byted.org/simple/ \
  bash agent-runtimes/openclaw/build-image.sh
# → lift-openclaw-base:latest

# 字节内网：with-evolve 镜像
OPENCLAW_BASE_IMAGE=ghcr.milu.moe/openclaw/openclaw:latest \
APT_MIRROR=http://mirrors.byted.org \
PIP_INDEX_URL=https://bytedpypi.byted.org/simple/ \
  bash agent-runtimes/openclaw/build-image.sh --with-evolve
# → lift-openclaw-with-evolve:latest
```

### 各环境镜像源切换一览

| 环境 | 命令 |
|------|------|
| 公网（默认） | `bash agent-runtimes/openclaw/build-image.sh` |
| 字节内网 | `OPENCLAW_BASE_IMAGE=ghcr.milu.moe/openclaw/openclaw:latest`<br>`APT_MIRROR=http://mirrors.byted.org`<br>`PIP_INDEX_URL=https://bytedpypi.byted.org/simple/`<br>`bash agent-runtimes/openclaw/build-image.sh` |
| 其它内网/自建源 | 把 `OPENCLAW_BASE_IMAGE` 指向已同步过 `ghcr.io/openclaw/openclaw` 的内网 registry；`APT_MIRROR` 指向布局与官方一致的 Debian 镜像（`<APT_MIRROR>/debian` + `<APT_MIRROR>/debian-security`）；`PIP_INDEX_URL` 指向 PEP 503 兼容的 simple 索引 |

要点说明：

- `OPENCLAW_BASE_IMAGE` 决定 `FROM` 这一步去哪里拉基础镜像。**Docker daemon 的 `registry-mirrors` 只代理 `docker.io`，不代理 `ghcr.io`**，所以仅在 daemon.json 里加加速器对 base image pull 无效，必须在这里显式切源。首次拉完后本地有缓存，后续 build 即便 `docker pull` 失败也会自动 fallback 到本地镜像（脚本里有 `|| echo WARN` 兜底）。
- `APT_MIRROR` 仅在构建期生效（用于 `apt-get install` 装系统包）。
- `PIP_INDEX_URL` 既影响构建期（uv / pip 装 self-evolving plugin 等），也写入运行期 ENV——容器内 plugin 装包时也走这里。
- 同时需要切换的还有仓库根 `requirements.txt` 顶部的 `--extra-index-url`（用于宿主机 conda 环境装 `bytedtos` 等内部包）；纯外网用户若不需要 `bytedtos` 可直接删那行。

验证镜像：

```bash
bash agent-runtimes/openclaw/verify-image.sh lift-openclaw-with-evolve:latest
```

## 环境变量

将 [`.env.docker.example`](.env.docker.example) 拷贝为仓库根目录的 `.env`：

本镜像构建时会 merge `config/plugins.fragment.json`、`config/gateway.fragment.json`、`config/agents.fragment.json`、`config/skills.fragment.json` 与 `config/models.fragment.json` 进容器内的 `openclaw.json`，并遵守仓库公共契约：[Agent 模型配置契约](../../docs/eval-flow.md#126-agent-模型配置契约lift--容器运行时)（能力在 fragment / 选用在 `.env` `MODEL_NAME`）。

- `WORK_OPENAI_API_KEY` —— **构建镜像时必填**；会注入到 `config/models.fragment.json` 的 `custom` provider `apiKey`
- `MODEL_NAME` —— **构建镜像时必填**，必须是 `custom/model_id` 格式（provider 前缀恒为 `custom`）。构建期斜杠后的 `model_id` 注入 `models.fragment.json` 的 `models[].id`，整串注入 `agents.fragment.json` 的 `primary` / `models` key；运行时 LIFT `agents add --model $MODEL_NAME` 使用同一串
- `LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY` —— 运行时需要
- `LANGFUSE_BASE_URL` —— 容器内使用 `http://host.docker.internal:3000`
- LIFT `ContainerSession` 会附加 `--add-host=host.docker.internal:host-gateway`（Linux 上 langfuse-tracer 上报必需）
- `LIFT_EVAL_RUN_TAG` —— 由 LIFT 设为 `run_id`；插件会写入 trace tags（与框架 pre-chat 的 `tags.run` 配对）

### 与 LIFT pre-chat 的 Langfuse 关联

每一轮对话会产生两条 Langfuse trace：宿主机侧的框架 **`emit_pre_chat_state`**（`work_agent` / `judge_agent`），以及容器内的 **`langfuse-tracer`**（`openclaw-plugin`）。后处理通过 **`session_id`**（与 `openclaw agent --session-id` 同值）将两者配对。

插件侧要求：同一份 Langfuse 项目 keys、trace name 为 `openclaw-plugin`、`openclaw.json` 中 `hooks.allowConversationAccess: true`，且 trace 上的 `sessionId` 必须等于 LIFT 的 `--session-id`。

完整的写入 / 拉取 / 配对契约见：[docs/eval-flow.md §12.5](../../docs/eval-flow.md#125-trace_backfill观测)。

<a id="token-5-fields"></a>
### Token 5 字段落库状态

全 5 字段（`input_fresh` / `cache_write` / `cache_read` / `output` / `reasoning`）齐。

**已修复的历史坑**：

1. **microtask 竞争**（agent 侧 accumulator 拿 0）
   - 根因：OpenClaw `runVoidHook` 内部把 handler 排到 microtask 队列。语法上
     `llm_output` 先 fire、`agent_end` 后 await；但 async 函数首次调用**立即 return
     pending Promise 并把 handler body 排入 microtask**，如果 agent_end handler 用
     `await` 让出，handler 反而先跑，accumulator 是空的。
   - 修法：`agent_end` handler 里让一个 macrotask 排空 microtask 队列：
     ```javascript
     // plugins/langfuse-tracer/index.js
     api.on('agent_end', async (event, ctx) => {
       await new Promise((resolve) => setImmediate(resolve));  // KEY FIX
       const accumulated = pendingUsage.get(key);
     });
     ```

2. **usage → usageDetails 承载错位**（Langfuse 存了但字段丢失）
   - 根因：Langfuse `/api/public/ingestion` 的 `usage` 字段**只识别
     `input / output / total / unit`**，Anthropic-style key 必须写在 sibling
     `usageDetails`，否则被静默丢弃。
   - 修法：`generation-create` body 同时写 `usage` + `usageDetails`，用
     `usageDetailsFromUsage()` 复制 5 字段。

**配置前提**：

- `openclaw.json` 中 `hooks.allowConversationAccess: true`（见
  [plugins.fragment.json](config/plugins.fragment.json)），否则 `llm_output` /
  `agent_end` hook 从未被订阅
- `REASONING_EFFORT=high` 需通过 `--thinking high` 传给 CLI，`chat_agent.py` 已 wire
- plugin log 落 host：`LANGFUSE_TRACER_LOG_FILE=/workspace/task/langfuse-tracer.log`
  （见 [session.py](../../src/lift/adapters/openclaw/session.py)），事后诊断必需

统一口径 / 跨 runtime 排障方法见
[skill/lift-integrate-agent-runtime/docs/token-observability.md](../../skill/lift-integrate-agent-runtime/docs/token-observability.md)。

### 任务级 extra skills

Benchmark 任务可在 `requirements.extra_skills_dir` 中提供额外 skill 目录。OpenClaw 容器会把该目录挂载到 `/workspace/task/skills`，容器启动时由 LIFT 把挂载内容复制进 `${OPENCLAW_STATE_DIR:-/root/.openclaw}/skills`，使得 warmup 阶段的 `docker commit` 能把任务 skill 一并打进 delta 镜像；而 bind mount 本身的 `/workspace/task/skills` 内容是不会被 `docker commit` 捕获的。

### 插件清单与 OpenClaw 版本兼容性

构建期 [install-heavy.sh](file:///root/workspace/agent_evolve_evaluation/agent-runtimes/openclaw/scripts/install-heavy.sh)（unzip / npm / pip / clone 等重活）+ [install-config.sh](file:///root/workspace/agent_evolve_evaluation/agent-runtimes/openclaw/scripts/install-config.sh)（fragment 渲染 + `openclaw plugins enable` / `mcp set` 等秒级配置）会装这三个插件（运行时 enable）：

| 插件 | 来源 | 运行时依赖 | 备注 |
|------|------|------------|------|
| `langfuse-tracer` | repo 内 `plugins/langfuse-tracer` | `LANGFUSE_*` env | LIFT trace 写入 |
| `self-evolving-plugin-pro` | repo 内 `plugins/self-evolving-plugin-pro-*.zip` | 仅 with-evolve 镜像装 | warmup→evolve 阶段 `openclaw learn review` |
| `firecrawl` (`@openclaw/firecrawl-plugin`) | npm（构建期 `openclaw plugins install` 拉取） | `FIRECRAWL_API_KEY` env | 提供 `web_search` / `scrape` / `browser` 工具 |
| `openspace`（MCP server） | 构建期 `git clone` OpenSpace 到 `/opt/OpenSpace` | 仅 `--with-openspace` 镜像装；独立 3.12 venv | 基于 MCP 的 skill hub：`execute_task` / `search_skills` / `cloud_browse_skills` 等；stdio `command: openspace-mcp` |

**注意 firecrawl 的来源会随 OpenClaw 大版本飘移**：

- 旧版本（≲ 2026.5.x）firecrawl 是 stock plugin（`/app/dist/extensions/firecrawl/`），脚本里 `openclaw plugins enable firecrawl` 就能直接生效。
- 当前版本（2026.6.10 起）firecrawl 已被剥离为外置 npm 包，必须显式 `openclaw plugins install @openclaw/firecrawl-plugin`，否则 gateway 启动会打 warning，`web_search` 工具调用会返回 `{"status":"error","error":"web_search is disabled or no provider is available."}`。

排查 `web_search` 不工作时，先 `docker run --rm <image> openclaw plugins list | grep -i firecrawl` 看是否 `enabled`；如果只看到 stock list 里没有 firecrawl，且 `~/.openclaw/extensions/`、`~/.openclaw/npm/projects/` 都没有，就是这一步漏装了。升级 base image 后如果 firecrawl 的分发方式又变（重新进 stock 或换包名），同步更新 [install-heavy.sh](file:///root/workspace/agent_evolve_evaluation/agent-runtimes/openclaw/scripts/install-heavy.sh) 第 3 步（`openclaw plugins install`）即可。

## Workspace 布局（镜像 FS vs bind mount）

OpenClaw 有意采用**两个**工作区根。agent 实际只看到一个（`/root/.openclaw/workspace`），但任务的输入/输出住在另一个（`/workspace/task`），让评测器可以从宿主机直接读写。**这套布局同时也是 OpenClaw 的默认进化机制载体** —— 见下面 *默认进化机制* 小节。

| 容器内路径 | 后端 | 是否被 `docker commit` 持久化 | 用途 |
|---|---|---|---|
| `/root/.openclaw/workspace/` | 镜像 FS（overlayfs） | ✅ 是 | agent 实际的 cwd。构建期由 `workspace_seed/` 注入（SOUL.md、USER.md、AGENTS.md、HEARTBEAT.md 等）。warmup 中 agent 写下的所有日常记忆 / SOUL 修改都落在这里，并随 `docker commit` 进入 delta 镜像。 |
| `/workspace/task/` | 宿主机 bind mount → `results/{run_id}/outcome/.../{phase}/{category}/` | ❌ 否（mount） | 每题的 IO 通道：评测器把 `qN_materials/` 送进来，agent 写 `result/result_qN/` 到这里供宿主机判分。 |
| `/workspace/task/skills/`（可选） | 宿主机 bind mount → benchmark 任务的 `extra_skills_dir` | ❌ 否（启动时被复制进 `/root/.openclaw/skills`，那份副本会被 commit 捕获） | 每题独立的 skill 包。 |

### 它们是怎么拼到一起的

`workspace_seed/` 在构建期被 `COPY` 进镜像内的 `/root/.openclaw/workspace`（见 [Dockerfile](file:///root/workspace/agent_evolve_evaluation/agent-runtimes/openclaw/Dockerfile)），同时 [agents.fragment.json](file:///root/workspace/agent_evolve_evaluation/agent-runtimes/openclaw/config/agents.fragment.json) 中的 `agents.defaults.workspace` 也指向同一路径，让 agent 运行起来的 cwd 就是镜像 FS。

容器启动时，LIFT 的 `_bridge_workspace` hook（见 [openclaw/session.py](file:///root/workspace/agent_evolve_evaluation/src/lift/adapters/openclaw/session.py)）会把 bind mount 上的 IO 表面以软链方式挂进 agent workspace。这样 benchmark query 里写 "打开 `qN_materials/foo.xlsx`" 或 "写入 `result/result_qN/`" 都能在 agent 的 cwd 下直接命中：

```
/root/.openclaw/workspace/
├── SOUL.md、USER.md、AGENTS.md、memory/、MEMORY.md、…   # baked seed + agent 自写的记忆
├── q1_materials/  →  /workspace/task/q1_materials/      # 软链到 bind mount
├── q2_materials/  →  /workspace/task/q2_materials/      # 软链到 bind mount
└── result/        →  /workspace/task/result/            # 软链：agent 写入直接落到宿主机
```

为什么要拆成两份：

- **`docker commit` 只会捕获镜像 FS，不会捕获 bind mount**。把 agent 的"家"放在镜像 FS 上，SOUL / 记忆的更新天然在 warmup → evolve → delta 这条链路里被保留，不用额外的 snapshot 机制。
- **评测产物必须能在宿主机上看到**。判分阶段直接读 `results/{run_id}/outcome/...`，所以 `result/` 被软链回 bind mount。
- **任务素材保持只读+宿主机所有**。`qN_materials/` 由宿主机在容器启动前 `cp -a` 进 bind mount，再由 hook 软链进来；既不重复复制进镜像，也不会有过期素材误进 delta 镜像。

### 两个相关的 adapter 参数 / 脚本

- `seed_workspace=True`（出现在 `start_container` / `start_openclaw_container`）：历史标志位，对 OpenClaw 已是 no-op（种子已 baked 进镜像）。保留参数仅为兼容 `group_memory` mixin 的签名。
- `_PREPARE_WORKSPACE_GIT_SCRIPT`（位于 [openclaw_with_evolve/evolve.py](file:///root/workspace/agent_evolve_evaluation/src/lift/adapters/openclaw_with_evolve/evolve.py)）：在 `openclaw learn review` 之前把 `/root/.openclaw/workspace` `git init` + 一次空 commit，因为 self-evolving-plugin-pro 的 `/instances/onboard` 要求 workspace_root 是带 HEAD 的 git repo。

## 默认进化机制

OpenClaw 有 3 个 runtime 变体,进化触发方式差异如下:

| Runtime | 触发方式 | `evolve_after_warmup` 行为 | 进化产物 |
|---|---|---|---|
| `-r openclaw` (baseline) | **被动隐式** | no-op | warmup 期 agent 在 `/root/.openclaw/workspace/`(SOUL.md / MEMORY.md / `memory/` / 日常记忆)与 `/root/.openclaw/skills/`(任务 skill 副本)自然写入,由 `docker commit` 捕获 |
| `-r openclaw_with_evolve` | **主动显式** | 调 self-evolving-plugin-pro 的 `openclaw learn review` HTTP 接口 | 同 baseline + `learn review` 结构化蒸馏出的 SOUL / SOP 更新 |
| `-r openclaw_with_openspace` | **被动隐式** | no-op | 同 baseline;OpenSpace MCP 只提供 skill hub,不改变 evolve 钩子 |

三者共享同一份 `evolve_paths` 白名单(`/root/.openclaw/workspace/memory`、
`/root/.openclaw/skill-workshop`,见 [`openclaw/adapter.py`](../../src/lift/adapters/openclaw/adapter.py) 的 `evolve_paths`)。
注意白名单是 **workspace 的 `memory` 子目录**而不是 workspace 整体——workspace 根目录
含 SOUL/IDENTITY 种子和 git 元数据(粒度太粗容易误计入进化产物),因此只对
`memory/` 与 `skill-workshop/` 做 delta preflight 的负向判定;`docker commit` 依然
捕获整个容器 FS,所以 workspace 根目录下 agent 写入的 SOUL / MEMORY 更新照样进 delta。

**跨 session / 跨任务共享**:warmup `parallel_single` 下同一容器承载所有 warmup 任务,
每题都是独立 LIFT session,但共享同一份 `/root/.openclaw/workspace/`(agent 的"家"),
SOUL / MEMORY 的更新在任务之间自然可见;warmup → holdout 之间通过 `docker commit`
把 workspace 与 skills 冻结进 delta 镜像。

`-r openclaw_with_evolve` 相比 baseline 多做的一步:warmup 结束后 adapter 会先 `git init +
空 commit` 保证 workspace 是合法 git repo,再调 `openclaw learn review` 让插件反思整个
warmup 阶段的对话与工具轨迹,把提炼结果写回 `/root/.openclaw/workspace/`,最后由基类
`materialize_delta` 做统一 commit。详见
[`openclaw_with_evolve/evolve.py`](../../src/lift/adapters/openclaw_with_evolve/evolve.py)。

## 与 LIFT 集成（`src`）

```bash
bash agent-runtimes/openclaw/build-image.sh
python -m src.cli.lift_main -r openclaw_with_evolve --benchmark_dir assets/benchmarks_demo --suite hello.json --warmup-only
python -m src.cli.lift_main -r openclaw_with_evolve --benchmark_dir assets/benchmarks_demo --suite hello.json
```

默认镜像：

- `-r openclaw` → `lift-openclaw-base:latest`（不带进化插件，常量 `OPENCLAW_BASE_DOCKER_IMAGE`）
- `-r openclaw_with_evolve` → `lift-openclaw-with-evolve:latest`（带 self-evolving-plugin-pro，常量 `OPENCLAW_WITH_EVOLVE_DOCKER_IMAGE`）
- `-r openclaw_with_openspace` → `lift-openclaw-with-openspace:latest`（带 OpenSpace MCP 插件，常量 `OPENCLAW_WITH_OPENSPACE_DOCKER_IMAGE`）
- `-r openclaw_with_agentmemory` → `lift-openclaw-with-agentmemory:latest`（带 agentmemory memory plugin，常量 `OPENCLAW_WITH_AGENTMEMORY_DOCKER_IMAGE`）
- `-r openclaw_with_agentmemory_active_evolve` → 复用同一个 `lift-openclaw-with-agentmemory:latest`；只在 host adapter 层加"主动进化"：warmup 容器点火 agentmemory 的 LLM provider（`WORK_OPENAI_*` + `MODEL_NAME` → `OPENAI_API_KEY`/`OPENAI_BASE_URL`/`OPENAI_MODEL`），warmup 后显式 POST `consolidate-pipeline{tier:all}` / `crystals/auto` 到 `:3111` 再 `docker commit`。**无需单独构建镜像**。

均定义于 `src/paths.py`。

## 实例生命周期（手工调试）

```bash
./agent-runtimes/openclaw/scripts/openclaw-instance.sh create --id run-a
eval "$(./agent-runtimes/openclaw/scripts/openclaw-instance.sh env run-a)"
./agent-runtimes/openclaw/scripts/openclaw-instance.sh destroy run-a
```

把 warmup work 容器 commit 成 delta 镜像：

```bash
./agent-runtimes/openclaw/scripts/openclaw-instance.sh commit run-a --tag lift-delta:my-run-r0-suite
```

## Compose（可选）

```bash
docker compose -f agent-runtimes/openclaw/compose.openclaw.yml up -d --build
```
