# Agent runtimes (`agent-runtimes/`)

每个子目录拥有**一个 agent runtime 镜像**的 Dockerfile / 插件覆盖 / 容器配置。
宿主侧的编排（adapter）落在 [`src/lift/adapters/<runtime>/`](../src/lift/adapters/)，
通过 [`src/lift/adapters/registry.py`](../src/lift/adapters/registry.py) 注册并
绑定到 CLI 的 `-r <runtime>` 选项。

| 镜像目录 | 默认 tag | 使用它的 CLI `-r` 取值 |
|---|---|---|
| [`openclaw/`](openclaw/) | `lift-openclaw-base:latest`（基础，构建时 `INSTALL_SELF_EVOLVING=false`） | `openclaw`、`multi_user_openclaw` |
| [`openclaw/`](openclaw/) | `lift-openclaw-with-evolve:latest`（带 `self-evolving-plugin-pro`） | `openclaw_with_evolve` |
| [`genericagent/`](genericagent/) | `lift-genericagent:latest` | `genericagent`、`genericagent_active_evolve` |

> 注：`*_with_evolve` / `*_active_evolve` 等"带进化"变体共享同一个镜像
> ——区别在于 adapter 侧的 `evolve_after_warmup` 钩子是否实际触发学习；
> 镜像层不需要重新构建。

| CLI `-r` 取值 | Adapter 类 | 简述 |
|---|---|---|
| `openclaw` | [`OpenClawAdapter`](../src/lift/adapters/openclaw/adapter.py) | OpenClaw 基础，warmup 后只 `docker commit` 不触发进化 |
| `openclaw_with_evolve` | [`OpenClawWithEvolveAdapter`](../src/lift/adapters/openclaw_with_evolve/adapter.py) | warmup 结束触发 `openclaw learn review`，把经验固化进 delta 镜像 |
| `multi_user_openclaw` | [`MultiUserOpenClawAdapter`](../src/lift/adapters/openclaw_multi_user/adapter.py) | 多容器 warmup + 外部群体记忆；`materialize_delta` 不做 commit |
| `genericagent` | [`GenericAgentAdapter`](../src/lift/adapters/genericagent/adapter.py) | GA `agentmain.py --task` 文件 I/O，无显式 evolve hook |
| `genericagent_active_evolve` | [`GenericAgentActiveEvolveAdapter`](../src/lift/adapters/genericagent_active_evolve/adapter.py) | per-task + suite 收尾各发一次 reflection chat |

新增 runtime 的步骤见 skill [`lift-integrate-agent-runtime`](../skill/lift-integrate-agent-runtime/SKILL.md)。
