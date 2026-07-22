# 验收清单

集成新 runtime 完成后按顺序过一遍;前一步过了再下一步。**hello.json 只能证连通性,evolve 是否真生效必须靠 [三层证据交叉验证](./three-layer-verification.md);token 5 字段落库必须靠 [token 观测](./token-observability.md) §1 接入检查清单**。

## 6.0 推荐本地测试工作流

LIFT 一次跑下来时间偏长(warmup + commit + holdout 串起来),推荐 nohup 后台启 + dashboard + tail 日志:

```bash
nohup python -m src.cli.lift_main \
  -r <runtime> \
  --benchmark_dir assets/benchmarks_demo \
  --suite hello.json \
  --run_id <run_id> \
  --dashboard 0.0.0.0:<port> \
  > logs/<run_id>.log 2>&1 &

tail -f logs/<run_id>.log               # 主进度看这里
# 浏览器开 http://<host>:<port>         # 结构化状态
```

> **不要用默认 `nohup.out`**:所有 run 会 append 到同一文件,多 run 并行 / 反复跑会互相污染。统一显式写 `logs/<run_id>.log`(先 `mkdir -p logs`),文件名对应 `results/lift-runid-<run_id>/`。

`assets/benchmarks_demo/` 里三个常用 sanity suite:

| suite | 结构 | 用途 |
|---|---|---|
| `hello.json` | 1 W + 1 H 寒暄 | 基本 chat / warmup-commit-holdout 流水线连通性 |
| `test_search.json` | 1 W + 1 H 联网题 | agent 联网工具是否生效(无联网工具的 runtime 可跳) |
| `integration_check.json` | 4 W + 2 H 个人偏好档案 | **集成验收专用**——每题显式让 agent"以后都遵守",最能触发 memory / reflection hook 往 `evolve_paths` 白名单目录真写产物,runtime-agnostic |

> `--dashboard 0.0.0.0:<port>` 远程机器开 dashboard 必须 `0.0.0.0`;只在本机调试用 PORT 单字段(默认绑 `127.0.0.1`)。

## 6.1 镜像构建

```bash
cd agent-runtimes/<runtime> && bash build-image.sh
docker images | grep lift-<runtime>
```

构建期检查:
- `mykey.py` 占位符全部替换:`docker run --rm <image> grep __ /opt/<runtime>/mykey.py` 应当 0 行
- `langfuse_tracing_overlay.py` 已覆盖:`docker run --rm <image> head /opt/<runtime>/plugins/langfuse_tracing.py`
- 上游 patch 已生效:`docker run --rm <image> grep -n /workspace/task /opt/<runtime>/<patched>.py`
- 运行期 import smoke:`docker run --rm <image> python -c 'import sys; sys.path.insert(0, "/opt/<runtime>"); import agentmain'`

## 6.1a `MAX_TOKENS` 落地审计(硬指标)

**不要相信"顶层 `.env` 有就等于生效"** —— 前车之鉴:GA / EvoScientist / OpenHuman 三家的
`.env → 容器 env → 上游 LLM 请求 body` 链路各断一处,长产出被服务端默认 4096 截断,LIFT
的 `success` / `content_score` 只看到 "content 不完整",看不到 truncation 根因。同一 runtime
的 baseline vs evolved 会被同样截断,delta 上 **完全对消**,数字看似正常,横向对比其它 runtime
才暴露。

集成完必须过三点证据链(缺一不可):

1. **容器 env 层**:`docker run --rm --env-file .env <image> env | grep MAX_TOKENS`
   应打印 `MAX_TOKENS=51200`(或 `.env` 里配的值)
2. **配置/代码层**:
   - Python runtime(GA / OpenClaw / EvoScientist):`docker run --rm <image> grep -rn max_tokens /opt/<runtime>/ | head` 应能看到实际读值(如 `mykey.py`、`config.yaml`、`langfuse_tracing_overlay.py` 的 `_get_request_payload` patch)
   - Rust / 无入口 runtime(OpenHuman):必须有 `max_tokens_proxy.py` 之类的容器内代理,`config.toml`
     的 `inference_url` 指向 `127.0.0.1:${LIFT_PROXY_PORT}/v3`
3. **HTTP payload 层(权威证据)**:实际抓一次 LLM 请求 body,确认里面有 `max_tokens` / `max_completion_tokens` / `max_output_tokens` 之一 == 期望值
   - 抓法 A:临时代理法 —— 起 fake upstream(`python -c` 起 `http.server.ThreadingHTTPServer` 打印收到的 body),把 runtime 的 base URL 指向它,发一个 hello 请求
   - 抓法 B:Langfuse trace 法 —— 跑一次 hello.json,到 Langfuse 找 LLM span,`input.metadata` 或 raw request 里查 `max_tokens` 字段
   - 抓法 C(仅 OpenHuman 类代理方案):`docker exec <cid> cat /workspace/task/max-tokens-proxy.log`,期望
     `patched={'injected': 'max_tokens', 'value': 51200}` 或 `body already has max_tokens`

