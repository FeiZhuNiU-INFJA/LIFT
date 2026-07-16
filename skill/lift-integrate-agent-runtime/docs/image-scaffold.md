# 镜像脚手架:`agent-runtimes/<runtime>/`

> [`SKILL.md`](../SKILL.md) 的第 1 步深化文档。本文件覆盖:目录结构、Dockerfile / build-image / install-in-image 三方同步、Langfuse overlay 关键约束、上游硬编码 patch、工具 schema、字节内网构建。
> 关于**进化产物落地契约**(Docker commit 陷阱)见姊妹文档 [`evolve-artifact-contract.md`](evolve-artifact-contract.md)。
> 关于 overlay 里 5 字段 token 落库口径见 [`token-observability.md`](token-observability.md)。

新建目录 `agent-runtimes/<runtime>/`,复制 GenericAgent 样板做减法或加项。

| 文件 | 必要性 | 作用 |
|---|---|---|
| `Dockerfile` | 必须 | 多阶段构建 agent 镜像;ENTRYPOINT 一般是 `tini` + `tail -f /dev/null`(容器空转等 docker exec) |
| `build-image.sh` | 必须 | 读 `.env` 获取 WORK_OPENAI / Langfuse / 第三方 secret,`--build-arg` 透传 |
| `install-in-image.sh` | 必须 | 镜像内执行:`sed` 渲染 `mykey.py.template` → `mykey.py`、覆盖 `langfuse_tracing_overlay.py`、patch 上游硬编码 |
| `mykey.py.template` | 必须 | 凭据模板,占位符 `__WORK_OPENAI_API_KEY__` 等由 `install-in-image.sh` 用 sed 渲染 |
| `langfuse_tracing_overlay.py` | 必须 | LIFT 自有 tracing overlay:强制 root span name = `<runtime>-plugin`、注入 `session_id` / tags |
| `workspace_seed/` | 可选 | 容器内 `/workspace/task` 初始内容(如 README、人设文件);GA baseline 仅一个 README |
| `.dockerignore` | 推荐 | 屏蔽 `.git` / `temp/` 减小 build context |

## 1. `mykey.py.template` 占位符规范

```python
native_oai_config = {"name": "doubao", "apikey": "__WORK_OPENAI_API_KEY__", "apibase": "__WORK_OPENAI_BASE_URL__", "model": "__MODEL_NAME__", "api_mode": "openai-completions"}
langfuse_config = {"public_key": "__LANGFUSE_PUBLIC_KEY__", "secret_key": "__LANGFUSE_SECRET_KEY__", "host": "__LANGFUSE_HOST__"}
```

`install-in-image.sh` 里要:
1. `escape_sed` 转义所有 `__XXX__` 注入值(防 `/` 与换行污染 sed)。
2. 一条 `sed -e ... -e ...` 替换全部占位符。
3. 严禁把空字符串当成 valid secret 写进镜像 — 上层 `build-image.sh` 应预先 `${VAR:-}` fallback 成空,由 plugin 自身在运行期再做 "未配置" 校验(参考 [`firecrawl_plugin.py`](../../../agent-runtimes/genericagent/firecrawl_plugin.py))。

