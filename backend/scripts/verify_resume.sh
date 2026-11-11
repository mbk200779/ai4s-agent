#!/usr/bin/env bash
# 断点续跑端到端验证：上传 → 跑到 1/3 → 重启 worker → 观察续跑 → 核对结果。
#
# 必须在宿主机执行（用到 docker restart），脚本内部再去容器里跑 Python。
# 用法：./scripts/verify_resume.sh [页数，默认 200]
set -euo pipefail

# Git Bash 会把容器内路径 /app/... 转换成 Windows 路径，必须关掉
export MSYS_NO_PATHCONV=1

PAGES="${1:-200}"
API_CONTAINER="${API_CONTAINER:-ai4s-api}"
WORKER_CONTAINER="${WORKER_CONTAINER:-ai4s-worker}"

up_out=$(docker exec "$API_CONTAINER" python /app/scripts/verify_resume.py up --pages "$PAGES")
echo "$up_out"
VERSION=$(echo "$up_out" | sed -n 's/^version_id=//p' | head -1)
if [ -z "$VERSION" ]; then
  echo "拿不到 version_id，终止"
  exit 1
fi

echo ">>> 重启 worker 制造中断（任务应被自动重新投递并续跑）"
docker restart "$WORKER_CONTAINER" >/dev/null
sleep 8

docker exec "$API_CONTAINER" python /app/scripts/verify_resume.py watch --version "$VERSION"
