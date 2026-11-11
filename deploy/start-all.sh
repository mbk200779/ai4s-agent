#!/usr/bin/env bash
# 开机后一键恢复 https://demo.mabokai.site
#
# 用法：
#   bash deploy/start-all.sh              # 默认：不重建镜像，最快把服务拉起来（推荐日常用）
#   bash deploy/start-all.sh --rebuild    # 改过代码/需要重建镜像时用（串行构建，输出实时可见）
#   bash deploy/start-all.sh --status     # 只体检，不做任何变更
#
# 设计说明（都是踩过的坑）：
#   1. 默认不构建。`docker compose build api worker` 会并行构建两个服务，
#      它们共用同一个 Dockerfile 但互不共享缓存，依赖会被装两遍，
#      无缓存时要十几分钟；日常恢复完全不需要。
#   2. 构建输出不再重定向到文件。之前重定向导致终端看起来像卡死，
#      实际在跑，人只能干等。
#   3. compose up 失败时自动收集失败容器的健康检查输出与日志，
#      常见原因是依赖容器 unhealthy（例如 Next.js 只绑容器 IP 不绑 0.0.0.0）。

set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
export MSYS_NO_PATHCONV=1

DOMAIN="demo.mabokai.site"
PROBE=$(mktemp)  # Git Bash 下 curl -o /dev/null 会报 write error，用真实文件
MODE="fast"
for a in "$@"; do
  case "$a" in
    --rebuild)  MODE="rebuild" ;;
    --no-build) MODE="fast" ;;
    --status)   MODE="status" ;;
    -h|--help)  sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数: $a"; exit 1 ;;
  esac
done

ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }

# ---------- 0. Docker 引擎 ----------
echo "==> 检查 Docker 引擎"
if ! docker info >/dev/null 2>&1; then
  bad "Docker 引擎没有运行"
  cat <<'EOF'

  请先做这一步，然后再跑本脚本：

    1) 从开始菜单打开「Docker Desktop」
    2) 等右下角鲸鱼图标停止转动、显示 "Engine running"（通常 30~60 秒）
    3) 重新执行：bash deploy/start-all.sh
EOF
  exit 1
fi
ok "引擎正常"

if [[ "$MODE" == "status" ]]; then
  # 体检逻辑统一收敛到 demo-check.sh：两套写法并存必然出现
  # "start-all 说通、demo-check 说不通"这种自相矛盾的结论。
  exec bash "$(dirname "$0")/demo-check.sh" --quiet
fi

# ---------- 1. 预检 .env ----------
if ! grep -q '^CLOUDFLARE_TUNNEL_TOKEN=.\+' .env 2>/dev/null; then
  bad ".env 里缺少 CLOUDFLARE_TUNNEL_TOKEN"
  echo "     去 Cloudflare 面板 Tunnels → ai4s → 安装连接器 → Docker 复制 token，"
  echo "     然后执行：bash deploy/apply-tunnel.sh --cmd \"docker run ... --token eyJ...\""
  exit 1
fi
ok "隧道 token 已配置"

# ---------- 2. 镜像（默认跳过） ----------
if [[ "$MODE" == "rebuild" ]]; then
  echo "==> 重建镜像（串行，日志实时输出）"
  echo "    api 构建中……可能 3~10 分钟，取决于缓存与网络"
  docker compose build api || { bad "api 构建失败"; exit 1; }
  ok "api 镜像就绪"
  echo "    worker 构建中（应命中 api 的缓存，通常很快）"
  docker compose build worker || { bad "worker 构建失败"; exit 1; }
  ok "worker 镜像就绪"
else
  warn "跳过镜像构建（需要重建时用 --rebuild）"
fi

# ---------- 3. 清理 compose 之外的遗留容器 ----------
echo "==> 清理手工创建的遗留容器"
for c in ai4s-gateway ai4s-tunnel; do
  if docker ps -a --format '{{.Names}}' | grep -qx "$c"; then
    proj=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$c" 2>/dev/null || echo "")
    if [[ -z "$proj" || "$proj" == "<no value>" ]]; then
      docker rm -f "$c" >/dev/null 2>&1 && ok "$c（手工容器，已交由 compose 接管）"
    else
      ok "$c 已由 compose 管理"
    fi
  fi
