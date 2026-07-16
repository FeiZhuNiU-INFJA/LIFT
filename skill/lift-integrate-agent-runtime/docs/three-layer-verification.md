# 三层证据交叉验证

要证明 evolve 有效必须做 **Log × Langfuse × Layer** 三层交叉验证 —— 三个证据缺一不可,证明的不是同一件事。

**默认 suite**:`assets/benchmarks_demo/integration_check.json`(4 W + 2 H 个人偏好档案)—— 每题都要求 agent"以后都遵守",是最容易让 memory / reflection hook 真写产物的 suite,且 runtime-agnostic(不依赖联网工具、不依赖 material_dir)。如果 runtime 已通过 hello.json 但在 integration_check 上仍然 evolve-only WARNING,就是本节要抓的问题。

---

## 证据 A:Log —— agent 真的对话了吗?

验证 work agent / judge agent 是否都跑了、reflection 钩子(如 `evolve_after_task` / `evolve_after_warmup`)是否触发。

```bash
LOG=logs/<run_id>.log

# work / judge chat 次数(每题至少一对,评测多轮会更多)
grep -cE "work-agent chat start|user-[0-9a-f]+ session" "$LOG"
grep -cE "judge-agent chat start|judge-[0-9a-f]+ session" "$LOG"

# reflection 钩子触发(active_evolve variant)
grep -E "\[active_evolve\] reflection chat" "$LOG"

# reflection 回复 head —— 全 DONE 说明 LLM 没写东西;有具体内容说明真触发写入
grep "reply_head=" "$LOG"

# 高频错误信号
grep -E "wait output timeout|Cannot connect to Docker|Judge response is not valid JSON" "$LOG" | head
```

**红旗**:所有 reflection `reply_head='DONE\n'` 且证据 C 里 delta diff 也空 → suite 太简单,换更复杂的 suite 再验证。

---

## 证据 A':内容审阅 —— 光"发生了"不够,还得"内容合理" ⚠️

计数通过(chat 次数、trace 数量、delta 有文件)**不代表内容对**。新 runtime 首次跑通后**必须**至少肉眼扫一遍下面这几层内容,否则会踩到"流水线全绿但 agent 什么都没做对"的假阳性。

### A'.1 Material 可读性哨兵(bind mount / workspace seed 路径最常见踩坑点)

```bash
LOG=logs/<run_id>.log

# 文件系统层面报错(work / judge 尝试 open material 失败)
grep -iE "no such file|permission denied|cannot read|读取失败|open .* failed|q[0-9]+_materials.*not found|材料.*不存在" "$LOG"

# 模型自身"逃避语"(LLM 明说看不到附件 → 通常也是路径挂错,只是没冒 IO 异常)
grep -iE "cannot access|don't have access|no attachment|I cannot see|I do not see any" "$LOG"
```

任何一条命中 → `session.py` 的 `task_volume_binds` / `workspace_seed` / 上游 cwd patch 三者有一处错位,回 [evolve-artifact-contract](./evolve-artifact-contract.md) + [adapter-quartet](./adapter-quartet.md) §2.2 排查。

### A'.2 Work / Judge response 抽样(不看数量,看长度和"味道")

打开 `results/lift-runid-<run_id>/dashboard.html`,随手点开 1~2 个 phase 的对话弹窗,或直接从 `*_backfilled.json` 抽:

```bash
JSON=results/lift-runid-<run_id>/lift-runid-<run_id>_backfilled.json
python -c "
import json
r = json.load(open('$JSON'))
for rp in r['runs']:
    for s in rp['suites']:
        for t in s['tasks']:
            for ph_name in ('baseline','evolved'):
                ph = t[ph_name]
                outc = (ph.get('outcome') or {})
                content = (outc.get('content') or '')[:200]
                turns = ph.get('turns') or 0
                score = outc.get('content_score')
                print(f'  {t[\"name\"]:6} {ph_name:8} turns={turns} score={score}')
                print(f'    head: {content!r}')
"
```

看三件事:
- `content` 长度 **> 100 chars** 且不含 `Traceback` / `Error:` / `I cannot` / `I do not have` 逃避语
- turns > 0,且随任务复杂度合理增长(hello 类 1~2 轮,复杂检索 3~10 轮)
- 至少有一部分题 baseline 与 evolved 的 content 有可见差异(否则 evolve 大概率没生效,回证据 C)

