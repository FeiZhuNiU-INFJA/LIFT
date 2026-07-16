# Adapter 三件套 + Registry 注册

> [`SKILL.md`](../SKILL.md) 的第 2、3 步深化文档。本文件覆盖:`src/lift/adapters/<runtime>/` 四个文件的实现要点、`evolve_paths` 白名单声明、CLI registry / paths 常量注册。

容器型 runtime 一律继承 `ContainerAgentRuntimeAdapter`,需要四个文件:

```
src/lift/adapters/<runtime>/
├── __init__.py            # 通常空文件或仅 re-export adapter
├── adapter.py             # AgentRuntimeAdapter 子类
├── session.py             # start_<runtime>_container + workspace seed hooks
├── container_exec.py      # ContainerContext dataclass + exec wrapper
└── chat_agent.py          # ChatAgent + WorkerJudgerPairFactory
```

## 2.1 `adapter.py`(必须 override 的 4 个方法)

参考 [`src/lift/adapters/genericagent/adapter.py`](../../../src/lift/adapters/genericagent/adapter.py):

| Method | 作用 | 最简实现要点 |
|---|---|---|
| `resolve_docker_image` | 从 `RunOptions.docker_image` override → `<RUNTIME>_DOCKER_IMAGE` 默认 | 一行 `return override or DEFAULT` |
| `start_container` | 委托给 `start_<runtime>_container`(在 `session.py`) | 透传 `instance_id` / `image` / `ctx` / `workspace_dir` 等参数 |
| `worker_judger_factory` | 把 `ContainerSession` 包成 `WorkerJudgerPairFactory` | `return <Runtime>WorkerJudgerPairFactory(container=..., workspace_dir=...)` |
| `evolve_after_warmup` | 演化钩子;baseline runtime 是 no-op | `return None` |

另外**必须**声明一个类属性 —— `evolve_paths`(默认继承 `ContainerAgentRuntimeAdapter.evolve_paths = ()`):

| 类属性 | 作用 | 最简声明(**实测路径,勿照抄文档猜测**) |
|---|---|---|
| `evolve_paths: tuple[str, ...]` | 声明本 runtime "真进化产物"落地的**容器内绝对路径**白名单,供 `commit_delta_image` 在 `docker diff` 后单独打一行 `evolve-only` 摘要;计数为 0 时 WARNING(负向信号),并追加一行 candidate unlisted paths 提示 | GA: `("/opt/GenericAgent/memory",)`;OpenClaw: `("/root/.openclaw/workspace/memory", "/root/.openclaw/skill-workshop")`;Hermes: `("/opt/hermes-state/skills", "/opt/hermes-state/memories")`;OpenHuman: `("/root/.openhuman/users", "/root/.openhuman/skill-registry")` |

