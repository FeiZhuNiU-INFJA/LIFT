---
name: "setup-lift-env"
description: "从零配置 agent_evolve_evaluation (LIFT) 评测环境：conda python、本地 docker 部署 langfuse、引导 .env、preprocess 拉数据、构建 openclaw-with-evolve 镜像，并用 hello.json 冒烟（TUI + dashboard 可视化）。支持 macOS / Linux。当用户要求搭建/初始化/配置评测环境，或第一次跑通本仓库时使用。"
---

# setup-lift-env

引导用户在 **macOS** 或 **Linux** 上从零把 `agent_evolve_evaluation`（LIFT 评测框架）跑起来。
按下面 6 个步骤顺序执行；每步先检测是否已就绪，避免重复操作。

## 何时使用

- 新机器 / 新同事第一次配置本仓库
- 想从零跑通一个最小冒烟（`hello.json`）并看到 TUI + 浏览器 dashboard
- 环境损坏需要重新搭建依赖、镜像、Langfuse

## 前置约定

- 项目根：包含 `requirements.txt` / `agent-runtimes/` / `src/cli/` 的目录（下文记为 `<ROOT>`）。
- 全程在 `<ROOT>` 下执行命令。
- **忽略 hermes 相关变量**（`HERMES_API_KEY` / `HERMES_ENV_FILE` / `API_SERVER_*`），本最小链路不需要。

---

## 步骤 0：检测系统与 Docker

```bash
uname -s                      # Darwin=macOS, Linux=Linux
docker info >/dev/null 2>&1 && echo "docker OK" || echo "docker NOT ready"
conda --version || echo "conda missing"
```

### 探测是否在字节内网（决定后续构建镜像走公网还是内网）

```bash
# ≤3s 内出结论；两个都通 = 内网，任意一个不通就当作公网
getent hosts mirrors.byted.org >/dev/null 2>&1 && \
  curl -sf --max-time 3 https://bytedpypi.byted.org/simple/pip/ -o /dev/null && \
  echo "==> 字节内网环境" || echo "==> 公网环境"
```

- **内网**：后续 openclaw / genericagent 镜像构建**务必**传
  `APT_MIRROR=http://mirrors.byted.org` 与 `PIP_INDEX_URL=https://bytedpypi.byted.org/simple/`
  两个环境变量，否则会卡在 `deb.debian.org` 或 `pypi.org`。
- **公网**：直接跑，无需额外变量。

### 安装 Docker

- **macOS（方式 A，官方）**：安装 Docker Desktop，启动后 `docker info` 应正常。
- **macOS（方式 B，轻量纯命令行，已验证）**：
  ```bash
  brew install colima docker docker-compose
  colima start --cpu 4 --memory 8   # 首次会下载 VM 镜像，需数分钟
  docker info
  ```
  若 `docker compose` 找不到插件，在 `~/.docker/config.json` 加
  `"cliPluginsExtraDirs": ["/opt/homebrew/lib/docker/cli-plugins"]`（Intel 为 `/usr/local/lib/docker/cli-plugins`）。
