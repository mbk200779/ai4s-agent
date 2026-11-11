#!/usr/bin/env bash
# 在云服务器上导入 migrate-export.sh 产出的数据。
#
# 用法（在仓库根目录执行）：
#   bash deploy/migrate-import.sh --from /opt/ai4s/migration-20261109-1240
#   bash deploy/migrate-import.sh --from <目录> --overwrite-env   # 强制用导出包的 .env 覆盖
#
# 前置条件：已装 Docker + compose 插件，已 clone 仓库；.env 可先用导出包里的 env.production 复制一份并改好口令。
set -euo pipefail

SRC=""
OVERWRITE_ENV=0

usage() { sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --from) SRC="${2:?--from 需要目录参数}"; shift 2 ;;
    --overwrite-env) OVERWRITE_ENV=1; shift ;;
    --skip-env) OVERWRITE_ENV=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1"; usage; exit 2 ;;
  esac
done

[[ -n "$SRC" ]] || { usage; exit 2; }
cd "$(dirname "$0")/.."

log() { printf '\n==> %s\n' "$*"; }
ok()  { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn(){ printf '  \033[33m!\033[0m %s\n' "$*"; }
die() { printf '  \033[31m✗\033[0m %s\n' "$*"; exit 1; }

SRC_ABS="$(cd "$SRC" && pwd)"
log "数据来源：$SRC_ABS"
[[ -f "$SRC_ABS/qdrant.tgz" ]] || die "缺少 qdrant.tgz"
[[ -f "$SRC_ABS/minio.tgz"  ]] || die "缺少 minio.tgz"

docker info >/dev/null 2>&1 || die "Docker 未运行或当前用户无权限（试试 sudo）"

# ---------- 1. 环境变量 ----------
# 默认不覆盖已有 .env：通常你会先按第 4 节把 MinIO / JUDGE 口令换掉，
# 如果这里无条件用导出包覆盖，刚改的强口令就被打回默认值了。
if [[ "$OVERWRITE_ENV" == "1" ]]; then
  log "覆盖 .env（--overwrite-env）"
  [[ -f .env ]] && cp .env ".env.bak.$(date +%s)" && warn "已备份原有 .env"
  cp "$SRC_ABS/env.production" .env
  chmod 600 .env
  ok ".env 已用导出包覆盖"
elif [[ -f .env ]]; then
  ok "保留服务器现有 .env（如需用导出包覆盖，加 --overwrite-env）"
else
  log "写入 .env"
  cp "$SRC_ABS/env.production" .env
  chmod 600 .env
  ok ".env 已从导出包复制"
fi
grep -q '^MINIO_ROOT_PASSWORD=minioadmin$' .env 2>/dev/null \
  && warn "MINIO_ROOT_PASSWORD 仍是默认值，公网部署前务必改掉"
grep -q '^JUDGE_PASSWORD=ai4s2024$' .env 2>/dev/null \
  && warn "JUDGE_PASSWORD 仍是默认值，建议改掉"

# ---------- 2. 建卷 ----------
# 卷名带 compose 项目名前缀（= 仓库目录名），先让 compose 建出来再查真实名字
log "创建数据卷"
docker compose up -d qdrant minio redis >/dev/null 2>&1 || die "基础服务启动失败，先手动执行 docker compose up -d qdrant minio redis 看报错"

QVOL=$(docker volume ls -q --filter label=com.docker.compose.volume=qdrant_storage | head -1)
MVOL=$(docker volume ls -q --filter label=com.docker.compose.volume=minio_data | head -1)
[[ -n "$QVOL" && -n "$MVOL" ]] || die "未找到数据卷，检查 compose 项目名"
ok "qdrant_storage=$QVOL"
ok "minio_data=$MVOL"

# ---------- 3. 停写 → 还原 → 重启 ----------
log "停止写入方"
docker compose stop api worker qdrant minio >/dev/null 2>&1 || true
ok "api / worker / qdrant / minio 已停"

unpack() {
  local vol="$1" file="$2"
  docker run --rm -i -v "$vol:/dst" alpine \
    sh -c 'find /dst -mindepth 1 -delete; tar xzf - -C /dst' < "$SRC_ABS/$file" \
    || die "还原失败：$file"
}
log "还原 Qdrant"; unpack "$QVOL" qdrant.tgz; ok "完成"
log "还原 MinIO";  unpack "$MVOL" minio.tgz;  ok "完成"

log "启动全部服务"
docker compose --profile tunnel up -d >/dev/null 2>&1 || die "compose up 失败，查看 docker compose logs"
ok "已下发启动指令"

# ---------- 4. 等待就绪 ----------
log "等待 qdrant 就绪（最多 120 秒）"
for i in $(seq 1 24); do
  if curl -sS -m 3 http://localhost:6333/collections >/dev/null 2>&1; then
    ok "第 $((i*5)) 秒就绪"; break
  fi
  sleep 5
  [[ $i -eq 24 ]] && die "qdrant 未就绪，docker logs ai4s-qdrant"
done

# ---------- 5. 数据比对 ----------
log "比对数据（与 MANIFEST.txt 对照）"
if [[ -f "$SRC_ABS/MANIFEST.txt" ]]; then
  printf '  迁移前：\n'; sed -n '/## Qdrant/,/## 镜像/p' "$SRC_ABS/MANIFEST.txt" | grep '=' | sed 's/^/    /'
fi
printf '  迁移后：\n'
for c in $(curl -sS -m 5 http://localhost:6333/collections | grep -o '"name":"[^"]*"' | cut -d'"' -f4); do
  n=$(curl -sS -m 5 "http://localhost:6333/collections/$c" | grep -o '"points_count":[0-9]*' | head -1 | cut -d: -f2)
  printf '    %s = %s\n' "$c" "${n:-?}"
done
docker exec ai4s-minio sh -c '
  mc alias set l http://127.0.0.1:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null 2>&1
  for b in papers figures provenance; do
    printf "    %s = %s\n" "$b" "$(mc ls -r "l/$b" 2>/dev/null | wc -l | tr -d " ")"
  done' 2>/dev/null || warn "MinIO 尚未就绪，稍后用 mc 复查"

log "完成"
cat <<'EOF'

接下来：
  1. 看容器健康   docker compose ps
  2. 本地验证     curl -sS http://localhost:8080/api/v1/health/
  3. 隧道注册     docker logs --tail 20 ai4s-tunnel | grep 'Registered tunnel connection'
  4. 公网验证     curl -sS https://demo.mabokai.site/api/v1/health/
EOF
