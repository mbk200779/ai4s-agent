#!/usr/bin/env bash
# Dify 容器开关：与 AI4S 共用同一个 Docker 引擎和内存上限，演示期可临时让路。
#
# 用法：
#   bash deploy/dify-toggle.sh status   # 只看状态，不做变更
#   bash deploy/dify-toggle.sh stop     # 停掉 + 改为不自启（重启电脑也不会回来）
#   bash deploy/dify-toggle.sh start    # 恢复原重启策略并拉起
#
# 为什么 stop 还要改重启策略：Dify 的 11 个容器是 restart: always，
# 官方语义是「即使手动 stop，daemon 启动时仍会拉起」。只 docker stop，
# 电脑重启一次它们就全回来了，等于没停。备份见 deploy/dify-restart-policy.bak。
#
# 自定义路径：DIFY_DIR=/path/to/dify/docker bash deploy/dify-toggle.sh stop

set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
export MSYS_NO_PATHCONV=1

DIFY_DIR="${DIFY_DIR:-D:/docker desktop/dify/dify/docker}"
PROJ="${DIFY_PROJ:-docker}"
BAK="deploy/dify-restart-policy.bak"
ACTION="${1:-status}"

if [[ ! -d "$DIFY_DIR" ]]; then
  echo "找不到 Dify 编排目录：$DIFY_DIR"
  echo "用 DIFY_DIR=/实际路径 bash $0 $ACTION"
  exit 1
fi

names() { docker ps -a --filter "name=^${PROJ}-" --format '{{.Names}}'; }

case "$ACTION" in
  status)
    echo "==> 容器"
    docker ps -a --filter "name=^${PROJ}-" --format 'table {{.Names}}\t{{.Status}}' 2>/dev/null || echo "（无）"
    echo
    echo "==> 重启策略"
    for c in $(names); do printf '  %-28s %s\n' "$c" "$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$c")"; done
    echo
    echo "==> AI4S 未受影响"
    docker ps --filter 'name=^ai4s-' --format '  {{.Names}}\t{{.Status}}'
    ;;

  stop)
    # 第一次 stop 时把原始策略存下来，之后重复执行不会用 "no" 覆盖掉备份
    if [[ ! -f "$BAK" ]]; then
      : > "$BAK"
      for c in $(names); do printf '%s\t%s\n' "$c" "$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$c")" >> "$BAK"; done
      echo "==> 已备份原始重启策略 -> $BAK"
    fi
    echo "==> 停止 Dify"
    ( cd "$DIFY_DIR" && docker compose -p "$PROJ" stop 2>&1 | tail -5 )
    echo "==> 改为不自启"
    for c in $(names); do docker update --restart no "$c" >/dev/null 2>&1 && echo "  $c -> no"; done
    ;;

  start)
    echo "==> 恢复重启策略"
    if [[ -f "$BAK" ]]; then
      while IFS=$'\t' read -r c pol; do
        [[ -z "${c:-}" ]] && continue
        docker update --restart "${pol:-always}" "$c" >/dev/null 2>&1 && echo "  $c -> ${pol:-always}"
      done < "$BAK"
    else
      echo "  没有备份文件，统一按 always 处理"
      for c in $(names); do docker update --restart always "$c" >/dev/null 2>&1; done
    fi
    echo "==> 拉起 Dify"
    ( cd "$DIFY_DIR" && docker compose -p "$PROJ" up -d 2>&1 | tail -8 )
    echo
    echo "就绪后访问 http://localhost（Dify 自带 nginx 占 80 端口）"
    ;;

  *)
    echo "未知动作：$ACTION（可选：status / stop / start）"
    exit 1
    ;;
esac
