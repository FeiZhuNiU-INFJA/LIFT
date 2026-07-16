---
name: "lift-integrate-agent-runtime"
description: "LIFT 评测框架接入新 agent runtime 的端到端清单:镜像脚手架 + adapter 三件套 + registry 注册 + Langfuse trace 拼装 + 5 字段 token 落库 + 验收。用户说\"集成/接入新 agent runtime\"或想新增 CLI `-r` 可选项、或需要修复某 runtime 的 cache_read/reasoning 统计时调用。"
---

# LIFT: 集成新 Agent Runtime

把一个 agent(OpenClaw / GenericAgent / Hermes / OpenHuman 之类)接入 LIFT 评测流水线,需要同步五个层面:

**镜像脚手架** → **adapter 三件套** → **CLI 注册** → **后处理类型同步** → **Langfuse trace 拼装 + 5 字段 token 落库**

这份 skill 把三次成功集成(OpenClaw + GenericAgent + Hermes + OpenHuman)+ 一次全面 token 观测治理沉淀出来的所有 touchpoint 固化成清单,减少漏改。**观测(token 5 字段)是接入的内建步骤,不是事后排障**——[docs/token-observability.md](./docs/token-observability.md) 是必读章节之一。

> **原则**:先把 baseline 跑通(hello.json sanity → integration_check.json 验收),再考虑 `_with_evolve` / `_active_evolve` 之类衍生 runtime。衍生只是在 baseline adapter 上叠 `evolve_after_warmup` 钩子或镜像 tag。

> **两个反例警示**(在集成过程中主动验证,别只信"跑通了 hello.json"):
> 1. **进化产物不进 delta 镜像**([docs/evolve-artifact-contract.md](./docs/evolve-artifact-contract.md)) —— warmup 阶段 agent 写的 memory / skills 如果落进 bind mount,`docker commit` 不会捕获,evolved 与 baseline 完全一致,improvement 恒为 0
> 2. **hello.json 走通 ≠ evolve 生效 ≠ token 5 字段落库**([docs/three-layer-verification.md](./docs/three-layer-verification.md)) —— 必须跑一个会让 agent 有话可记的复杂 suite 并做 Log × Langfuse × Layer 三层证据交叉验证,同时审计 5 字段 token 落库

---

## 何时用这个 skill

- 用户说 "集成新 agent runtime" / "接入新 runtime" / "新增 CLI `-r` 可选项"
- 用户报告某 runtime 的 `cache_read_tokens` / `reasoning_tokens` 全 0 或 NaN([docs/token-observability.md](./docs/token-observability.md) 主要 workflow)
- 用户说 "evolved 和 baseline 结果一样" / "improvement 是 0" / "delta 里没东西"([docs/evolve-artifact-contract.md](./docs/evolve-artifact-contract.md) + [docs/three-layer-verification.md](./docs/three-layer-verification.md) 证据 C)
- 用户说 "trace 拼不上" / "Langfuse 上没有 plugin trace" / "static dashboard tool_calls 列空"([docs/adapter-quartet.md](./docs/adapter-quartet.md) + [docs/postprocess-and-stitching.md](./docs/postprocess-and-stitching.md))

---

## 必备前置认知

- LIFT 走 **warmup → docker commit → holdout** 流水线:每个 runtime 都要能从 baseline 镜像 commit 出"演化过的"镜像(不演化也得 commit,让流水线统一)
- LIFT 通过 **`AgentRuntimeAdapter` ABC + `ContainerAgentRuntimeAdapter` 模板方法** 接入容器化 agent;非容器型 runtime(如 Hermes 直连 OpenAI)走 `AgentRuntimeAdapter` 直接 override
- Chat 协议是 `WorkerJudgerPair`(一次 task 一对独立 ChatAgent;work / judge 互不干扰),由 `worker_judger_factory` 在每题创建
- Langfuse trace 拼装要求 plugin 侧 trace 的 **`name` 在 `LANGFUSE_PLUGIN_TRACE_NAMES` 白名单里**,且 trace 的 **`session_id` 与 LIFT 侧 `work-/judge-` session 对齐**
- **Token 5 字段口径**必须先读 [docs/token-observability.md](./docs/token-observability.md) §0 —— `reasoning ⊆ output`,不是 sibling;`total = input + output + cache_read`,reasoning 不入 total

