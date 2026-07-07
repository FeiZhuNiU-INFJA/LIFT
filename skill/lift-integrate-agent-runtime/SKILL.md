---
name: "lift-integrate-agent-runtime"
description: "把一个新的 agent runtime（如 OpenClaw / GenericAgent）接入 LIFT 评测框架的端到端清单：镜像脚手架 + adapter 三件套 + 注册点 + 后处理 Literal 同步 + Langfuse trace 拼装 + 验收 checklist。在用户说\"集成 / 接入 / 添加新 agent runtime\"或\"-r <name>\"想新增可选项时调用。"
---

# LIFT: 集成新 Agent Runtime

把一个 agent（OpenClaw、GenericAgent、Hermes 之类）接入 LIFT 评测流水线，需要同步五个层面：**镜像脚手架** → **adapter 三件套** → **CLI 注册** → **后处理类型同步** → **Langfuse trace 拼装**。这份 skill 把两次成功集成（OpenClaw + GenericAgent）的所有 touchpoint 固化成清单，减少漏改。

> **原则**：先把 baseline 跑通（hello.json sanity → test_search.json benchmark），再考虑 `_with_evolve` / `_active_evolve` 之类衍生 runtime。衍生只是在 baseline adapter 上叠 `evolve_after_warmup` 钩子或镜像 tag。

> **两个反例警示**（在集成过程中主动验证，别只信"跑通了 hello.json"）：
> 1. **进化产物不进 delta 镜像**（§1.7）—— warmup 阶段 agent 写的 memory / skills 如果落进 bind mount，`docker commit` 不会捕获，evolved 与 baseline 完全一致，improvement 恒为 0。
> 2. **hello.json 走通 ≠ evolve 生效**（§6.5）—— 必须跑一个会让 agent 有话可记的复杂 suite 并做 Log × Langfuse × Layer 三层证据交叉验证。

---

## 0. 必备前置认知

- LIFT 走 **warmup → docker commit → holdout** 流水线：每个 runtime 都要能从 baseline 镜像 commit 出"演化过的"镜像（不演化也得 commit，让流水线统一）。
- LIFT 通过 **`AgentRuntimeAdapter` ABC + `ContainerAgentRuntimeAdapter` 模板方法** 接入容器化 agent；非容器型 runtime（如 Hermes 直连 OpenAI）走 `AgentRuntimeAdapter` 直接 override。
- Chat 协议是 `WorkerJudgerPair`（一次 task 一对独立 ChatAgent；work / judge 互不干扰），由 `worker_judger_factory` 在每题创建。
- Langfuse trace 拼装要求 plugin 侧 trace 的 **`name` 在 `LANGFUSE_PLUGIN_TRACE_NAMES` 白名单里**，且 trace 的 **`session_id` 与 LIFT 侧 `work-/judge-` session 对齐**。

