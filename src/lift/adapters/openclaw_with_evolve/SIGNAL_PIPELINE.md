# LIFT × self-evolving-plugin-pro 信号管道修复实录

> 本文档复盘 LIFT 评测在 `OpenClawWithEvolveAdapter` 路径下让进化插件
> ``self-evolving-plugin-pro`` 真正"看见对话、学到经验"的整套修复过程。
> 给后续维护者一份完整的因果链——任何一环打回原形都能定位到对应症状。

## 背景

LIFT 通过 `OpenClawWithEvolveAdapter` 跑带进化插件的 OpenClaw 容器：

- warmup 阶段：跑题 → 写信号 → `learn review` → `docker commit` 出 delta 镜像
- holdout 阶段：在 baseline / evolved（delta）两个镜像上各跑同一题，对比得分

进化插件 `self-evolving-plugin-pro` 的工作前提是 **SignalRecord 表非空**。
信号在常规使用场景下由 work agent 自己用 exec 工具调
`curl POST /signals` 上报。

## 症状

最初日志里 `learn review` 阶段反复出现"查看对话=0"——也就是
`SignalRecord` 表是空的，插件什么都没学到，自然也产不出 active 规则。

## 第一层根因：评测语境下 plugin 自带的 signal 通道几乎零产出

### 先把 plugin 真实的 signal 上报链路理清楚

读 plugin 源码（[`hooks.js`][hooks] / [`api.js`][api]）会发现：**正常使用场景下，agent
根本不应该自己 curl `POST /signals`**。plugin 在 `register.js` 注册了 3 个自动
hook，由 plugin 自己拦截 LLM 输出 / tool 调用来发 signal：

[hooks]: https://example.invalid/hooks.js
[api]: https://example.invalid/api.js

| Hook | 触发 | 自动行为 |
|------|------|----------|
| `before_prompt_build` | 构 prompt 前 | 把 `<signal-protocol>` XML 注入 system prompt |
| `after_tool_call` | tool 调用完 | 失败时自发 `kind=tool_failure` |
| `llm_output` | LLM 出文 | 解析输出里的 `<self-learning-record>` XML 自发 signal |

而 `postSignal` 第一行有个静默守卫：

```js
async function postSignal(payload) {
  if (!config.instanceId || !config.instanceToken) return;  // 静默 return
  ...
}
```

`config.instanceId / instanceToken` 来自 `loadRuntimeState()` →
`runtime-state.json`。**只要这个文件没落盘，无论是 hook 自发还是 agent 自调，
postSignal 都在第一行 silent return**。

### 评测语境下 plugin 自带的 3 个 hook 仍然不够

bootstrap 修复（第二层）让 `runtime-state.json` 落盘后，`postSignal` 不再
silent return；但 `learn review` 看到的 signals 依然主要来自 LIFT 代发
（`source: lift_eval_critique`），**plugin 自带通道产出极低**：

- `after_tool_call` 只在 tool **失败**时发——评测题里 work agent 写文件读文件
  绝大多数都成功，这条几乎不触发。
- `llm_output` 要求 LLM 输出里**自带 `<self-learning-record>` XML 块**——评测
  里 work agent 把 judge 反馈当工单（"缺少问候语，请补上"看起来像系统改写指令
  而不是用户口头反馈），不会主动写这种结构化块。
- `before_prompt_build` 不发 signal，只注入协议 XML。

加上 `max_conversation_turns=5` 卡得紧，agent 优先"赶紧做对题"，更不会花 turn
去手写 `<self-learning-record>`。

### 修复：LIFT 代发兜底

评测系统已知每题的 `success` / `content_score` / `work_session_id`——这是
ground truth，比让 agent 揣摩 judge 语气精准多了。LIFT 直接代发：

- [base.py](./../base.py) `evolve_after_task` 钩子（基类原本就是 no-op，为这种
  场景预留）签名加 `result: PhaseRun` 参数，让子类拿到 session_id 和成败信息
- [adapter.py](./adapter.py) 子类覆盖 `evolve_after_task`：题级粒度调
  `post_signal_via_container`
- [evolve.py](./evolve.py) `post_signal_via_container`：容器内 `docker exec`
  bash 拉 `runtime-state.json` 里的 `instanceId` / `instanceToken`，curl
  `POST /signals`，走和 plugin hook 自发完全相同的 HTTP 路径

抽象层零新方法、无侵入。

