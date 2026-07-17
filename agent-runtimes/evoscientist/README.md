# EvoScientist runtime image (`agent-runtimes/evoscientist`)

LIFT 评测用的 [EvoScientist](https://github.com/EvoScientist/EvoScientist) 镜像。
LIFT (`src`) 在每题独立容器内通过 **`docker exec … EvoSci -p <prompt> --output-format stream-json --auto-mode --session-id <sid>`** 调起 EvoScientist 完成一轮 chat。

## Layout

```
agent-runtimes/evoscientist/
├── Dockerfile
├── build-image.sh
├── install-in-image.sh          # build 期渲染 EvoScientist 的 config.yaml（注入 WORK_OPENAI_* / MODEL_NAME）
├── langfuse_tracing_overlay.py  # sitecustomize 挂钩：包装 EvoScientist.stream.events.stream_agent_events，把 3 段 trace 打齐；同时 patch langchain-openai BaseChatOpenAI 强制吐 usage
├── workspace_seed/              # （占位）holdout 容器启动前 copy 进 /workspace/task
└── README.md
```

## Build

```bash
bash agent-runtimes/evoscientist/build-image.sh
```

产出 `lift-evoscientist:latest`，对应 LIFT `-r evoscientist`。

内网构建：`APT_MIRROR=... PIP_INDEX_URL=... UV_INDEX_URL=... bash agent-runtimes/evoscientist/build-image.sh`（`build-image.sh` 会把变量透传到 `docker build --build-arg`）。

基础镜像固定为 `ghcr.io/evoscientist/evoscientist:latest`（overlay 策略，不做二次编译）。

## Environment

镜像内 EvoScientist 读 `WORK_OPENAI_API_KEY` / `WORK_OPENAI_BASE_URL` / `MODEL_NAME`，由 `install-in-image.sh` 生成 `~/.evoscientist/config.yaml` 的 `custom-openai` provider 条目——`model_id` 取 `MODEL_NAME` 中 `/` 之后的部分。

Langfuse：`LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST`（LIFT 会把宿主 `http://localhost:3000` 改写成 `http://host.docker.internal:3888` 或对应容器可达地址；见 [session.py::rewrite_langfuse_base_url_for_container](../../src/lift/adapters/evoscientist/session.py)）。

## Delta 物化路径

warmup 结束后 `docker commit` 覆盖以下目录（EvoScientist 在容器内以 root 运行时的实际写入路径）：

- `/root/.evoscientist/`（memories、`sessions.db`、autoskill 蒸馏物等）

**注意**：EvoScientist 的默认 XDG config 路径是 `/home/evosci/.evoscientist/.config/`，但 LIFT 容器内以 root 启动，实际产物落在 `/root/.evoscientist/` —— M1 baseline 验证时踩过一次坑，见 [common-pitfalls.md](../../skill/lift-integrate-agent-runtime/docs/common-pitfalls.md)。

## Tools (MCP)

镜像在 build 期通过 [`install-in-image.sh`](install-in-image.sh) 的 `EvoSci mcp add ...` 注册以下 MCP servers（写入 `/home/evosci/.evoscientist/.config/evoscientist/mcp.yaml`，属于 image 层，不受 `docker commit` 影响）：

| Name | Command | Exposed to | Env |
|---|---|---|---|
| `firecrawl-search` | `npx -y firecrawl-mcp` | `main`, `research-agent` | `FIRECRAWL_API_KEY`（runtime env-ref） |

`FIRECRAWL_API_KEY` 通过 LIFT 的 `--env-file .env` 在 `docker run` 时注入容器（见 [session.py](../../src/lift/adapters/evoscientist/session.py) `env_file=Path.cwd() / ".env"`），EvoScientist MCP client 在启动 stdio subprocess 时按 `--env-ref` 从进程环境读取。

`npx -y firecrawl-mcp` 的 npm 包在 build 期通过 `FIRECRAWL_API_KEY=stub npx -y firecrawl-mcp --help` 预拉到 `~/.npm/_npx`，首次运行时无需在线拉包。

如需添加更多 MCP server：编辑 `install-in-image.sh` 的 `EvoSci mcp add ...` 块并重 build 镜像；warmup / holdout 产生的 delta commit 不会覆盖 build 期的 MCP 配置（config 落在 `/home/evosci/...`，不在 `evolve_paths` 内）。

## Langfuse correlation with LIFT pre-chat

trace name 白名单：`evoscientist-plugin`（在 [`src/models.py::LANGFUSE_PLUGIN_TRACE_NAMES`](../../src/models.py) 中登记）。

流程：
1. LIFT 侧 `chat_agent.py` 起 pre-chat span（`work_agent` 或 `judge_agent`），把 `session_id` 写进 `LIFT_EVOSCI_SESSION_ID` 环境变量传进容器。
2. 容器内 `langfuse_tracing_overlay.py` 通过 `sitecustomize.py` + `site-packages/lift_evoscientist_overlay.py` 拿到 hook 权：包装 EvoScientist `stream.events.stream_agent_events`，把 `session_id` 覆盖到 langfuse trace 根，trace name 强制为 `evoscientist-plugin`，并透传 `**extra_kwargs`（关键：upstream 的 `events: ToolSelectionView | None` 参数必须在 wrapper 签名中显式列出，否则 CLI 会以 `Goodbye!` 早退，详见 [common-pitfalls.md](../../skill/lift-integrate-agent-runtime/docs/common-pitfalls.md)）。
3. post-process `trace_backfill` 按 tag `lift-runid-<run_id>` + 白名单拼接三段 trace。

## Token 5 字段

EvoScientist 的 `custom-openai` provider 经 `EvoScientist/llm/models.py::_OPENAI_ROUTED_PROVIDERS` 路由后，实际建的是 `langchain_openai.chat_models.base.ChatOpenAI` —— **不经过** `OpenAICompatContentMixin`（那是 DeepSeek/kimi 专用）。

**原生行为**：langchain-openai + langfuse otel autoinstrumentation 已能从 chunk `usage_metadata` 抽出 `input_tokens` / `output_tokens` / `input_token_details.cache_read` / `output_token_details.reasoning`，落到 `llm.chat` observation 的 `usageDetails`。

**overlay 增强**：由于 LIFT 配置的 `base_url` 非默认，langchain 的 `ChatOpenAI.__init__` 会跳过 `stream_usage` 自动开启逻辑，导致部分请求（judge / 短 turn）不带 `stream_options.include_usage=True`、chunk 无 usage。overlay 双管齐下 patch [`BaseChatOpenAI._astream` + `_should_stream_usage`](./langfuse_tracing_overlay.py)：

- `_should_stream_usage`：caller 未显式关闭时一律返回 True，让 langchain 自己在请求体里塞 `stream_options.include_usage`
- `_astream`：把每 chunk 的 `usage_metadata` 累积到 ContextVar bucket，wrapper 结束时打 5 字段 usage_details（备份路径，防上游未来变动）

经 b3→b4 对比：patch 前 `llm.chat` observation usage 覆盖率 22/50=44%（judge 请求全空），patch 后 22/22=100%。

**开关**：`LIFT_EVOSCI_TOKEN_PATCH=0` 关闭 patch（隔离调试用）；`LIFT_EVOSCI_OVERLAY=0` 完全关闭 overlay；`LIFT_EVOSCI_OVERLAY_DEBUG=1` 打详细 log。

## LIFT integration (`src`)

- Registry：[`src/lift/adapters/registry.py`](../../src/lift/adapters/registry.py) 注册 `evoscientist`
- Adapter：[`src/lift/adapters/evoscientist/`](../../src/lift/adapters/evoscientist)
  - `session.py`：容器生命周期、Langfuse host 重写、delta commit 覆盖 `/root/.evoscientist/`
  - `chat_agent.py`：`docker exec EvoSci -p ...` 调用；PATH 修复；`LIFT_EVOSCI_SESSION_ID` 注入
  - `run_before_load.py` / `run_after_load.py`：baseline / evolved 阶段脚手架
- Chat 超时：`CHAT_EXEC_TIMEOUT_SECONDS=1000`（超时按 provider 错误重试）

## Known limitation: stream-json whitespace

EvoScientist 的 `--output-format stream-json` 输出不是底层 LLM 原始 delta，而是
`EvoScientist.stream.events` 归一化后的事件流。该层的 `_emit_text` 会丢弃
`text.isspace()` 的纯空白 chunk；如果模型把换行单独作为一个 stream delta 发出，
最终 `done.response` 里就已经没有 `\n`，LIFT 的
[`chat_agent.py::_extract_done_response`](../../src/lift/adapters/evoscientist/chat_agent.py)
无法再无损恢复。

实测证据：

- `text` event 可以保留普通空格（例如 `" item"`），但纯换行 chunk 不会进入最终
  `done.response`。
- 默认 Rich/text 输出路径也展示同样的粘连结果，例如
  `A B- item one- item two`，说明问题发生在 EvoScientist stream 归一化层，而不是
  LIFT 的 JSONL parser。
- `done.response` 与所有 `text` chunks 拼接结果一致，不存在更完整的
  `final` / `assistant` / `message` event 可供 LIFT 读取。

因此，benchmark / integration suite 不应把“必须换行”“每条单独一行”
“Markdown 列表必须真实分行”等作为 EvoScientist runtime 的硬性成功条件；否则会把
runtime transport 限制误判为 agent 偏好迁移失败。当前
`assets/benchmarks_demo/integration_check.json` 已避免依赖换行保真。

如果未来要从根上修复，应优先在 EvoScientist stream 层保留主 agent response 的
whitespace-only text chunk（或在 overlay 中 patch `_V3EventProcessor._emit_text`），
而不是在 LIFT adapter 中用启发式规则插入换行；后者会改写 agent 输出，污染评测口径。

## Manual sanity check

```bash
# 1. 镜像能起容器
docker run --rm --env-file .env lift-evoscientist:latest bash -lc 'EvoSci --version'

# 2. patch 生效验证
docker run --rm \
  -e CUSTOM_OPENAI_API_KEY=dummy -e CUSTOM_OPENAI_BASE_URL=http://x \
  lift-evoscientist:latest python3 -c "
from EvoScientist.llm import models as em
llm = em.get_chat_model(model='ep-test', provider='custom-openai')
from langchain_openai.chat_models.base import BaseChatOpenAI
print('_should_stream_usage patched:', getattr(BaseChatOpenAI._should_stream_usage, '_lift_patched', False))
print('_astream patched:', getattr(BaseChatOpenAI._astream, '_lift_patched', False))
"

# 3. smoke run
python -m src.cli.lift_main -r evoscientist \
  --benchmark_dir assets/benchmarks_demo --suite hello.json \
  --run_id evosci-smoke --max-parallel-suites 1 --max-concurrent-tasks 1
```

跑完检查 `results/lift-runid-evosci-smoke/lift-runid-evosci-smoke_comparison_metrics.csv` 里 `input_tokens` / `output_tokens` / `cache_read_tokens` / `reasoning_tokens` 均非 NaN。
