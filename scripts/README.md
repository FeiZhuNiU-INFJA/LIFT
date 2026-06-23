# scripts/

评测主流程之外的运维 / 分析小工具集合。每个脚本都能独立运行，互相不依赖。

> 默认在 `evolve_eval` conda 环境里跑（`/tmp/miniconda3/envs/evolve_eval/bin/python`）；
> 涉及 Langfuse / `.env` 的脚本会自动 `load_dotenv()` 读项目根的 `.env`。

## 脚本一览

| 脚本 | 用途 | 关键依赖 |
|------|------|---------|
| [cleanup_langfuse_traces.py](./cleanup_langfuse_traces.py) | 清理自部署 Langfuse 中超过 N 天的 trace（含 observations / scores 级联删除） | `httpx`, `python-dotenv`, `LANGFUSE_*` 凭据 |
| [extract_enriched_csv.py](./extract_enriched_csv.py) | 把 enriched eval JSON 抽成扁平 CSV（每题 baseline / evolved 两行） | `pandas` |
| [compute_improvement_metrics.py](./compute_improvement_metrics.py) | 基于上一步 CSV 计算 evolved/baseline 各指标提升比 + 全局/分类汇总 | `pandas` |
| [screenshot_dashboard.py](./screenshot_dashboard.py) | 用 Playwright 把 LIFT dashboard（HTTP 或静态 HTML）截成 PNG | `playwright`（需另装） |
| [clean-results.sh](./clean-results.sh) | 回收 OpenClaw Docker workspace 里 root 拥有的 `results/` 文件并清空 | `sudo`, `bash` |

## 常用流水线

```bash
# 1) 评测跑完 → 后处理生成 enriched JSON（由 src/postprocess 完成，略）
# 2) 抽 CSV
python scripts/extract_enriched_csv.py results/<run>/enriched.json

# 3) 算 evolved/baseline 提升
python scripts/compute_improvement_metrics.py results/<run>/enriched_extracted.csv

# 4) 截 dashboard 截图（可选，分享用）
python scripts/screenshot_dashboard.py --url http://localhost:8765 -o run.png
```

## 运维任务

### 定期清 Langfuse trace

自部署 Langfuse 没有自带 retention（社区版），用 `cleanup_langfuse_traces.py` 走公共 REST
API 删除过期 trace（Langfuse 后端会异步级联清理 observations / scores / 媒体）。

```bash
# 预演（默认 dry-run）
python scripts/cleanup_langfuse_traces.py --older-than-days 30

# 真删
python scripts/cleanup_langfuse_traces.py --older-than-days 30 --execute

# server 压力大时减小批量
python scripts/cleanup_langfuse_traces.py --older-than-days 30 --execute --batch-size 50
```

接 cron 示例（每天凌晨 3 点清 30 天前的 trace）：

```cron
0 3 * * * cd /root/workspace/agent_evolve_evaluation && /tmp/miniconda3/envs/evolve_eval/bin/python scripts/cleanup_langfuse_traces.py --older-than-days 30 --execute >> logs/langfuse-cleanup.log 2>&1
```

### 清空跑批结果

```bash
./scripts/clean-results.sh   # 自动 sudo 接管 root 拥有的文件再删
```

## 加新脚本时的约定

- 单文件、无内部 import 依赖（用 `src/` 的能力请走 `python -m src.xxx`）。
- 顶部 docstring 写清「做什么 / 用例命令」，参数走 `argparse`。
- 涉及外部副作用（删数据、写 issue、推 git 等）的，**默认 dry-run**，显式 flag 才执行。
- 加完后在本 README 的「脚本一览」表格里登记。