### 为什么"知道 plugin 自带 hook 后"仍然保留主动 curl

读完 plugin 源码后会发现一个微妙的疑问：**既然 plugin 在 hook 上自发 signal、
agent 协议层面也不应该主动 curl，那 LIFT 这层为什么还要 docker exec curl？**

答案是：**plugin 协议讲的"agent 不主动 curl"和这里 LIFT 主动 curl，主体不同。**

|   | 谁来发 | 用什么通道 |
|---|--------|------------|
| plugin 设计意图 | plugin runtime 自己 | `before_prompt_build` / `after_tool_call` / `llm_output` 三个 hook |
| plugin 协议里写的 "agent 自调 curl" | **work agent** 根据 system prompt 里注入的协议 XML，自己在某个 turn 里 `exec curl` | agent 主动一个 turn |
| **本仓库这段代发** | **LIFT host 进程**通过 docker exec 跑 bash + curl | 评测旁路，不占 agent turn |

所以 plugin 协议层面"agent 不主动 curl"仍然成立，**LIFT 评测层面是评测系统在
代发，并不违反协议**——发出去的 SignalRecord 与 hook 自发或 agent 自调走的是
**完全相同**的 `POST /signals` HTTP 路由，对 plugin review worker 而言没有区别。

具体保留它的三个理由：

1. **plugin hook 自发通道在评测语境下产出仍然偏低**——`after_tool_call` 鲜
   少触发（评测题里 tool 多数成功），`llm_output` 又依赖 LLM 在输出里**自带**
   `<self-learning-record>` XML 块（受 `max_conversation_turns=5` 和"赶紧做
   对题"压力影响，agent 不会主动写）。在这两条通道稳定产出之前，没有 LIFT
   代发的话 `learn review` 会因为 signal 太少而几乎选不到 session 进入
   review。
2. **代发拿到的是 ground truth**——`PhaseRun.success` / `content_score` /
   `work_session_id` 由评测系统直接判定，比 LLM 自己揣摩 judge 反馈精准；
   trust 设到 0.95 / 0.9 也是因为这是显式判定，不是模糊隐式信号。
3. **不占 agent turn**——LIFT 是 host 进程在 `evolve_after_task` 钩子里发，
   不消耗 work agent 的 conversation turn 预算，对评测主流程零干扰。

> **退出条件（未来）**：如果某次 warmup 跑完后 `learn review` 看到的
> SignalRecord 里出现稳定数量的 `source: tool_runtime` / `llm_output_feedback`
> 等 plugin hook 自发来源（说明 judge 反馈拟人化或 turns 放宽真的把 hook 自发
> 通道激活了），并且 hook 自发的 signal 数 ≥ LIFT 代发数 + active 规则不退化，
> 可以把 `evolve_after_task` 改成 no-op 或者退化成"plugin 自发 signal=0 时才
> 兜底"的混合策略。

> **注意**：LIFT 代发是兜底；理想路径仍然是让 plugin 的 `llm_output` hook
> 自发 signal——这要求 work agent 真的按 plugin 协议输出
> `<self-learning-record>` XML 块。要在评测里诱导 agent 这么做，可以从两端调：
> 一是把 judge 反馈写得更"像真人随口一句"（见
> [`run_task._build_judge_prompt`](../../eval/run_task.py)），让 work agent
> 把它识别为反馈而不是工单；二是放宽 `max_conversation_turns`，给 agent 留
> 出"额外做点 plugin 协议里要求的事"的预算。

## 第二层根因：`runtime-state.json` 不存在

### 症状

代发逻辑写完后，日志里所有 `post_signal` 都被脚本第一道关卡短路：

```
post_signal: runtime-state.json missing at /root/.openclaw/evolution-runtime/runtime-state.json
```

self-check 段也证实文件确实不在容器里。

### 根因（看 plugin 源码）

`runtime-state.json` 由 [`runtimeManager.js` saveRuntimeState()][rm-save] 写入。
触发点是 [`bootstrapInstance()`][rm-bootstrap] 跑通后，含三条入口：

[rm-save]: https://example.invalid/runtimeManager.js#L122
[rm-bootstrap]: https://example.invalid/runtimeManager.js#L331

1. `service.start`（plugin service 生命周期启动）
2. `session_start` hook（agent 第一次起 session）
3. CLI 命令进 `ensureReady` 路径（如 `learn status` / `learn review`）