> ⚠️ **不要照抄上游文档 / 项目 README 里给的路径就当声明完了**。同一 agent 的"记忆目录"文档路径与实际写入路径经常错位(OpenClaw 文档写 `/root/.openclaw/memory`,实际写 `/root/.openclaw/workspace/memory`,因为 `learn review` 写的是 workspace 子目录)。**唯一可信来源是 [`three-layer-verification.md` 证据 C](three-layer-verification.md#证据-clayer--delta-镜像真的包含进化内容吗) 的 `results/{run_id}/delta_diff_*.txt` dump**:先用一个"必然会写记忆"的 suite(推荐 `integration_check.json`,见 [`acceptance-checklist.md`](acceptance-checklist.md))跑一次 `--warmup-only`,读 dump 文件找到真实落地目录,再回填 `evolve_paths`。

漏声明的后果:pipeline 日志只有 `full` 摘要(含 pip / cache / temp 等噪声),无法在无人值守下自动预警"warmup 没写出任何进化产物"。参考 [`GenericAgentAdapter.evolve_paths`](../../../src/lift/adapters/genericagent/adapter.py#L38-L42)、[`OpenClawAdapter.evolve_paths`](../../../src/lift/adapters/openclaw/adapter.py#L36-L45)、[`HermesAdapter.evolve_paths`](../../../src/lift/adapters/hermes/adapter.py#L39-L49)、[`OpenHumanAdapter.evolve_paths`](../../../src/lift/adapters/openhuman/adapter.py#L48-L51) 的定义。声明的路径应与 [`evolve-artifact-contract.md`](evolve-artifact-contract.md) "三点错位"里的**引擎读路径**一致(引擎去哪读,就在哪声明)。

**声明错了怎么办 —— LIFT 已内建两级自动诊断**(不用再手动保留 delta 镜像 `docker diff`):

1. **`evolve-only` WARNING**([`commit_delta_image`](../../../src/lift/adapters/container/delta.py) 触发条件:白名单命中数 = 0):
   ```
   WARNING Delta preflight diff (evolve-only) [<container>]: no changes under evolve_paths=['/wrong/path'] — warmup produced no evolve artifacts
   ```
   —— 你声明的路径下什么都没写;下一行 `candidate` 会给出线索。

2. **`candidate unlisted evolve paths` INFO**(仅在上一条 WARNING 触发时追加):从 `docker diff` 全集里剔除 `_NOISE_PATH_PREFIXES`(`/tmp` / `/root/.cache` / `pip` / `apt` 等噪音黑名单)+ 已声明白名单,剩余的前 3 层目录按出现次数 top-5:
   ```
   INFO Delta preflight diff (candidate unlisted evolve paths) [<container>]: /root/.openhuman/users x67, /root/.openhuman/skill-registry x2 — 若这些是真进化产物,请把顶层目录加入 adapter.evolve_paths 声明
   ```
   —— 直接对着建议名单更新 `evolve_paths` 即可,不需要再手动 `docker diff` 反查。

3. **完整 `docker diff` 落盘**:`commit_delta_image` 无条件把原始 diff 全量落到 `results/{run_id}/delta_diff_{container_name}.txt`(约 MB 量级;含每条 `A|C|D <absolute_path>`)。集成期需要看任意深度的具体路径(log 摘要只按前 3 层聚合)时:
   ```bash
   grep -vE "^[ACD] (/root/\.cache|/tmp|/var/lib/(apt|dpkg))" \
     results/lift-runid-<run_id>/delta_diff_*.txt | head -80
   ```
   这个文件在 delta 镜像被清理后仍在,是集成期定位 `evolve_paths` 的黄金证据来源。

> **典型迭代路径**:新 runtime 首次跑 `--warmup-only` → 看到 `evolve-only` WARNING → 看 candidate 那行的 top 目录 → grep dump 文件确认那些目录里确实是 memory / wiki / skill 类内容 → 更新 `evolve_paths` 声明 → 二次跑,WARNING 消失即视为白名单声明正确。

## 2.2 `session.py`(容器启动)

模板:[`src/lift/adapters/genericagent/session.py`](../../../src/lift/adapters/genericagent/session.py)

必须做的:
1. `_CONTAINER_PREFIX = "evolve-<runtime>"` — 容器名前缀,方便 `docker ps` / cleanup grep。
2. `start_<runtime>_container` 调 `ContainerSession.start`,传 `port_mappings` / `env_vars` / `volume_binds` / `readiness_check` / `post_start_hooks` / `pre_cleanup_hooks`。
3. `default_volume_binds` + `task_volume_binds` 是标准 bind(`/workspace/outcome`、`/workspace/task`、`/workspace/benchmarks`),照抄即可。
4. `seed_eval_workspace`:宿主机端把 `workspace_seed/` 拷进 `workspace_dir`,留 `.lift-workspace-ready` marker。
5. `_reclaim_volume_ownership`:cleanup 前把 bind mount 目录 `chown` 回宿主用户,避免 root-owned 文件污染 `results/`。
6. **`env_vars` 覆写 `LANGFUSE_BASE_URL` / `LANGFUSE_HOST`**:宿主 `.env` 里通常写的是 `LANGFUSE_BASE_URL=http://localhost:3888`(宿主视角),通过 `env_file=Path.cwd()/".env"` 全量注入容器后,容器内 `localhost` 指向自己不通宿主 Langfuse。**Langfuse SDK v4 的 OTel span exporter 会读 `LANGFUSE_BASE_URL` env 覆盖 `Langfuse(host=...)` 显式构造参数**,即使 overlay 里 `mykey.py` host 写对了也会 0 plugin trace。修法:在 `env_vars` 层(`-e` flag,优先级高于 `env_file`)把 loopback host 段改写为 `host.docker.internal`。参考 GA [`session.py:38-53,179-184`](../../../src/lift/adapters/genericagent/session.py#L38-L53) 的 `_rewrite_langfuse_host_for_container` 辅助函数。

> **gateway-less runtime**(GA 这种):`port_mappings=[]`、`readiness_check=None`,`docker exec` 直接调起进程;
> **gateway-ful runtime**(OpenClaw 这种):`port_mappings=[(host_port, 80, "tcp")]`、`readiness_check=ReadinessCheck(...)` + token / cookie 透传。

## 2.3 `container_exec.py`(exec 上下文)

定义 `<Runtime>ContainerContext` dataclass,最简只含 `container_name: str`(GA 样板);如果有 gateway / token / 端口要带,就再加字段。提供 `exec_<runtime>_async` 包一层 `docker_exec_async`。

## 2.4 `chat_agent.py`(ChatAgent 协议)

**核心**:实现 `ChatAgent.chat(message, *, session_id) -> str`。

三种主流 transport:
- **HTTP REST gateway**(OpenClaw):post 到容器内 gateway,response 直接拿 turn 输出。
- **HTTP JSON-RPC**(OpenHuman):`POST http://127.0.0.1:{host_port}/rpc`,body 是 `{"jsonrpc":"2.0","id":1,"method":"<runtime>.agent_chat","params":{"message":..., "thread_id":session_id}}`;response 里取 `result.result`。参考 [`src/lift/adapters/openhuman/chat_agent.py`](../../../src/lift/adapters/openhuman/chat_agent.py)。
- **文件 I/O**(GA):写 `input.txt` → `docker exec -d` 启子进程 → 轮询 `output<N>.txt` 直到 `[ROUND END]`。GA 样板见 [`chat_agent.py:39-189`](../../../src/lift/adapters/genericagent/chat_agent.py#L39-L189)。

**统一约束**:
- `WorkerJudgerPairFactory.__call__(task)` 每次 **新建 work / judge 各一个 ChatAgent 实例**,互相隔离。
- `work_session_id = f"user-{short_id()}"`、`judge_session_id = f"judge-{short_id()}"` — 这俩前缀被 `langfuse_trace_stitch` 当作分类信号,**不要改前缀**。
- 单轮 wall-clock 上限统一 1000s(`CHAT_EXEC_TIMEOUT_SECONDS = 1000.0`),超时返回 `CHAT_EXEC_TIMEOUT_MARKER` 前缀字符串走 LIFT 的 provider error 重试通道。

---

## 3. 注册到 CLI(`registry.py` + `paths.py`)

### 3.1 `src/lift/adapters/registry.py`

[`registry.py`](../../../src/lift/adapters/registry.py) 两处改:

1. `SUPPORTED_RUNTIMES` 元组加新 runtime 名(小写下划线,CLI `-r` 直传值)。
2. `create_adapter` 加一条 `if normalized == "<runtime>": from ... import ...; return XAdapter(options)`。

> Lazy import 故意保留 — 不要在文件顶部 import adapter,免得用户没有该 runtime 依赖时整个 CLI 崩。

### 3.2 `src/paths.py`

[`paths.py`](../../../src/paths.py) 加常量:

```python
<RUNTIME>_AGENT_DIR = PROJECT_ROOT / "agent-runtimes" / "<runtime>"
<RUNTIME>_DOCKER_IMAGE = "lift-<runtime>:latest"
<RUNTIME>_WORKSPACE_SEED_DIR = <RUNTIME>_AGENT_DIR / "workspace_seed"
```

衍生 runtime(如 `_with_evolve`)通常只多一条 `<RUNTIME>_WITH_EVOLVE_DOCKER_IMAGE`,目录沿用基础版。