### A'.3 Judge 分数分布

```bash
python -c "
import json,collections
r = json.load(open('$JSON'))
buckets = collections.Counter()
for rp in r['runs']:
    for s in rp['suites']:
        for t in s['tasks']:
            for ph_name in ('baseline','evolved'):
                sc = (t[ph_name].get('outcome') or {}).get('content_score')
                if sc is None: buckets['none'] += 1
                elif sc <= 0.05: buckets['0'] += 1
                elif sc >= 0.95: buckets['1'] += 1
                else: buckets['mid'] += 1
print(dict(buckets))
"
```

- **全 0**:通常是 material 都没读到、judge 直接判 fail;或 judge prompt 没渲染任务描述。回 A'.1 / A'.2。
- **全 1**:通常是 judge prompt 里 rubric 塌了(如任务描述被 truncate),judge 无从判分只能全给通过。开 dashboard 抽 1 条 judge dialogue 看 rubric 有没有正常出现。
- **healthy**:0 / mid / 1 都有,或按 baseline 偏低 / evolved 偏高分布。

### A'.4 进化产物内容抽样(把证据 C 的 `ls` 升级成 `cat`)

单看 delta 有文件不够,还得看内容是不是"agent 学到了什么"的自然语言,而不是空文件 / stack trace / 无意义字符:

```bash
DELTA=$(docker images --format '{{.Repository}}:{{.Tag}}' | grep 'lift-delta:.*<run_id>' | head -1)

docker run --rm --entrypoint sh "$DELTA" -c '
  find /opt/<runtime>/memory -type f -size +10c 2>/dev/null | head -5 | while read f; do
    echo "===== $f ====="; cat "$f"
  done
'
```

要求:
- 至少一个 memory 文件非空,内容是**自然语言**(经验总结 / 步骤 / 反例),不是纯 JSON dump / Python traceback / 空 markdown 标题
- 内容与证据 A 里 `reply_head=` 打出的 reflection 摘要在语义上一致(LLM 说要记什么就真记了什么)

### A'.5 综合红旗

| 现象 | 大概率原因 |
|---|---|
| A'.1 命中"逃避语"但没 IO error | LLM 拿到的 material 路径提示错,或 cwd 与 material 挂载点不一致 |
| A'.2 每题 content 都 < 50 chars | agent 主循环提前退出,`docker exec` timeout 或 provider 报错吞掉了 body |
| A'.3 全 0 或全 1 | judge rubric 塌了 / material 缺失连锁反应 |
| A'.4 memory 全空 / 全是 traceback | reflection prompt 未激活 / 上游 memory 写入路径挂错([evolve-artifact-contract](./evolve-artifact-contract.md)) |
| A'.2 baseline == evolved(字节级一致) | evolve 完全没生效,回证据 C 三点错位 |

> **A' 与 A / B / C 的关系**:A / B / C 是"计数在不在",A' 是"内容对不对"。跑完 A / B / C 全绿 **且** A' 抽样合理,才算"新 runtime 接入完备";否则就算 4 项绿灯,后续 benchmark 数据仍然可能是伪造。

---

## 证据 B:Langfuse —— trace 写入 & 后处理拼装

验证容器里的调用确实上报到 Langfuse,且后处理的 backfill 能拿回来做 stitching。

```bash
RID=lift-runid-<run_id>
JSON=results/$RID/${RID}_backfilled.json

# B.1 后处理 backfill 成功(有 work_agent_traces / judge_agent_traces)
python -c "
import json
r = json.load(open('$JSON'))
for rp in r['runs']:
    for s in rp['suites']:
        for t in s['tasks']:
            for ph_name in ('baseline','evolved'):
                ph = t[ph_name]
                lf = ph.get('langfuse') or {}
                wt = len(lf.get('work_agent_traces') or [])
                jt = len(lf.get('judge_agent_traces') or [])
                pt = len(lf.get('plugin_traces') or [])
                tc = ph.get('tool_calls')
                print(f'  {t[\"name\"]:6} {ph_name:8} work={wt} judge={jt} plugin={pt} tool_calls={tc}')
"

# B.2 检查后处理告警
grep -E "trace not found|Failed to fetch trace|trace_backfill" "$LOG"

# B.3 Langfuse UI 侧交叉检查:随便挑一条 trace
# 打开 http://<langfuse-host>/project/<pid>,按 session_id (user-xxx / judge-xxx) 搜
# 应该看到 name=<runtime>-plugin、session/tags 列非空的 root span
```