- **Linux**：按 [Docker Engine 安装指南](https://docs.docker.com/engine/install/) 安装；
  当前用户须在 `docker` 组（否则后续 build/run 报 permission denied）。

---

## 步骤 1：Conda 环境 + Python 依赖

```bash
conda create -y -n evolve_eval python=3.12
conda activate evolve_eval
pip install -r requirements.txt
```

验证：`python -c "import langfuse, pydantic, rich; print('deps OK')"`。

---

## 步骤 2：本地 Docker 部署 Langfuse（必需）

Langfuse 用于 pre-chat 上报、容器内 trace、后处理 backfill，**不可跳过**。

```bash
# 建议放到 <ROOT> 外的独立目录，例如 ~/langfuse
git clone https://github.com/langfuse/langfuse.git ~/langfuse
cd ~/langfuse
# 优先用 v2 compose 插件；机器上只有 v1 docker-compose 时回退
docker compose up -d 2>/dev/null || docker-compose up -d
```

> **compose 版本兼容**：新版 Docker Desktop / Docker Engine 已内置 v2（`docker compose`）；
> 老机器 / 只装了 `apt install docker-compose` 的环境走 v1（`docker-compose`）。
> 命令一样，语法完全兼容，本 skill 后续所有 `docker compose xxx` 都可替换为 `docker-compose xxx`。

### 端口自定义（例如端口 3000 已被占用，换成 3888）

`docker-compose.yml` 的 3000 出现在两处（`ports:` 与 `NEXTAUTH_URL:`），
`.env` 里 `LANGFUSE_BASE_URL` 一处，**三处必须同步改**，否则登录跳转会 404 或链接不通：

```yaml
# ~/langfuse/docker-compose.yml
services:
  langfuse-worker:
    environment: &langfuse-worker-env
      NEXTAUTH_URL: ${NEXTAUTH_URL:-http://localhost:3888}   # ← 从 3000 改
  langfuse-web:
    ports:
      - 3888:3000                                            # ← 从 3000:3000 改（左侧宿主端口，右侧容器内固定 3000 不动）
```

改完后 `docker compose up -d`（或 `docker-compose up -d`）会自动 recreate 受影响容器。

> `.env` 里 `LANGFUSE_BASE_URL` 直接填 `http://localhost:<新端口>` 即可，src 会自动
> 处理容器内主机名，见步骤 3 表格。

### 就绪检测（比看日志更可靠）

```bash
# 端口默认 3000；换端口后改这里
PORT=3000
curl -sf "http://localhost:${PORT}" -o /dev/null && echo "langfuse ready" || echo "not ready yet"
docker compose ps 2>/dev/null || docker-compose ps
```

期望的 `compose ps` 输出（全部 `Up`，且 postgres/clickhouse/redis/minio 标为 `healthy`）：

```
NAME                         STATE               PORTS
langfuse_langfuse-web_1      Up                  0.0.0.0:3000->3000/tcp
langfuse_langfuse-worker_1   Up                  127.0.0.1:3030->3030/tcp
langfuse_clickhouse_1        Up (healthy)        127.0.0.1:8123->8123/tcp, 127.0.0.1:9000->9000/tcp
langfuse_postgres_1          Up (healthy)        127.0.0.1:5432->5432/tcp
langfuse_redis_1             Up (healthy)        127.0.0.1:6379->6379/tcp
langfuse_minio_1             Up (healthy)        0.0.0.0:9090->9000/tcp, 127.0.0.1:9091->9001/tcp
```

打开 `http://localhost:3000`（或你自定义的端口）：
注册账号 → 创建 Organization/Project → 在 **Settings → API Keys** 创建一对密钥，
拿到 **Public Key**（`pk-lf-...`）和 **Secret Key**（`sk-lf-...`），下一步填入 `.env`。

完成后 `cd <ROOT>` 回到项目根。

---

## 步骤 3：引导 .env（忽略 hermes 相关）

```bash
cp .env.example .env
```

按下表引导用户填写 `.env`（**只关注以下项，hermes / API_SERVER 项保持默认或留空，无需理会**）：

| 变量 | 必填 | 说明 |
|------|------|------|
| `MODEL_NAME` | 是 | `provider/model_id`，须与镜像内注册的 provider/model 一致，默认值可直接用 |
| `ARK_API_KEY` | 建议 | 构建镜像前填好，会写入 models fragment；留空则镜像内 apiKey 为空 |
| `LANGFUSE_PUBLIC_KEY` | 是 | 步骤 2 拿到的 `pk-lf-...` |
| `LANGFUSE_SECRET_KEY` | 是 | 步骤 2 拿到的 `sk-lf-...` |
| `LANGFUSE_BASE_URL` | 是 | 本地填 `http://localhost:3000`（容器内 src 自动改 host.docker.internal） |
| `TOS_ACCESS_KEY` / `TOS_SECRET_KEY` | 仅步骤 4 方式 A 需要 | preprocess 从 TOS 拉 benchmark_mds 用；只跑 hello.json 可暂不填 |
| `BENCHMARK_HF_REPO` | 否 | HuggingFace dataset 仓库 id；默认 `FeiZhuNiU-INFJA/EALE`（公开），自建镜像才需要覆盖 |
| `HF_TOKEN` | 仅上传到 HF 需要 | 维护者推 benchmark_mds.zip 到 HF 时的写权限 token |
| `DO_TRAJECTORY_JUDGE` / `OPENAI_*` | 否 | 轨迹评判可选，默认 false |
| `FIRECRAWL_API_KEY` | 否 | 部分 benchmark 联网搜索可选 |

> 跑 `hello.json` 冒烟只强依赖：`MODEL_NAME` + `ARK_API_KEY` + `LANGFUSE_*`。

校验：
```bash
grep -E '^(MODEL_NAME|ARK_API_KEY|LANGFUSE_PUBLIC_KEY|LANGFUSE_SECRET_KEY|LANGFUSE_BASE_URL)=' .env
```

---

## 步骤 4：preprocess 拉取 benchmark 数据（完整 benchmark 才需要）

> **仅跑 hello.json 冒烟可跳过本步**（demo suite 已随仓库提供）。
> 需要完整 benchmark 时，从 **TOS**（字节内网）或 **HuggingFace**（公开）二选一拉取。

**方式 A：从 TOS 下载**（依赖 `.env` 的 `TOS_ACCESS_KEY` / `TOS_SECRET_KEY`，bucket `aml-fde-boe`）：

```bash
python -m src.cli.preprocess                 # 从 TOS 下载 benchmark_mds.zip → 生成 assets/benchmarks/*.json
python -m src.cli.preprocess --force-download # 强制重新下载
python -m src.cli.preprocess --skip-download  # 已有本地 assets/benchmark_mds/ 时跳过下载
```

**方式 B：从 HuggingFace dataset 仓库下载**（无需 TOS 凭证；公开仓库读取无需 HF_TOKEN）：

```bash
# 默认仓库 FeiZhuNiU-INFJA/EALE，自建镜像时在 .env 设 BENCHMARK_HF_REPO=<user-or-org>/<dataset-name>
python -m src.cli.preprocess --source huggingface
# 或者在 .env 设 BENCHMARK_SOURCE=huggingface 后直接：
python -m src.cli.preprocess
```

> **维护者**：benchmark 源（markdown）有更新时，跑 `python scripts/upload_benchmark_to_hf.py`
> 把 `benchmark_mds.zip` 同步推送到 HuggingFace（需 `.env` 中 `HF_TOKEN` 写权限 token 与 `BENCHMARK_HF_REPO`；仓库不存在会自动创建为 public）。

---

## 步骤 5：构建 openclaw-with-evolve 镜像

`self-evolving-plugin-pro`、`langfuse-tracer` 已内置，宿主机无需手动装插件。

```bash
bash agent-runtimes/openclaw/build-image.sh
# 默认产出：evolve-eval-openclaw-with-evolve:latest（带进化插件）
# 国内拉取慢可换基础镜像源：
#   OPENCLAW_BASE_IMAGE=ghcr.milu.moe/openclaw/openclaw:latest bash agent-runtimes/openclaw/build-image.sh
# 只要 base 镜像（不带进化插件）：
#   INSTALL_SELF_EVOLVING=false bash agent-runtimes/openclaw/build-image.sh
```

> **内网/拉取慢**（构建阶段卡在 `Get:1 http://deb.debian.org/...` 或后续 pip / uv 安装）：
> 同时设置 apt 与 pip 镜像源（详见 [agent-runtimes/openclaw/README.md](../../agent-runtimes/openclaw/README.md)）：
>
> ```bash
> # 字节内网
> APT_MIRROR=http://mirrors.byted.org \
> PIP_INDEX_URL=https://bytedpypi.byted.org/simple/ \
>   bash agent-runtimes/openclaw/build-image.sh
> ```
>
> - `APT_MIRROR` 仅构建期生效（替换基础镜像内 `deb.debian.org`），需指向 PEP 一致布局的 Debian 源（`<APT_MIRROR>/debian` + `<APT_MIRROR>/debian-security`）。
> - `PIP_INDEX_URL` 同时影响 `uv` / `pip`（self-evolving-plugin-pro 装 Python runtime 用）。
> - 其它内网源把上面两个变量替换成自己的镜像即可。

验证：
```bash
docker images | grep evolve-eval-openclaw
docker run --rm evolve-eval-openclaw-with-evolve:latest openclaw plugins list
```

> 构建前确保 `.env` 已填 `ARK_API_KEY`，否则镜像内模型 apiKey 为空。

---

## 步骤 6：跑 hello.json 冒烟（TUI + Dashboard 可视化）

`hello.json` 在 `assets/benchmarks_demo/`，与完整 benchmark 目录分离。
同时开启终端 TUI 面板（`--tui`）和浏览器 HTTP dashboard（`--dashboard`）：

```bash
python -m src.cli.lift_main \
  -r openclaw \
  --benchmark_dir assets/benchmarks_demo \
  --suite hello.json \
  --run_id hello-smoke \
  --tui \
  --dashboard 8080
```

- **TUI**：终端实时刷新 warmup / holdout / phase 状态面板（依赖 `rich`）。
- **Dashboard**：浏览器打开 `http://localhost:8080`（`--dashboard` 也可写 `HOST:PORT`）。
- 运行结束后默认自动后处理，产物在 `results/lift-runid-hello-smoke/`：
  `report.json`、`outcome/`、`*_backfilled.json`（Langfuse trace 回填）、对比 CSV / HTML。

仅重跑后处理：
```bash
python -m src.cli.lift_main -r openclaw --evaluate-only --run_id hello-smoke
```

---

## 验收清单

- [ ] `docker info` 正常；macOS 用 Colima 时 `colima status` 为 Running
- [ ] `conda activate evolve_eval` 后依赖导入无误
- [ ] `http://localhost:3000` 可登录 Langfuse，`.env` 已填 `LANGFUSE_*`
- [ ] `docker images` 含 `evolve-eval-openclaw-with-evolve:latest`
- [ ] hello.json 冒烟结束，TUI 正常刷新、`http://localhost:8080` 可见 dashboard
- [ ] `results/lift-runid-hello-smoke/report.json` 生成

## 常见问题

- **report.json 里 `langfuse` 为 null**：执行期只写结论，trace 在后处理阶段填入 `*_backfilled.json`；确认 Langfuse 已启动且 `.env` 的 `LANGFUSE_*` 正确。
- **build 报 permission denied**：当前用户不在 docker 组（Linux）或 Docker daemon 未启动（macOS 未 `colima start` / 未开 Docker Desktop）。
- **拉取基础镜像慢/失败**：设 `OPENCLAW_BASE_IMAGE` 切换加速源后重试。
- **想清理残留容器/镜像再重来**：使用同仓库的 `cleanup-lift-env` skill。
