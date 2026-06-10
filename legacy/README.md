# Legacy evaluation stack

宿主机直跑时代的 OpenClaw / Hermes 评测入口，已被正式 [`src/`](../src/) LIFT 路径取代。

**不要**与新版同时使用 `PYTHONPATH=legacy:.`，否则 `import src` 会歧义。

## 运行方式

在仓库根目录：

```bash
# OpenClaw 宿主机模式
PYTHONPATH=legacy python legacy/openclaw_main.py --mode exam --suite hello.json

# Hermes 宿主机模式
PYTHONPATH=legacy python legacy/hermes_main.py --suite hello.json

# 仅后处理
PYTHONPATH=legacy python legacy/postprocess/run_post_process.py --run_id <id>
```

## 目录

| 路径 | 说明 |
|------|------|
| `src/` | 旧 Python 包（`src.agents`、`src.eval_core` 等） |
| `openclaw_main.py` / `hermes_main.py` | 旧 CLI 入口 |
| `preprocess/` / `postprocess/` | 旧预处理与后处理 |
| `langfuse-hermes/` | Hermes Langfuse 插件源码 |
| `scripts/` | `rebuild-evolution-runtime.sh`、`reset-evolution.sh` 等 |

## 正式入口

```bash
python -m src.cli.lift_main -r openclaw --suite hello.json
```

见 [docs/lift-framework-guide-cn.md](../docs/lift-framework-guide-cn.md)。