但容器里 LIFT 用 `openclaw agent --local` 单次 CLI 调用驱动 work agent，**这条
路径不走 plugin service start，也不会让 session_start hook 完成 bootstrap**。
backend uvicorn 是 systemd-like 的启动器拉起来的（所以 `/ready` 返回 200），
但 plugin 自己的 onboarding 步骤（写 `runtime-state.json` + 创建 InstanceRecord）
从未执行。

直到所有 warmup 题都做完，LIFT 才调 `openclaw learn review`，那时才触发
bootstrap——但已经太晚，前面所有 `post_signal` 早就 short-circuit 完了。

### 修复

`OpenClawWithEvolveAdapter.start_warmup_environment` 在容器起好后，**warmup
主循环开始之前**显式调一次 `openclaw learn status`：

```python
@override
async def start_warmup_environment(self, ctx, resources, workspace_dir):
    env = await super().start_warmup_environment(ctx, resources, workspace_dir)
    session: ContainerSession = env.handle
    await bootstrap_evolution_runtime(openclaw_context(session))
    return env
```

`learn status` 的 CLI handler 内部 `await ensureReady()`，会跑通
`bootstrapInstance → /instances/onboard → saveRuntimeState`——把
`runtime-state.json` 一次性落盘。一次调用就够了，之后 N 道题的
`post_signal` 都能命中。

## 第三层根因：onboard 返回 HTTP 400

### 症状

第二层修复加上去之后，日志变成 9 个 warmup 容器全部：

```
evolution runtime bootstrap (learn status) failed (...):
  curl: (22) The requested URL returned error: 400
```

### 根因

plugin 后端的 `onboard_instance` 服务严格要求：

- `workspace_root` 是个 git repo
- 且 `git_root == workspace_root`
- 且至少有一个 HEAD commit

但 LIFT 的 warmup workspace 是裸目录，没 git init。原来 `openclaw_learn_review`
里有 `git init + commit` 准备脚本，**但它在 `learn review` 之前才跑**——
现在 bootstrap 比 `learn review` 早得多，撞上了 plugin 的 git 校验。

### 修复

抽出 `_PREPARE_WORKSPACE_GIT_SCRIPT` 常量，让 `bootstrap_evolution_runtime`
自己在调 `learn status` 之前先 `git init + commit`：

```bash
mkdir -p /workspace/task
git config --global --add safe.directory /workspace/task
git config --global user.email "lift@local"
git config --global user.name "lift"
if [[ ! -d /workspace/task/.git ]]; then
  git -C /workspace/task init -q
  git -C /workspace/task add -A
  git -C /workspace/task commit -q --allow-empty -m "lift: warmup baseline"
fi
```

`openclaw_learn_review` 里也复用这个常量（脚本幂等：第二次跑时
`[[ ! -d /workspace/task/.git ]]` 短路）。

## 第四层根因：容器里没装 `jq`

### 症状

git 修复加上去之后：

- bootstrap 9/9 成功（`evolution runtime bootstrapped` 日志 + 输出"当前实例 | pro-..."）
- self-check 段 `cat /root/.openclaw/evolution-runtime/runtime-state.json`
  能完整 dump 出含 `instanceId` / `instanceToken` 的 JSON
- **但每题 `post_signal` 仍然报 "instance_id/token not yet provisioned"**

### 根因

post_signal 脚本用 `jq` 解析 `runtime-state.json`：

```bash
INSTANCE_ID=$(jq -r '.instanceId // empty' "${STATE_FILE}" 2>/dev/null)
```

直接 `docker exec` 进容器手动验证：

```
$ jq --version
bash: line 7: jq: command not found
```

容器里**没装 jq**。`2>/dev/null` 把 "command not found" 错误吞了，jq 啥也没
输出，`INSTANCE_ID` 为空字符串，触发 "not yet provisioned" 短路。

self-check 段同样的 jq 调用也一直被 skip——但因为它前面紧跟着 `cat <state.json>`
直接 dump 全文，肉眼看上去文件内容是有的，掩盖了 jq 不可用的事实。

### 修复

容器里没 `jq` 但**一定有** `python3`（plugin runtime 用的就是 venv python）。
把 evolve.py 里所有 jq 调用换成 python：

```bash
read -r INSTANCE_ID INSTANCE_TOKEN < <(python3 - "${STATE_FILE}" <<'PY'
import json, sys
try:
    s = json.load(open(sys.argv[1]))
    print((s.get("instanceId") or "").strip(),
          (s.get("instanceToken") or "").strip())
except Exception:
    print("", "")
PY
)
```

