# 常见坑速查

集成新 runtime / 排障时按症状定位到具体文档。前 10 行是"接入必踩"的高频坑;后半 §2 是未来 TODO;§3 是参考实现速查。

## 1. 症状 → 原因 → 定位跳转

| 现象 | 原因 | 排查 |
|---|---|---|
| `docker exec` 起不来 agent 进程 | 上游硬编码 cwd 没 patch | `docker exec <c> grep -n script_dir /opt/<runtime>/agentmain.py`;见 [image-scaffold](./image-scaffold.md) §1.4 |
| Langfuse 全无 plugin trace(`plugin=0`) | ① overlay 没生效;② trace name 没在 `LANGFUSE_PLUGIN_TRACE_NAMES`;③ 端点被 `.env` 里的 `localhost` 污染 | `docker exec <c> head /opt/<runtime>/plugins/langfuse_tracing.py` 看是否 LIFT overlay 版本;容器日志有 `Failed to export span batch due to timeout` → 走端点排查 |
| trace 拼装 work / judge 不对应 | session_id 前缀错;不是 `user-*` / `judge-*` | grep `WorkerJudgerPair` 调用处的 sid 拼接逻辑;见 [adapter-quartet](./adapter-quartet.md) §2.4 |
| 镜像构建"诡异地快" / 改了 plugin 没生效 | docker layer cache 全命中,COPY 没触发重打 | 改完 plugin 强制 `docker build --no-cache` 重打;或 `touch agent-runtimes/<runtime>/<file>` 让 mtime 变 |
| build 期 git clone 卡死 | GitHub 直连失败 | `<RUNTIME>_GIT_URL` 用 `https://ghfast.top/<github URL>` 反代;见 [image-scaffold](./image-scaffold.md) §1.6 |
| build 期 apt / pip 卡死 | 公网仓库不通 | 设 `APT_MIRROR` + `PIP_INDEX_URL`(字节内网见 [image-scaffold](./image-scaffold.md) §1.6) |
| build-image 静默成功但凭据没注入 | `.env` 没被 source / Dockerfile ARG / build-image.sh `--build-arg` / install-in-image.sh sed 三方没同步 | `docker run --rm <image> grep __ /opt/<runtime>/mykey.py` 应 0 行;非 0 行说明三方有缺口(见 [image-scaffold](./image-scaffold.md) §1.2) |
| GA 模型回复 "I cannot find this tool" | 只 append 了英文 schema,中文模型加载的是 `tools_schema_cn.json` | `docker run --rm <image> python -c 'import json; print([t["function"]["name"] for t in json.load(open("/opt/GenericAgent/assets/tools_schema_cn.json"))])'`;见 [image-scaffold](./image-scaffold.md) §1.5 |
| `MODEL_NAME` 在两个 runtime 间互相污染 | `.env` 共享 `MODEL_NAME`,但 OpenClaw / GA 期望值不同 | 各 runtime 用专属变量名(`GENERICAGENT_MODEL_NAME` / `OPENCLAW_MODEL_NAME`),fallback 到 `MODEL_NAME` |
| 容器内 Langfuse 连不上宿主 | `LANGFUSE_HOST` 写了 `localhost` / `127.0.0.1`(**镜像里**或**宿主 `.env` 通过 `env_file` 注入**——SDK v4 的 OTel span exporter 会读 env 覆盖 `Langfuse(host=...)` 显式参数,即使 overlay 完全正确也 0 plugin trace) | ① 镜像里固定写 `http://host.docker.internal:3000`(`Dockerfile` ARG default 已这样);② LIFT `start_*_container` 的 `env_vars` 覆写 `LANGFUSE_BASE_URL` / `LANGFUSE_HOST`(参考 GA `_rewrite_langfuse_host_for_container`,见 [adapter-quartet](./adapter-quartet.md) §2.2 第 6 点);③ Linux 下 docker run 加 `--add-host host.docker.internal:host-gateway`(LIFT `start_*_container` 已处理) |
| `agent_source` 未被后处理识别 | `SUPPORTED_RUNTIMES` 忘了加新 runtime,argparse choices / dispatch 都从这里派生 | 在 [`registry.py`](file:///root/workspace/agent_evolve_evaluation/src/lift/adapters/registry.py#L12) 的 tuple 补一行即可;`grep -rn "AgentSource\s*=\s*Literal" src/` 应为 0 行(历史 Literal 已下线) |
| `Judge response is not valid JSON` 重试日志 | 这是 prompt sanity 设计行为,不是 bug | 偶发可忽略;高频出现说明 judge prompt 没渲染干净 |
| `logs/<run_id>.log` 看到 `wait output timeout` | GA 主循环 600s 内没产出 / 死循环 / LLM 卡 | `docker exec <c> tail -50 /opt/GenericAgent/temp/<iodir>/ga.stderr.log` 看 GA 自己日志 |
| Langfuse trace 上 `Session` / `Tags` 列空 | overlay 还在用 v3 的 `obs.update_trace(...)` / `client.update_current_trace(...)`,4.x SDK 已删除 | overlay 改成 `propagate_attributes(session_id=, tags=)` 上下文管理器 + `start_as_current_observation`(见 [image-scaffold](./image-scaffold.md) §1.3) |
| 多轮对话 Langfuse 上 `messages` 只有某一轮 / 拼不出整段会话(单进程跨多轮 runtime) | transcript 累积器绑在单次 `agent_runner_loop` 的 `locals()` / `threading.local` 上,每轮 `agent_before` 各自重建 → 每轮 root span 只含本轮增量 | 累积器提升到**进程级全局** dict,root span 仍每轮一条、每轮写"截至当前全量"(见 [image-scaffold](./image-scaffold.md) §1.3.1 规则 1+2);用 2 轮对话 + `session_id=user-*` 搜 Langfuse 判定 |
| 多轮 runtime 的 trace Input/Output 全 `undefined`,且整进程只留一条没闭合的 span | 想做"整进程一条 root span、`atexit` 收尾",但容器被 `docker rm -f`(SIGKILL)杀掉,`atexit` 不执行 → span 从没 end/flush | 改回**每轮 `agent_after` 同步 `__exit__` + `flush`**,别依赖进程退出钩子收尾(见 [image-scaffold](./image-scaffold.md) §1.3.1 规则 2) |
| 多轮 runtime 的 `tool_use_num` 虚高(约等于实际值 × 轮次) | `toolCallBlocks` 写成了跨轮全局累积值,但后处理 `build_work_analytics` 对它按轮 SUM → 重复累加 | root metadata 里 `toolCallBlocks` 写**本轮 per-round 增量**(`messages` 才用全量 champion 口径),见 [image-scaffold](./image-scaffold.md) §1.3.1 规则 3 |
| static dashboard tools 列空,但 `*_backfilled.json` 里 `tool_calls` 已有数 | B 路径 langfuse 兜底拿到了值但没回写 tracker,`tracker.snapshot()` 仍是 None → 嵌入 HTML 后显示 "—" | 确认 `run_post_process_pipeline` 调了 `tracker.set_phase_tool_calls(...)`(见 [postprocess-and-stitching](./postprocess-and-stitching.md) §5.3.1);运行期实时 dashboard 看不到 B 路径 tools 是设计行为 |
| evolved 与 baseline 结果几乎一致(improvement ≈ 0),或 LLM 明说"写了 memory"但 delta 镜像里没有 | Warmup 期 agent 的 evolve 产物落进了 bind mount / tmpfs(LLM 用 `memory/xxx` 相对路径,cwd 又在 bind mount 之内),`docker commit` 没捕获到 → delta 镜像内容 = baseline | 走 [three-layer-verification](./three-layer-verification.md) 证据 C 检查 delta diff;若 diff 为空回 [evolve-artifact-contract](./evolve-artifact-contract.md) 三点错位排查 |
| pipeline 日志出现 `Delta preflight diff (evolve-only) ... no changes under evolve_paths=...` WARNING,但 `full` 有大量变更 | `evolve_paths` 白名单声明与实际落地路径不符(新 runtime 集成时最常见——上游文档给的路径与代码实际写的路径有偏差) | 直接看 WARNING 下面那行 `candidate unlisted evolve paths ... top: /X x67, /Y x2`——LIFT 已经从 `docker diff` 全集里剔除噪音后挑好了候选。对着建议名单更新 `adapter.evolve_paths`,重跑 `--warmup-only` 直到 WARNING 消失。若 candidate 也没线索,grep `results/{run_id}/delta_diff_*.txt` dump 文件自查(见 [adapter-quartet](./adapter-quartet.md) §2.1 "声明错了怎么办" / [three-layer-verification](./three-layer-verification.md) §C.1-C.2) |
| 流水线全绿、report.json `success=true`,但 work agent 回复里出现 "I cannot access" / "no attachment" / `q1_materials.*not found` 等逃避语 | task materials bind mount 路径与 agent 侧 cwd / system prompt 里的路径不一致;LLM 拿到任务描述里的相对路径解析不出真实位置 | 走 [three-layer-verification](./three-layer-verification.md) 证据 A'.1 命中项;`docker exec <c> ls -la /workspace/task /root/.openclaw/workspace` 对比宿主 bind mount 目录,核对 `session.py:task_volume_binds` 与上游 cwd patch 是否指向同一目录;必要时在 workspace startup hook 里加 `qN_materials/ → cwd` 的软链 |
| Judge `content_score` 全 0 或全 1(A'.3 分布异常) | 全 0:material 缺失/LLM 拿不到任务上下文,judge 一律判 fail;全 1:judge prompt 里 rubric 或任务描述被 truncate,judge 无凭无据一律放行 | 打开 dashboard 抽 1 条 judge dialogue,检查 rubric 字段与任务描述是否完整;再回 A'.1 排查 material 挂载 |
| 首次 chat 直接返回 `SESSION_EXPIRED` / `backend session not active` / 强制走 OAuth 登录 | 上游 runtime(尤其闭源 Rust / Go 二进制)没有 Python plugin 系统,headless 模式下缺少显式 CLI 开关 → 默认走 backend `app-session` JWT 校验 | 先 `docker run --rm --entrypoint sh <image> -c 'strings /usr/local/bin/<binary> \| grep -iE "session\|agentbox\|bypass\|headless\|maas" '  \| head -50` 反查隐藏 env 开关;OpenHuman 案例:找到 `OPENHUMAN_AGENTBOX_MODE=1` + `GMI_MAAS_BASE_URL/API_KEY/MODELS` 三件套,`chat-factory` 会走 "AgentBox mode ... bypassing app-session gate" 分支直连 OpenAI 兼容端点(见 [Dockerfile:81-95](file:///root/workspace/agent_evolve_evaluation/agent-runtimes/openhuman/Dockerfile#L81-L95));能通过 env 绕过就**别**上 sed 二进制补丁 |
| CSV / dashboard `total_tokens=0`,但 `*_backfilled.json` 里 `global_stats.total_tokens` 明显有值 | `_make_row_openclaw`(default)只识别 `usage.totalTokens`(camelCase);OpenHuman / OpenAI SDK 原生 schema 是 `{prompt_tokens, completion_tokens, total_tokens}` / `{input, output, cached_input}`,匹配不上被静默丢弃 | 新增 `_make_row_<runtime>` 分支从 `global_stats.total_tokens` 累加(详见 [postprocess-and-stitching](./postprocess-and-stitching.md) §4.1);`cached_token` 同理从 assistant messages `usage.cached_input` 累加。验证:`head -2 results/<run>/*_comparison_metrics.csv` 应看到 `total_tokens` 列非零 |
| Agent 回复 "搜索权限没开 / 无法联网 / 我没有工具" 但工具明明已配 | LLM 幻觉话术;工具 schema 未主动暴露给 LLM 或 prompt 没触发 tool 触发链,模型选择保守拒答,而不是真实的系统权限报错 | 双管齐下:① suite JSON 的 `query` 与 `expected_result.trajectory_reqs` 明确要求 "至少调用一次 `web_search_tool` 或 `web_fetch`";② `workspace_seed/AGENTS.md` 列出可用工具清单并显式禁止 "权限没开/无法联网" 类逃避语。参考 [hello_multi.json](file:///root/workspace/agent_evolve_evaluation/assets/benchmarks_demo/hello_multi.json) 和 [openhuman/workspace_seed/AGENTS.md](file:///root/workspace/agent_evolve_evaluation/agent-runtimes/openhuman/workspace_seed/AGENTS.md) |
| CSV `cache_read_tokens` / `reasoning_tokens` 全 0 但 provider 明明支持 | 5 字段落库链路某一层断了(A 层 microtask 竞争 / non-stream 漏 wrap / B 层塞错字段 / C 层 camelCase 兼容缺失) | 详见 [token-observability](./token-observability.md) §2-§5 三层断层图与逐层修法 |

## 2. 未来优化 TODO

集成过程中沉淀出来的可选增强项,暂未落地;后续接入新 runtime 时踩到相关坑可以顺手实现掉。

- [ ] **`Delta preflight diff` 结构化输出到 report.json**:目前 diff 摘要只落在 pipeline 日志(`Delta preflight diff (...): +NA ~NC -ND ...`)。可以把 `+NA ~NC -ND` 加它的 top-paths 数组挂到 `PhaseRun.langfuse` 平级的 `PhaseRun.delta_diff` 字段(或 `SuiteRun.delta_diff`),让后处理 CSV / HTML dashboard 也能一眼看出"这一轮 warmup 有没有真的落东西",不用翻日志。见 [container/delta.py](file:///root/workspace/agent_evolve_evaluation/src/lift/adapters/container/delta.py) `_summarize_diff` 的返回值改成 dict 就行。
- [ ] **evolve 产物落地契约的静态自检脚本**:把 [evolve-artifact-contract](./evolve-artifact-contract.md) "三点错位" 验证清单(引擎读路径 / system prompt 提示路径 / Dockerfile mkdir 路径)沉淀成 `agent-runtimes/<runtime>/verify_evolve_contract.sh`,接入新 runtime 时 `bash verify_evolve_contract.sh <image>` 一键跑完输出 pass/fail,比每次 grep 手敲更省事。GA 的 3 处 `sed` patch 也可以做成脚本形式复用给下一个 runtime。
- [ ] **Langfuse SDK v3 → v4 overlay 迁移脚本**:见 [image-scaffold](./image-scaffold.md) §1.3 —— 目前只在文档里描述了 v4 的 `propagate_attributes + start_as_current_observation` 用法,下次遇到只支持 v3 API 的上游 plugin 时,需要手动改。可以固化一个 `overlay_migrate_v3_to_v4.py` codemod(针对 `observation.update_trace(...)` / `client.update_current_trace(...)` 的 AST 替换)加进 skill。

## 3. 参考实现速查

| 场景 | 看哪个文件 |
|---|---|
| 容器无 gateway / 文件 I/O 协议 | [`src/lift/adapters/genericagent/`](file:///root/workspace/agent_evolve_evaluation/src/lift/adapters/genericagent) |
| 容器有 gateway / HTTP 协议 | [`src/lift/adapters/openclaw/`](file:///root/workspace/agent_evolve_evaluation/src/lift/adapters/openclaw) |
| 衍生 runtime(叠 evolve 钩子) | [`src/lift/adapters/genericagent_active_evolve/adapter.py`](file:///root/workspace/agent_evolve_evaluation/src/lift/adapters/genericagent_active_evolve/adapter.py) |
| 多容器 warmup(群体记忆) | [`src/lift/adapters/openclaw_multi_user/`](file:///root/workspace/agent_evolve_evaluation/src/lift/adapters/openclaw_multi_user) |
| 镜像脚手架最简版 | [`agent-runtimes/genericagent/`](file:///root/workspace/agent_evolve_evaluation/agent-runtimes/genericagent) |
| 第三方工具 plugin 模板 | [`agent-runtimes/genericagent/firecrawl_plugin.py`](file:///root/workspace/agent_evolve_evaluation/agent-runtimes/genericagent/firecrawl_plugin.py) |
| sanity benchmark | [`hello.json`](file:///root/workspace/agent_evolve_evaluation/assets/benchmarks_demo/hello.json) / [`test_search.json`](file:///root/workspace/agent_evolve_evaluation/assets/benchmarks_demo/test_search.json) / [`integration_check.json`](file:///root/workspace/agent_evolve_evaluation/assets/benchmarks_demo/integration_check.json) |
