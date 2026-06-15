# cleanup-eval-env

清理 LIFT / agent_evolve_evaluation 评测环境产生的残留物——孤儿容器、delta
镜像、dangling 镜像，以及（可选的）旧 results / logs。

## 何时使用

- 评测主进程被异常杀掉（trae sandbox `--die-with-parent`、OOM、Ctrl-C 等）后
  留下了 `evolve-openclaw-*` 容器、`evolve-eval-delta:*` 镜像
- docker 磁盘吃紧、`docker images` 里堆了一串 `<none>` dangling 镜像
- 准备下一轮评测前需要一个干净环境

## 清理范围与策略

| 资源 | 默认行为 | 备注 |
|---|---|---|
| `evolve-openclaw-*` 容器（exited / running） | **删除** | running 容器先尝试 stop 再 rm -f |
| `evolve-eval-delta:*` 镜像（warmup commit 产物） | **删除** | 评测期间会重新 commit，不必保留 |
| dangling `<none>` 镜像 | **删除** | 旧构建的中间层 |
| 根目录 `evolve_eval.log` | **删除** | 每次运行前都清，避免新旧日志混淆 |
| `evolve-eval-openclaw-base/with-evolve:latest` | 保留 | 当前 base 镜像，下次评测要用 |
| `ghcr.io/openclaw/openclaw:*` 等上游镜像 | 保留 | docker pull 慢，不删 |
| `results/` 子目录 | **保留** | 默认不清；带 `--results` 才清 |
| `logs/` 子目录 | **保留** | 默认不清；带 `--logs` 才清 |

## 用法

脚本本身用 `BASH_SOURCE` 自定位，不依赖当前工作目录；从任何位置直接调用都可以。

```bash
# 仅打印将要做什么，不动手
bash <项目根>/skill/cleanup-eval-env/cleanup.sh --dry-run

# 默认：清容器 + delta 镜像 + dangling 镜像 + evolve_eval.log
bash <项目根>/skill/cleanup-eval-env/cleanup.sh

# 同时清 results/ 和 logs/
bash <项目根>/skill/cleanup-eval-env/cleanup.sh --results --logs

# 全清（容器 + 镜像 + 日志 + results + logs）
bash <项目根>/skill/cleanup-eval-env/cleanup.sh --all
```

> 把 `<项目根>` 换成自己环境里 `agent_evolve_evaluation` 的实际路径即可；
> 脚本会以自身位置推断项目根，无须先 `cd`。

## 安全提示

- 脚本只匹配特定名字模式（`evolve-openclaw-*` / `evolve-eval-delta:*`），
  不会误删其它 docker 资源。
- `--results` / `--logs` 是不可逆操作，启用前先确认报告/日志已经备份。
- 如果同时有别的同事在用同一台机器跑评测，先用 `--dry-run` 看清单再执行。

## 常见报错

- `docker rm -f` 报 `No such container`：上一次 cleanup 已经删过；忽略。
- `permission denied`：docker daemon socket 权限，加用户到 docker 组或 sudo 跑。
