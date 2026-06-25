# scripts/

评测主流程之外的运维 / 分析小工具集合。每个脚本都能独立运行，互相不依赖。

> 默认在 `lift` conda 环境里跑（`/root/miniconda3/envs/lift/bin/python`）；
> 涉及 Langfuse / `.env` 的脚本会自动 `load_dotenv()` 读项目根的 `.env`。

## 脚本一览

| 脚本 | 用途 | 关键依赖 |
|------|------|---------|
| [cleanup_langfuse_traces.py](./cleanup_langfuse_traces.py) | 清理自部署 Langfuse 中超过 N 天的 trace（含 observations / scores 级联删除） | `httpx`, `python-dotenv`, `LANGFUSE_*` 凭据 |
| [upload_benchmark_to_hf.py](./upload_benchmark_to_hf.py) | 把 `benchmark_mds.zip` 从 TOS 镜像推到 HuggingFace dataset 仓库（维护者一次性脚本） | `huggingface_hub`, `HF_TOKEN`（写权限） |
| [screenshot_dashboard.py](./screenshot_dashboard.py) | 用 Playwright 把 LIFT dashboard（HTTP 或静态 HTML）截成 PNG | `playwright`（需另装） |
| [clean-results.sh](./clean-results.sh) | 回收 OpenClaw Docker workspace 里 root 拥有的 `results/` 文件并清空 | `sudo`, `bash` |
| [extract_enriched_csv.py](./extract_enriched_csv.py) ⚠️ legacy | 把 enriched eval JSON 抽成扁平 CSV。**已被 `src/postprocess/extract.py` 取代**（自动随 `--evaluate-only` 跑，且按 agent runtime 分支选择正确口径）；保留供历史 run 临时检查使用 | `pandas` |
| [compute_improvement_metrics.py](./compute_improvement_metrics.py) ⚠️ legacy | 基于上一步 CSV 算 evolved/baseline 提升。**已被 `src/postprocess/metrics.py` 取代**（用 `(evolved-baseline)/baseline` 百分比，含离群剔除、`cached_token_ratio` 等扩展列）；保留供历史 run 临时检查使用 | `pandas` |

## 主流程：跑完直接走 `src/postprocess`

正常流程下，`python -m src.cli.lift_main` **跑完会自动触发后处理**，无需再调用 scripts/
里的 extract / compute。手动重跑后处理只需要：

```bash
python -m src.cli.lift_main -r openclaw --run_id <existing-run-id> --evaluate-only
```

这会重新生成 `results/<run>/`：

- `*_comparison_metrics.csv` —— 每题 baseline ↔ evolved 对照 + 各指标 `impr_*` / `diff_*`
- `*_summary_metrics.csv` —— 按 scope（run / suite / category）的聚合 + `mean_impr_*`
- `*_metrics_report.html` —— 可分享的可视化报告
- `*_backfilled.json` —— Langfuse trace 回填后的 enriched 报告

后处理细节见 [`docs/eval-flow.md §13`](../docs/eval-flow.md)。

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
0 3 * * * cd /root/workspace/agent_evolve_evaluation && /root/miniconda3/envs/lift/bin/python scripts/cleanup_langfuse_traces.py --older-than-days 30 --execute >> logs/langfuse-cleanup.log 2>&1
```

### 把最新 benchmark 推到 HuggingFace

数据集仓库默认是 [`FeiZhuNiU-INFJA/EALE`](https://huggingface.co/datasets/FeiZhuNiU-INFJA/EALE)
（可用 `.env` 的 `BENCHMARK_HF_REPO` 覆盖）。仓库不存在时脚本会用 `HF_TOKEN`
自动 `create_repo(exist_ok=True)`。

```bash
# 默认从 TOS 拉最新 zip 再推
python scripts/upload_benchmark_to_hf.py

# 推已有的本地 zip
python scripts/upload_benchmark_to_hf.py --zip /path/to/benchmark_mds.zip
```

### 清空跑批结果

```bash
./scripts/clean-results.sh   # 自动 sudo 接管 root 拥有的文件再删
```

### 给 dashboard 截图

```bash
python scripts/screenshot_dashboard.py --url http://localhost:8080 -o run.png
# 也支持静态 dashboard.html：
python scripts/screenshot_dashboard.py --html results/<run>/dashboard.html -o run.png
```

## 加新脚本时的约定

- 单文件、无内部 import 依赖（用 `src/` 的能力请走 `python -m src.xxx`）。
- 顶部 docstring 写清「做什么 / 用例命令」，参数走 `argparse`。
- 涉及外部副作用（删数据、写 issue、推 git 等）的，**默认 dry-run**，显式 flag 才执行。
- 加完后在本 README 的「脚本一览」表格里登记。
- 如果新脚本和 `src/postprocess/` 等正式模块功能重合，请优先把能力做进 `src/`，
  scripts/ 只放运维 / 一次性 / 外部依赖型工具。
