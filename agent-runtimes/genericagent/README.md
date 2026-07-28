# GenericAgent runtime image (`agent-runtimes/genericagent`)

LIFT 评测用的 [GenericAgent](https://github.com/lsdefine/GenericAgent) 镜像。
LIFT (`src`) 在每个 phase 的 work 容器内通过 **`docker exec … python /opt/GenericAgent/agentmain.py
--task <iodir>`** 调起 GA 完成一轮 chat；judge agent 运行在同镜像、同 workspace、同 load_state 的 sibling 容器中。

## Layout

```
agent-runtimes/genericagent/
├── .dockerignore
├── Dockerfile
├── build-image.sh
├── scripts/
│   ├── install-heavy.sh            # L2 重量层占位（GA 目前无耗时装配）
│   └── install-config.sh           # L4 轻量层：渲染 mykey.py + patch GA + overlay langfuse_tracing.py
├── mykey.py.template               # GA 配置模板（占位符由 scripts/install-config.sh sed 渲染）
├── langfuse_tracing_overlay.py     # 替换 GA 自带 plugins/langfuse_tracing.py，强制 LIFT trace 名/session/tag
├── workspace_seed/                 # （占位）holdout 容器启动前 copy 进 /workspace/task
└── README.md
```

## Build (recommended)

从仓库根：

```bash
bash agent-runtimes/genericagent/build-image.sh
```

默认产出 `lift-genericagent:latest`，对应 LIFT `-r genericagent`。

构建步骤：

1. `git clone https://github.com/lsdefine/GenericAgent.git /opt/GenericAgent`
2. 安装 GA 依赖 + langfuse Python SDK + 评测常用库（pandas / openpyxl / python-docx 等，与
   OpenClaw 镜像保持一致）
3. 通过 `scripts/install-config.sh` 把仓库根 `.env` 中的 `WORK_OPENAI_API_KEY` / `MODEL_NAME` /
   `LANGFUSE_*` 注入 `mykey.py`
4. 用 `langfuse_tracing_overlay.py` 覆盖 GA 自带 `plugins/langfuse_tracing.py`，确保
   trace name = `genericagent-plugin`，并把 LIFT session_id / run tag 写入 trace 根

### 内网/外网构建（APT 与 PyPI 镜像源）

与 OpenClaw 镜像一致，公网拉取不稳定时通过环境变量切换：

| 环境 | 命令 |
|------|------|
| 公网（默认） | `bash agent-runtimes/genericagent/build-image.sh` |
| 内网 | `APT_MIRROR=http://mirrors.byted.org PIP_INDEX_URL=https://bytedpypi.byted.org/simple/ bash agent-runtimes/genericagent/build-image.sh` |

### Override

| 变量 | 默认 | 说明 |
|------|------|------|
| `GENERICAGENT_IMAGE` | `lift-genericagent:latest` | 构建产物 tag |
| `GENERICAGENT_GIT_URL` | `https://github.com/lsdefine/GenericAgent.git` | clone 源 |
| `GENERICAGENT_GIT_REF` | `main` | git checkout 分支 / commit |

## Environment

镜像要求宿主仓库根 `.env` 提供：

- `WORK_OPENAI_API_KEY` — **build-time required**；通过 sed 烧入 `mykey.py`。运行期不消费。
- `GENERICAGENT_MODEL_NAME` — **build-time required**；GA 直连 work LLM，必须是 provider 真实
  endpoint id（如 `ep-20260529115331-9zxpm`），不是 OpenClaw 内部 gateway 的命名空间。
  优先级高于共享的 `MODEL_NAME`，避免污染 OpenClaw 镜像。
- `MODEL_NAME` — fallback；若没设 `GENERICAGENT_MODEL_NAME` 才生效。
- `WORK_OPENAI_BASE_URL` — 默认 `https://ark.cn-beijing.volces.com/api/v3`
- `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` — build-time 烧入 `mykey.py`
- `LANGFUSE_HOST` — 容器内默认 `http://host.docker.internal:3000`
- `LIFT_EVAL_RUN_TAG`, `LIFT_GA_SESSION_ID` — 由 LIFT adapter 在 `docker exec -e ...` 时
  注入（不在 build 期烧入），由 overlay 的 langfuse_tracing 在每条 trace 写入

> 与 OpenClaw 不同，GA 通过 Python 模块 `mykey.py` 拿配置，**不读环境变量**。
> 所以模型 / Langfuse 凭据是 build-time bake-in。运行期切 Key 时，需要 LIFT adapter
> 临时 bind-mount 替代 `mykey.py`。

## Langfuse correlation with LIFT pre-chat

每轮 chat 产生两条 trace：

- 框架 `emit_pre_chat_state` 在宿主机生成 `work_agent` / `judge_agent` span
- GA 容器内 `langfuse_tracing_overlay.py` 生成 `genericagent-plugin` trace

后处理通过 **session_id** 1:1 配对（即 GA 进程 `--task <iodir>` 时通过 LIFT 写入的
`LIFT_GA_SESSION_ID` env）。

完整契约：[docs/eval-flow.md §12.5](../../docs/eval-flow.md#125-trace_backfill观测)。

<a id="token-5-fields"></a>
### Token 5 字段落库状态

全 5 字段（`input_fresh` / `cache_write` / `cache_read` / `output` / `reasoning`）齐。

**已修复的历史坑**：overlay 只 wrap SSE parser 时 non-stream 路径丢字段。

- 早期版本 [`langfuse_tracing_overlay.py`](langfuse_tracing_overlay.py) 只
  `_wrap_parser(_parse_claude_sse)` / `_parse_openai_sse`，但 GA `llmcore.py`
  里有对称的 `_parse_claude_json` / `_parse_openai_json` 走 non-stream 路径
  （`sess.stream=False`），没被 wrap → `_tls._usage` 永远是 None →
  `gen.update(usage_details=None)` → Langfuse observation `usage_details={}`。
- **修法**：直接 wrap llmcore 内的**公共汇聚点** `_record_usage(usage, api_mode)`。
  该函数被 messages / chat_completions / responses 三种 api_mode 的 SSE + JSON
  parser **7 处**调用，wrap 它比 wrap 所有 parser 更根本。三个 api_mode 各自的
  reasoning / cache 字段位置：
  * `messages`：`cache_read_input_tokens` / `cache_creation_input_tokens` 顶层
  * `chat_completions`：`prompt_tokens_details.cached_tokens` /
    `completion_tokens_details.reasoning_tokens`
  * `responses`：`input_tokens_details.cached_tokens` /
    `output_tokens_details.reasoning_tokens`

**关键坑**：overlay 是构建时 `COPY` 进镜像的，修改后**必须 rebuild**
（`bash agent-runtimes/genericagent/build-image.sh`）才生效——运行期 bind
mount 覆盖不适用于 GA。

统一口径 / 跨 runtime 排障方法见
[skill/lift-integrate-agent-runtime/docs/token-observability.md](../../skill/lift-integrate-agent-runtime/docs/token-observability.md)。

## Task extra skills

Benchmark 任务可通过 `requirements.extra_skills_dir` 提供额外技能。LIFT 在容器启动时
把该目录挂到 `/workspace/task/skills`(与 OpenClaw 一致),GA workspace 在 `/workspace/task`
下可直接读取。

GA 自身没有 skills 装载机制,本能力主要供 GA 在工作目录里 `cat skills/*.md` 读取
benchmark 提供的辅助技能描述。

## 默认进化机制(baseline `-r genericagent`)

GenericAgent baseline 采用**被动隐式**进化——LIFT 不主动触发任何蒸馏/复盘,
[`GenericAgentAdapter.evolve_after_warmup`](../../src/lift/adapters/genericagent/adapter.py) 就是一个 `return None`。
真正的进化载体是 warmup 期间 GA 自己在容器 FS 层写入的 memory 文件,
warmup 结束后由 `ContainerAgentRuntimeAdapter.materialize_delta` 通过 `docker commit`
一并打进 delta 镜像;holdout evolved 阶段从这份镜像启动即为进化后状态。

### 进化产物路径

| 容器内路径 | 后端 | 是否被 `docker commit` 持久化 | 用途 |
|---|---|---|---|
| `/opt/GenericAgent/memory/global_mem.txt` | 镜像 FS | ✅ 是 | L2 长文本记忆,warmup 期 GA 按 `memory_management_sop.md` 主动写入 |
| `/opt/GenericAgent/memory/global_mem_insight.txt` | 镜像 FS | ✅ 是 | L1 精炼索引,启动时随 system prompt 拼进 `[Memory]` 段落供 LLM 读到 |
| `/opt/GenericAgent/memory/*.md` | 镜像 FS | ✅ 是 | L3 task-level SOP / 结构化知识片段 |
| `/opt/GenericAgent/temp/<iodir>/` | 镜像 FS | ✅ 是(但非"真进化产物") | LIFT 侧 I/O 通道(input.txt / output*.txt / reply.txt);GA process 副作用,声明在 `evolve_paths` 之外 |
| `/workspace/task/` | 宿主机 bind mount → `results/{run_id}/outcome/.../{phase}/{category}/` | ❌ 否(mount) | 每题的 IO 表面 — task materials / result 都在这里,**不进 delta** |

adapter 的 `evolve_paths` 白名单声明为 `("/opt/GenericAgent/memory",)`,仅作 delta preflight
"负向判定"(warmup 到底有没有产出),不影响 `docker commit` 实际捕获内容。

### 跨 session / 跨任务共享

Warmup 默认 `parallel_single`(同一 suite 所有 warmup 任务共享同一容器)。GA 每题会启动
一对**独立进程**(work / judge,各自独立 `iodir` + LIFT `session_id`),但**所有进程都读写同一份**
`/opt/GenericAgent/memory/`:

1. GA 引擎侧读 memory 用 `script_dir = /opt/GenericAgent` 拼绝对路径(不受进程 cwd 影响)。
2. system prompt 里的 `[Memory]` 段落也被 [`scripts/install-config.sh`](scripts/install-config.sh) patch 成
   `/opt/GenericAgent/memory` 绝对路径(**关键 patch**,详见下面 "*已知陷阱*")。
3. 因此前一题 GA 进程写入的 `global_mem.txt` / `global_mem_insight.txt`,后一题(不同 session、
   不同 iodir)的 GA 进程一启动就能读到——跨 session 传递经验的路径就是共享 FS。

Warmup → holdout 之间则通过 `docker commit` 把 `/opt/GenericAgent/memory/` 冻结进 delta 镜像,
holdout evolved 容器从这份镜像启动即继承 warmup 阶段积累的记忆。

### 已知陷阱:memory 路径三点错位(已修)

GA 上游默认让 LLM 用相对路径 `../memory/xxx` 写 memory,而 GA 进程 cwd 在
`/workspace/task`(bind mount);相对路径解析结果落到 bind mount 内,
`docker commit` 不会捕获 → 表面 warmup 成功,delta 镜像里 memory 空空如也 → evolve 失效。

[`scripts/install-config.sh`](scripts/install-config.sh) 在 build 期 patch 三处相对路径为绝对路径
`/opt/GenericAgent/memory`(引擎读、system prompt 提示、SOP 加载),让 LLM 写、GA 读、
`docker commit` 捕获三点对齐。这是 GA 集成时踩过的核心坑,新集成 runtime 时的通用规范
见 [skill/lift-integrate-agent-runtime/docs/evolve-artifact-contract.md](../../skill/lift-integrate-agent-runtime/docs/evolve-artifact-contract.md)。

## 主动进化变体(`-r genericagent_active_evolve`)

`genericagent_active_evolve` 复用同一镜像 `lift-genericagent:latest`,但在
[`adapter.py`](../../src/lift/adapters/genericagent_active_evolve/adapter.py) 中额外 override
两个钩子,叠加**主动复盘**层:

1. **`evolve_after_task`**:每题完成后在 warmup 容器内起一个独立 GA 进程(独立 `iodir` +
   独立 `session_id = reflect-task-*`)发 per-task 复盘 prompt,让 GA 按
   `memory_management_sop.md` 决定写哪一层记忆。
2. **`evolve_after_warmup`**:所有 warmup 完成后再起一个 GA 进程发 suite 级总复盘 prompt
   (`session_id = reflect-suite-*`),做全 suite 汇总反思。

两次复盘产物都落在 `/opt/GenericAgent/memory/`,被同一次 `docker commit` 带走。相当于
在 baseline 的运行时副作用累积之上,再强制 agent 停下来做一次显式反思。

## LIFT integration (`src`)

```bash
bash agent-runtimes/genericagent/build-image.sh
python -m src.cli.lift_main -r genericagent --benchmark_dir assets/benchmarks_demo \
    --suite hello.json --warmup-only
python -m src.cli.lift_main -r genericagent --benchmark_dir assets/benchmarks_demo \
    --suite hello.json
```

默认镜像:

- `-r genericagent` → `lift-genericagent:latest`(常量
  `GENERICAGENT_DOCKER_IMAGE`,定义于 [`src/paths.py`](../../src/paths.py))
- `-r genericagent_active_evolve` → 同一镜像,adapter 侧叠 reflection 钩子

## Manual sanity check

```bash
docker run --rm lift-genericagent:latest \
    python -c 'import sys; sys.path.insert(0, "/opt/GenericAgent"); import agentmain'
```