**通过标准**:
- 每题两个 phase 都有 `work` ≥ 1、`judge` ≥ 1(`turns` 数对齐)
- 静态 dashboard tools 列有非 null 值(说明兜底链路走通,见 [postprocess-and-stitching](./postprocess-and-stitching.md) §5.3)
- Langfuse UI 上 trace 的 Session / Tags 列非空(说明 overlay 用的是 v4 上下文管理器,见 [image-scaffold](./image-scaffold.md) §1.3)

**红旗**:`work=0` / `judge=0` 且日志无 timeout → overlay 没生效或 trace name 没进 `LANGFUSE_PLUGIN_TRACE_NAMES`(见 [common-pitfalls](./common-pitfalls.md))。

**另一红旗**:`work` / `judge` 齐全但 `plugin=0`,容器日志出现 `Failed to export span batch due to timeout` → 容器内 exporter 端点不通宿主 Langfuse。`docker exec <c> env | grep LANGFUSE` 看 `LANGFUSE_BASE_URL` 是否被 `.env` 里的 `localhost` / `127.0.0.1` 污染;修法见 [adapter-quartet](./adapter-quartet.md) §2.2 第 6 点(`env_vars` 覆写)。

---

## 证据 B':5 字段 Token 落库审计 ⚠️

trace 拼上、tool_calls 有值,不代表 **5 字段(input/output/cache_read/cache_write/reasoning)** 都真的到位了 —— **这一层最容易静默失败**,因为 Langfuse ingestion 会**默默丢弃**它不识别的 usage key(不报错),后处理拿到就是 0。

**先按 LIFT 5 字段口径判定"这个 runtime 应该有几个字段"**(见 [token-observability](./token-observability.md) §0):
- 全 5 字段全绿:上游 provider 是 OpenAI Chat Completions / Anthropic 系(cache/reasoning 都能拿)
- reasoning 缺:上游 schema 无独立 reasoning 字段(如 OpenHuman),reasoning 隐式入 output,不算 bug
- cache_read 缺:上游 provider 不支持 prompt caching,或者是纯冷启动 turn(hello.json 每题就一 turn,没历史 → cache_read=0 是真值)

**验收命令**(在 IC 或 hello.json 跑完后跑):

```bash
RUN=lift-runid-<run_id>
BF=results/$RUN/$RUN\_backfilled.json

# 每条 trace 的 5 字段展开(过滤掉 evolve 阶段,只看 work/judge)
/root/miniconda3/envs/lift/bin/python -c "
import json, pathlib
d = json.loads(pathlib.Path('$BF').read_text())
for run in d['runs']:
    for suite in run['suites']:
        for t in suite['tasks']:
            for ph in ('baseline','evolved'):
                lf = t[ph].get('langfuse') or {}
                for i, tr in enumerate(lf.get('work_agent_traces') or []):
                    tk = tr.get('tokens') or {}
                    print(f\"{t['name']} {ph} tr[{i}] in={tk.get('input_tokens')} out={tk.get('output_tokens')} cr={tk.get('cache_read_tokens')} cw={tk.get('cache_write_tokens')} rs={tk.get('reasoning_tokens')}\")
"

# 汇总维度(_comparison_metrics.csv):
column -t -s, results/$RUN/${RUN}_comparison_metrics.csv | grep -E "input_tokens|output_tokens|cache_read|cache_write|reasoning"
```

**通过标准**(按 [token-observability](./token-observability.md) §0 口径,按 runtime 上游能力打分):

| 字段 | 通过标准 |
|---|---|
| `output_tokens` | **必须全非 0**(hard fail 条件:任何 turn 都必须有输出) |
| `input_tokens` | **必须全非 0** |
| `cache_read_tokens` | turn 1 冷启动可为 0;turn ≥ 2 应 > 0(除非 provider 不支持 caching) |
| `cache_write_tokens` | 视 provider — 大多数 API 不显式暴露,可能永远为 0(不算 bug) |
| `reasoning_tokens` | 若 runtime 开了 thinking/reasoning → 应 > 0;上游无字段(如 OpenHuman)则接受为 0 |

