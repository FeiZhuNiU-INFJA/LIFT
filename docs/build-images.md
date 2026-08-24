# Build Images — 所有 runtime 镜像构建命令(SSoT)

> 单一事实来源。各 runtime README 只讲**自身**的构建细节;跨 runtime 的"我该 build 什么、按什么顺序"以本页为准。一键脚本见 [scripts/build-all-images.sh](../scripts/build-all-images.sh)。

## 前置

- `.env` 已配置(`MODEL_NAME` / `WORK_OPENAI_API_KEY` / `WORK_OPENAI_BASE_URL` / `MAX_TOKENS` 等)
- Docker daemon 可用
- 工作目录切到仓库根:`cd /root/workspace/agent_evolve_evaluation`

## 镜像清单

| # | Runtime tag | 命令 | 何时需要 |
|---|---|---|---|
| 1 | `lift-openclaw-base:latest` | `bash agent-runtimes/openclaw/build-image.sh` | `-r openclaw` |
| 2 | `lift-openclaw-with-evolve:latest` | `bash agent-runtimes/openclaw/build-image.sh --with-evolve` | `-r openclaw_with_evolve` |
| 3 | `lift-openclaw-with-openspace:latest` | `bash agent-runtimes/openclaw/build-image.sh --with-openspace` | `-r openclaw_with_openspace` |
| 4 | `lift-openclaw-with-agentmemory:latest` | `bash agent-runtimes/openclaw/build-image.sh --with-agentmemory` | `-r openclaw_with_agentmemory` |
| 5 | `lift-hermes:latest` | `bash agent-runtimes/hermes/build-image.sh` | `-r hermes` |
| 6 | `lift-hermes-with-openspace:latest` | `bash agent-runtimes/hermes/build-image.sh --with-openspace` | `-r hermes_with_openspace` |
| 7 | `lift-hermes-with-agentmemory:latest` | `bash agent-runtimes/hermes/build-image.sh --with-agentmemory` | `-r hermes_with_agentmemory` |
| 8 | `lift-openhuman:latest` | `DOCKER_BUILD_NETWORK=host bash agent-runtimes/openhuman/build-image.sh` | `-r openhuman` |
| 9 | `lift-openhuman-with-agentmemory:latest` | `DOCKER_BUILD_NETWORK=host bash agent-runtimes/openhuman/build-image.sh --with-agentmemory` | `-r openhuman_with_agentmemory` |
| 10 | `lift-genericagent:latest` | `bash agent-runtimes/genericagent/build-image.sh` | `-r genericagent` / `-r genericagent_active_evolve` |
| 11 | `lift-evoscientist:latest` | `bash agent-runtimes/evoscientist/build-image.sh` | `-r evoscientist` / `-r evoscientist_active_evolve` |
| 12 | `lift-prime-agent:latest` | `bash agent-runtimes/prime_agent/build-image.sh` | `-r prime_agent` / `-r prime_agent_active_evolve` |

> `_active_evolve` 变体和 base 变体**共享同一镜像**,只需 build 一次。
>
> `_with_openspace` 只加到有 MCP 客户端能力的 runtime(OpenClaw / Hermes);GenericAgent / OpenHuman / EvoScientist 不是 MCP 客户端,所以没有对应变体;Prime Agent 自带 firecrawl remote MCP skill,亦无 OpenSpace 变体。见 [../CLAUDE.md](../CLAUDE.md)。

## 共同环境开关

| 环境变量 | 作用 | 何时用 |
|---|---|---|
| `APT_MIRROR=http://mirrors.byted.org` | apt 走字节内网镜像 | 字节内网构建 |
| `PIP_INDEX_URL=https://bytedpypi.byted.org/simple/` | pip 走字节内网镜像 | 字节内网构建 |
| `LIFT_INTRANET_AUTODETECT=0` | 关闭内网自动探测,强制走公网 | 内网源 429 时兜底 |
| `DOCKER_BUILD_NETWORK=host` | docker build 用宿主网络栈 | 拉大 tarball(如 openhuman-core)时 |

> 字节内网构建时,以上 `APT_MIRROR` / `PIP_INDEX_URL` 会自动探测生效(除非显式 `LIFT_INTRANET_AUTODETECT=0`)。

## 一键 build

```bash
# 全部 build(受环境变量控制哪些跳过,见 --help)
bash scripts/build-all-images.sh

# 只 build 部分,逗号分隔
bash scripts/build-all-images.sh --only openclaw-base,hermes,evoscientist

# 跳过某些
bash scripts/build-all-images.sh --skip openhuman,openhuman-with-agentmemory

# 后台跑 + 看日志
bash scripts/build-all-images.sh > /tmp/lift-build/all.log 2>&1 &
tail -f /tmp/lift-build/all.log

# 列出所有 target
bash scripts/build-all-images.sh --list
```

## 常见坑

- **openhuman-core tarball 拉不动**:必须加 `DOCKER_BUILD_NETWORK=host`。docker bridge 默认 MTU 1500 / DNS 转发在拉 GitHub 74 MB tarball 时几乎完全阻塞;host 网络恢复 ~300 KB/s
- **`bytedpypi.byted.org` 429**:内网源可能被限速;`LIFT_INTRANET_AUTODETECT=0` 走公网 pip
- **`MAX_TOKENS=51200` 未生效**:请查 [../skill/lift-integrate-agent-runtime/docs/acceptance-checklist.md#61a-max_tokens-落地审计硬指标](../skill/lift-integrate-agent-runtime/docs/acceptance-checklist.md),按 3 层证据链(env / 配置 / HTTP payload)审计
- **`workspace_seed` 目录不存在导致 COPY 失败**:通过 `.gitkeep` 保留空目录;别人 clone 后应直接可 build

## 逐镜像 build 细节

各 runtime 的镜像结构、build-arg、workspace_seed 内容等在各自 README:

- [agent-runtimes/openclaw/README.md](../agent-runtimes/openclaw/README.md)
- [agent-runtimes/hermes/README.md](../agent-runtimes/hermes/README.md)
- [agent-runtimes/openhuman/README.md](../agent-runtimes/openhuman/README.md)
- [agent-runtimes/genericagent/README.md](../agent-runtimes/genericagent/README.md)
- [agent-runtimes/evoscientist/README.md](../agent-runtimes/evoscientist/README.md)
- [agent-runtimes/prime_agent/README.md](../agent-runtimes/prime_agent/README.md)
