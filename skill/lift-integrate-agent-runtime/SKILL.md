---
name: "lift-integrate-agent-runtime"
description: "把一个新的 agent runtime（如 OpenClaw / GenericAgent）接入 LIFT 评测框架的端到端清单：镜像脚手架 + adapter 三件套 + 注册点 + 后处理 Literal 同步 + Langfuse trace 拼装 + 验收 checklist。在用户说\"集成 / 接入 / 添加新 agent runtime\"或\"-r <name>\"想新增可选项时调用。"
---

# LIFT: 集成新 Agent Runtime

把一个 agent（OpenClaw、GenericAgent、Hermes 之类）接入 LIFT 评测流水线，需要同步五个层面：**镜像脚手架** → **adapter 三件套** → **CLI 注册** → **后处理类型同步** → **Langfuse trace 拼装**。这份 skill 把两次成功集成（OpenClaw + GenericAgent）的所有 touchpoint 固化成清单，减少漏改。

> **原则**：先把 baseline 跑通（hello.json sanity → test_search.json benchmark），再考虑 `_with_evolve` / `_active_evolve` 之类衍生 runtime。衍生只是在 baseline adapter 上叠 `evolve_after_warmup` 钩子或镜像 tag。

---

## 0. 必备前置认知

- LIFT 走 **warmup → docker commit → hold-out** 流水线：每个 runtime 都要能从 baseline 镜像 commit 出"演化过的"镜像（不演化也得 commit，让流水线统一）。
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
| `build-image.sh` | 必须 | 读 `.env` 获取 ARK / Langfuse / 第三方 secret，`--build-arg` 透传 |
| `install-in-image.sh` | 必须 | 镜像内执行：`sed` 渲染 `mykey.py.template` → `mykey.py`、覆盖 `langfuse_tracing_overlay.py`、patch 上游硬编码 |
| `mykey.py.template` | 必须 | 凭据模板，占位符 `__ARK_API_KEY__` 等由 `install-in-image.sh` 用 sed 渲染 |
| `langfuse_tracing_overlay.py` | 必须 | LIFT 自有 tracing overlay：强制 root span name = `<runtime>-plugin`、注入 `session_id` / tags |
| `workspace_seed/` | 可选 | 容器内 `/workspace/task` 初始内容（如 README、人设文件）；GA baseline 仅一个 README |
| `.dockerignore` | 推荐 | 屏蔽 `.git` / `temp/` 减小 build context |

### 1.1 `mykey.py.template` 占位符规范

```python
native_oai_config = {"name": "doubao", "apikey": "__ARK_API_KEY__", "apibase": "__ARK_BASE_URL__", "model": "__MODEL_NAME__", "api_mode": "openai-completions"}
langfuse_config = {"public_key": "__LANGFUSE_PUBLIC_KEY__", "secret_key": "__LANGFUSE_SECRET_KEY__", "host": "__LANGFUSE_HOST__"}
```

