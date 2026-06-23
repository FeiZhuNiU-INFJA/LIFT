#!/usr/bin/env bash
# 清理 agent_evolve_evaluation 评测环境产生的孤儿容器、delta 镜像、dangling 镜像。
# 详见 SKILL.md。
set -euo pipefail

CLEAN_RESULTS=0
CLEAN_LOGS=0
DRY_RUN=0

usage() {
    cat <<'EOF'
用法: cleanup.sh [--dry-run] [--results] [--logs] [--all]

  --dry-run   只打印将要执行的操作，不真正动手
  --results   同时清 results/ 目录（默认不清）
  --logs      同时清 logs/ 目录（默认不清）
  --all       等价于 --results --logs
  -h, --help  显示此帮助
EOF
}

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --results) CLEAN_RESULTS=1 ;;
        --logs)    CLEAN_LOGS=1 ;;
        --all)     CLEAN_RESULTS=1; CLEAN_LOGS=1 ;;
        -h|--help) usage; exit 0 ;;
        *)
            echo "未知参数: $arg" >&2
            usage >&2
            exit 2
            ;;
    esac
done

# 项目根目录：脚本所在目录的上两级（skill/cleanup-eval-env/cleanup.sh -> 项目根）
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/../.." &> /dev/null && pwd)"

run() {
    if [[ $DRY_RUN -eq 1 ]]; then
        echo "[dry-run] $*"
    else
        echo "+ $*"
        eval "$@"
    fi
}

section() {
    echo
    echo "==== $* ===="
}

# 0. 评测主进程（python -m src.cli.lift_main ...）
# 必须先杀进程再清容器/镜像，否则 dashboard 上会看到大片 ✗ 错误：
# 主进程还活着但它正在用的容器/镜像被你删了。
section "0. 清理 lift_main 主进程"
mapfile -t lift_pids < <(pgrep -f 'python.* -m src\.cli\.lift_main' || true)
if [[ ${#lift_pids[@]} -eq 0 ]]; then
    echo "(无 lift_main 进程)"
else
    printf '将停止 %d 个 lift_main 进程:\n' "${#lift_pids[@]}"
    for pid in "${lift_pids[@]}"; do
        cmdline="$(tr '\0' ' ' < /proc/$pid/cmdline 2>/dev/null | head -c 160 || true)"
        printf '  - PID=%s  %s\n' "$pid" "$cmdline"
    done
    if [[ $DRY_RUN -eq 1 ]]; then
        echo "[dry-run] kill -TERM ${lift_pids[*]}"
    else
        kill -TERM "${lift_pids[@]}" 2>/dev/null || true
        # 给 graceful shutdown 一点时间
        for _ in 1 2 3 4 5; do
            sleep 1
            mapfile -t still_alive < <(pgrep -f 'python.* -m src\.cli\.lift_main' || true)
            [[ ${#still_alive[@]} -eq 0 ]] && break
        done
        # 还活着 → SIGKILL 兜底
        if [[ ${#still_alive[@]} -gt 0 ]]; then
            echo "  graceful shutdown 超时, SIGKILL: ${still_alive[*]}"
            kill -KILL "${still_alive[@]}" 2>/dev/null || true
        fi
    fi
fi

# 1. 评测容器（按 SKILL "lift-integrate-agent-runtime" §2.2 约定，
# 所有 runtime 的 _CONTAINER_PREFIX 都形如 "evolve-<runtime>"，
# 例如 evolve-openclaw / evolve-genericagent。统一用 "evolve-" 前缀过滤，
# 未来新加 runtime 不必再改 cleanup 脚本。）
section "1. 清理 evolve-* 评测容器"
mapfile -t containers < <(docker ps -a --filter "name=evolve-" --format "{{.Names}}" || true)
if [[ ${#containers[@]} -eq 0 ]]; then
    echo "(无残留容器)"
else
    printf '将删除 %d 个容器:\n' "${#containers[@]}"
    printf '  - %s\n' "${containers[@]}"
    run "docker rm -f ${containers[*]} >/dev/null"
fi

# 2. delta 镜像（warmup commit 产物，命名 evolve-eval-delta:*）
section "2. 清理 evolve-eval-delta:* 镜像"
mapfile -t delta_imgs < <(docker images --format "{{.Repository}}:{{.Tag}}" \
    | awk '/^evolve-eval-delta:/' || true)
if [[ ${#delta_imgs[@]} -eq 0 ]]; then
    echo "(无 delta 镜像)"
else
    printf '将删除 %d 个 delta 镜像:\n' "${#delta_imgs[@]}"
    printf '  - %s\n' "${delta_imgs[@]}"
    run "docker rmi -f ${delta_imgs[*]} >/dev/null"
fi

# 3. dangling <none> 镜像
section "3. 清理 dangling 镜像 (<none>:<none>)"
mapfile -t dangling < <(docker images -f "dangling=true" -q || true)
if [[ ${#dangling[@]} -eq 0 ]]; then
    echo "(无 dangling 镜像)"
else
    printf '将删除 %d 个 dangling 镜像层\n' "${#dangling[@]}"
    run "docker rmi -f ${dangling[*]} >/dev/null"
fi

# 4. results/ 目录（可选）
if [[ $CLEAN_RESULTS -eq 1 ]]; then
    section "4. 清理 results/ 子目录"
    if [[ -d "$PROJECT_ROOT/results" ]]; then
        mapfile -t result_dirs < <(find "$PROJECT_ROOT/results" -mindepth 1 -maxdepth 1 -type d || true)
        if [[ ${#result_dirs[@]} -eq 0 ]]; then
            echo "(results/ 已空)"
        else
            printf '将删除 %d 个 run 目录:\n' "${#result_dirs[@]}"
            printf '  - %s\n' "${result_dirs[@]}"
            run "rm -rf ${result_dirs[*]}"
        fi
    else
        echo "(results/ 不存在)"
    fi
fi

# 5. logs/ 目录（可选）
if [[ $CLEAN_LOGS -eq 1 ]]; then
    section "5. 清理 logs/ 文件"
    if [[ -d "$PROJECT_ROOT/logs" ]]; then
        mapfile -t log_files < <(find "$PROJECT_ROOT/logs" -mindepth 1 -maxdepth 1 || true)
        if [[ ${#log_files[@]} -eq 0 ]]; then
            echo "(logs/ 已空)"
        else
            printf '将删除 %d 个日志文件/目录:\n' "${#log_files[@]}"
            printf '  - %s\n' "${log_files[@]}"
            run "rm -rf ${log_files[*]}"
        fi
    else
        echo "(logs/ 不存在)"
    fi
fi

# 6. 根目录 evolve_eval.log（每次运行前都清，避免新旧日志混淆）
section "6. 清理根目录 evolve_eval.log"
if [[ -f "$PROJECT_ROOT/evolve_eval.log" ]]; then
    echo "将删除: $PROJECT_ROOT/evolve_eval.log ($(du -h "$PROJECT_ROOT/evolve_eval.log" | cut -f1))"
    run "rm -f \"$PROJECT_ROOT/evolve_eval.log\""
else
    echo "(evolve_eval.log 不存在)"
fi

section "完成"
echo "当前 evolve-* 评测容器:"
docker ps -a --filter "name=evolve-" --format "  {{.Names}}\t{{.Status}}" || true
echo
echo "当前镜像:"
docker images --format "  {{.Repository}}:{{.Tag}}\t{{.Size}}" \
    | grep -E "^evolve-eval" || echo "  (无相关镜像)"
