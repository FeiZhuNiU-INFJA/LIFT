# OpenHuman runtime image (`agent-runtimes/openhuman`)

LIFT 评测用的 [OpenHuman](https://github.com/tinyhumansai/openhuman) 镜像。
`openhuman-core` 是 Rust 实现的 HTTP JSON-RPC server；LIFT 通过
**`POST http://127.0.0.1:{host_port}/rpc` (`method: "openhuman.agent_chat"`)** 完成
一轮对话。work agent 与 judge agent 分别运行在同镜像、同 workspace、同 load_state 的 sibling 容器中。

## Layout

```
agent-runtimes/openhuman/
├── .dockerignore
├── Dockerfile
├── build-image.sh
├── install-in-image.sh        # build 期渲染 ~/.openhuman/config.toml
├── config.toml.template       # OpenAI-兼容直连模板；占位符由 install-in-image.sh sed 渲染
├── workspace_seed/            # （占位）holdout 容器启动前 copy 进 /workspace/task
└── README.md
```

## Build (recommended)

从仓库根：

```bash
bash agent-runtimes/openhuman/build-image.sh
```

默认产出 `lift-openhuman:latest`，对应 LIFT `-r openhuman`。

流程：

1. 从 [GitHub Releases](https://github.com/tinyhumansai/openhuman/releases/latest)
   拉取 latest `.deb` 安装包（`amd64` 默认；`OPENHUMAN_ARCH=arm64` 切 arm64）
2. `apt-get install ./OpenHuman_*.deb` 装 `openhuman-core` 二进制
3. `install-in-image.sh` 把 `.env` 中的 `ARK_API_KEY` / `MODEL_NAME` /
   `ARK_BASE_URL` 通过 sed 渲染到 `/root/.openhuman/config.toml`，配置 OpenHuman
   的 OpenAI-兼容直连模式（`inference_url` + `api_key` + `default_model` 三字段
   配套，绕开 OpenHuman backend；见 upstream `Config.inference_url` 字段注释）

### 带 agentmemory backend 的变体

```bash
bash agent-runtimes/openhuman/build-image.sh --with-agentmemory
# 产出 lift-openhuman-with-agentmemory:latest；对应 LIFT -r openhuman_with_agentmemory
```

agentmemory（官方 wiki config.toml backend 切换）采用**纯本地**模式：`all-MiniLM-L6-v2` 嵌入 +
BM25 + 知识图，**零 API Key、离线**（构建期预热 iii-engine 与嵌入模型进镜像）。构建期
`install-in-image.sh` 在 `config.toml` 追加 `[memory] backend = "agentmemory"`，装 Node ≥20 +
`@agentmemory/agentmemory`；`openhuman-core` 启动时旁路自家 SQLite，把 Memory trait 调用代理到
容器内 agentmemory server（`:3111`）。镜像 ENTRYPOINT 包装脚本
`scripts/openhuman-agentmemory-entrypoint.sh` 在 `openhuman-core` 启动**前**先拉起并等待 `:3111`
就绪（OpenHuman 的 agentmemory backend **无自动回退 SQLite**，daemon 不可达会报错）。work 容器中的记忆落在
`/root/.agentmemory`，随 `docker commit` 进 delta 镜像；judge sibling 容器只负责验收，不参与 commit。源可用 env `AGENTMEMORY_GIT_URL` /
`AGENTMEMORY_GIT_REF` 覆盖，Node 主版本用 `NODE_MAJOR`（默认 20），npm registry 用 `NPM_CONFIG_REGISTRY`。

> ⚠️ **端口与网络**：agentmemory server 每容器绑定 `:3111`。该变体在 adapter 层**强制 bridge 网络**
> （`force_bridge_network=True`），忽略全局 `CONTAINER_NETWORK_MODE`（若设为 `host` 会打 WARNING 并
> 回退 bridge），避免同一宿主并发容器抢同一端口冲突。对应常量 `OPENHUMAN_WITH_AGENTMEMORY_DOCKER_IMAGE`。
>
> 验证时留意 `openhuman-core` 日志出现 `[memory::factory] using agentmemory backend at <url>`。

### 内网/外网构建（APT 镜像）

```bash
# 公网（默认）
bash agent-runtimes/openhuman/build-image.sh

# 内网
APT_MIRROR=http://mirrors.byted.org bash agent-runtimes/openhuman/build-image.sh
```

`.deb` 直接从 GitHub Release 拉；如公网不稳定可通过 `OPENHUMAN_DEB_URL` 指向
内网镜像。

### Override

| 变量 | 默认 | 说明 |
|------|------|------|
| `OPENHUMAN_IMAGE` | `lift-openhuman:latest` | 构建产物 tag |
| `OPENHUMAN_VERSION` | *(latest)* | 指定 upstream release 版本（如 `0.58.12`） |
| `OPENHUMAN_ARCH` | `amd64` | .deb 架构（arm64 host 需显式设 `arm64`） |
| `OPENHUMAN_DEB_URL` | — | 显式指向 .deb 下载 URL（离线构建 / 内网镜像） |
| `OPENHUMAN_MODEL_NAME` | — | 覆盖 `.env` 里的共享 `MODEL_NAME`（避免污染其他镜像） |

## Environment

镜像要求宿主仓库根 `.env` 提供：

- `ARK_API_KEY` — **build-time required**；烧入 `~/.openhuman/config.toml` 的 `api_key`
- `MODEL_NAME` / `OPENHUMAN_MODEL_NAME` — **build-time required**；作为
  `default_model` 烧入。LIFT 约定所有 agent 用同一 seed 模型
  （`doubao-seed-2-0-pro-260215`）。若拿到 `provider/model` 复合形式，脚本会
  自动剥去 provider 前缀。
- `ARK_BASE_URL` — 默认 `https://ark.cn-beijing.volces.com/api/v3`
- `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL` —
  运行期由 LIFT adapter 通过 `docker run --env-file` 注入，容器内 `localhost` /
  `127.0.0.1` 会自动改写为 `host.docker.internal`

## Runtime CMD & port

容器 `CMD` 是：

```
openhuman-core run --host 0.0.0.0 --port 7788
```

`--host 0.0.0.0` 是必需的：`openhuman-core` 默认 bind `127.0.0.1`，容器外无法
访问；LIFT 通过 `docker run -p 0:7788` 让 Docker 分配宿主端口，再 curl
`/health`（fallback: `/` / `/rpc`）探活，然后走 JSON-RPC 对话。

## Evolve delta

OpenHuman 没有独立的 `evolve` 命令。warmup 阶段 work agent 自然写入的：

- `/root/.openhuman/workspace/memory_tree/` — 结构化长期记忆树
- `/root/.openhuman/workspace/wiki/` — Obsidian 风格的自建知识库

两条路径被 [`OpenHumanAdapter.evolve_paths`](../../src/lift/adapters/openhuman/adapter.py)
声明为白名单，`docker commit` 把它们随 work 容器 FS 层带入 delta 镜像；delta.py 的
"仅演化路径" diff 用来校验真进化 vs 系统噪声。

## Langfuse

OpenHuman 的 Langfuse 集成走自家私有的 `/telemetry/langfuse/ingestion` proxy
路径（源见 upstream `src/observability/agent_tracing/langfuse.rs`）。**第一版
镜像不改 Langfuse 传输层**：`push_spans` / `push_observations` 的失败会被
caller swallow 成 warning，不影响 chat 主干。跑通 chat 后再迭代 Langfuse 联通。

<a id="token-5-fields"></a>
### Token 5 字段落库状态

- ✅ `input_fresh` / `cache_write` / `cache_read` / `output` 有值
- ⚠️ **`reasoning` 恒为 `0` 合规**——不是丢失，是隐式合并

**cache_read**：OpenHuman adapter 侧 `_usage_details_from_assistant` 从
`msg.usage.cached_input` 映射到 `cache_read_input_tokens`，已正确落库。

**reasoning 隐式合并进 output**：

- OH 二进制的 `MessageUsage` schema 只有 `{input, output, cached_input,
  cost_usd, context_window}`，**没有独立 `reasoning_tokens` 字段**。
- 经统计对比，OH 的 `output` 均值（≈3219）≈ 其他 runtime 的 `output`（≈1300）
  + `reasoning`（≈550）——reasoning 已被合并进 output。
- 结合 LIFT 口径「保底 output、尽力 reasoning」：OH 的 output 已含 reasoning，
  符合业务需求；`reasoning=0` 是"抽不出"而非"没算"。
- 后处理 backfill 时会显式打上 `reasoning_tokens=null(counted_into_output)`
  注记，防止与其他 runtime 横向 diff 时误判为 bug。

**若研究口径需要分离 reasoning**：只能向 OH 上游提 PR 在 `MessageUsage`
加 `reasoning_tokens` 字段，binary 目前无 hook 可绕。

统一口径 / 跨 runtime 排障方法见
[skill/lift-integrate-agent-runtime/docs/token-observability.md](../../skill/lift-integrate-agent-runtime/docs/token-observability.md)。

<a id="transcript-push"></a>
### Transcript → Langfuse 反哺（LIFT 独有）

`openhuman-core` 是 Rust 二进制，没有 Python plugin 接口，自家 Langfuse 走
`/telemetry/langfuse/ingestion` proxy 到上游账号（不是 LIFT 用的 Langfuse
project），overlay 拦不到。**LIFT 走"宿主侧 transcript 后处理 → SDK v4 push"**
反哺 LIFT 自己的 Langfuse。参考实现 470 行：
[`src/lift/adapters/openhuman/transcript_langfuse.py`](../../src/lift/adapters/openhuman/transcript_langfuse.py)。

**数据流**：

```
容器 /root/.openhuman/users/local/workspace/session_raw/<ts>_<agent>.jsonl
       │  首行 _meta: {agent, thread_id, model, turn_count, ...}
       │  之后: role=system|user|assistant|tool 各一行
       ▼
LIFT adapter chat() 返回前：docker exec cat → 宿主解析
       ▼
按 _meta.thread_id == session_id 过滤 → 汇总
       ▼
Langfuse SDK v4:
  propagate_attributes(session_id=..., tags=[run_tag, session_id])
    start_as_current_observation(name='openhuman-plugin', as_type='agent',
                                 metadata=LangfusePluginTraceMetadata)
      ├── generation obs（usage_details = {input, output, total,
      │    cache_read_input_tokens}）
      └── tool obs（as_type='tool'，input=args，update(output=result)）
  client.flush()
```

**hook 位点**：

- [`adapter.py`](../../src/lift/adapters/openhuman/adapter.py) 的
  `worker_judger_factory` 把 `ctx.run_id` 透传成 `run_tag`
- [`chat_agent.py`](../../src/lift/adapters/openhuman/chat_agent.py) 在
  `return reply_text` 前用 `asyncio.to_thread(push_openhuman_plugin_trace_safe, …)`
  异步 push——SDK v4 `client.flush()` 是同步阻塞的，塞 event loop 会拖慢下一轮 chat

**必守约束**（少一个就丢配对）：

| 约束 | 具体要求 |
|---|---|
| trace name | `"openhuman-plugin"`（进 [`src/models.py`](../../src/models.py) 的 `LANGFUSE_PLUGIN_TRACE_NAMES` 白名单） |
| session_id | 走 `propagate_attributes(session_id=...)` 设置，等于 LIFT `chat()` 传入的 `user-*` / `judge-*` |
| tags | ⊇ `{run_tag, session_id}` |
| metadata | 能被 `LangfusePluginTraceMetadata.from_langfuse_dict` 反序列化（camelCase / snake_case 都读） |
| generation `usage_details` | 只放 `{input, output, total, cache_read_input_tokens}`——Langfuse SDK v4 只认这四个 key |
| tool observation | 挂 `as_type='tool'`（不是 `'default'`），`count_tool_observations` 才能兜底 tools 列 |
| 异常隔离 | `push_openhuman_plugin_trace_safe` 薄封装 swallow 所有异常，只 `LOGGER.warning`，不影响 chat 主流程 |

**后处理配套**：因 OH usage schema 不含 `totalTokens`，post-process 需要专属
`_make_row_openhuman` 从 `global_stats.total_tokens` 累加，详见
[skill/lift-integrate-agent-runtime/docs/postprocess-and-stitching.md §4.1](../../skill/lift-integrate-agent-runtime/docs/postprocess-and-stitching.md#41-usage-schema-分支_make_row_runtime)。

> **成本**：这条路径比 overlay 慢一点（宿主 `docker exec` + push 都是 chat 主
> 路径同步阻塞），但完全不用改上游。work/judge 分容器后并发规模更容易到数十甚至上百容器，瓶颈通常仍在 Docker/VM 资源而不是这段 push 逻辑。

## LIFT integration

```bash
bash agent-runtimes/openhuman/build-image.sh
python -m src.cli.lift_main -r openhuman \
    --benchmark_dir assets/benchmarks_demo --suite hello.json --warmup-only
python -m src.cli.lift_main -r openhuman \
    --benchmark_dir assets/benchmarks_demo --suite hello.json
```

默认镜像：

- `-r openhuman` → `lift-openhuman:latest`（常量 `OPENHUMAN_DOCKER_IMAGE`，
  定义于 [`src/paths.py`](../../src/paths.py)）
- `-r openhuman_with_agentmemory` → `lift-openhuman-with-agentmemory:latest`（常量
  `OPENHUMAN_WITH_AGENTMEMORY_DOCKER_IMAGE`）

## Manual sanity check

```bash
docker run --rm lift-openhuman:latest /usr/local/bin/openhuman-core help
docker run --rm -p 7788:7788 lift-openhuman:latest &
curl -s http://127.0.0.1:7788/health || curl -s http://127.0.0.1:7788/
curl -s http://127.0.0.1:7788/rpc \
    -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","id":1,"method":"openhuman.agent_chat","params":{"message":"hi","thread_id":"t1"}}'
```