done
for c in ai4s-qdrant ai4s-minio ai4s-redis ai4s-api ai4s-worker ai4s-web; do
  docker ps -a --format '{{.Names}}' | grep -qx "$c" && docker update --restart unless-stopped "$c" >/dev/null 2>&1
done

# ---------- 4. 起服务 ----------
echo "==> 启动全部服务（含隧道）"
if docker compose --profile tunnel up -d; then
  ok "已下发启动指令"
else
  bad "compose up 失败，开始逐项诊断"
  echo
  for c in $(docker ps -a --filter name=ai4s --format '{{.Names}}'); do
    st=$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null)
    hl=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}-{{end}}' "$c" 2>/dev/null)
    [[ "$hl" == "unhealthy" || "$st" == "exited" ]] || continue
    echo "--- $c  status=$st health=$hl"
    [[ "$hl" == "unhealthy" ]] && docker inspect -f '{{range .State.Health.Log}}{{.Output}}{{end}}' "$c" 2>/dev/null | tail -3
    docker logs --tail 12 "$c" 2>&1 | sed 's/^/    /'
    echo
  done
  cat <<'EOF'
提示：
  · 某容器 unhealthy 但端口其实可用（如 Next.js 只绑容器 IP 不绑 0.0.0.0），
    症状是 localhost 探不通、容器间访问正常，需在 environment 里加 HOSTNAME=0.0.0.0
  · 修好后可只重建该服务：docker compose up -d --no-deps --force-recreate <服务名>
EOF
  exit 1
fi

# ---------- 5. 等就绪 ----------
echo "==> 等待隧道注册（最多 90 秒）"
REG=0
for i in $(seq 1 30); do
  sleep 3
  if docker logs --tail 30 ai4s-tunnel 2>&1 | grep -q 'Registered tunnel connection'; then
    REG=1; ok "隧道已注册（第 $((i*3)) 秒）"; break
  fi
  if docker logs --tail 30 ai4s-tunnel 2>&1 | grep -q 'Invalid tunnel secret'; then
    bad "token 已失效（Invalid tunnel secret）"
    echo "     面板「刷新令牌」后，用 deploy/apply-tunnel.sh 换 token，并同步修改 .env"
    break
  fi
done
[[ "$REG" -eq 0 ]] && warn "90 秒内未见注册，稍后用 --status 复查"

echo "==> 等待后端健康"
for i in $(seq 1 20); do
  [[ "$(docker inspect -f '{{.State.Health.Status}}' ai4s-api 2>/dev/null)" == "healthy" ]] && { ok "ai4s-api healthy"; break; }
  sleep 3
done

# ---------- 6. 验收 ----------
echo
echo "==> 本地网关 :8080"
for p in / /writing /api/v1/health/; do
  code=$(curl -sS -m 25 -o "$PROBE" -w '%{http_code}' "http://localhost:8080$p" 2>/dev/null || echo 000)
  [[ "$code" == "200" ]] && ok "$p -> 200" || bad "$p -> $code"
done

echo
echo "==> 公网 https://$DOMAIN"
PUB_OK=0
for p in / /writing; do
  code=000
  for t in 1 2 3; do
    code=$(curl -sS --noproxy '*' -m 30 -o "$PROBE" -w '%{http_code}' "https://$DOMAIN$p" 2>/dev/null || echo 000)
    [[ "$code" == "200" ]] && break
    sleep 2
  done
  [[ "$code" == "200" ]] && { ok "$p -> 200"; PUB_OK=1; } || bad "$p -> $code"
done
body=$(curl -sS --noproxy '*' -m 25 "https://$DOMAIN/api/v1/health/" 2>/dev/null || echo "")
[[ "$body" == *'"status":"ok"'* ]] && { ok "健康检查: $body"; PUB_OK=1; } || bad "健康检查异常: ${body:0:60}"

echo
echo "----------------------------------------------------------------"
if [[ "$REG" -eq 1 && "$PUB_OK" -eq 1 ]]; then
  echo "演示地址已可用：https://$DOMAIN"
  echo "本机等价入口：  http://localhost:8080"
else
  echo "服务已启动，但对外链路未完全确认，跑一次：bash deploy/start-all.sh --status"
fi
echo "----------------------------------------------------------------"