> **`MODEL_NAME` 必须是 provider-native 标识**:GA / 任意直连 work LLM 的 runtime,`MODEL_NAME` 要是 provider 真实 endpoint id(形如 `ep-2025xxxx-xxxxx`),不是 OpenClaw gateway 的命名空间值。如果同一个 `.env` 同时给 OpenClaw / GA 用,建议在 `build-image.sh` 里走专属变量名(参考 GA 用 `GENERICAGENT_MODEL_NAME` 优先于共享 `MODEL_NAME`,见 [`build-image.sh:61`](../../../agent-runtimes/genericagent/build-image.sh#L61)),避免一改就同时污染另一个 runtime 的镜像。

## 2. 三方同步:Dockerfile ARG/ENV ↔ build-image.sh `--build-arg` ↔ install-in-image.sh sed

新加一个凭据/开关变量必须**同时**改三个地方,少一处就静默失效:

| 位置 | 形式 | 例(FIRECRAWL_API_KEY) |
|---|---|---|
| `Dockerfile` | `ARG FOO=` + `ENV FOO=${FOO}` | [Dockerfile:96-103](../../../agent-runtimes/genericagent/Dockerfile#L96-L103) |
| `build-image.sh` | `FOO="${FOO:-}"` + `--build-arg "FOO=${FOO}"` | [build-image.sh:65,84](../../../agent-runtimes/genericagent/build-image.sh#L65) |
| `install-in-image.sh` | `escape_sed` + sed 替换占位符 | [install-in-image.sh:20,29](../../../agent-runtimes/genericagent/install-in-image.sh#L20) |

> 验证手段:build 完后 `docker run --rm <image> grep __ /opt/<runtime>/mykey.py` 应当 0 行。

## 3. `langfuse_tracing_overlay.py` 关键约束

每条 root span 必须满足:
- `name == "<runtime>-plugin"`(如 `"genericagent-plugin"` / `"openclaw-plugin"`)— 让 [`src/models.py:96-100`](../../../src/models.py#L96-L100) 的 `LANGFUSE_PLUGIN_TRACE_NAMES` 能匹配。
- `session_id = $LIFT_GA_SESSION_ID`(或 runtime 等价 env,每轮 chat 由 `docker exec -e` 注入)。
- `tags ⊇ {LIFT_EVAL_RUN_TAG, LIFT_<RUNTIME>_SESSION_ID}` — `langfuse_trace_merge` 既靠 tag 也靠 sid 做 work / judge 拼接,少一个就丢 trace。

> **Langfuse Python SDK v3 → v4 breaking change**:v3 时代常见的 `observation.update_trace(session_id=, tags=)` / `client.update_current_trace(...)` 在 SDK 4.x 上已经**全部移除**(`LangfuseAgent` / `Langfuse` 都没有这些方法),`hasattr` 检查会静默 False,导致 session_id / tags **永远写不到 trace 根** —— overlay 看上去工作正常,但 `langfuse_trace_stitch` 按 sid 找不到 plugin trace,dashboard tools 列空。
>
> 4.x 必须用上下文管理器:`from langfuse import propagate_attributes` + `_lf.start_as_current_observation(name=..., as_type='agent', ...)`。GA hook 是分散回调(`agent_before` / `agent_after` 不是 with 块),需要手动 `__enter__` / `__exit__` 配对,反序退出(先退 obs_cm 再退 attr_cm),用 thread-local 跨 hook 传递。参考 [`agent-runtimes/genericagent/langfuse_tracing_overlay.py`](../../../agent-runtimes/genericagent/langfuse_tracing_overlay.py)。
>
> 验证:容器内 `python -c "from langfuse import Langfuse; print(dir(Langfuse(...).start_as_current_observation(name='x')))"` 看不到 `update_trace` 即说明在 v4。再去 langfuse UI 看一条 trace 的 `Session` / `Tags` 列是否非空,是 → overlay 正确;空 → 还在用 v3 API。

**5 字段 usage 落库口径**:overlay 里 `usage_details=` 或 sibling `usageDetails` 必须传细分字段(cache/reasoning),否则 Langfuse ingestion 会静默丢弃。详见 [`token-observability.md` §断点 B](token-observability.md#4-断点-b-修复langfuse-存了但字段丢失)。

### 3.1 多轮对话的 root span 生命周期 — transcript 累积器必须**进程级**,root span **每轮一条** ⚠️

> **背景(GA 实测踩坑)**:文件 I/O 型 runtime(GA 那种:宿主写 `input.txt` / `reply.txt`,容器内一个常驻进程轮询)在**同一个进程**里跑多轮对话。但 GA 上游每收到一次 `reply.txt` 就重新调用一次 `agent_runner_loop`,而 `agent_before` / `agent_after` / `llm_before` / `llm_after` 这些 hook 是在 `agent_runner_loop` **内部**触发的(见 GA `agent_loop.py`:`_hook('agent_before', ...)` 在 loop 开头、`_hook('agent_after', ...)` 在 loop 结尾)。**结论:一次多轮对话 = N 次 `agent_runner_loop` = N 次 `agent_before`/`agent_after`**。真正的跨轮历史存在上游 LLM client 的 `backend.history` 里,每次 loop 的 `messages` 只从"本轮新 user"重建。

接入任何 **"单进程跨多轮"** 的 runtime(文件 I/O 型、长连接 stdin/stdout 型都算)时,overlay 的 transcript 累积必须满足两条,否则 Langfuse 上 `messages` 无法还原整段会话:

**规则 1:transcript / 工具名 累积器提升到进程级全局(模块级 dict),不要放 `threading.local`、更不要放单次 loop 的 `locals()`。**
- 每轮 `agent_before` 只往这个全局累积器**追加**当轮 user,`llm_after` 追加 assistant(含归一化 `tool_calls`)——跨轮持续 append。
- GA 单进程只服务一个 `session_id`(LIFT factory 每题每 role 各起一个进程 / iodir),所以"进程级 = 会话级",无需再按 sid 分桶。若未来复用单进程跑多 session,才需要按 session_id 分桶。

**规则 2:root span 仍然"每轮一条"(`agent_before` 建、`agent_after` 关闭并 `flush`),每轮把"截至当前轮的全量 transcript"写进该轮 root span 的 metadata。**
- 为什么不做成"整进程一条 root span、退出时才 end"?因为**容器是被 `docker rm -f`(SIGKILL)杀的,`atexit` / 信号 handler 根本不会执行** → root span 永远 end 不掉 / flush 不出去 → Langfuse 上该 trace 的 Input / Output 全是 `undefined`(实测踩过这个坑)。所以必须每轮在 `agent_after` 里同步 `__exit__` + `flush`,保证每一轮都是一条**完整落库**的 trace。
- 每轮写"截至当前的全量"后,**最后一轮的 trace 天然含整段会话**,后处理 `TranscriptChampion`([`langfuse_trace_fetch.py`](../../../src/report/langfuse_trace_fetch.py) 按 timestamp "取最晚一条" work transcript)拿到的正好是完整对话。这与 Hermes"每轮全量 transcript"的 champion 口径一致。

**规则 3(工具计数口径,容易反向踩坑):`messages` 走 champion"取最晚一条"(全量),但 `toolCallBlocks` 在后处理 [`build_work_analytics`](../../../src/report/langfuse_work_analytics.py) 里是按轮 SUM(`g.tool_call_blocks += t.stats.tool_call_blocks`)。**
- 因此写进 root metadata 的 `toolCallBlocks` / `toolRoundtrips` 必须是**本轮 per-round 增量**(用一个 `threading.local` 的 round 计数器),SUM 后才是正确总数。
- 如果 `toolCallBlocks` 也用全局累积值,SUM 会把 round1 的量重复累加进 round2 → 工具数虚高。
- `toolNamesDistinct` 用全局累积去重即可(它只做展示,不参与 SUM)。

> **判定 runtime 是否踩这条**:run 一次 2 轮对话,去 Langfuse 按 `session_id=user-*` 搜。
> - 只看到 **1 条** trace 且它只含某一轮的 messages → 你把 span/累积器绑在单次 loop 上了(每轮互相覆盖 / 各自独立),走规则 1+2。
> - 看到 **N 条** trace 但每条只含单轮增量、无法拼出全量 → 累积器没提升到进程级,走规则 1。
> - trace 的 Input / Output 是 `undefined` → span 没 end/flush(多半是想靠 atexit 收尾但被 SIGKILL),走规则 2 改回每轮 `agent_after` 收尾。
>
> 参考实现见 [`agent-runtimes/genericagent/langfuse_tracing_overlay.py`](../../../agent-runtimes/genericagent/langfuse_tracing_overlay.py) 的 `_STATE`(进程级全量)+ `_tls.round_tool_call_blocks`(per-round SUM)+ `agent_after` 每轮 `__exit__` + `flush`。

#### 3.1.1 怎么**发现**这个问题(两种诊断法)

跑一个**至少 2 轮对话**的 suite(`assets/benchmarks_demo/hello.json` 就够——它 warmup Q1 + holdout Q2,且 judge 复跑会天然凑出多轮),然后二选一验证。

**方法 A(推荐,最简,有实锤):直接查 `results` 里的 backfilled JSON。** 不用开 Langfuse UI,离线可查。

定位路径(结构见下方实测样本):`runs[].suites[].tasks[].{baseline|evolved}.langfuse.work_analytics.all_messages`,同级还有 `chat_turns`。

```bash
RID=lift-runid-<run_id>
JSON=results/$RID/${RID}_backfilled.json
python3 -c "
import json, collections
d = json.load(open('$JSON', encoding='utf-8'))
for rp in d['runs']:
    for s in rp['suites']:
        for t in s['tasks']:
            for ph in ('baseline','evolved'):
                wa = (t[ph].get('langfuse') or {}).get('work_analytics') or {}
                am = wa.get('all_messages') or []
                ct = wa.get('chat_turns') or []
                roles = collections.Counter(m.get('role') for m in am if isinstance(m, dict))
                users = roles.get('user', 0)
                flag = '  <-- 断裂!' if (len(ct) >= 2 and users < len(ct)) else ''
                print(f'{t[\"task_name\"]:6} {ph:8} chat_turns={len(ct)} all_messages={len(am)} roles={dict(roles)}{flag}')
"
```

**判定标准**:
- **健康**:`user` 数 ≈ `chat_turns` 数,`assistant` 数也随轮次增长;多条 user 的时间戳/内容各不相同(能看出是不同轮)。
- **断裂(就是本节 bug)**:`chat_turns >= 2` 但 `all_messages` 里**显然只有 1 条 user**(或 user 数明显少于 chat_turns),且 assistant 只有 1 条汇报 —— 说明只捕获了某一轮,前面的轮次没续上。

实测**健康样本**(`hello.json`,Q2 holdout,judge 复跑成 2 轮,来自 `results/lift-runid-ga-hello-test-1-h/lift-runid-ga-hello-test-1-h_backfilled.json`):

```
Q2     baseline chat_turns=2 all_messages=4 roles={'user': 2, 'assistant': 2}
Q2     evolved  chat_turns=2 all_messages=4 roles={'user': 2, 'assistant': 2}
```

`all_messages` 里两条 user 的时间戳不同(`[Mon ... 16:44:23]` 第一轮 vs `[Mon ... 16:44:57]` 第二轮),第二条 user 是 judge 的"你再试一次…"续问 —— 这就是续上了。若 bug 未修,这里会退化成 `chat_turns=2` 但 `roles={'user': 1, 'assistant': 1}`。

> backfilled JSON 的完整层级:`runs[] → suites[] → tasks[] → {baseline,evolved} → langfuse → work_analytics → {chat_turns[], all_messages[], global_stats{total_tokens, tool_call_blocks, ...}, trace_chain[]}`。`all_messages` 每条是 `{role, content, tool_calls?}`(GA overlay `_normalize_message` 规约后的最小形状)。

**方法 B(无 backfilled 产物 / 想看原始 trace 时):查 Langfuse UI。** 按 `session_id = user-*`(或 runtime 对应前缀)搜 root span,展开 `metadata.messages`:
- 如果只看到 **1 条** root trace 且它的 messages 明显没续上前面轮次 → 累积器/span 绑在单次 loop 上(§3.1 规则 1+2)。
- 如果 root trace 的 Input/Output 是 `undefined` → span 没 end/flush(§3.1 规则 2)。

> **证据优先级**:方法 A 有 backfilled JSON 落盘即有实锤,优先用。**如果你只是"怀疑"多轮没续上但手里没有 backfilled 产物 / 拿不到实际证据,不要主观下结论——让用户去 Langfuse UI 按 session_id 查 root span 的 messages 确认**,再决定要不要动 overlay。

## 4. patch 上游硬编码(如有)

GA 上游把 `Handler.cwd` 与 system prompt cwd 都硬编码成 `os.path.join(script_dir, 'temp')`,LIFT 把 task materials bind 到 `/workspace/task`,必须在 build 期 patch 上游源码(见 [`install-in-image.sh:51-85`](../../../agent-runtimes/genericagent/install-in-image.sh#L51-L85) 的 python in-place 替换)。换 runtime 时先 grep `script_dir` / `os.getcwd()` / `os.path.dirname(__file__)` 找类似硬编码。

## 5. 工具 schema 中英双份(如果 runtime 用了 GA 那套 schema)

GA 风格 runtime 通过 `assets/tools_schema.json`(英文)+ `assets/tools_schema_cn.json`(中文)两套声明告知 LLM 可用工具。GA 上游 `agentmain.py` 按模型名(`glm` / `minimax` / `kimi` 走 cn)切换。新加 plugin tool 时,**两套 schema 都要 append**,不然中文模型看不到工具。参考 [`install-in-image.sh:93-208`](../../../agent-runtimes/genericagent/install-in-image.sh#L93-L208) 的 idempotent append 实现(已存在则跳过,避免重复 append)。

## 6. 字节内网 / GitHub 拉取受限时的构建环境变量

镜像构建期可能卡在三个地方,全部通过环境变量切镜像源解决;这些变量都被 `build-image.sh` 透传给 Dockerfile:

| 卡点 | 环境变量 | 字节内网值 / 推荐值 |
|---|---|---|
| `apt-get update` | `APT_MIRROR` | `http://mirrors.byted.org` |
| `pip install` / `uv pip install` | `PIP_INDEX_URL` | `https://bytedpypi.byted.org/simple/` |
| `git clone <agent 上游>` | `<RUNTIME>_GIT_URL`(每个 runtime 自己的 build-arg) | 用 `ghfast.top` 反代前缀 |

**GitHub 反代写法**(参考 [`build-image.sh:54`](../../../agent-runtimes/genericagent/build-image.sh#L54)):

```bash
GIT_URL="${GENERICAGENT_GIT_URL:-https://ghfast.top/https://github.com/lsdefine/GenericAgent.git}"
```

注意是 `ghfast.top/` **前缀拼接** 完整 https URL,不是替换 host。新建 runtime 的 `build-image.sh` 默认值就这样写,公网环境用户不传变量也能跑(ghfast 公网可访问,只是慢一点),内网用户传专属变量覆盖即可。

**字节内网完整一行启动**:

```bash
APT_MIRROR=http://mirrors.byted.org \
PIP_INDEX_URL=https://bytedpypi.byted.org/simple/ \
  bash agent-runtimes/<runtime>/build-image.sh
```

`build-image.sh -h` 必须把这三个变量列在 `Override via env:` 区域(参考 [GA build-image.sh:25-38](../../../agent-runtimes/genericagent/build-image.sh#L25-L38)),方便后续接手者 `--help` 直接看到。

> **可选**:在 `build-image.sh` 里加"探测到 `mirrors.byted.org` 就自动默认 APT_MIRROR / PIP_INDEX_URL"逻辑(参考 [GA build-image.sh:25-35](../../../agent-runtimes/genericagent/build-image.sh#L25-L35)),免掉每次手敲 env;留一个 `LIFT_INTRANET_AUTODETECT=0` 兜底开关。
