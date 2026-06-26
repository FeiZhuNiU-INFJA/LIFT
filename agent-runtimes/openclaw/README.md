# OpenClaw agent（`agent-runtimes/openclaw`）

预装了 **self-evolving-plugin-pro** 与 **langfuse-tracer** 的 gateway 镜像。LIFT（`src`）通过在一次性的"每题独立容器"里执行 **`docker exec … openclaw`** 来驱动 agent。

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

默认产出 `evolve-eval-openclaw-base:latest`（不带 self-evolving-plugin-pro 进化插件，对应 LIFT `-r openclaw`；`workspace_seed/` 在构建期已 baked 进镜像内的 `/root/.openclaw/workspace`）。

带进化插件的变体：

```bash
bash agent-runtimes/openclaw/build-image.sh --with-evolve
# 产出 evolve-eval-openclaw-with-evolve:latest
```

种子文件直接位于**镜像内**的 `/root/.openclaw/workspace`，也是 agent 真正的工作目录；因此 warmup 期间 agent 生成的 SOUL / MEMORY / 日常记忆都会被 `docker commit` 一并打进 delta 镜像（详见下文 *Workspace 布局*）。

### 字节内网构建（推荐复制即用）

公网默认走 `ghcr.io/openclaw/openclaw:latest` + `deb.debian.org` + `https://pypi.org/simple/`，字节内网拉取会很慢甚至超时。下面这套环境变量就是字节内网构建的固定写法，Dockerfile 与脚本本身**不需要改动**：

```bash
# 字节内网：base 镜像
OPENCLAW_BASE_IMAGE=ghcr.milu.moe/openclaw/openclaw:latest \
APT_MIRROR=http://mirrors.byted.org \
PIP_INDEX_URL=https://bytedpypi.byted.org/simple/ \
  bash agent-runtimes/openclaw/build-image.sh
# → evolve-eval-openclaw-base:latest

# 字节内网：with-evolve 镜像
OPENCLAW_BASE_IMAGE=ghcr.milu.moe/openclaw/openclaw:latest \
APT_MIRROR=http://mirrors.byted.org \
PIP_INDEX_URL=https://bytedpypi.byted.org/simple/ \
  bash agent-runtimes/openclaw/build-image.sh --with-evolve
# → evolve-eval-openclaw-with-evolve:latest
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
bash agent-runtimes/openclaw/verify-image.sh evolve-eval-openclaw-with-evolve:latest
```

## 环境变量

将 [`.env.docker.example`](.env.docker.example) 拷贝为仓库根目录的 `.env`：

