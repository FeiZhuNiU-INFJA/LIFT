# 进化产物落地契约:Docker commit 陷阱

> [`SKILL.md`](../SKILL.md) 的关键子文档,专治"hello.json 全绿但 evolve 无效"的隐形失败。
> 关于 delta diff 的三层验证方法见 [`three-layer-verification.md` 证据 C](three-layer-verification.md#证据-clayer--delta-镜像真的包含进化内容吗)。

LIFT 的核心命题:**baseline 镜像 vs evolved 镜像的差异 = warmup 阶段 agent 学到的东西**。要成立必须让 evolve 产物(memory / skills / SOP)都落在 `docker commit` 能捕获的**容器根 FS 层**(`/opt/**`、`/root/**`、`/etc/**`)。**bind mount / named volume / tmpfs / 任何非本地 FS 挂载都不进 commit**。

## 三点错位

新 runtime 接入时**必须**同时保证下面三个位置一致,任何一处错位都会让 evolve 无声无息地失效:

| 位置 | 具体形态 | 陷阱 |
|---|---|---|
| **引擎读**:agent 上游代码读 memory / skills 的绝对路径 | GA `script_dir = /opt/GenericAgent`;OpenClaw `~/.openclaw/workspace` | 通常上游用 `os.path.dirname(__file__)` 或 `~` 拼绝对路径,天然在容器 FS 层 ✅ |
| **LLM 写**:LLM 通过 tool call(`file_write` / `bash`)实际写文件的路径 | 由 system prompt 里的 `cwd` 和 `[Memory]` 提示决定 | LLM 用 `memory/xxx` 相对路径 → 落在 cwd = `/workspace/task/memory` = **bind mount** ❌ |
| **docker commit 捕获**:容器 FS 层 | 由 Dockerfile 的 `mkdir -p /opt/<runtime>/memory` 决定 | 只有落到容器 FS 才能被 commit ✅ |

## 验证清单(新 runtime 必跑)

```bash
# 1. 引擎侧读什么绝对路径?
docker run --rm <image> sh -c 'grep -nE "memory|skill|sop" /opt/<runtime>/<main>.py | head'

# 2. system prompt 告诉 LLM 什么路径?
docker run --rm <image> sh -c 'grep -nE "cwd\s*=|\[Memory\]|\.\./memory|\./memory" /opt/<runtime>/<main>.py'

# 3. Dockerfile 是否 mkdir 了这些绝对路径?
grep -nE "mkdir.*memory|mkdir.*skill" agent-runtimes/<runtime>/Dockerfile
```

三处路径**必须**指向同一个容器 FS 绝对路径(例如 `/opt/<runtime>/memory`)。如果 LLM 会看到相对路径(`memory/xxx` / `../memory/xxx`)且 cwd 在 bind mount 之内,就必须在 `scripts/install-config.sh` 里 patch 上游源码把提示改成绝对路径,同时(双保险)在 reflection prompt 里显式告诉 LLM"cwd 是 bind mount,只能用 `/opt/<runtime>/memory` 绝对路径"。

## 历史案例(GA memory patch)

- GA 引擎读 `script_dir + 'memory/'` → 绝对路径 `/opt/GenericAgent/memory` ✅
- GA system prompt 告诉 LLM `cwd = /workspace/task (./)` + `[Memory] (../memory)` → LLM 解析为 `/workspace/memory`(不存在)或 `/workspace/task/memory`(bind mount)❌
- 引擎读的位置**永远拿不到** LLM 写的内容 → warmup 表面成功,delta 镜像里 `/opt/GenericAgent/memory` 空空如也 → evolved 与 baseline 无差异 → LIFT 数据毫无意义
- 修复:`scripts/install-config.sh` patch [ga.py:518,590,591](../../../agent-runtimes/genericagent/scripts/install-config.sh#L86-L108) 三处相对路径都改成 `/opt/GenericAgent/memory` 绝对路径;reflection prompt 也加 `_MEMORY_PATH_NOTE`。

## 验证方式

首选看 pipeline 日志里 `Delta preflight diff` 两行(`commit_delta_image` 在 `docker commit` 前自动跑 `docker diff` 打摘要):

- `Delta preflight diff (full) [<container>]: +NA ~NC -ND ...` — upperdir 全集,含 pip / cache / temp 副作用
- `Delta preflight diff (evolve-only) [<container>]: +NA ~NC -ND at /opt/<runtime>/memory` — 只统计 adapter `evolve_paths` 白名单目录(见 [`adapter-quartet.md` §2.1 evolve_paths](adapter-quartet.md#21-adapterpy-必须-override-的-4-个方法)),evolve-only 计数为 0 时会直接打 WARNING

如果 `full` 显示 `no changes` 或 `evolve-only` 触发 WARNING 就是三点错位。也可以按 [`three-layer-verification.md` C.4](three-layer-verification.md#c4-兜底方案手动保留-delta-镜像做内容级-diff) 跑一个非 hello 的复杂 suite + `--warmup-only` 手动 diff delta 镜像。

## README 必备条款

集成一个 runtime **必须**在 `agent-runtimes/<runtime>/README.md` 里写清默认进化机制,让后续读者不用翻 `adapter.py` docstring 就能回答"这个 runtime 是怎么进化的"。README 至少覆盖以下 4 点:

1. **进化产物路径**:列出所有会进 delta 镜像的容器 FS 绝对路径(与 adapter 的 `evolve_paths` 白名单一致),例如 `/root/.openclaw/workspace/`、`/opt/GenericAgent/memory/`、`/root/.evoscientist/`。若同时存在 bind mount(不进 delta)与镜像 FS 目录(进 delta),用对照表明确"哪个进/哪个不进",避免读者误以为 bind mount 里的产物会被保留。
2. **触发方式**:说明 baseline runtime 是**被动隐式**(``evolve_after_warmup`` 为 no-op,靠 warmup 期 agent 运行时自主写入 + `docker commit` 捕获),还是**主动显式**(有 `learn review` / AutoSkills / reflection prompt 之类的钩子)。被动隐式的 runtime 需要在 README 明确写出这一点,否则读者会误以为 LIFT 会主动触发。
3. **跨 session / 跨任务共享方式**:warmup 阶段多个任务(可能属于不同 LIFT session、不同 runtime conversation thread)如何共享同一份 memory / skills — 通过哪个绝对路径、由 agent 上游怎么读到。若上游用相对路径(`./memory` / `../memory`)且 cwd 在 bind mount 之内,必须在 README 描述已做的 patch 与规避方式。
4. **衍生 runtime 差异**:若存在 `_with_evolve` / `_active_evolve` 等衍生变体,说明它相对 baseline 多了哪些钩子(通常只 override `evolve_after_warmup` 或 `evolve_after_task`)、是否复用同一镜像、以及触发时机。

参考实现:[`agent-runtimes/openclaw/README.md`](../../../agent-runtimes/openclaw/README.md) 的 *Workspace 布局(镜像 FS vs bind mount)* 章节是完整样板 —— 有对照表、有 "为什么要拆成两份" 的解释、有衍生 runtime(`openclaw_with_evolve`)对应钩子的说明。

⚠ **单独在 `adapter.py` docstring 里写不算数**:那份注释只对读源码的人可见,而 README 是 runtime 集成的对外文档,必须自足。