`install-in-image.sh` 里要：
1. `escape_sed` 转义所有 `__XXX__` 注入值（防 `/` 与换行污染 sed）。
2. 一条 `sed -e ... -e ...` 替换全部占位符。
3. 严禁把空字符串当成 valid secret 写进镜像 — 上层 `build-image.sh` 应预先 `${VAR:-}` fallback 成空，由 plugin 自身在运行期再做 "未配置" 校验（参考 [`firecrawl_plugin.py`](file:///root/workspace/agent_evolve_evaluation/agent-runtimes/genericagent/firecrawl_plugin.py)）。

> **`MODEL_NAME` 必须是 provider-native 标识**：GA / 任意直连 ARK 的 runtime，`MODEL_NAME` 要是 ARK 真实 endpoint id（形如 `ep-2025xxxx-xxxxx`），不是 OpenClaw gateway 的命名空间值。如果同一个 `.env` 同时给 OpenClaw / GA 用，建议在 `build-image.sh` 里走专属变量名（参考 GA 用 `GENERICAGENT_MODEL_NAME` 优先于共享 `MODEL_NAME`，见 [`build-image.sh:61`](file:///root/workspace/agent_evolve_evaluation/agent-runtimes/genericagent/build-image.sh#L61)），避免一改就同时污染另一个 runtime 的镜像。

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

### 2.2 `session.py`（容器启动）

模板：[`src/lift/adapters/genericagent/session.py`](file:///root/workspace/agent_evolve_evaluation/src/lift/adapters/genericagent/session.py)

必须做的：
1. `_CONTAINER_PREFIX = "evolve-<runtime>"` — 容器名前缀，方便 `docker ps` / cleanup grep。
2. `start_<runtime>_container` 调 `ContainerSession.start`，传 `port_mappings` / `env_vars` / `volume_binds` / `readiness_check` / `post_start_hooks` / `pre_cleanup_hooks`。
3. `default_volume_binds` + `task_volume_binds` 是标准 bind（`/workspace/outcome`、`/workspace/task`、`/workspace/benchmarks`），照抄即可。
4. `seed_eval_workspace`：宿主机端把 `workspace_seed/` 拷进 `workspace_dir`，留 `.lift-workspace-ready` marker。
5. `_reclaim_volume_ownership`：cleanup 前把 bind mount 目录 `chown` 回宿主用户，避免 root-owned 文件污染 `results/`。

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
- 单轮 wall-clock 上限统一 600s（`CHAT_EXEC_TIMEOUT_SECONDS = 600.0`），超时返回 `CHAT_EXEC_TIMEOUT_MARKER` 前缀字符串走 LIFT 的 provider error 重试通道。

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
  --status-http 0.0.0.0:<port> &

tail -f nohup.out                       # 主进度看这里
# 浏览器开 http://<host>:<port>         # 看 run / repeat / suite / task / phase 结构化状态
```

> `assets/benchmarks_demo/` 里两个常用 sanity suite：
> - **`hello.json`** — 1 W + 1 H 寒暄（Q1 "回复你好" / Q2 "自我介绍"），测的是基本 chat / warmup-commit-holdout 流水线连通性。
> - **`test_search.json`** — 1 W + 1 H 联网题（W1 查 2026 北京展会、H1 查 Node.js LTS 版本），测的是 agent 的联网工具是否生效。如果 runtime 没接联网工具，H1 会失败但流水线本身仍然走完。

> `--status-http 0.0.0.0:<port>` 远程机器开 dashboard 必须 0.0.0.0；只在本机调试用 PORT 单字段（默认绑 127.0.0.1）即可。

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

```bash
nohup python -m src.cli.lift_main \
  -r <runtime> --benchmark_dir assets/benchmarks_demo \
  --suite hello.json --run_id <run_id> --status-http 0.0.0.0:<port> &
tail -f nohup.out
```

验证点：
- 容器拉起、warmup 单题跑完、`docker commit` 成功、hold-out 跑完
- `results/lift-runid-<run_id>/report.json` 存在且 task `outcome.success: true`
- nohup.out 没有 `wait output timeout` / `Cannot connect to Docker daemon` / `Judge response is not valid JSON` 高频重试

### 6.3 Trace stitching 对齐

run 完后默认会自动跑后处理；想单独重跑：

```bash
python -m src.cli.lift_main -r <runtime> --evaluate-only --run_id <run_id>
```

验证点：
- `results/lift-runid-<run_id>/*_backfilled.json` 中每题都拼到 `work_agent_traces` / `judge_agent_traces`（数量与 `report.json` 的 turn 数对齐）
- 没有 "trace not found" 告警

### 6.4 test_search.json 联网能力 sanity（可选）

```bash
nohup python -m src.cli.lift_main \
  -r <runtime> --benchmark_dir assets/benchmarks_demo \
  --suite test_search.json --run_id <run_id> --status-http 0.0.0.0:<port> &
tail -f nohup.out | grep -E 'firecrawl|search|scrape|Action'
```

如果 runtime 配了联网工具（如 firecrawl），应当看到 W1 / H1 调用搜索工具拿到当日数据；没接联网工具的 runtime 这步可以跳过。

### 6.5 衍生 runtime（可选）

如果还要做 `<runtime>_with_evolve` / `<runtime>_active_evolve`：
1. 镜像 tag 多加一条 `<RUNTIME>_WITH_EVOLVE_DOCKER_IMAGE`（或复用基础镜像）。
2. 新建 `src/lift/adapters/<runtime>_<variant>/adapter.py` **继承** baseline adapter，只 override `evolve_after_warmup`。
3. registry / Literal 五处 / `LANGFUSE_PLUGIN_TRACE_NAMES`（如果 trace name 不同）也要加。

---

## 7. 常见坑总结

| 现象 | 原因 | 排查 |
|---|---|---|
| `docker exec` 起不来 agent 进程 | 上游硬编码 cwd 没 patch | `docker exec <c> grep -n script_dir /opt/<runtime>/agentmain.py` |
| Langfuse 报告 0 trace | overlay 没生效 / `LANGFUSE_PLUGIN_TRACE_NAMES` 漏加 | `docker exec <c> head /opt/<runtime>/plugins/langfuse_tracing.py` 看是不是 LIFT overlay 版本 |
| trace 拼装 work / judge 不对应 | session_id 前缀错；不是 `user-*` / `judge-*` | grep `WorkerJudgerPair` 调用处的 sid 拼接逻辑 |
| 镜像构建"诡异地快" / 改了 plugin 没生效 | docker layer cache 全命中，COPY 没触发重打 | 改完 plugin 强制 `docker build --no-cache` 重打；或 `touch agent-runtimes/<runtime>/<file>` 让 mtime 变 |
| build 期 git clone 卡死 | GitHub 直连失败 | `<RUNTIME>_GIT_URL` 用 `https://ghfast.top/<github URL>` 反代 |
| build 期 apt / pip 卡死 | 公网仓库不通 | 设 `APT_MIRROR` + `PIP_INDEX_URL`（字节内网见 §1.6） |
| build-image 静默成功但凭据没注入 | `.env` 没被 source / Dockerfile ARG / build-image.sh `--build-arg` / install-in-image.sh sed 三方没同步 | `docker run --rm <image> grep __ /opt/<runtime>/mykey.py` 应 0 行；非 0 行说明三方有缺口（见 §1.2） |
| GA 模型回复 "I cannot find this tool" | 只 append 了英文 schema，中文模型加载的是 `tools_schema_cn.json` | `docker run --rm <image> python -c 'import json; print([t["function"]["name"] for t in json.load(open("/opt/GenericAgent/assets/tools_schema_cn.json"))])'` |
| `MODEL_NAME` 在两个 runtime 间互相污染 | `.env` 共享 `MODEL_NAME`，但 OpenClaw / GA 期望值不同 | 各 runtime 用专属变量名（`GENERICAGENT_MODEL_NAME` / `OPENCLAW_MODEL_NAME`），fallback 到 `MODEL_NAME` |
| 容器内 Langfuse 连不上宿主 | `LANGFUSE_HOST` 写了 `localhost` / `127.0.0.1` | 镜像里固定写 `http://host.docker.internal:3000`（`Dockerfile` ARG default 已这样）；Linux 下 docker run 加 `--add-host host.docker.internal:host-gateway`（LIFT `start_*_container` 已处理） |
| Type checker 报 `AgentSource` 不一致 | 5 处 Literal 漏改 | `grep -rn "AgentSource\s*=\s*Literal" src/` 五处都要 |
| `Judge response is not valid JSON` 重试日志 | 这是 prompt sanity 设计行为，不是 bug | 偶发可忽略；高频出现说明 judge prompt 没渲染干净 |
| nohup.out 看到 `wait output timeout` | GA 主循环 600s 内没产出 / 死循环 / LLM 卡 | `docker exec <c> tail -50 /opt/GenericAgent/temp/<iodir>/ga.stderr.log` 看 GA 自己日志 |

---

## 8. 集成完成后的一次性产出

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

## 9. 参考实现速查

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

## 10. 配套 skill

| skill | 何时调用 |
|---|---|
| [`setup-eval-env`](file:///root/workspace/agent_evolve_evaluation/skill/setup-eval-env/SKILL.md) | 还没装 conda / docker / langfuse / 跑过 hello.json 的全新机器 |
| [`cleanup-eval-env`](file:///root/workspace/agent_evolve_evaluation/skill/cleanup-eval-env/SKILL.md) | 评测中途 Ctrl-C / OOM 后留下 `evolve-<runtime>-*` 容器、`evolve-eval-delta:*` 镜像，重跑前清场用 |

集成新 runtime 时，先用 `setup-eval-env` 把基础环境备好（如果还没），再按本 skill 走流程；调试中遇到容器残留就用 `cleanup-eval-env` 清场再重试。
