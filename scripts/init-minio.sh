#!/bin/sh
# MinIO 启动并初始化 bucket
# 关键设计决策：把 bucket 创建逻辑内嵌到 minio 服务自身的入口脚本，
# 避免额外的一次性 init 服务，确保 docker compose 正好拉起 6 个服务。

set -e

# 后台启动 MinIO 服务
minio server /data --console-address ":9001" &
MINIO_PID=$!

# 等待 MinIO 就绪
until mc alias set local http://localhost:9000 "${MINIO_ROOT_USER}" "${MINIO_ROOT_PASSWORD}" >/dev/null 2>&1; do
  echo "Waiting for MinIO to be ready..."
  sleep 1
done

# 创建 bucket（忽略已存在），figures 桶允许公开下载
mc mb local/papers --ignore-existing
mc mb local/figures --ignore-existing
mc mb local/provenance --ignore-existing
mc anonymous set download local/figures

echo "MinIO buckets initialized."

# 将 MinIO 切回前台，保持容器生命周期
wait "$MINIO_PID"