**若不通过 —— 定位到哪层断了(三层断层图)**:详见 [token-observability](./token-observability.md) §2-§5:

| 断层 | 症状 | 修法要点 |
|---|---|---|
| **A. agent 内部→plugin** | plugin log 里 `accumulator=(none)` 或 `usage=(no usage)` | microtask 竞争(OpenClaw)/ non-stream JSON 路径漏 wrap(GA)/ provider 归一层丢字段 |
| **B. plugin→Langfuse** | plugin log 里 accumulator 有值,但 Langfuse observation `usage_details={}` | Langfuse ingestion 只识别 `usage.input/output/total`,fine-grained 必须写 `usageDetails` sibling |
| **C. Langfuse→backfill** | Langfuse UI 有值,但 CSV / backfilled JSON 里 0 | `src/report/langfuse_trace_fetch.py::_usage_breakdown` 必须同时读 snake_case `usage_details` 和 camelCase `usageDetails` |

---

## 证据 C:Layer —— delta 镜像真的包含进化内容吗?

这是 LIFT 全流程的**核心命题**。**LIFT 已内建自动化落盘**,绝大多数情况**不需要**手动保留 delta 镜像做 `docker diff` —— 直接看 pipeline 日志三行 + `results/{run_id}/delta_diff_*.txt` dump 文件即可完成 layer 层验证。

### C.1 pipeline 日志三行

