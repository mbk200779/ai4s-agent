#!/usr/bin/env bash
# 采样 worker 容器的内存占用，用于长文档实测的"峰值内存"。
#
# 为什么要单独采而不是在进程内打点：
# 进程内打点只能看到当前子进程，看不到"父进程 + 子进程 + 模型常驻"的整体水位；
# 而 OOM Killer 看的是 cgroup，不是哪个 Python 对象。所以从容器外面采更接近真相。
#
# 用法：
#   ./scripts/bench_rss_sampler.sh [容器名] [采样间隔秒] [输出文件]
set -u

CONTAINER="${1:-ai4s-worker}"
INTERVAL="${2:-2}"
OUT="${3:-/tmp/worker_rss.log}"

: > "$OUT"
echo "# ts unix_mem_usage container=${CONTAINER} interval=${INTERVAL}s" >> "$OUT"

while true; do
  ts=$(date +%s)
  mem=$(docker stats --no-stream --format '{{.MemUsage}}' "$CONTAINER" 2>/dev/null | awk '{print $1}')
  [ -n "$mem" ] && echo "$ts $mem" >> "$OUT"
  sleep "$INTERVAL"
done
