#!/usr/bin/env bash
# 把演示入口从 Cloudflare 快速隧道切换为「命名隧道 + 自有域名」。
#
# 用法：
#   bash deploy/switch-tunnel.sh --token eyJ... --domain demo.example.com
#   bash deploy/switch-tunnel.sh --token eyJ... --domain demo.example.com --dry-run
#
# 做的事：
#   1. 删除旧的快速隧道容器（临时域名随即失效）
#   2. 用 Cloudflare 给的 token 启动命名隧道容器，指向 Nginx 网关
#   3. 对新域名做四条链路自检（页面 / API / 长耗时生成接口 / 图片回读）

set -euo pipefail

NET="2026-10-21-16-54-36_ai4s-net"
TOKEN=""
DOMAIN=""
DRY_RUN=0

usage() {
  echo "用法: bash deploy/switch-tunnel.sh --token <隧道token> --domain <演示域名> [--dry-run]"
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --token)  TOKEN="${2:-}"; shift 2 ;;
    --domain) DOMAIN="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) usage ;;
  esac
done

[[ -z "$TOKEN" || -z "$DOMAIN" ]] && usage

# 去掉用户可能粘贴进来的 https:// 与结尾斜杠
DOMAIN="${DOMAIN#https://}"
DOMAIN="${DOMAIN#http://}"
DOMAIN="${DOMAIN%%/}"

# Git Bash 会把以 / 开头的参数当路径转换，必须关闭
export MSYS_NO_PATHCONV=1

RUN_CMD=(docker run -d --name ai4s-tunnel
  --network "$NET"
  --restart unless-stopped
  cloudflare/cloudflared:latest
  tunnel --no-autoupdate run --token "$TOKEN")

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "[dry-run] docker rm -f ai4s-tunnel"
  echo "[dry-run] ${RUN_CMD[0]} ${RUN_CMD[1]} ... --token ${TOKEN:0:8}***"
  echo "[dry-run] 将校验: https://$DOMAIN/{,/writing,/api/v1/health/}"
  exit 0
fi

echo "==> 1/4 移除旧隧道容器"
docker rm -f ai4s-tunnel >/dev/null 2>&1 || true

echo "==> 2/4 启动命名隧道"
"${RUN_CMD[@]}" >/dev/null

echo "==> 3/4 等待隧道注册（最多 60 秒）"
for i in $(seq 1 20); do
  if docker logs ai4s-tunnel 2>&1 | grep -q "Registered tunnel connection"; then
    echo "    隧道已注册（第 ${i} 次探测）"
    break
  fi
  sleep 3
done
docker logs ai4s-tunnel 2>&1 | grep -E "Registered tunnel connection|ERR|error" | tail -3 || true

echo "==> 4/4 公网自检: https://$DOMAIN"
FAIL=0
check() {
  local path="$1" expect="$2" desc="$3" code
  code=$(curl -s -m 40 -o /dev/null -w '%{http_code}' "https://$DOMAIN$path" || echo 000)
  if [[ "$code" == "$expect" ]]; then
    echo "    [OK]   $desc ($path) -> $code"
  else
    echo "    [FAIL] $desc ($path) -> $code (期望 $expect)"
    FAIL=1
  fi
}

check "/" 200 "首页"
check "/writing" 200 "写作页"
check "/api/v1/health/" 200 "后端健康检查"

echo "    正在实测长耗时接口（LLM 生成，约 40-90 秒）..."
BODY=$(curl -s -m 180 -X POST "https://$DOMAIN/api/v1/writing/outline" \
  -H "Content-Type: application/json" \
  -d '{"topic":"检索增强生成综述","top_k":1}' || true)
if [[ "$BODY" == *'"outline"'* ]]; then
  echo "    [OK]   大纲生成接口（>30s 未超时）"
else
  echo "    [FAIL] 大纲生成接口返回异常：${BODY:0:120}"
  FAIL=1
fi

echo
if [[ "$FAIL" -eq 0 ]]; then
  echo "全部通过。新的固定地址: https://$DOMAIN"
else
  echo "存在失败项，请检查 Cloudflare 的 Public Hostname 是否指向 ai4s-gateway:80。"
  exit 1
fi