本镜像构建时会 merge `config/plugins.fragment.json`、`config/gateway.fragment.json`、`config/agents.fragment.json`、`config/skills.fragment.json` 与 `config/models.fragment.json` 进容器内的 `openclaw.json`，并遵守仓库公共契约：[Agent 模型配置契约](../../docs/eval-flow.md#126-agent-模型配置契约lift--容器运行时)（能力在 fragment / 选用在 `.env` `MODEL_NAME`）。

- `ARK_API_KEY` —— **构建镜像时必填**；会注入到 `config/models.fragment.json`
- `MODEL_NAME` —— LIFT `agents add --model` 使用的运行时模型 id（必须是 fragment 中已登记的 `provider/model_id`）
- `LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY` —— 运行时需要
- `LANGFUSE_BASE_URL` —— 容器内使用 `http://host.docker.internal:3000`
- LIFT `ContainerSession` 会附加 `--add-host=host.docker.internal:host-gateway`（Linux 上 langfuse-tracer 上报必需）
- `LIFT_EVAL_RUN_TAG` —— 由 LIFT 设为 `run_id`；插件会写入 trace tags（与框架 pre-chat 的 `tags.run` 配对）

### 与 LIFT pre-chat 的 Langfuse 关联

每一轮对话会产生两条 Langfuse trace：宿主机侧的框架 **`emit_pre_chat_state`**（`work_agent` / `judge_agent`），以及容器内的 **`langfuse-tracer`**（`openclaw-plugin`）。后处理通过 **`session_id`**（与 `openclaw agent --session-id` 同值）将两者配对。

插件侧要求：同一份 Langfuse 项目 keys、trace name 为 `openclaw-plugin`、`openclaw.json` 中 `hooks.allowConversationAccess: true`，且 trace 上的 `sessionId` 必须等于 LIFT 的 `--session-id`。

完整的写入 / 拉取 / 配对契约见：[docs/eval-flow.md §12.5](../../docs/eval-flow.md#125-trace_backfill观测)。

### 任务级 extra skills

Benchmark 任务可在 `requirements.extra_skills_dir` 中提供额外 skill 目录。OpenClaw 容器会把该目录挂载到 `/workspace/task/skills`，容器启动时由 LIFT 把挂载内容复制进 `${OPENCLAW_STATE_DIR:-/root/.openclaw}/skills`，使得 warmup 阶段的 `docker commit` 能把任务 skill 一并打进 delta 镜像；而 bind mount 本身的 `/workspace/task/skills` 内容是不会被 `docker commit` 捕获的。

### 插件清单与 OpenClaw 版本兼容性

构建期 [install-plugins-in-image.sh](file:///root/workspace/agent_evolve_evaluation/agent-runtimes/openclaw/install-plugins-in-image.sh) 会装这三个插件（运行时 enable）：

| 插件 | 来源 | 运行时依赖 | 备注 |
|------|------|------------|------|
| `langfuse-tracer` | repo 内 `plugins/langfuse-tracer` | `LANGFUSE_*` env | LIFT trace 写入 |
| `self-evolving-plugin-pro` | repo 内 `plugins/self-evolving-plugin-pro-*.zip` | 仅 with-evolve 镜像装 | warmup→evolve 阶段 `openclaw learn review` |
| `firecrawl` (`@openclaw/firecrawl-plugin`) | npm（构建期 `openclaw plugins install` 拉取） | `FIRECRAWL_API_KEY` env | 提供 `web_search` / `scrape` / `browser` 工具 |

**注意 firecrawl 的来源会随 OpenClaw 大版本飘移**：

- 旧版本（≲ 2026.5.x）firecrawl 是 stock plugin（`/app/dist/extensions/firecrawl/`），脚本里 `openclaw plugins enable firecrawl` 就能直接生效。
- 当前版本（2026.6.10 起）firecrawl 已被剥离为外置 npm 包，必须显式 `openclaw plugins install @openclaw/firecrawl-plugin`，否则 gateway 启动会打 warning，`web_search` 工具调用会返回 `{"status":"error","error":"web_search is disabled or no provider is available."}`。

排查 `web_search` 不工作时，先 `docker run --rm <image> openclaw plugins list | grep -i firecrawl` 看是否 `enabled`；如果只看到 stock list 里没有 firecrawl，且 `~/.openclaw/extensions/`、`~/.openclaw/npm/projects/` 都没有，就是这一步漏装了。升级 base image 后如果 firecrawl 的分发方式又变（重新进 stock 或换包名），同步更新 [install-plugins-in-image.sh](file:///root/workspace/agent_evolve_evaluation/agent-runtimes/openclaw/install-plugins-in-image.sh) 第 3 步即可。

## Workspace 布局（镜像 FS vs bind mount）

OpenClaw 有意采用**两个**工作区根。agent 实际只看到一个（`/root/.openclaw/workspace`），但任务的输入/输出住在另一个（`/workspace/task`），让评测器可以从宿主机直接读写。

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

## 与 LIFT 集成（`src`）

```bash
bash agent-runtimes/openclaw/build-image.sh
python -m src.cli.lift_main -r openclaw_with_evolve --benchmark_dir assets/benchmarks_demo --suite hello.json --warmup-only
python -m src.cli.lift_main -r openclaw_with_evolve --benchmark_dir assets/benchmarks_demo --suite hello.json
```

默认镜像：

- `-r openclaw` → `evolve-eval-openclaw-base:latest`（不带进化插件，常量 `OPENCLAW_BASE_DOCKER_IMAGE`）
- `-r openclaw_with_evolve` → `evolve-eval-openclaw-with-evolve:latest`（带 self-evolving-plugin-pro，常量 `OPENCLAW_WITH_EVOLVE_DOCKER_IMAGE`）

均定义于 `src/paths.py`。

## 实例生命周期（手工调试）

```bash
./agent-runtimes/openclaw/scripts/openclaw-instance.sh create --id run-a
eval "$(./agent-runtimes/openclaw/scripts/openclaw-instance.sh env run-a)"
./agent-runtimes/openclaw/scripts/openclaw-instance.sh destroy run-a
```

把 warmup 容器 commit 成 delta 镜像：

```bash
./agent-runtimes/openclaw/scripts/openclaw-instance.sh commit run-a --tag evolve-eval-delta:my-run-r0-suite
```

## Compose（可选）

```bash
docker compose -f agent-runtimes/openclaw/compose.openclaw.yml up -d --build
```