[`commit_delta_image`](file:///root/workspace/agent_evolve_evaluation/src/lift/adapters/container/delta.py) 在 `docker commit` 之前自动打:

```
INFO Delta preflight diff (full dump) [evolve-<runtime>-...]: -> /root/.../results/lift-runid-<run_id>/delta_diff_evolve-<runtime>-...txt
INFO Delta preflight diff (full) [evolve-<runtime>-...]: +2038A ~14C -0D across 17 paths (top: /usr/local/lib x1800, /opt/<runtime>/memory x9, ...)
INFO Delta preflight diff (evolve-only) [evolve-<runtime>-...]: +9A ~2C -0D across 1 paths (top: /opt/<runtime>/memory x11)
```

含义速查:
- **`full dump`** = 完整 `docker diff` 原始输出落盘路径(每行 `A|C|D <absolute_path>`;MB 级;delta 镜像被 pipeline 清理后仍可回溯)
- **`full`** = upperdir 全集摘要,`+NA ~NC -ND` = 新增 / 修改 / 删除 的容器 FS 层文件计数(bind mount 天然不进 upperdir)
- **`evolve-only`** = 只统计 adapter `evolve_paths` 白名单目录下的变更;未声明白名单则不打此行

### C.1 红旗速查

| 现象 | 含义 | 处理路径 |
|---|---|---|
| `full` = `no changes (empty upperdir)` | warmup 完全没往容器 FS 层写东西 | [evolve-artifact-contract](./evolve-artifact-contract.md) 三点错位(LLM 写到了 bind mount / tmpfs) |
| `evolve-only` 升级为 **`WARNING`** + `no changes under evolve_paths=...` | 白名单目录里没落东西 | 看紧跟着的 `candidate unlisted evolve paths` 那行,或 grep dump 文件(C.2) |
| WARNING 下面又跟一行 `candidate unlisted evolve paths ... top: /X x67, /Y x2` | LIFT 已经从 dump 里挑出疑似 evolve 顶层目录 | 直接对着建议名单更新 `evolve_paths`,见 [adapter-quartet](./adapter-quartet.md) §2.1 "声明错了怎么办" |
| 未声明 `evolve_paths`(无 `evolve-only` 行)+ `full` 的 `top:` 里没出现你期望的 `/opt/<runtime>/memory` | 三点错位或路径声明缺失 | 补 `evolve_paths` 后即可自动 WARNING / candidate |

### C.2 dump 文件抽样(当 log 摘要不够看时)

log 里的 `top:` 只按前 3 层目录聚合,看不到具体是哪个文件。想看具体文件路径(哪一层挂的、mtime、大小)时直接读 dump:

```bash
DUMP=$(ls results/lift-runid-<run_id>/delta_diff_*.txt | head -1)

# 剔除噪音看剩下的(如果白名单声明正确,这里应能看到 memory / skill / wiki 类路径)
grep -vE "^[ACD] (/root/\.cache|/tmp|/var/(cache|lib/(apt|dpkg))|/proc|/sys)" "$DUMP" | head -50

# 想看某个具体目录深度的所有变更(比 log 的 3 层聚合更细)
grep -E "^A /opt/<runtime>/memory/" "$DUMP" | head
```

### C.3 通过 = 什么样

- `full` 行有实质变更(不是 `no changes`)
- `evolve-only` 是 `INFO` 级别(不是 `WARNING`)+ 非零计数
- 抽样 dump 文件里 `evolve_paths` 白名单目录下的新增 / 修改内容与证据 A 里的 reflection reply 语义一致(LLM 说要写什么就真的写下了什么,配合证据 A'.4 的内容抽样)

### C.4 兜底方案:手动保留 delta 镜像做内容级 diff(仅当想比对文件**内容**而非路径列表时)

`--warmup-only` 会跳过 holdout 且不 `docker rmi` delta,最方便:

```bash
# 用 --warmup-only 只跑 warmup + commit,delta 镜像会保留下来
nohup python -m src.cli.lift_main -r <runtime> \
  --benchmark_dir assets/benchmarks_demo --suite <suite>.json \
  --run_id <run_id> --warmup-only > logs/<run_id>.log 2>&1 &
wait

# 找出 delta 镜像
DELTA=$(docker images --format '{{.Repository}}:{{.Tag}}' | grep "lift-delta:.*<run_id>" | head -1)
echo "delta = $DELTA"

# 与 baseline 镜像做内容级 diff(看 memory 文件的具体自然语言内容)
BASE=lift-<runtime>:latest
docker run --rm --entrypoint sh "$BASE"  -c 'find /opt/<runtime>/memory -type f -exec md5sum {} +' | sort > /tmp/base_md5.txt
docker run --rm --entrypoint sh "$DELTA" -c 'find /opt/<runtime>/memory -type f -exec md5sum {} +' | sort > /tmp/delta_md5.txt
diff /tmp/base_md5.txt /tmp/delta_md5.txt

# 或者直接读一个 memory 文件的内容看是不是自然语言(配合 A'.4)
docker run --rm --entrypoint sh "$DELTA" -c 'cat /opt/<runtime>/memory/*.md | head -100'
```

正式跑(含 holdout)时可以在另一个 shell 循环抢 tag:

```bash
while true; do
  D=$(docker images -q "lift-delta:*<run_id>*" | head -1)
  if [[ -n "$D" ]]; then docker tag "$D" "kept-delta:<run_id>"; break; fi
  sleep 2
done
```

> **走 C.4 的场景**:dump 文件已能告诉你"哪些路径变了",但如果要看某个 memory 文件的**具体内容**(自然语言 vs traceback vs 空文件),必须从 delta 镜像里 `cat`。这也是 A'.4 内容抽样的入口。

---

## 综合判断表

| 证据 A | A' | 证据 B | 证据 C | 结论 |
|---|---|---|---|---|
| ✅ chat / reflection 都触发 | ✅ 内容合理 | ✅ trace 齐全 | ✅ delta 有内容 | Runtime 接入完备 ✅ |
| ✅ | ❌ material 逃避语 / content 极短 | — | — | material / cwd 路径挂错,回 [evolve-artifact-contract](./evolve-artifact-contract.md) + [adapter-quartet](./adapter-quartet.md) §2.2 |
| ✅ | ❌ judge score 全 0 或全 1 | — | — | material 缺失连锁反应 / judge prompt rubric 塌了 |
| ✅ | ❌ memory 全空 / traceback | — | ✅ delta 有文件 | reflection 触发但写入错乱,回 [evolve-artifact-contract](./evolve-artifact-contract.md) |
| ✅ | ✅ | ✅ | ❌ delta 与 baseline 一致 | 三点错位 bug,evolve **无效**,必须修 |
| ✅ | ✅ | ❌ trace 缺失 | ✅ delta 有内容 | overlay 或 `LANGFUSE_PLUGIN_TRACE_NAMES` 有问题,evolve 有效但 dashboard / 后处理拿不到分析数据 |
| ❌ reflection 无 / timeout | — | — | — | reflection 钩子未生效或 chat 卡死,先修 chat 再验证其他 |
| ✅ 全 DONE | ✅ | ✅ | ❌ | suite 太简单不触发写入,换更复杂的 suite 再验 |
