# LIFT × self-evolving-plugin-pro 信号管道修复实录

> 本文档复盘 LIFT 评测在 `OpenClawWithEvolveAdapter` 路径下让进化插件
> ``self-evolving-plugin-pro`` 真正"看见对话、学到经验"的整套修复过程。
> 给后续维护者一份完整的因果链——任何一环打回原形都能定位到对应症状。

## 背景

LIFT 通过 `OpenClawWithEvolveAdapter` 跑带进化插件的 OpenClaw 容器：

- warmup 阶段：跑题 → 写信号 → `learn review` → `docker commit` 出 delta 镜像
- hold-out 阶段：在 baseline / evolved（delta）两个镜像上各跑同一题，对比得分

进化插件 `self-evolving-plugin-pro` 的工作前提是 **SignalRecord 表非空**。
信号在常规使用场景下由 work agent 自己用 exec 工具调
`curl POST /signals` 上报。

## 症状

最初日志里 `learn review` 阶段反复出现"查看对话=0"——也就是
`SignalRecord` 表是空的，插件什么都没学到，自然也产不出 active 规则。

## 第一层根因：评测语境下 agent 不发 signal

### 问题

plugin 的协议设计假设是真人交互：

- 真人："不对，标题应该居中" → 明显的 correction → agent 自觉调 curl 上报
- 真人："你做得很好" → success_confirmation → agent 自觉调 curl 上报

LIFT 评测里：

- "用户消息" 实际是 judge agent 包装回来的 reason（"缺少问候语，请补上"）
- 这种**系统化机械反馈**，work agent LLM 大概率识别为评测系统的修改指令而不是
  用户反馈，直接去改答案，不去执行 plugin 协议里的 `exec curl`
- `max_conversation_turns=5` 卡得很紧，agent 优先"赶紧把题做对"，没空发 signal

### 修复

LIFT 直接代发——评测系统已知每题的 `success` / `content_score` /
`work_session_id` 这是 ground truth，比 agent 揣摩用户语气精准。

落地形态（方案 B 收敛后，最小侵入）：

- [base.py](./../base.py) `evolve_after_task` 钩子（基类原本就是 no-op，为这种
  场景预留）签名加 `result: PhaseRun` 参数，让子类拿到 session_id 和成败信息
- [adapter.py](./adapter.py) 子类覆盖 `evolve_after_task`：题级粒度调
  `post_signal_via_container`
- [evolve.py](./evolve.py) `post_signal_via_container`：容器内 `docker exec`
  bash 拉 `runtime-state.json` 里的 `instanceId` / `instanceToken`，curl
  `POST /signals`，走的是和 plugin 协议完全一致的请求

抽象层零新方法、无侵入。

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
  - `openclaw_learn_review` — `learn review` + self-check
  - `_PREPARE_WORKSPACE_GIT_SCRIPT` — 共享的 git 准备脚本（bootstrap / review 复用）
  - `_SELF_EVOLVING_HEALTHCHECK` — plugin 加载/onboard/backend 状态完整 dump
- [base.py](../base.py)：`evolve_after_task` 钩子签名（仅扩 `result: PhaseRun` 参数）

## 反思 / 注意事项

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