参考实现速查:
- 容器型简单样板:[`src/lift/adapters/genericagent/`](file:///root/workspace/agent_evolve_evaluation/src/lift/adapters/genericagent)(无 gateway / 无 evolve 钩子,最干净)
- 容器型完整样板:[`src/lift/adapters/openclaw/`](file:///root/workspace/agent_evolve_evaluation/src/lift/adapters/openclaw)(有 HTTP gateway / readiness check / token)
- 镜像脚手架:[`agent-runtimes/genericagent/`](file:///root/workspace/agent_evolve_evaluation/agent-runtimes/genericagent)、[`agent-runtimes/openclaw/`](file:///root/workspace/agent_evolve_evaluation/agent-runtimes/openclaw)

---

## 接入决策树

```
新 runtime 接入 →
├── 是否走容器?
│   ├── 是 → ContainerAgentRuntimeAdapter 模板;走 docs/adapter-quartet.md
│   └── 否 → AgentRuntimeAdapter 直接 override(参考 Hermes)
│
├── 上游是否 Python?
│   ├── 是 → langfuse_tracing_overlay.py 用 hook 覆盖(GenericAgent 样板)
│   ├── Node → 写独立 plugin(OpenClaw langfuse-tracer 样板)
│   └── 闭源二进制 → env 绕过 + reverse mode(OpenHuman 样板)
│
├── 单进程跨多轮?(文件 I/O 型 / 长连接 stdin-stdout 型)
│   └── overlay 必须处理进程级 transcript 累积器 + 每轮 root span
│      详见 docs/image-scaffold.md §1.3.1
│
├── 进化产物是否落 container FS 层?
│   ├── 是(需要 evolve) → docs/evolve-artifact-contract.md 三点错位检查
│   └── 否 → skip
│
└── Token 5 字段落库 →(必修,不是可选)
    docs/token-observability.md 全流程
```

---

## 5 步 workflow

按顺序推进,前一步过了再进下一步。每一步跳转到对应 docs 文档看细节。

### Step 1. 镜像脚手架

新建 `agent-runtimes/<runtime>/`,准备 Dockerfile / build-image.sh / install-in-image.sh / mykey.py.template / langfuse_tracing_overlay.py / workspace_seed/。

⚠ **overlay 里 usage 必须塞 `usageDetails` 才能落库**,见 [docs/token-observability.md §断点 B](./docs/token-observability.md)
⚠ **单进程跨多轮 runtime** overlay 需按 [docs/image-scaffold.md §1.3.1](./docs/image-scaffold.md) 处理进程级 transcript

详见 → [docs/image-scaffold.md](./docs/image-scaffold.md)

### Step 2. Evolve 产物落地契约

在写 adapter 之前,先确定 agent 的 memory / skills / wiki 会落到容器 FS 的**哪个绝对路径**。三点必须对齐:

1. 引擎读路径(上游 agent 加载 memory 时读哪里)
2. system prompt 提示路径(prompt 里告诉 LLM "写到哪里")
3. Dockerfile mkdir 路径(镜像基线必须存在)

任何一点错位 → warmup 期产物落进 bind mount / tmpfs → `docker commit` 不捕获 → evolve **无效**。

详见 → [docs/evolve-artifact-contract.md](./docs/evolve-artifact-contract.md)

### Step 3. Adapter 三件套 + registry 注册

新建 `src/lift/adapters/<runtime>/`:
- `adapter.py`:实现 4 个抽象方法(`produce_delta` / `worker_judger_factory` / `run_before_load` / `run_after_load`),声明 `evolve_paths` 白名单
- `session.py`:`start_baseline_container` / `start_evolved_container` / `task_volume_binds` / `env_vars`
- `container_exec.py`:与容器内 agent 通信(HTTP gateway 或 `docker exec` + 文件 I/O)
- `chat_agent.py`:实现 `WorkerJudgerPair`,session_id 前缀走 `user-` / `judge-`

CLI 注册:
- [`registry.py::SUPPORTED_RUNTIMES`](file:///root/workspace/agent_evolve_evaluation/src/lift/adapters/registry.py#L12) 补一行
- [`paths.py`](file:///root/workspace/agent_evolve_evaluation/src/paths.py) 加 `<RUNTIME>_AGENT_DIR / DOCKER_IMAGE / SEED_DIR`

详见 → [docs/adapter-quartet.md](./docs/adapter-quartet.md)

### Step 4. 后处理 + trace 拼装

- `src/models.py::LANGFUSE_PLUGIN_TRACE_NAMES` 加 `"<runtime>-plugin"`,让后处理能匹配 plugin trace
- 如果 trace 布局不复用 OpenClaw sid-only 或 transcript usage schema 不含 `totalTokens`,新增 `_make_row_<runtime>` 分支

⚠ **5 字段 provider 归一 + camelCase/snake_case 兼容**,见 [docs/token-observability.md §断点 C](./docs/token-observability.md)
⚠ **tool_calls 兜底链**必须回写 tracker,否则静态 dashboard 空,见 [docs/postprocess-and-stitching.md §5.3.1](./docs/postprocess-and-stitching.md)

详见 → [docs/postprocess-and-stitching.md](./docs/postprocess-and-stitching.md)

### Step 5. 验收

按 [docs/acceptance-checklist.md](./docs/acceptance-checklist.md) 逐项过:
- §6.0 本地测试工作流(nohup + dashboard + tail)
- §6.1 镜像构建检查
- §6.2 hello.json sanity(连通性)
- §6.3 trace stitching 对齐
- §6.4 test_search.json(可选,联网工具)
- §6.5 **三层证据交叉验证**(必跑) — [docs/three-layer-verification.md](./docs/three-layer-verification.md) 包含证据 A / A' / B / B' / C 五种交叉证据
- §6.6 衍生 runtime(可选)

⚠ **5 字段落库审计是硬指标**,不是 nice-to-have —— 详细 3 层断层图 + 逐层修法见 [docs/token-observability.md](./docs/token-observability.md)

---

## 常见坑速查

按症状定位到具体 docs 文档,详见 → [docs/common-pitfalls.md](./docs/common-pitfalls.md)

高频前 5:
1. `docker exec` 起不来 → 上游硬编码 cwd 没 patch,见 [docs/image-scaffold.md §1.4](./docs/image-scaffold.md)
2. Langfuse 全无 plugin trace → overlay 没生效 / trace name 未加白名单 / `LANGFUSE_HOST=localhost` 污染
3. `cache_read_tokens` / `reasoning_tokens` 全 0 → 5 字段落库链路某层断了,走 [docs/token-observability.md §2-§5](./docs/token-observability.md)
4. evolved == baseline → 进化产物三点错位,走 [docs/evolve-artifact-contract.md](./docs/evolve-artifact-contract.md)
5. `Delta preflight diff (evolve-only) ... no changes` WARNING → `evolve_paths` 声明错,看 log 里 `candidate unlisted evolve paths` 建议名单

---

## docs 目录索引

| 文档 | 何时看 |
|---|---|
| [docs/image-scaffold.md](./docs/image-scaffold.md) | Step 1;新建 `agent-runtimes/<runtime>/` 时 |
| [docs/evolve-artifact-contract.md](./docs/evolve-artifact-contract.md) | Step 2;要评估 evolve 有效性时 |
| [docs/adapter-quartet.md](./docs/adapter-quartet.md) | Step 3;新建 `src/lift/adapters/<runtime>/` 时 |
| [docs/postprocess-and-stitching.md](./docs/postprocess-and-stitching.md) | Step 4;要理解 trace 拼装、tool_calls 兜底、CSV 列生成 |
| [docs/token-observability.md](./docs/token-observability.md) | Step 1 / 4 / 5 都会跳来看;`cache_read / reasoning` 出现 0 或 NaN 时的排障 workflow |
| [docs/acceptance-checklist.md](./docs/acceptance-checklist.md) | Step 5;逐项验收 |
| [docs/three-layer-verification.md](./docs/three-layer-verification.md) | Step 5 §6.5;必跑,证据 A / A' / B / B' / C |
| [docs/common-pitfalls.md](./docs/common-pitfalls.md) | 遇到问题时按症状速查 |
| [docs/environment-cleanup.md](./docs/environment-cleanup.md) | 评测中途 Ctrl-C / OOM 后清残留容器 & 镜像;集成迭代 rebuild 前清中间层 |

---

## 配套 skill

| skill | 何时调用 |
|---|---|
| [`setup-lift-env`](file:///root/workspace/agent_evolve_evaluation/skill/setup-lift-env/SKILL.md) | 还没装 conda / docker / langfuse / 跑过 hello.json 的全新机器 |