payload 注入 `instance_id` 字段同样改用 python json 处理（避免 bash + jq
混合带来的转义陷阱）。

self-check 段 `/signals?limit=1` 探测也同步替换。

## 修复结果

四层根因依次拆完后，日志数据：

| 指标 | 修复前 | 修复后 |
|------|--------|--------|
| bootstrap 成功 | 0/9 | **9/9** |
| `post_signal: http_code=200` | 0 | **32/32** |
| `post_signal: instance_id/token not yet provisioned` | 全部 | **0** |
| `learn review` 看到的 signals | 0 | **25** |
| `learn review` 产出的 active 规则 | 无 | **1 条** |

`learn review` 真正学出了一条具体规则：

> 处理数据报表任务时，金额类字段保留2位小数，完成率类字段保留1位小数，
> 生成的 Markdown 报告第一行必须为「日期：YYYY-MM-DD」格式，列顺序严格按照
> 用户要求排列，移除所有未明确要求的多余列。
>
> confidence: 0.9 | risk: 0.2 | status: active（自动启用）

证据章节引用的 4 条 user critique 全部来自 LIFT judge 的反馈，端到端贯通。

## 关键文件

- [adapter.py](./adapter.py)：`OpenClawWithEvolveAdapter`，覆写
  `start_warmup_environment` / `evolve_after_task` / `evolve_after_warmup`
- [evolve.py](./evolve.py)：核心 helper
  - `bootstrap_evolution_runtime` — git init + `learn status` 触发 onboard
  - `post_signal_via_container` — 容器内 python3 解析 runtime-state.json + curl `POST /signals`
  - `openclaw_learn_review` — git 准备脚本 + worker thinking=off + `learn review`
  - `_PREPARE_WORKSPACE_GIT_SCRIPT` — 共享的 git 准备脚本（bootstrap / review 复用）
- [base.py](../base.py)：`evolve_after_task` 钩子签名（仅扩 `result: PhaseRun` 参数）

## 反思 / 注意事项

- **plugin 自带 hook 才是 signal 上报的"主路径"，agent 自调 curl 是协议里的兜底**：
  早期复盘里写过的"评测语境下 agent 不调 curl"判断有偏——读 `hooks.js` 后才看清
  plugin 在 `before_prompt_build` / `after_tool_call` / `llm_output` 这三个 hook
  上自动 `postSignal`，`postSignal` 又被 `runtime-state.json` 落盘与否门控。
  bootstrap 修复同时让 hook 自发与 LIFT 代发两条通道都具备生效条件，但 hook 自发
  通道在评测语境下产出仍然偏低（tool 鲜少失败、`<self-learning-record>` 块不会
  自动生成），所以 LIFT 代发是必要的兜底。
- **不要假设容器里"标准 Linux 工具一定在"**——OpenClaw 镜像基于精简 base，
  `jq` / `tree` / `htop` 这类常用工具都可能缺。坚持用 `python3` 这种 plugin
  runtime 自带的工具更稳。
- **Plugin 的"健康自检"输出 != 实际 plugin runtime 的 process 状态**：bootstrap
  之后 plugin 进程内 `config.instanceId` 可能是有的，但 `runtime-state.json`
  文件落盘 / 当前进程外读出来又是另一回事。验证 helper 行为时优先用
  **真实容器 + 真实 helper 函数**，而不是只看日志里的 markdown 状态卡。
- **抽象层最小侵入**：信号代发只挂在 `evolve_after_task` 钩子上，没新加抽象方法、
  没改 `OnTurnCallback` 签名。基类对"题完成后做点啥"早就开了口子，子类填进去就好。
- **bootstrap 必须先于 warmup 主循环**：因为 `evolve_after_task` 在第一题完成
  时就会发 signal——任何"懒触发"或"等到第一次 post_signal 失败再触发"都会丢掉
  第一题的信号，给后续 review 阶段统计带来漂移。
- **judge 反馈风格会反向影响 plugin hook 自发通道的产出**：judge reason 越像
  "审计员清单"，work agent 越倾向于把它当工单直接修答案；越像真人随口一句，
  work agent 越可能在 LLM 输出里写 `<self-learning-record>`。这是把 hook 自发
  通道激活的一个潜在杠杆——见 [`run_task._build_judge_prompt`](../../eval/run_task.py)。