只有三点全绿才算通过。**只有 env 层 / 只有代码 grep 到不算数** —— 中间可能有 rename(如 langchain-openai 把 `max_tokens` rename 成 `max_completion_tokens`)、有硬编码 fallback(如 GA 早期版本 8192)、有上游 binary 忽略(如 OpenHuman Rust)。

`finish_reason=="length"` 是自动告警的兜底信号,已经在 post-process 里增加检测(TODO:待补),集成时如果看到该告警高频,回来复查这一节。

## 6.2 hello.json sanity(基本流水线)

按 §6.0 模板跑,`--suite hello.json`。

验证点:
- 容器拉起 → warmup 单题跑完 → `docker commit` 成功 → holdout 跑完
- `results/lift-runid-<run_id>/report.json` 存在且 task `outcome.success: true`
- `logs/<run_id>.log` 没有 `wait output timeout` / `Cannot connect to Docker daemon` / `Judge response is not valid JSON` 高频重试

> ⚠️ **hello.json 只能证连通性 —— evolve_paths 声明是否正确必须靠 `integration_check.json` 才能触发**(见 [三层证据交叉验证](./three-layer-verification.md))。hello 题目太简单 agent 根本不会写记忆,即使 `evolve_paths` 声明错了也不会 WARNING("白名单里 0 条"与"agent 根本没写"外观相同)。集成新 runtime 不要在 hello 全绿就停手。

## 6.3 Trace stitching 对齐

run 完后默认自动跑后处理;想单独重跑:

```bash
python -m src.cli.lift_main -r <runtime> --evaluate-only --run_id <run_id>
```

验证点:
- `results/lift-runid-<run_id>/*_backfilled.json` 中每题都拼到 `work_agent_traces` / `judge_agent_traces`(数量与 `report.json` 的 turn 数对齐)
- 没有 "trace not found" 告警
- `results/lift-runid-<run_id>/dashboard.html` 同步刷新(mtime 更新;`tool_calls` 列填上 langfuse 兜底统计的非 null 值;含 final_summary 表格)

> `--evaluate-only` 始终把 `report.json` 反向 replay 成事件总线广播(`emit_run_plan` / `emit_suite_plan` / `emit_stage`)重建 tracker 骨架(repeat × suite × task × phase + score / success / turns / tool_calls / status),后处理跑完用同一个 tracker 重导静态 dashboard,不依赖 `--dashboard`。

## 6.4 test_search.json 联网能力 sanity(可选)

按 §6.0 模板跑,`--suite test_search.json`;日志开 `grep -E 'firecrawl|search|scrape|Action'`。如果 runtime 配了联网工具(如 firecrawl),应当看到 W1 / H1 调用搜索工具拿到当日数据;没接联网工具的 runtime 这步可以跳过。

## 6.5 三层证据交叉验证(必跑)

hello.json 只能证连通性;evolve 是否真生效、token 5 字段是否落库,必须做 **Log × Langfuse × Layer** 三层交叉验证。默认用 `integration_check.json`,详细步骤见 [三层证据交叉验证](./three-layer-verification.md)。

## 6.6 衍生 runtime(可选)

若还要做 `<runtime>_with_evolve` / `<runtime>_active_evolve`:
1. 镜像 tag 多加一条 `<RUNTIME>_WITH_EVOLVE_DOCKER_IMAGE`(或复用基础镜像)
2. 新建 `src/lift/adapters/<runtime>_<variant>/adapter.py` **继承** baseline adapter,只 override `evolve_after_warmup`
3. `registry.py` 的 `SUPPORTED_RUNTIMES` 加名字(`AgentSource` 已收敛到 str,无需再改 Literal);`LANGFUSE_PLUGIN_TRACE_NAMES` 若 trace name 变了也要加

## 6.7 集成完成后的一次性产出

`git status` 应包含:

```
M  src/lift/adapters/registry.py             # 加新 runtime + lazy import(SUPPORTED_RUNTIMES 是唯一事实源)
M  src/paths.py                              # <RUNTIME>_AGENT_DIR / DOCKER_IMAGE / SEED_DIR
M  src/models.py                             # LANGFUSE_PLUGIN_TRACE_NAMES 加 "<runtime>-plugin"
# 后处理 AgentSource 已收敛到 str,无需改动 extract / run_post_process /
# trace_backfill / report_html / langfuse_trace_stitch,除非 trace 布局不复用
# OpenClaw sid-only 或 transcript usage schema 不含 totalTokens(见 postprocess-and-stitching §4)

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

如果还集成了第三方工具(如 firecrawl),再加一份 `agent-runtimes/<runtime>/<tool>_plugin.py` + `install-in-image.sh` 里 `cp + tools_schema*.json patch` 的相关段落。
