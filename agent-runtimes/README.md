# Agent runtimes (`agent-runtimes/`)

每个子目录拥有**一个 agent runtime 镜像**的 Dockerfile / 插件覆盖 / 容器配置。
宿主侧的编排（adapter）落在 [`src/lift/adapters/<runtime>/`](../src/lift/adapters/)，
通过 [`src/lift/adapters/registry.py`](../src/lift/adapters/registry.py) 注册并
绑定到 CLI 的 `-r <runtime>` 选项。

运行时执行时，LIFT 会为 work agent 与 judge agent 启动同镜像、同 workspace、
同 `load_state` 的 sibling 容器；evolve / `docker commit` 只作用于 work 容器。

| 镜像目录 | 默认 tag | 使用它的 CLI `-r` 取值 |
|---|---|---|
| [`openclaw/`](openclaw/) | `lift-openclaw-base:latest`（基础，构建时 `INSTALL_SELF_EVOLVING=false`） | `openclaw`、`multi_user_openclaw` |
| [`openclaw/`](openclaw/) | `lift-openclaw-with-evolve:latest`（带 `self-evolving-plugin-pro`） | `openclaw_with_evolve` |
| [`openclaw/`](openclaw/) | `lift-openclaw-with-openspace:latest`（带 OpenSpace MCP 插件） | `openclaw_with_openspace` |
| [`openclaw/`](openclaw/) | `lift-openclaw-with-agentmemory:latest`（带 agentmemory memory plugin） | `openclaw_with_agentmemory` |
| [`genericagent/`](genericagent/) | `lift-genericagent:latest` | `genericagent`、`genericagent_active_evolve` |
| [`hermes/`](hermes/) | `lift-hermes:latest` | `hermes` |
| [`hermes/`](hermes/) | `lift-hermes-with-openspace:latest`（带 OpenSpace MCP 插件） | `hermes_with_openspace` |
| [`hermes/`](hermes/) | `lift-hermes-with-agentmemory:latest`（带 agentmemory memory provider） | `hermes_with_agentmemory` |
| [`openhuman/`](openhuman/) | `lift-openhuman:latest` | `openhuman` |
| [`openhuman/`](openhuman/) | `lift-openhuman-with-agentmemory:latest`（带 agentmemory backend） | `openhuman_with_agentmemory` |
| [`evoscientist/`](evoscientist/) | `lift-evoscientist:latest` | `evoscientist`、`evoscientist_active_evolve` |

> 注：`*_active_evolve` 这类 host-side 变体通常共享 baseline 镜像，只在 adapter
> 侧覆写 `evolve_after_warmup`；`*_with_evolve` 是否使用独立镜像取决于 runtime
> 本身。OpenClaw 的 `openclaw_with_evolve` 使用 `lift-openclaw-with-evolve:latest`，
> EvoScientist 的 `evoscientist_active_evolve` 复用 `lift-evoscientist:latest`。

| CLI `-r` 取值 | Adapter 类 | 简述 |
|---|---|---|
| `openclaw` | [`OpenClawAdapter`](../src/lift/adapters/openclaw/adapter.py) | OpenClaw 基础，warmup 后只 `docker commit` 不触发进化 |
| `openclaw_with_evolve` | [`OpenClawWithEvolveAdapter`](../src/lift/adapters/openclaw_with_evolve/adapter.py) | warmup 结束触发 `openclaw learn review`，把经验固化进 delta 镜像 |
| `openclaw_with_openspace` | [`OpenClawWithOpenSpaceAdapter`](../src/lift/adapters/openclaw_with_openspace/adapter.py) | OpenClaw + OpenSpace MCP skill hub |
| `openclaw_with_agentmemory` | [`OpenClawWithAgentMemoryAdapter`](../src/lift/adapters/openclaw_with_agentmemory/adapter.py) | OpenClaw + agentmemory memory plugin，强制 bridge 网络 |
| `multi_user_openclaw` | [`MultiUserOpenClawAdapter`](../src/lift/adapters/openclaw_multi_user/adapter.py) | 多容器 warmup + 外部群体记忆；`materialize_delta` 不做 commit |
| `genericagent` | [`GenericAgentAdapter`](../src/lift/adapters/genericagent/adapter.py) | GA `agentmain.py --task` 文件 I/O，无显式 evolve hook |
| `genericagent_active_evolve` | [`GenericAgentActiveEvolveAdapter`](../src/lift/adapters/genericagent_active_evolve/adapter.py) | per-task + suite 收尾各发一次 reflection chat |
| `hermes` | [`HermesAdapter`](../src/lift/adapters/hermes/adapter.py) | 容器空转 + `hermes_runner.py`；Hermes review 流程写 `/opt/hermes-state` |
| `hermes_with_openspace` | [`HermesWithOpenSpaceAdapter`](../src/lift/adapters/hermes_with_openspace/adapter.py) | Hermes + OpenSpace MCP skill hub |
| `hermes_with_agentmemory` | [`HermesWithAgentMemoryAdapter`](../src/lift/adapters/hermes_with_agentmemory/adapter.py) | Hermes + agentmemory memory provider，强制 bridge 网络 |
| `openhuman` | [`OpenHumanAdapter`](../src/lift/adapters/openhuman/adapter.py) | `openhuman-core serve` + HTTP JSON-RPC `agent.chat` |
| `openhuman_with_agentmemory` | [`OpenHumanWithAgentMemoryAdapter`](../src/lift/adapters/openhuman_with_agentmemory/adapter.py) | OpenHuman + agentmemory backend，强制 bridge 网络 |
| `evoscientist` | [`EvoScientistAdapter`](../src/lift/adapters/evoscientist/adapter.py) | `EvoSci -p ... --output-format stream-json`；warmup 自然写入的 memories/skills 由 commit 携带 |
| `evoscientist_active_evolve` | [`EvoScientistActiveEvolveAdapter`](../src/lift/adapters/evoscientist_active_evolve/adapter.py) | warmup 后调用 EvoMemory AutoSkills graph，等待完成后 commit `/root/.evoscientist` |

新增 runtime 的步骤见 skill [`lift-integrate-agent-runtime`](../skill/lift-integrate-agent-runtime/SKILL.md)。