参考样本：
- 容器型简单样板：[`src/lift/adapters/genericagent/`](file:///root/workspace/agent_evolve_evaluation/src/lift/adapters/genericagent)（无 gateway / 无 evolve 钩子，最干净）
- 容器型完整样板：[`src/lift/adapters/openclaw/`](file:///root/workspace/agent_evolve_evaluation/src/lift/adapters/openclaw)（有 HTTP gateway / readiness check / token）
- 镜像脚手架：[`agent-runtimes/genericagent/`](file:///root/workspace/agent_evolve_evaluation/agent-runtimes/genericagent)、[`agent-runtimes/openclaw/`](file:///root/workspace/agent_evolve_evaluation/agent-runtimes/openclaw)

---

## 1. 镜像脚手架（`agent-runtimes/<runtime>/`）

新建目录 `agent-runtimes/<runtime>/`，复制 GenericAgent 样板做减法或加项。

| 文件 | 必要性 | 作用 |
|---|---|---|
| `Dockerfile` | 必须 | 多阶段构建 agent 镜像；ENTRYPOINT 一般是 `tini` + `tail -f /dev/null`（容器空转等 docker exec） |
| `build-image.sh` | 必须 | 读 `.env` 获取 WORK_OPENAI / Langfuse / 第三方 secret，`--build-arg` 透传 |
| `install-in-image.sh` | 必须 | 镜像内执行：`sed` 渲染 `mykey.py.template` → `mykey.py`、覆盖 `langfuse_tracing_overlay.py`、patch 上游硬编码 |
| `mykey.py.template` | 必须 | 凭据模板，占位符 `__WORK_OPENAI_API_KEY__` 等由 `install-in-image.sh` 用 sed 渲染 |
| `langfuse_tracing_overlay.py` | 必须 | LIFT 自有 tracing overlay：强制 root span name = `<runtime>-plugin`、注入 `session_id` / tags |
| `workspace_seed/` | 可选 | 容器内 `/workspace/task` 初始内容（如 README、人设文件）；GA baseline 仅一个 README |
| `.dockerignore` | 推荐 | 屏蔽 `.git` / `temp/` 减小 build context |

### 1.1 `mykey.py.template` 占位符规范

```python
native_oai_config = {"name": "doubao", "apikey": "__WORK_OPENAI_API_KEY__", "apibase": "__WORK_OPENAI_BASE_URL__", "model": "__MODEL_NAME__", "api_mode": "openai-completions"}
langfuse_config = {"public_key": "__LANGFUSE_PUBLIC_KEY__", "secret_key": "__LANGFUSE_SECRET_KEY__", "host": "__LANGFUSE_HOST__"}
```

`install-in-image.sh` 里要：
1. `escape_sed` 转义所有 `__XXX__` 注入值（防 `/` 与换行污染 sed）。
2. 一条 `sed -e ... -e ...` 替换全部占位符。
3. 严禁把空字符串当成 valid secret 写进镜像 — 上层 `build-image.sh` 应预先 `${VAR:-}` fallback 成空，由 plugin 自身在运行期再做 "未配置" 校验（参考 [`firecrawl_plugin.py`](file:///root/workspace/agent_evolve_evaluation/agent-runtimes/genericagent/firecrawl_plugin.py)）。

> **`MODEL_NAME` 必须是 provider-native 标识**：GA / 任意直连 work LLM 的 runtime，`MODEL_NAME` 要是 provider 真实 endpoint id（形如 `ep-2025xxxx-xxxxx`），不是 OpenClaw gateway 的命名空间值。如果同一个 `.env` 同时给 OpenClaw / GA 用，建议在 `build-image.sh` 里走专属变量名（参考 GA 用 `GENERICAGENT_MODEL_NAME` 优先于共享 `MODEL_NAME`，见 [`build-image.sh:61`](file:///root/workspace/agent_evolve_evaluation/agent-runtimes/genericagent/build-image.sh#L61)），避免一改就同时污染另一个 runtime 的镜像。

### 1.2 三方同步：Dockerfile ARG/ENV ↔ build-image.sh `--build-arg` ↔ install-in-image.sh sed

新加一个凭据/开关变量必须**同时**改三个地方，少一处就静默失效：

| 位置 | 形式 | 例（FIRECRAWL_API_KEY） |
|---|---|---|
| `Dockerfile` | `ARG FOO=` + `ENV FOO=${FOO}` | [Dockerfile:96-103](file:///root/workspace/agent_evolve_evaluation/agent-runtimes/genericagent/Dockerfile#L96-L103) |
| `build-image.sh` | `FOO="${FOO:-}"` + `--build-arg "FOO=${FOO}"` | [build-image.sh:65,84](file:///root/workspace/agent_evolve_evaluation/agent-runtimes/genericagent/build-image.sh#L65) |
| `install-in-image.sh` | `escape_sed` + sed 替换占位符 | [install-in-image.sh:20,29](file:///root/workspace/agent_evolve_evaluation/agent-runtimes/genericagent/install-in-image.sh#L20) |

> 验证手段：build 完后 `docker run --rm <image> grep __ /opt/<runtime>/mykey.py` 应当 0 行。

### 1.3 `langfuse_tracing_overlay.py` 关键约束

每条 root span 必须满足：
- `name == "<runtime>-plugin"`（如 `"genericagent-plugin"` / `"openclaw-plugin"`）— 让 [`src/models.py:96-100`](file:///root/workspace/agent_evolve_evaluation/src/models.py#L96-L100) 的 `LANGFUSE_PLUGIN_TRACE_NAMES` 能匹配。
- `session_id = $LIFT_GA_SESSION_ID`（或 runtime 等价 env，每轮 chat 由 `docker exec -e` 注入）。
- `tags ⊇ {LIFT_EVAL_RUN_TAG, LIFT_<RUNTIME>_SESSION_ID}` — `langfuse_trace_merge` 既靠 tag 也靠 sid 做 work / judge 拼接，少一个就丢 trace。

> **Langfuse Python SDK v3 → v4 breaking change**：v3 时代常见的 `observation.update_trace(session_id=, tags=)` / `client.update_current_trace(...)` 在 SDK 4.x 上已经**全部移除**（`LangfuseAgent` / `Langfuse` 都没有这些方法），`hasattr` 检查会静默 False，导致 session_id / tags **永远写不到 trace 根** —— overlay 看上去工作正常，但 `langfuse_trace_stitch` 按 sid 找不到 plugin trace，dashboard tools 列空。
>
> 4.x 必须用上下文管理器：`from langfuse import propagate_attributes` + `_lf.start_as_current_observation(name=..., as_type='agent', ...)`。GA hook 是分散回调（`agent_before` / `agent_after` 不是 with 块），需要手动 `__enter__` / `__exit__` 配对，反序退出（先退 obs_cm 再退 attr_cm），用 thread-local 跨 hook 传递。参考 [`agent-runtimes/genericagent/langfuse_tracing_overlay.py`](file:///root/workspace/agent_evolve_evaluation/agent-runtimes/genericagent/langfuse_tracing_overlay.py)。
>
> 验证：容器内 `python -c "from langfuse import Langfuse; print(dir(Langfuse(...).start_as_current_observation(name='x')))"` 看不到 `update_trace` 即说明在 v4。再去 langfuse UI 看一条 trace 的 `Session` / `Tags` 列是否非空，是 → overlay 正确；空 → 还在用 v3 API。

### 1.4 patch 上游硬编码（如有）

GA 上游把 `Handler.cwd` 与 system prompt cwd 都硬编码成 `os.path.join(script_dir, 'temp')`，LIFT 把 task materials bind 到 `/workspace/task`，必须在 build 期 patch 上游源码（见 [`install-in-image.sh:51-85`](file:///root/workspace/agent_evolve_evaluation/agent-runtimes/genericagent/install-in-image.sh#L51-L85) 的 python in-place 替换）。换 runtime 时先 grep `script_dir` / `os.getcwd()` / `os.path.dirname(__file__)` 找类似硬编码。

### 1.5 工具 schema 中英双份（如果 runtime 用了 GA 那套 schema）

GA 风格 runtime 通过 `assets/tools_schema.json`（英文）+ `assets/tools_schema_cn.json`（中文）两套声明告知 LLM 可用工具。GA 在 [`agentmain.py:96-98`](file:///tmp/_ga_agentmain.py#L96-L98) 按模型名（`glm` / `minimax` / `kimi` 走 cn）切换。新加 plugin tool 时，**两套 schema 都要 append**，不然中文模型看不到工具。参考 [`install-in-image.sh:93-208`](file:///root/workspace/agent_evolve_evaluation/agent-runtimes/genericagent/install-in-image.sh#L93-L208) 的 idempotent append 实现（已存在则跳过，避免重复 append）。

### 1.6 字节内网 / GitHub 拉取受限时的构建环境变量

镜像构建期可能卡在三个地方，全部通过环境变量切镜像源解决；这些变量都被 `build-image.sh` 透传给 Dockerfile：

| 卡点 | 环境变量 | 字节内网值 / 推荐值 |
|---|---|---|
| `apt-get update` | `APT_MIRROR` | `http://mirrors.byted.org` |
| `pip install` / `uv pip install` | `PIP_INDEX_URL` | `https://bytedpypi.byted.org/simple/` |
| `git clone <agent 上游>` | `<RUNTIME>_GIT_URL`（每个 runtime 自己的 build-arg） | 用 `ghfast.top` 反代前缀 |

**GitHub 反代写法**（参考 [`build-image.sh:54`](file:///root/workspace/agent_evolve_evaluation/agent-runtimes/genericagent/build-image.sh#L54)）：

```bash
GIT_URL="${GENERICAGENT_GIT_URL:-https://ghfast.top/https://github.com/lsdefine/GenericAgent.git}"
```

注意是 `ghfast.top/` **前缀拼接** 完整 https URL，不是替换 host。新建 runtime 的 `build-image.sh` 默认值就这样写，公网环境用户不传变量也能跑（ghfast 公网可访问，只是慢一点），内网用户传专属变量覆盖即可。

**字节内网完整一行启动**：

```bash
APT_MIRROR=http://mirrors.byted.org \
PIP_INDEX_URL=https://bytedpypi.byted.org/simple/ \
  bash agent-runtimes/<runtime>/build-image.sh
```

`build-image.sh -h` 必须把这三个变量列在 `Override via env:` 区域（参考 [GA build-image.sh:25-38](file:///root/workspace/agent_evolve_evaluation/agent-runtimes/genericagent/build-image.sh#L25-L38)），方便后续接手者 `--help` 直接看到。

> **可选**：在 `build-image.sh` 里加"探测到 `mirrors.byted.org` 就自动默认 APT_MIRROR / PIP_INDEX_URL"逻辑（参考 [GA build-image.sh:25-35](file:///root/workspace/agent_evolve_evaluation/agent-runtimes/genericagent/build-image.sh#L25-L35)），免掉每次手敲 env；留一个 `LIFT_INTRANET_AUTODETECT=0` 兜底开关。

### 1.7 进化产物落地契约（Docker commit 陷阱） ⚠️

LIFT 的核心命题：**baseline 镜像 vs evolved 镜像的差异 = warmup 阶段 agent 学到的东西**。要成立必须让 evolve 产物（memory / skills / SOP）都落在 `docker commit` 能捕获的**容器根 FS 层**（`/opt/**`、`/root/**`、`/etc/**`）。**bind mount / named volume / tmpfs / 任何非本地 FS 挂载都不进 commit**。

**三点错位** —— 新 runtime 接入时**必须**同时保证下面三个位置一致，任何一处错位都会让 evolve 无声无息地失效：

| 位置 | 具体形态 | 陷阱 |
|---|---|---|
| **引擎读**：agent 上游代码读 memory / skills 的绝对路径 | GA `script_dir = /opt/GenericAgent`；OpenClaw `~/.openclaw/workspace` | 通常上游用 `os.path.dirname(__file__)` 或 `~` 拼绝对路径，天然在容器 FS 层 ✅ |
| **LLM 写**：LLM 通过 tool call（`file_write` / `bash`）实际写文件的路径 | 由 system prompt 里的 `cwd` 和 `[Memory]` 提示决定 | LLM 用 `memory/xxx` 相对路径 → 落在 cwd = `/workspace/task/memory` = **bind mount** ❌ |
| **docker commit 捕获**：容器 FS 层 | 由 Dockerfile 的 `mkdir -p /opt/<runtime>/memory` 决定 | 只有落到容器 FS 才能被 commit ✅ |

**验证清单**（新 runtime 必跑）：

```bash
# 1. 引擎侧读什么绝对路径？
docker run --rm <image> sh -c 'grep -nE "memory|skill|sop" /opt/<runtime>/<main>.py | head'

# 2. system prompt 告诉 LLM 什么路径？
docker run --rm <image> sh -c 'grep -nE "cwd\s*=|\[Memory\]|\.\./memory|\./memory" /opt/<runtime>/<main>.py'

# 3. Dockerfile 是否 mkdir 了这些绝对路径？
grep -nE "mkdir.*memory|mkdir.*skill" agent-runtimes/<runtime>/Dockerfile
```

三处路径**必须**指向同一个容器 FS 绝对路径（例如 `/opt/<runtime>/memory`）。如果 LLM 会看到相对路径（`memory/xxx` / `../memory/xxx`）且 cwd 在 bind mount 之内，就必须在 `install-in-image.sh` 里 patch 上游源码把提示改成绝对路径，同时（双保险）在 reflection prompt 里显式告诉 LLM"cwd 是 bind mount，只能用 `/opt/<runtime>/memory` 绝对路径"。

**历史案例**（GA memory patch）：
- GA 引擎读 `script_dir + 'memory/'` → 绝对路径 `/opt/GenericAgent/memory` ✅
- GA system prompt 告诉 LLM `cwd = /workspace/task (./)` + `[Memory] (../memory)` → LLM 解析为 `/workspace/memory`（不存在）或 `/workspace/task/memory`（bind mount）❌
- 引擎读的位置**永远拿不到** LLM 写的内容 → warmup 表面成功，delta 镜像里 `/opt/GenericAgent/memory` 空空如也 → evolved 与 baseline 无差异 → LIFT 数据毫无意义
- 修复：`install-in-image.sh` patch [ga.py:518,590,591](file:///root/workspace/agent_evolve_evaluation/agent-runtimes/genericagent/install-in-image.sh#L86-L108) 三处相对路径都改成 `/opt/GenericAgent/memory` 绝对路径；reflection prompt 也加 `_MEMORY_PATH_NOTE`。

> **验证方式**：首选看 pipeline 日志里 `Delta preflight diff` 两行（`commit_delta_image` 在 `docker commit` 前自动跑 `docker diff` 打摘要）：
>
> - `Delta preflight diff (full) [<container>]: +NA ~NC -ND ...` — upperdir 全集，含 pip / cache / temp 副作用
> - `Delta preflight diff (evolve-only) [<container>]: +NA ~NC -ND at /opt/<runtime>/memory` — 只统计 adapter `evolve_paths` 白名单目录（见 §2.1），evolve-only 计数为 0 时会直接打 WARNING
>
> 如果 `full` 显示 `no changes` 或 `evolve-only` 触发 WARNING 就是三点错位。也可以按 §6.5 "证据 C：Layer" 跑一个非 hello 的复杂 suite + `--warmup-only` 手动 diff delta 镜像。

---

## 2. Adapter 三件套（`src/lift/adapters/<runtime>/`）

容器型 runtime 一律继承 `ContainerAgentRuntimeAdapter`，需要四个文件：

```
src/lift/adapters/<runtime>/
├── __init__.py            # 通常空文件或仅 re-export adapter
├── adapter.py             # AgentRuntimeAdapter 子类
├── session.py             # start_<runtime>_container + workspace seed hooks
├── container_exec.py      # ContainerContext dataclass + exec wrapper
└── chat_agent.py          # ChatAgent + WorkerJudgerPairFactory
```

### 2.1 `adapter.py`（必须 override 的 4 个方法）

参考 [`src/lift/adapters/genericagent/adapter.py`](file:///root/workspace/agent_evolve_evaluation/src/lift/adapters/genericagent/adapter.py)：

| Method | 作用 | 最简实现要点 |
|---|---|---|
| `resolve_docker_image` | 从 `RunOptions.docker_image` override → `<RUNTIME>_DOCKER_IMAGE` 默认 | 一行 `return override or DEFAULT` |
| `start_container` | 委托给 `start_<runtime>_container`（在 `session.py`） | 透传 `instance_id` / `image` / `ctx` / `workspace_dir` 等参数 |
| `worker_judger_factory` | 把 `ContainerSession` 包成 `WorkerJudgerPairFactory` | `return <Runtime>WorkerJudgerPairFactory(container=..., workspace_dir=...)` |
| `evolve_after_warmup` | 演化钩子；baseline runtime 是 no-op | `return None` |

另外**强烈建议**声明一个类属性 —— `evolve_paths`（默认继承 `ContainerAgentRuntimeAdapter.evolve_paths = ()`）：

| 类属性 | 作用 | 最简声明 |
|---|---|---|
| `evolve_paths: tuple[str, ...]` | 声明本 runtime "真进化产物"落地的**容器内绝对路径**白名单，供 `commit_delta_image` 在 `docker diff` 后单独打一行 `evolve-only` 摘要；计数为 0 时 WARNING（负向信号） | GA: `("/opt/GenericAgent/memory",)`；OpenClaw: `("/root/.openclaw/memory", "/root/.openclaw/skills")` |

漏声明的后果：pipeline 日志只有 `full` 摘要（含 pip / cache / temp 等噪声），无法在无人值守下自动预警"warmup 没写出任何进化产物"。参考 [`GenericAgentAdapter.evolve_paths`](file:///root/workspace/agent_evolve_evaluation/src/lift/adapters/genericagent/adapter.py#L38-L42)、[`OpenClawAdapter.evolve_paths`](file:///root/workspace/agent_evolve_evaluation/src/lift/adapters/openclaw/adapter.py#L36-L42) 的定义。声明的路径应与 §1.7 "三点错位"里的**引擎读路径**一致（引擎去哪读，就在哪声明）。

### 2.2 `session.py`（容器启动）

模板：[`src/lift/adapters/genericagent/session.py`](file:///root/workspace/agent_evolve_evaluation/src/lift/adapters/genericagent/session.py)

必须做的：
1. `_CONTAINER_PREFIX = "evolve-<runtime>"` — 容器名前缀，方便 `docker ps` / cleanup grep。
2. `start_<runtime>_container` 调 `ContainerSession.start`，传 `port_mappings` / `env_vars` / `volume_binds` / `readiness_check` / `post_start_hooks` / `pre_cleanup_hooks`。
3. `default_volume_binds` + `task_volume_binds` 是标准 bind（`/workspace/outcome`、`/workspace/task`、`/workspace/benchmarks`），照抄即可。
4. `seed_eval_workspace`：宿主机端把 `workspace_seed/` 拷进 `workspace_dir`，留 `.lift-workspace-ready` marker。
5. `_reclaim_volume_ownership`：cleanup 前把 bind mount 目录 `chown` 回宿主用户，避免 root-owned 文件污染 `results/`。
6. **`env_vars` 覆写 `LANGFUSE_BASE_URL` / `LANGFUSE_HOST`**：宿主 `.env` 里通常写的是 `LANGFUSE_BASE_URL=http://localhost:3888`（宿主视角），通过 `env_file=Path.cwd()/".env"` 全量注入容器后，容器内 `localhost` 指向自己不通宿主 Langfuse。**Langfuse SDK v4 的 OTel span exporter 会读 `LANGFUSE_BASE_URL` env 覆盖 `Langfuse(host=...)` 显式构造参数**，即使 overlay 里 `mykey.py` host 写对了也会 0 plugin trace。修法：在 `env_vars` 层（`-e` flag，优先级高于 `env_file`）把 loopback host 段改写为 `host.docker.internal`。参考 GA [`session.py:38-53,179-184`](file:///root/workspace/agent_evolve_evaluation/src/lift/adapters/genericagent/session.py#L38-L53) 的 `_rewrite_langfuse_host_for_container` 辅助函数。

> **gateway-less runtime**（GA 这种）：`port_mappings=[]`、`readiness_check=None`，`docker exec` 直接调起进程；
> **gateway-ful runtime**（OpenClaw 这种）：`port_mappings=[(host_port, 80, "tcp")]`、`readiness_check=ReadinessCheck(...)` + token / cookie 透传。

### 2.3 `container_exec.py`（exec 上下文）

定义 `<Runtime>ContainerContext` dataclass，最简只含 `container_name: str`（GA 样板）；如果有 gateway / token / 端口要带，就再加字段。提供 `exec_<runtime>_async` 包一层 `docker_exec_async`。

### 2.4 `chat_agent.py`（ChatAgent 协议）

**核心**：实现 `ChatAgent.chat(message, *, session_id) -> str`。

两种主流 transport：
- **HTTP gateway**（OpenClaw）：post 到容器内 gateway，response 直接拿 turn 输出。
- **文件 I/O**（GA）：写 `input.txt` → `docker exec -d` 启子进程 → 轮询 `output<N>.txt` 直到 `[ROUND END]`。GA 样板见 [`chat_agent.py:39-189`](file:///root/workspace/agent_evolve_evaluation/src/lift/adapters/genericagent/chat_agent.py#L39-L189)。

**统一约束**：
- `WorkerJudgerPairFactory.__call__(task)` 每次 **新建 work / judge 各一个 ChatAgent 实例**，互相隔离。
- `work_session_id = f"user-{short_id()}"`、`judge_session_id = f"judge-{short_id()}"` — 这俩前缀被 `langfuse_trace_stitch` 当作分类信号，**不要改前缀**。
- 单轮 wall-clock 上限统一 1000s（`CHAT_EXEC_TIMEOUT_SECONDS = 1000.0`），超时返回 `CHAT_EXEC_TIMEOUT_MARKER` 前缀字符串走 LIFT 的 provider error 重试通道。

---

## 3. 注册到 CLI（`registry.py` + `paths.py`）

### 3.1 `src/lift/adapters/registry.py`

[`registry.py`](file:///root/workspace/agent_evolve_evaluation/src/lift/adapters/registry.py) 两处改：

1. `SUPPORTED_RUNTIMES` 元组加新 runtime 名（小写下划线，CLI `-r` 直传值）。
2. `create_adapter` 加一条 `if normalized == "<runtime>": from ... import ...; return XAdapter(options)`。

> Lazy import 故意保留 — 不要在文件顶部 import adapter，免得用户没有该 runtime 依赖时整个 CLI 崩。

### 3.2 `src/paths.py`

[`paths.py`](file:///root/workspace/agent_evolve_evaluation/src/paths.py) 加常量：

```python
<RUNTIME>_AGENT_DIR = PROJECT_ROOT / "agent-runtimes" / "<runtime>"
<RUNTIME>_DOCKER_IMAGE = "evolve-eval-<runtime>:latest"
<RUNTIME>_WORKSPACE_SEED_DIR = <RUNTIME>_AGENT_DIR / "workspace_seed"
```

衍生 runtime（如 `_with_evolve`）通常只多一条 `<RUNTIME>_WITH_EVOLVE_DOCKER_IMAGE`，目录沿用基础版。

---

## 4. 后处理类型同步（5 处 `AgentSource` Literal）

**这是最容易漏改的环节**。`AgentSource` Literal 散落在 5 个文件，必须保持一致：

| 文件 | 行号 | 当前值 |
|---|---|---|
| [`src/postprocess/extract.py`](file:///root/workspace/agent_evolve_evaluation/src/postprocess/extract.py#L15) | 15 | `Literal["openclaw", "openclaw_with_evolve", "hermes", "genericagent", "genericagent_active_evolve"]` |
| [`src/postprocess/run_post_process.py`](file:///root/workspace/agent_evolve_evaluation/src/postprocess/run_post_process.py#L36) | 36 | 同上 |
| [`src/postprocess/trace_backfill.py`](file:///root/workspace/agent_evolve_evaluation/src/postprocess/trace_backfill.py#L18) | 18 | 同上 |
| [`src/postprocess/report_html.py`](file:///root/workspace/agent_evolve_evaluation/src/postprocess/report_html.py#L19) | 19 | 同上 |
| [`src/report/langfuse_trace_stitch.py`](file:///root/workspace/agent_evolve_evaluation/src/report/langfuse_trace_stitch.py#L24) | 24 | `Literal["openclaw", "openclaw_with_evolve", "hermes", "genericagent"]` ⚠️ **历史漏同步** |

**操作**：用 `Grep "AgentSource\s*=\s*Literal"` 在 `src/` 下扫一遍，每处都把新 runtime 名加进去。

> **已知 hazard**：`langfuse_trace_stitch.py:24` 的 Literal 落了 `genericagent_active_evolve`，但 dispatch tuple [第 222 行](file:///root/workspace/agent_evolve_evaluation/src/report/langfuse_trace_stitch.py#L222) 又包含了。这是历史遗留 — 新加 runtime 时务必 5 处都同步，避免静态类型检查告警。

---

## 5. Langfuse Trace 拼装（`models.py` + `langfuse_trace_stitch.py`）

### 5.1 `src/models.py:96-100` 加 trace name

[`LANGFUSE_PLUGIN_TRACE_NAMES`](file:///root/workspace/agent_evolve_evaluation/src/models.py#L96-L100) 元组加 `"<runtime>-plugin"`：

```python
LANGFUSE_PLUGIN_TRACE_NAMES: tuple[str, ...] = (
    "openclaw-plugin",
    "Hermes turn",
    "genericagent-plugin",
    "<runtime>-plugin",  # 新加
)
```

这个元组与 `langfuse_tracing_overlay.py:_LIFT_TRACE_NAME` 是一对镜像 — overlay 写入的 trace name 必须出现在元组里，否则 [`langfuse_trace_parse.py:15`](file:///root/workspace/agent_evolve_evaluation/src/report/langfuse_trace_parse.py#L15) `is_plugin_trace` 会丢掉 trace。

### 5.2 `src/report/langfuse_trace_stitch.py` 加 dispatch

`stitch_phase_langfuse_traces` 末尾按 `agent_source` 选 `_stitch_openclaw` / `_stitch_hermes`：
- 走 **OpenClaw 拼装**（基于 `session_id` 直接 list trace）：runtime 输出 trace 已经写了 `session_id` = `user-*` / `judge-*` → 加进 `if agent_source in ("openclaw", ..., "<runtime>"): return _stitch_openclaw(...)`。GA 走这条。
- 走 **Hermes 拼装**（基于 `eval_run_tag` + 后置匹配 sid in tags）：runtime 自身 SDK 不支持设 session_id，只能写 tag → 走 `_stitch_hermes`。

> **判断标准**：你的 `langfuse_tracing_overlay.py` 是否能在 root span 上设 `session_id`。能 → OpenClaw 路线（推荐，简单）；不能 → Hermes 路线。

### 5.3 dashboard tools 列：tool_calls 通用兜底机制

dashboard 的 **tools 列**读 [`PhaseRun.tool_calls`](file:///root/workspace/agent_evolve_evaluation/src/models.py)（int / None）。这个字段有两条数据源，按下列优先级填：

| 优先级 | 数据源 | 触发位置 | 适用 runtime |
|---|---|---|---|
| 1（精确） | adapter override `count_tool_calls(env, task, result, ctx)` | holdout 题末 [`base.py:317`](file:///root/workspace/agent_evolve_evaluation/src/lift/adapters/base.py#L317) | OpenClaw（容器内 docker exec 读 `trajectory.jsonl`） |
| 2（兜底） | Langfuse `type=TOOL` observation 数 | 后处理 [`trace_backfill.py:55-58`](file:///root/workspace/agent_evolve_evaluation/src/postprocess/trace_backfill.py#L55-L58) | 任何 runtime（只要 overlay 给每次工具调用挂 `as_type='tool'` span） |

**兜底链路**（runtime-agnostic）：

```
runtime overlay 每次 tool 调用 → langfuse `as_type='tool'` span (type=TOOL)
  → langfuse_trace_fetch.count_tool_observations(detail)         # 数 type=TOOL
  → LangfuseTraceRef.tool_observation_count                      # 写入 plugin trace ref
  → langfuse_trace_merge 把字段从 plugin ref 搬运到 agent ref     # _orphan_plugin_ref + merge_plugin_into_agent
  → LangfuseTokenToolStats.tool_observation_count (work-analytics 全局聚合)
  → trace_backfill.backfill_phase: phase.tool_calls=None 时填上   # 不覆盖 OpenClaw 已有的精确值
  → run_post_process.build_phase_tool_calls_from_report          # 从 backfilled JSON 抽 (r,s,task,phase)→tool_calls
  → tracker.set_phase_tool_calls(bundle)                         # 回写到 RunStateTracker，供静态 dashboard 渲染
  → dashboard tools 列显示
```

**接入新 runtime 时不需要做任何额外工作** —— 只要 `langfuse_tracing_overlay.py` 在每次工具调用 / 每个 plugin 子操作上挂了 `as_type='tool'` 的 span，dashboard 就自动有数。如果你的 runtime 能像 OpenClaw 那样从容器内拿到精确轮次（`trajectory.jsonl` 之类），可以 override `count_tool_calls` 拿到比 observation count 更稳的值；不 override 也不会显示空 — 兜底链路接住。

> **GA 注意**：GA 的 plugin 函数都是 generator（`def do_xxx(self, args, response): yield ...; return StepOutcome(...)`），如果你给 plugin 包了 langfuse decorator 但忘了 `as_type='tool'`，observation 会落到 `type=DEFAULT`，count 仍是 0。验证手段：langfuse UI 上挑一条 `genericagent-plugin` trace，展开 observation 列表，看每次 tool 调用是不是 `tool` 类型。

#### 5.3.1 dashboard 实时 vs 静态导出 —— 为什么要回写 tracker

dashboard 实际有**两种形态**，两个都吃同一棵 `RunStateTracker.snapshot()` 状态树：

| 形态 | URL / 文件 | 数据来源 | 何时生效 |
|---|---|---|---|
| 运行期实时 | `http://<host>:<port>` | tracker `/snapshot` + SSE，跟着事件总线刷新 | run 进行中；run 结束 HTTP server 关停 |
| 静态导出 | `results/<run_id>/dashboard.html` | `build_static_dashboard_html(tracker.snapshot())` 把当前 snapshot 序列化嵌入 HTML | run 结束 + 后处理跑完，[`lift_main.py:347`](file:///root/workspace/agent_evolve_evaluation/src/cli/lift_main.py#L347) 自动导出 |

**关键事实**：A 路径（adapter `count_tool_calls`）是**运行期 phase 结束时**通过 `StageEvent(tool_calls=N)` 实时落 tracker；B 路径（langfuse 兜底）是**后处理阶段**才能拿到值，必须显式回写 tracker，否则：

- backfilled JSON 里有 `tool_calls=N` ✅
- 但 tracker.snapshot() 里 phase node 仍是 `tool_calls: None`
- 静态 dashboard 嵌入的 snapshot tools 列 → 显示 "—"

**回写位点**：[`run_post_process.py`](file:///root/workspace/agent_evolve_evaluation/src/postprocess/run_post_process.py) 在写完 backfilled JSON 后调 `build_phase_tool_calls_from_report` 抽出 `(repeat, suite, task, phase) → tool_calls` bundle，再调 `tracker.set_phase_tool_calls(bundle)`。这套机制是 runtime-agnostic 的：

- OpenClaw（A 路径）：运行期 tracker 已有精确值，回写值通常等于运行期值（noop 但无害）
- GA / Hermes / 任何走兜底的 runtime：回写让静态 dashboard 第一次看到数

> **对运行期实时 dashboard 的影响**：B 路径 runtime 的 tools 列在 run 进行中**注定显示空**——langfuse 是 async upload，trace 还没完整 flush，count 不出来；要看 tools 必须等后处理跑完打开静态 dashboard。如果新 runtime 想要实时 tools，得自己 override `count_tool_calls`（容器内累加 counter / 读日志文件）。

---

## 6. 验收 Checklist

按顺序跑，前一步过了再下一步。

### 6.0 推荐的本地测试工作流

LIFT 一次跑下来时间偏长（warmup + commit + holdout 串起来），推荐 nohup 后台启 + dashboard + tail 日志的组合：

```bash
nohup python -m src.cli.lift_main \
  -r <runtime> \
  --benchmark_dir assets/benchmarks_demo \
  --suite hello.json \
  --run_id <run_id> \
  --dashboard 0.0.0.0:<port> \
  > logs/<run_id>.log 2>&1 &

tail -f logs/<run_id>.log               # 主进度看这里
# 浏览器开 http://<host>:<port>         # 看 run / repeat / suite / task / phase 结构化状态
```

> **不要用默认 `nohup.out`**：默认行为会把所有 run 的日志拼在同一个 `nohup.out` 里（`>>` append），多 run 并行 / 反复跑会互相覆盖、相互污染，事后排查时分不清谁是谁。统一显式写到 `logs/<run_id>.log`（先 `mkdir -p logs`），一个 run 一份日志，文件名直接对应 `results/lift-runid-<run_id>/`。

> `assets/benchmarks_demo/` 里两个常用 sanity suite：
> - **`hello.json`** — 1 W + 1 H 寒暄（Q1 "回复你好" / Q2 "自我介绍"），测的是基本 chat / warmup-commit-holdout 流水线连通性。
> - **`test_search.json`** — 1 W + 1 H 联网题（W1 查 2026 北京展会、H1 查 Node.js LTS 版本），测的是 agent 的联网工具是否生效。如果 runtime 没接联网工具，H1 会失败但流水线本身仍然走完。

> `--dashboard 0.0.0.0:<port>` 远程机器开 dashboard 必须 0.0.0.0；只在本机调试用 PORT 单字段（默认绑 127.0.0.1）即可。

### 6.1 镜像构建

```bash
cd agent-runtimes/<runtime> && bash build-image.sh
docker images | grep evolve-eval-<runtime>
```

构建期检查：
- `mykey.py` 占位符全部被替换：`docker run --rm <image> grep __ /opt/<runtime>/mykey.py` 应当 0 行
- `langfuse_tracing_overlay.py` 已覆盖到镜像内：`docker run --rm <image> head /opt/<runtime>/plugins/langfuse_tracing.py`
- 上游 patch 已生效：`docker run --rm <image> grep -n /workspace/task /opt/<runtime>/<patched>.py`
- 运行期 import smoke：`docker run --rm <image> python -c 'import sys; sys.path.insert(0, "/opt/<runtime>"); import agentmain'`

### 6.2 hello.json sanity（基本流水线）

按 §6.0 模板跑，`--suite hello.json`。

验证点:
- 容器拉起、warmup 单题跑完、`docker commit` 成功、holdout 跑完
- `results/lift-runid-<run_id>/report.json` 存在且 task `outcome.success: true`
- `logs/<run_id>.log` 没有 `wait output timeout` / `Cannot connect to Docker daemon` / `Judge response is not valid JSON` 高频重试

### 6.3 Trace stitching 对齐

run 完后默认会自动跑后处理；想单独重跑：

```bash
python -m src.cli.lift_main -r <runtime> --evaluate-only --run_id <run_id>
```

验证点：
- `results/lift-runid-<run_id>/*_backfilled.json` 中每题都拼到 `work_agent_traces` / `judge_agent_traces`（数量与 `report.json` 的 turn 数对齐）
- 没有 "trace not found" 告警
- `results/lift-runid-<run_id>/dashboard.html` 同步刷新（mtime 更新；`tool_calls` 列填上 langfuse 兜底统计的非 null 值；含 final_summary 表格）

> `--evaluate-only` 始终把 `report.json` 反向 replay 成事件总线广播（`emit_run_plan` /
> `emit_suite_plan` / `emit_stage`）重建 tracker 骨架（repeat × suite × task ×
> phase + score / success / turns / tool_calls / status），后处理跑完后用同一个
> tracker 重导静态 dashboard，所以不依赖 `--dashboard`。

### 6.4 test_search.json 联网能力 sanity（可选）

按 §6.0 模板跑，`--suite test_search.json`；日志开 `grep -E 'firecrawl|search|scrape|Action'`。如果 runtime 配了联网工具（如 firecrawl），应当看到 W1 / H1 调用搜索工具拿到当日数据；没接联网工具的 runtime 这步可以跳过。

### 6.5 三层证据交叉验证（必跑；hello.json 只能证连通，不能证 evolve）

要证明 evolve 有效必须做 **Log × Langfuse × Layer** 三层交叉验证——三个证据缺一不可,证明的不是同一件事。选一个**会让 agent 有话可记**的 suite（`test_search.json` 或自己拼含明确 memory 写入指令的 warmup 题）。

#### 证据 A：Log —— agent 真的对话了吗？

验证 work agent / judge agent 是否都跑了、reflection 钩子（如 `evolve_after_task` / `evolve_after_warmup`）是否触发。

```bash
LOG=logs/<run_id>.log

# work / judge chat 次数（每题至少一对，评测多轮会更多）
grep -cE "work-agent chat start|user-[0-9a-f]+ session" "$LOG"
grep -cE "judge-agent chat start|judge-[0-9a-f]+ session" "$LOG"

# reflection 钩子触发（active_evolve variant）
grep -E "\[active_evolve\] reflection chat" "$LOG"

# reflection 回复 head —— 全 DONE 说明 LLM 没写东西；有具体内容说明真触发写入
grep "reply_head=" "$LOG"

# 高频错误信号
grep -E "wait output timeout|Cannot connect to Docker|Judge response is not valid JSON" "$LOG" | head
```

**红旗**：所有 reflection `reply_head='DONE\\n'` 且证据 C 里 delta diff 也空 → suite 太简单，换一个更复杂的 suite 再验证。

#### 证据 A'：内容审阅 —— 光"发生了"不够,还得"内容合理" ⚠️

计数通过（chat 次数、trace 数量、delta 有文件）**不代表内容对**。新 runtime 首次跑通后**必须**至少肉眼扫一遍下面这几层内容,否则会踩到"流水线全绿但 agent 什么都没做对"的假阳性。

**A'.1 Material 可读性哨兵**（新 runtime bind mount / workspace seed 路径最常见踩坑点）

```bash
LOG=logs/<run_id>.log

# 文件系统层面报错(work / judge 尝试 open material 失败)
grep -iE "no such file|permission denied|cannot read|读取失败|open .* failed|q[0-9]+_materials.*not found|材料.*不存在" "$LOG"

# 模型自身"逃避语"(LLM 明说看不到附件 → 通常也是路径挂错,只是没冒 IO 异常)
grep -iE "cannot access|don't have access|no attachment|I cannot see|I do not see any" "$LOG"
```

任何一条命中 → `session.py` 的 `task_volume_binds` / `workspace_seed` / 上游 cwd patch 三者有一处错位,回 §1.7 + §2.2 排查。

**A'.2 Work / Judge response 抽样(不看数量,看长度和"味道")**

打开 `results/lift-runid-<run_id>/dashboard.html`,随手点开 1~2 个 phase 的对话弹窗,或直接从 `*_backfilled.json` 抽:

```bash
JSON=results/lift-runid-<run_id>/lift-runid-<run_id>_backfilled.json
python -c "
import json
r = json.load(open('$JSON'))
for rp in r['runs']:
    for s in rp['suites']:
        for t in s['tasks']:
            for ph_name in ('baseline','evolved'):
                ph = t[ph_name]
                outc = (ph.get('outcome') or {})
                content = (outc.get('content') or '')[:200]
                turns = ph.get('turns') or 0
                score = outc.get('content_score')
                print(f'  {t[\"name\"]:6} {ph_name:8} turns={turns} score={score}')
                print(f'    head: {content!r}')
"
```

看三件事:
- `content` 长度 **> 100 chars** 且不含 `Traceback` / `Error:` / `I cannot` / `I do not have` 逃避语
- turns > 0,且随任务复杂度合理增长(hello 类 1~2 轮,复杂检索 3~10 轮)
- 至少有一部分题 baseline 与 evolved 的 content 有可见差异(否则 evolve 大概率没生效,回证据 C)

**A'.3 Judge 分数分布**

```bash
python -c "
import json,collections
r = json.load(open('$JSON'))
buckets = collections.Counter()
for rp in r['runs']:
    for s in rp['suites']:
        for t in s['tasks']:
            for ph_name in ('baseline','evolved'):
                sc = (t[ph_name].get('outcome') or {}).get('content_score')
                if sc is None: buckets['none'] += 1
                elif sc <= 0.05: buckets['0'] += 1
                elif sc >= 0.95: buckets['1'] += 1
                else: buckets['mid'] += 1
print(dict(buckets))
"
```

- **全 0**:通常是 material 都没读到、judge 直接判 fail;或 judge prompt 没渲染任务描述。回 A'.1 / A'.2。
- **全 1**:通常是 judge prompt 里 rubric 塌了(如任务描述被 truncate),judge 无从判分只能全给通过。开 dashboard 抽 1 条 judge dialogue 看 rubric 有没有正常出现。
- **healthy**:0 / mid / 1 都有,或者按 baseline 偏低 / evolved 偏高分布。

**A'.4 进化产物**内容**抽样(把 §6.5 证据 C 的 `ls` 升级成 `cat`)**

单看 delta 有文件不够,还得看内容是不是"agent 学到了什么"的自然语言,而不是空文件 / stack trace / 无意义字符:

```bash
DELTA=$(docker images --format '{{.Repository}}:{{.Tag}}' | grep 'evolve-eval-delta:.*<run_id>' | head -1)

docker run --rm --entrypoint sh "$DELTA" -c '
  find /opt/<runtime>/memory -type f -size +10c 2>/dev/null | head -5 | while read f; do
    echo "===== $f ====="; cat "$f"
  done
'
```

要求:
- 至少一个 memory 文件非空,内容是**自然语言**(经验总结 / 步骤 / 反例),不是纯 JSON dump / Python traceback / 空 markdown 标题
- 内容与证据 A 里 `reply_head=` 打出的 reflection 摘要在语义上一致(LLM 说要记什么就真记了什么)

**A'.5 综合红旗**

| 现象 | 大概率原因 |
|---|---|
| A'.1 命中"逃避语"但没 IO error | LLM 拿到的 material 路径提示错,或 cwd 与 material 挂载点不一致 |
| A'.2 每题 content 都 < 50 chars | agent 主循环提前退出,`docker exec` timeout 或 provider 报错吞掉了 body |
| A'.3 全 0 或全 1 | judge rubric 塌了 / material 缺失连锁反应 |
| A'.4 memory 全空 / 全是 traceback | reflection prompt 未激活 / 上游 memory 写入路径挂错(§1.7) |
| A'.2 baseline == evolved(字节级一致) | evolve 完全没生效,回证据 C 三点错位 |

> **A' 与 A / B / C 的关系**:A / B / C 是"计数在不在",A' 是"内容对不对"。跑完 A / B / C 全绿 **且** A' 抽样合理,才算"新 runtime 接入完备";否则就算 4 项绿灯,后续 benchmark 数据仍然可能是伪造。

#### 证据 B：Langfuse —— trace 写入 & 后处理拼装

验证容器里的调用确实上报到 Langfuse，且后处理的 backfill 能拿回来做 stitching。

```bash
RID=lift-runid-<run_id>
JSON=results/$RID/${RID}_backfilled.json

# B.1 后处理 backfill 成功（有 work_agent_traces / judge_agent_traces）
python -c "
import json
r = json.load(open('$JSON'))
for rp in r['runs']:
    for s in rp['suites']:
        for t in s['tasks']:
            for ph_name in ('baseline','evolved'):
                ph = t[ph_name]
                lf = ph.get('langfuse') or {}
                wt = len(lf.get('work_agent_traces') or [])
                jt = len(lf.get('judge_agent_traces') or [])
                pt = len(lf.get('plugin_traces') or [])
                tc = ph.get('tool_calls')
                print(f'  {t[\"name\"]:6} {ph_name:8} work={wt} judge={jt} plugin={pt} tool_calls={tc}')
"

# B.2 检查后处理告警
grep -E "trace not found|Failed to fetch trace|trace_backfill" "$LOG"

# B.3 Langfuse UI 侧交叉检查：随便挑一条 trace
# 打开 http://<langfuse-host>/project/<pid>，按 session_id (user-xxx / judge-xxx) 搜
# 应该看到 name=<runtime>-plugin、session/tags 列非空的 root span
```

**通过标准**：
- 每题两个 phase 都有 `work` ≥ 1、`judge` ≥ 1（`turns` 数对齐）
- 静态 dashboard tools 列有非 null 值（说明 §5.3 的兜底链路走通）
- Langfuse UI 上 trace 的 Session / Tags 列非空（说明 overlay 用的是 v4 上下文管理器，见 §1.3）

**红旗**：`work=0` / `judge=0` 且日志无 timeout → overlay 没生效或 trace name 没进 `LANGFUSE_PLUGIN_TRACE_NAMES`（见 §5.1）。

**另一红旗**：`work` / `judge` 齐全但 `plugin=0`，容器日志出现 `Failed to export span batch due to timeout` → 容器内 exporter 端点不通宿主 Langfuse。`docker exec <c> env | grep LANGFUSE` 看 `LANGFUSE_BASE_URL` 是否被 `.env` 里的 `localhost` / `127.0.0.1` 污染；修法见 §2.2 第 6 点（`env_vars` 覆写）。

#### 证据 C：Layer —— delta 镜像真的包含进化内容吗？

这是 LIFT 全流程的**核心命题**。必须在 warmup 结束、`docker commit` 之后、pipeline `docker rmi evolve-eval-delta:*` 之前抢到 delta 镜像做 diff。

**优先看 pipeline 日志的 `Delta preflight diff` 行**（[commit_delta_image](file:///root/workspace/agent_evolve_evaluation/src/lift/adapters/container/delta.py) 在 `docker commit` 之前会自动跑 `docker diff` 并打摘要，见 §2.1 的 `evolve_paths` 说明）：

```
INFO Delta preflight diff (full) [evolve-genericagent-xxxxx]: +2038A ~14C -0D across 17 paths (top: /usr/local/lib x1800, /opt/GenericAgent/memory x9, /opt/GenericAgent/temp x120, ...)
INFO Delta preflight diff (evolve-only) [evolve-genericagent-xxxxx]: +9A ~2C -0D across 1 paths (top: /opt/GenericAgent/memory x11)
INFO Delta materialized: evolve-eval-delta:<run_id>-r0-<suite>
```

- `full` 行 = upperdir 全集，`+NA ~NC -ND` = 新增 / 修改 / 删除 的容器 FS 层文件计数（bind mount 天然不进 diff）
- `evolve-only` 行 = 只统计 adapter `evolve_paths` 白名单目录下的变更；未声明白名单则不打此行
- **红旗 1**：`full` 行显示 `no changes (empty upperdir)` —— warmup 没往容器 FS 写任何东西，铁定是 §1.7 三点错位
- **红旗 2**：`evolve-only` 行升级为 `WARNING` 且带 `no changes under evolve_paths=...` —— 白名单目录里没落任何东西（可能写去了 `/tmp` / `/root` / bind mount），需要核对上游引擎的读路径
- **红旗 3**（未声明 `evolve_paths` 时的降级判定）：`full` 行 `top:` 里根本没出现 `/opt/<runtime>/memory` —— 走 §2.1 补上 `evolve_paths` 后就能自动 WARNING

如果日志摘要已经有明确红旗，可以跳过下面的手工 diff；如果想深挖具体新增了什么文件、mtime 情况：

**方案 1（推荐）**：在 pipeline 的清理钩子前拦一次（`--warmup-only` 会跳过 holdout 且不会 `docker rmi` delta，最方便）：

```bash
# 用 --warmup-only 只跑 warmup + commit，delta 镜像会保留下来
nohup python -m src.cli.lift_main -r <runtime> \
  --benchmark_dir assets/benchmarks_demo --suite <suite>.json \
  --run_id <run_id> --warmup-only > logs/<run_id>.log 2>&1 &
wait

# 找出 delta 镜像
DELTA=$(docker images --format '{{.Repository}}:{{.Tag}}' | grep "evolve-eval-delta:.*<run_id>" | head -1)
echo "delta = $DELTA"

# 与 baseline 镜像 diff（看真的多了什么）
BASE=evolve-eval-<runtime>:latest
docker run --rm --entrypoint sh "$BASE" -c 'ls -la /opt/<runtime>/memory' > /tmp/base_memory.txt
docker run --rm --entrypoint sh "$DELTA" -c 'ls -la /opt/<runtime>/memory' > /tmp/delta_memory.txt
diff /tmp/base_memory.txt /tmp/delta_memory.txt

# 也可以直接看 diff summary（A = added, C = changed, D = deleted）
docker run --rm --entrypoint sh "$DELTA" -c 'find /opt/<runtime>/memory -newer /opt/<runtime>/agentmain.py -type f'
```

**方案 2**：正式跑（含 holdout），在 pipeline 打日志"Delta materialized"之后立刻 tag 保护：

```bash
# 提前开另一个 shell 循环抓 delta，第一次抓到就 docker tag 保留
while true; do
  D=$(docker images -q "evolve-eval-delta:*<run_id>*" | head -1)
  if [[ -n "$D" ]]; then docker tag "$D" "kept-delta:<run_id>"; break; fi
  sleep 2
done
```

**通过标准**：
- delta 镜像的 `/opt/<runtime>/memory/` （或 runtime 对应目录）**存在**且**内容不同于 baseline**（有新文件 / 有 mtime 更新的现有文件）
- 具体新内容与日志证据 A 里的 reflection reply 一致（LLM 说要写什么就真的写下了什么）

**红旗**：delta 与 baseline 完全一致 —— 意味着 warmup 阶段 agent 学到的所有东西都写去了别处（bind mount / tmpfs / /tmp）没进 commit。这就是 §1.7 描述的**三点错位** bug。**必须**先修好这一层再谈其他，否则整个 LIFT 数据都是伪造的（baseline vs evolved 无差异，improvement 恒为 0）。

#### 综合判断表

| 证据 A | A' | 证据 B | 证据 C | 结论 |
|---|---|---|---|---|
| ✅ chat / reflection 都触发 | ✅ 内容合理 | ✅ trace 齐全 | ✅ delta 有内容 | Runtime 接入完备 ✅ |
| ✅ | ❌ material 逃避语 / content 极短 | — | — | material / cwd 路径挂错,回 §1.7 + §2.2 |
| ✅ | ❌ judge score 全 0 或全 1 | — | — | material 缺失连锁反应 / judge prompt rubric 塌了 |
| ✅ | ❌ memory 全空 / traceback | — | ✅ delta 有文件 | reflection 触发但写入错乱,回 §1.7 |
| ✅ | ✅ | ✅ | ❌ delta 与 baseline 一致 | §1.7 三点错位 bug,evolve **无效**,必须修 |
| ✅ | ✅ | ❌ trace 缺失 | ✅ delta 有内容 | overlay 或 `LANGFUSE_PLUGIN_TRACE_NAMES` 有问题,evolve 有效但 dashboard / 后处理拿不到分析数据 |
| ❌ reflection 无 / timeout | — | — | — | reflection 钩子未生效或 chat 卡死,先修 chat 再验证其他 |
| ✅ 全 DONE | ✅ | ✅ | ❌ | suite 太简单不触发写入,换更复杂的 suite 再验 |

### 6.6 衍生 runtime（可选）

如果还要做 `<runtime>_with_evolve` / `<runtime>_active_evolve`：
1. 镜像 tag 多加一条 `<RUNTIME>_WITH_EVOLVE_DOCKER_IMAGE`（或复用基础镜像）。
2. 新建 `src/lift/adapters/<runtime>_<variant>/adapter.py` **继承** baseline adapter，只 override `evolve_after_warmup`。
3. registry / Literal 五处 / `LANGFUSE_PLUGIN_TRACE_NAMES`（如果 trace name 不同）也要加。

---

## 7. 常见坑总结

| 现象 | 原因 | 排查 |
|---|---|---|
| `docker exec` 起不来 agent 进程 | 上游硬编码 cwd 没 patch | `docker exec <c> grep -n script_dir /opt/<runtime>/agentmain.py` |
| Langfuse 全无 plugin trace（`plugin=0`） | ① overlay 没生效；② trace name 没在 `LANGFUSE_PLUGIN_TRACE_NAMES`；③ 端点被 `.env` 里的 `localhost` 污染（见下一行） | `docker exec <c> head /opt/<runtime>/plugins/langfuse_tracing.py` 看是否 LIFT overlay 版本；容器日志有 `Failed to export span batch due to timeout` → 走端点排查 |
| trace 拼装 work / judge 不对应 | session_id 前缀错；不是 `user-*` / `judge-*` | grep `WorkerJudgerPair` 调用处的 sid 拼接逻辑 |
| 镜像构建"诡异地快" / 改了 plugin 没生效 | docker layer cache 全命中，COPY 没触发重打 | 改完 plugin 强制 `docker build --no-cache` 重打；或 `touch agent-runtimes/<runtime>/<file>` 让 mtime 变 |
| build 期 git clone 卡死 | GitHub 直连失败 | `<RUNTIME>_GIT_URL` 用 `https://ghfast.top/<github URL>` 反代 |
| build 期 apt / pip 卡死 | 公网仓库不通 | 设 `APT_MIRROR` + `PIP_INDEX_URL`（字节内网见 §1.6） |
| build-image 静默成功但凭据没注入 | `.env` 没被 source / Dockerfile ARG / build-image.sh `--build-arg` / install-in-image.sh sed 三方没同步 | `docker run --rm <image> grep __ /opt/<runtime>/mykey.py` 应 0 行；非 0 行说明三方有缺口（见 §1.2） |
| GA 模型回复 "I cannot find this tool" | 只 append 了英文 schema，中文模型加载的是 `tools_schema_cn.json` | `docker run --rm <image> python -c 'import json; print([t["function"]["name"] for t in json.load(open("/opt/GenericAgent/assets/tools_schema_cn.json"))])'` |
| `MODEL_NAME` 在两个 runtime 间互相污染 | `.env` 共享 `MODEL_NAME`，但 OpenClaw / GA 期望值不同 | 各 runtime 用专属变量名（`GENERICAGENT_MODEL_NAME` / `OPENCLAW_MODEL_NAME`），fallback 到 `MODEL_NAME` |
| 容器内 Langfuse 连不上宿主 | `LANGFUSE_HOST` 写了 `localhost` / `127.0.0.1`（**镜像里**或**宿主 `.env` 通过 `env_file` 注入**——SDK v4 的 OTel span exporter 会读 env 覆盖 `Langfuse(host=...)` 显式参数，即使 overlay 完全正确也 0 plugin trace） | ① 镜像里固定写 `http://host.docker.internal:3000`（`Dockerfile` ARG default 已这样）；② LIFT `start_*_container` 的 `env_vars` 覆写 `LANGFUSE_BASE_URL` / `LANGFUSE_HOST`（参考 GA `_rewrite_langfuse_host_for_container`，见 §2.2 第 6 点）；③ Linux 下 docker run 加 `--add-host host.docker.internal:host-gateway`（LIFT `start_*_container` 已处理） |
| Type checker 报 `AgentSource` 不一致 | 5 处 Literal 漏改 | `grep -rn "AgentSource\s*=\s*Literal" src/` 五处都要 |
| `Judge response is not valid JSON` 重试日志 | 这是 prompt sanity 设计行为，不是 bug | 偶发可忽略；高频出现说明 judge prompt 没渲染干净 |
| `logs/<run_id>.log` 看到 `wait output timeout` | GA 主循环 600s 内没产出 / 死循环 / LLM 卡 | `docker exec <c> tail -50 /opt/GenericAgent/temp/<iodir>/ga.stderr.log` 看 GA 自己日志 |
| Langfuse trace 上 `Session` / `Tags` 列空 | overlay 还在用 v3 的 `obs.update_trace(...)` / `client.update_current_trace(...)`，4.x SDK 已删除 | overlay 改成 `propagate_attributes(session_id=, tags=)` 上下文管理器 + `start_as_current_observation`（见 §1.3） |
| 静态 dashboard tools 列空，但 `*_backfilled.json` 里 `tool_calls` 已有数 | B 路径 langfuse 兜底拿到了值但没回写 tracker，`tracker.snapshot()` 仍是 None → 嵌入 HTML 后显示 "—" | 确认 `run_post_process_pipeline` 调了 `tracker.set_phase_tool_calls(...)`（见 §5.3.1）；运行期实时 dashboard 看不到 B 路径 tools 是设计行为 |
| evolved 与 baseline 结果几乎一致（improvement ≈ 0），或 LLM 明说"写了 memory"但 delta 镜像里没有 | Warmup 期 agent 的 evolve 产物落进了 bind mount / tmpfs（LLM 用 `memory/xxx` 相对路径，cwd 又在 bind mount 之内），`docker commit` 没捕获到 → delta 镜像内容 = baseline | 走 §6.5 证据 C 检查 delta diff；若 diff 为空回 §1.7 三点错位排查（引擎读 / LLM 写 / Dockerfile mkdir）。修法：`install-in-image.sh` patch 上游 system prompt 里 `[Memory]` 指示为绝对路径；reflection prompt 显式加"cwd 是 bind mount，只能用 `/opt/<runtime>/memory/`" |
| 流水线全绿、report.json `success=true`，但 work agent 回复里出现 "I cannot access" / "no attachment" / `q1_materials.*not found` 等逃避语 | task materials bind mount 路径与 agent 侧 cwd / system prompt 里的路径不一致；LLM 拿到任务描述里的相对路径解析不出真实位置 | 走 §6.5 证据 A'.1 命中项;`docker exec <c> ls -la /workspace/task /root/.openclaw/workspace` 对比宿主 bind mount 目录,核对 `session.py:task_volume_binds` 与上游 cwd patch 是否指向同一目录;必要时在 workspace startup hook 里加 `qN_materials/ → cwd` 的软链 |
| Judge `content_score` 全 0 或全 1(A'.3 分布异常) | 全 0:material 缺失/LLM 拿不到任务上下文,judge 一律判 fail;全 1:judge prompt 里 rubric 或任务描述被 truncate,judge 无凭无据一律放行 | 打开 dashboard 抽 1 条 judge dialogue,检查 rubric 字段与任务描述是否完整;再回 A'.1 排查 material 挂载 |

---

## 8. 未来优化 TODO

集成 GA 过程中沉淀出来的可选增强项，暂未落地；如果后续接入新 runtime 时踩到相关坑，可以顺手把对应条目实现掉。

- [ ] **`Delta preflight diff` 结构化输出到 report.json**：目前 diff 摘要只落在 pipeline 日志（`Delta preflight diff (...): +NA ~NC -ND ...`）。可以把 `+NA ~NC -ND` 加它的 top-paths 数组挂到 `PhaseRun.langfuse` 平级的 `PhaseRun.delta_diff` 字段（或 `SuiteRun.delta_diff`），让后处理 CSV / HTML dashboard 也能一眼看出"这一轮 warmup 有没有真的落东西"，不用翻日志。见 [container/delta.py](file:///root/workspace/agent_evolve_evaluation/src/lift/adapters/container/delta.py) `_summarize_diff` 的返回值改成 dict 就行。
- [ ] **evolve 产物落地契约的静态自检脚本**：把 §1.7 "三点错位" 验证清单（引擎读路径 / system prompt 提示路径 / Dockerfile mkdir 路径）沉淀成 `agent-runtimes/<runtime>/verify_evolve_contract.sh`，接入新 runtime 时 `bash verify_evolve_contract.sh <image>` 一键跑完输出 pass/fail，比每次 grep 手敲更省事。GA 的 3 处 `sed` patch 也可以做成脚本形式复用给下一个 runtime。
- [ ] **Langfuse SDK v3 → v4 overlay 迁移脚本**：见 §1.3 —— 目前只在文档里描述了 v4 的 `propagate_attributes + start_as_current_observation` 用法，下次遇到只支持 v3 API 的上游 plugin 时，需要手动改。可以固化一个 `overlay_migrate_v3_to_v4.py` codemod（针对 `observation.update_trace(...)` / `client.update_current_trace(...)` 的 AST 替换）加进 skill，让类似 patch 自动化。

---

## 9. 集成完成后的一次性产出

完成集成后，`git status` 应当包含：

```
M  src/lift/adapters/registry.py             # 加新 runtime + lazy import
M  src/paths.py                              # <RUNTIME>_AGENT_DIR / DOCKER_IMAGE / SEED_DIR
M  src/models.py                             # LANGFUSE_PLUGIN_TRACE_NAMES 加 "<runtime>-plugin"
M  src/postprocess/extract.py                # AgentSource Literal
M  src/postprocess/run_post_process.py       # AgentSource Literal
M  src/postprocess/trace_backfill.py         # AgentSource Literal
M  src/postprocess/report_html.py            # AgentSource Literal
M  src/report/langfuse_trace_stitch.py       # AgentSource Literal + dispatch tuple

?? agent-runtimes/<runtime>/Dockerfile
?? agent-runtimes/<runtime>/build-image.sh
?? agent-runtimes/<runtime>/install-in-image.sh
?? agent-runtimes/<runtime>/mykey.py.template
?? agent-runtimes/<runtime>/langfuse_tracing_overlay.py
?? agent-runtimes/<runtime>/.dockerignore
?? agent-runtimes/<runtime>/README.md
?? agent-runtimes/<runtime>/workspace_seed/...

?? src/lift/adapters/<runtime>/__init__.py
?? src/lift/adapters/<runtime>/adapter.py
?? src/lift/adapters/<runtime>/session.py
?? src/lift/adapters/<runtime>/container_exec.py
?? src/lift/adapters/<runtime>/chat_agent.py
```

如果还集成了第三方工具（如 firecrawl），再加一份 `agent-runtimes/<runtime>/<tool>_plugin.py` + `install-in-image.sh` 里 `cp + tools_schema*.json patch` 的相关段落。

---

## 10. 参考实现速查

| 场景 | 看哪个文件 |
|---|---|
| 容器无 gateway / 文件 I/O 协议 | [`src/lift/adapters/genericagent/`](file:///root/workspace/agent_evolve_evaluation/src/lift/adapters/genericagent) |
| 容器有 gateway / HTTP 协议 | [`src/lift/adapters/openclaw/`](file:///root/workspace/agent_evolve_evaluation/src/lift/adapters/openclaw) |
| 衍生 runtime（叠 evolve 钩子） | [`src/lift/adapters/genericagent_active_evolve/adapter.py`](file:///root/workspace/agent_evolve_evaluation/src/lift/adapters/genericagent_active_evolve/adapter.py) |
| 多容器 warmup（群体记忆） | [`src/lift/adapters/openclaw_multi_user/`](file:///root/workspace/agent_evolve_evaluation/src/lift/adapters/openclaw_multi_user) |
| 镜像脚手架最简版 | [`agent-runtimes/genericagent/`](file:///root/workspace/agent_evolve_evaluation/agent-runtimes/genericagent) |
| 第三方工具 plugin 模板 | [`agent-runtimes/genericagent/firecrawl_plugin.py`](file:///root/workspace/agent_evolve_evaluation/agent-runtimes/genericagent/firecrawl_plugin.py) |
| sanity benchmark | [`assets/benchmarks_demo/hello.json`](file:///root/workspace/agent_evolve_evaluation/assets/benchmarks_demo/hello.json) / [`test_search.json`](file:///root/workspace/agent_evolve_evaluation/assets/benchmarks_demo/test_search.json) |

---

## 11. 配套 skill

| skill | 何时调用 |
|---|---|
| [`setup-lift-env`](file:///root/workspace/agent_evolve_evaluation/skill/setup-lift-env/SKILL.md) | 还没装 conda / docker / langfuse / 跑过 hello.json 的全新机器 |
| [`cleanup-lift-env`](file:///root/workspace/agent_evolve_evaluation/skill/cleanup-lift-env/SKILL.md) | 评测中途 Ctrl-C / OOM 后留下 `evolve-<runtime>-*` 容器、`evolve-eval-delta:*` 镜像，重跑前清场用 |
