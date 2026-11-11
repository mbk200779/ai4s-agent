#!/usr/bin/env bash
# 导出本机演示栈的数据，供迁移到云服务器时导入。
#
# 用法：
#   bash deploy/migrate-export.sh                        # 导出 qdrant + minio + .env
#   bash deploy/migrate-export.sh --with-model-cache     # 额外导出 2.1G 的 embedding 模型缓存
#   bash deploy/migrate-export.sh --out /d/backup/ai4s   # 指定输出目录
#
# 产物（默认 ./migration-YYYYMMDD-HHMM/）：
#   qdrant.tgz           Qdrant 全部集合与向量
#   minio.tgz            MinIO 全部桶与对象
#   env.production       .env 副本（含密钥，权限 600，禁止提交）
#   MANIFEST.txt         迁移前的数据基线，导入后用于比对
#   model_cache.tgz      仅 --with-model-cache 时生成
set -euo pipefail

WITH_MODEL_CACHE=0
OUT=""

usage() {
  sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-model-cache) WITH_MODEL_CACHE=1; shift ;;
    --out) OUT="${2:?--out 需要目录参数}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1"; usage; exit 2 ;;
  esac
done

cd "$(dirname "$0")/.."
STAMP=$(date +%Y%m%d-%H%M)
OUT="${OUT:-migration-$STAMP}"
mkdir -p "$OUT"
OUT_ABS="$(cd "$OUT" && pwd)"

log() { printf '\n==> %s\n' "$*"; }
ok()  { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn(){ printf '  \033[33m!\033[0m %s\n' "$*"; }
die() { printf '  \033[31m✗\033[0m %s\n' "$*"; exit 1; }

log "导出目录：$OUT_ABS"

# ---------- 1. 前置检查 ----------
log "检查 Docker 与容器"
docker info >/dev/null 2>&1 || die "Docker 未运行，先启动 Docker Desktop"
for c in ai4s-qdrant ai4s-minio; do
  [[ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null || echo false)" == "true" ]] \
    || die "容器 $c 未运行，先执行 bash deploy/start-all.sh"
  ok "$c 运行中"
done

# 卷名前缀随 compose 项目名变化，动态探测而不是硬编码
QVOL=$(docker volume ls -q --filter label=com.docker.compose.volume=qdrant_storage | head -1)
MVOL=$(docker volume ls -q --filter label=com.docker.compose.volume=minio_data   | head -1)
CVOL=$(docker volume ls -q --filter label=com.docker.compose.volume=model_cache  | head -1)
[[ -n "$QVOL" ]] || die "找不到 qdrant_storage 卷"
[[ -n "$MVOL" ]] || die "找不到 minio_data 卷"
ok "qdrant_storage=$QVOL"
ok "minio_data=$MVOL"

# ---------- 2. 打包数据卷 ----------
# 用 stdout 管道取数据，不做宿主机目录绑定：
# Windows 下 Git Bash 的 /tmp 与 Docker 引擎看到的文件系统不是同一套，
# 绑定挂载会静默写到虚拟机内部，产物看起来是空的。
pack_volume() {
  local vol="$1" file="$2"
  if ! docker run --rm -v "$vol:/src:ro" alpine tar czf - -C /src . > "$OUT/$file"; then
    rm -f "$OUT/$file"
    die "打包失败：$vol"
  fi
  [[ -s "$OUT/$file" ]] || die "打包产物为空：$file"
}

log "打包 Qdrant"
pack_volume "$QVOL" qdrant.tgz
ok "qdrant.tgz  $(du -h "$OUT/qdrant.tgz" | cut -f1)"

log "打包 MinIO"
pack_volume "$MVOL" minio.tgz
ok "minio.tgz   $(du -h "$OUT/minio.tgz" | cut -f1)"

if [[ "$WITH_MODEL_CACHE" == "1" ]]; then
  log "打包模型缓存（约 2.1G，耗时较长）"
  if [[ -n "$CVOL" ]]; then
    pack_volume "$CVOL" model_cache.tgz
    ok "model_cache.tgz  $(du -h "$OUT/model_cache.tgz" | cut -f1)"
  else
    warn "未找到 model_cache 卷，跳过"
  fi
else
  warn "跳过模型缓存（迁移后首次启动会从 hf-mirror 重新下载，约 2.1G）"
fi

# ---------- 3. 记录基线 ----------
log "采集数据基线"
QJSON=$(curl -sS -m 10 http://localhost:6333/collections 2>/dev/null || echo '')
{
  echo "# 迁移基线  $(date '+%Y-%m-%d %H:%M:%S')"
  echo
  echo "## Qdrant 集合与点数"
  for c in $(printf '%s' "$QJSON" | grep -o '"name":"[^"]*"' | cut -d'"' -f4); do
    n=$(curl -sS -m 10 "http://localhost:6333/collections/$c" 2>/dev/null | grep -o '"points_count":[0-9]*' | head -1 | cut -d: -f2)
    echo "  $c = ${n:-?}"
  done
  echo
  echo "## MinIO 对象数"
  docker exec ai4s-minio sh -c '
    mc alias set l http://127.0.0.1:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null 2>&1
    for b in papers figures provenance; do
      printf "  %s = %s\n" "$b" "$(mc ls -r "l/$b" 2>/dev/null | wc -l | tr -d " ")"
    done' 2>/dev/null
  echo
  echo "## 镜像标签"
  docker images --format '  {{.Repository}}:{{.Tag}} {{.Size}}' | grep -E '2026-10' || true
} > "$OUT/MANIFEST.txt"
sed -n '1,20p' "$OUT/MANIFEST.txt"

# ---------- 4. 复制环境变量 ----------
log "复制环境变量"
if [[ -f .env ]]; then
  cp .env "$OUT/env.production"
  chmod 600 "$OUT/env.production"
  ok "env.production（含密钥，权限已设 600）"
else
  die ".env 不存在，无法导出配置"
fi

for pat in 'migration-*/' 'env.production' '.env.bak.*'; do
  grep -qxF "$pat" .gitignore 2>/dev/null || printf '%s\n' "$pat" >> .gitignore
done
ok ".gitignore 已忽略备份目录与 env 副本，避免误提交密钥"

# ---------- 5. 汇总 ----------
TOTAL=$(du -sh "$OUT" | cut -f1)
log "完成，总体积 $TOTAL"
cat <<EOF

下一步（把整个目录传到服务器）：

  scp -r "$OUT" root@<服务器IP>:/opt/ai4s/

到服务器上：

  cd /opt/ai4s/<仓库目录>
  bash deploy/migrate-import.sh --from /opt/ai4s/$(basename "$OUT")

EOF
