#!/usr/bin/env bash
# 用 Cloudflare 控制台给出的隧道 token 启动 ai4s-tunnel，并自检是否真正注册成功。
#
# 用法（二选一）：
#   bash deploy/apply-tunnel.sh --cmd "docker run cloudflare/cloudflared:latest tunnel --no-autoupdate run --token eyJ..."
#   bash deploy/apply-tunnel.sh --token eyJ...
#   可选 --network <dokcer网络名>  默认 2026-10-21-16-54-36_ai4s-net
#
# 与 switch-tunnel.sh 的区别：本脚本不做公网自检，只负责「让隧道活过来」，
# 并会在 token 无效时直接指出是哪一环不匹配（账号 / 隧道 / 密钥）。

set -uo pipefail

NET="2026-10-21-16-54-36_ai4s-net"
TOKEN=""

usage() {
  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --token)   TOKEN="${2:-}"; shift 2 ;;
    --cmd)     TOKEN="${2:-}"; shift 2 ;;
    --network) NET="${2:-}"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "未知参数: $1"; usage ;;
  esac
done

[[ -z "$TOKEN" ]] && usage

# 允许直接粘贴整条 docker run 命令：从中抽取 --token 后面的值
if [[ "$TOKEN" == *"--token"* ]]; then
  TOKEN="$(printf '%s' "$TOKEN" | sed -n 's/.*--token[= ]\{1,\}\([^ ]*\).*/\1/p')"
fi
TOKEN="$(printf '%s' "$TOKEN" | tr -d '\r\n\t "'"'"'')"

[[ -z "$TOKEN" ]] && { echo "没有从输入里解析出 token"; exit 1; }

export MSYS_NO_PATHCONV=1

# ---- 1. 解码 token，确认形状 ------------------------------------------------
decode() {
  local s="$1"
  s="${s//-/+}"; s="${s//_//}"
  case $(( ${#s} % 4 )) in
    2) s+="==" ;;
    3) s+="=" ;;
  esac
  printf '%s' "$s" | base64 -d 2>/dev/null
}

PAYLOAD="$(decode "$TOKEN")"
if [[ "$PAYLOAD" != *'"t"'* ]]; then
  echo "!! 这个字符串不像隧道 token（应为 eyJ 开头、base64 可解出 {\"a\":..,\"t\":..,\"s\":..}）"
  echo "   长度 ${#TOKEN}，前 12 位: ${TOKEN:0:12}"
  exit 1
fi

ACC="$(printf '%s' "$PAYLOAD" | sed -n 's/.*"a":"\([^"]*\)".*/\1/p')"
TID="$(printf '%s' "$PAYLOAD" | sed -n 's/.*"t":"\([^"]*\)".*/\1/p')"
SEC="$(printf '%s' "$PAYLOAD" | sed -n 's/.*"s":"\([^"]*\)".*/\1/p')"

echo "==> token 解析"
echo "    账号 Account ID : $ACC"
echo "    隧道 Tunnel ID  : $TID"
echo "    密钥长度        : ${#SEC} 字符 (base64 原值)"
if [[ ${#SEC} -lt 40 ]]; then
  echo "    !! 密钥偏短，token 可能被截断（复制时漏了尾巴）"
fi

# ---- 2. 换容器 --------------------------------------------------------------
echo "==> 移除旧容器并启动"
docker rm -f ai4s-tunnel >/dev/null 2>&1 || true
docker run -d --name ai4s-tunnel \
  --network "$NET" \
  --restart unless-stopped \
  cloudflare/cloudflared:latest \
  tunnel --no-autoupdate run --token "$TOKEN" >/dev/null || {
    echo "!! docker run 失败"; exit 1; }

# ---- 3. 等注册 --------------------------------------------------------------
echo "==> 等待隧道注册（最多 45 秒）"
OK=0
for i in $(seq 1 15); do
  sleep 3
  if docker logs ai4s-tunnel 2>&1 | grep -q "Registered tunnel connection"; then
    OK=1; echo "    [OK] 第 $((i*3)) 秒注册成功"; break
  fi
  if docker logs ai4s-tunnel 2>&1 | grep -q "Invalid tunnel secret"; then
    echo "    [FAIL] Invalid tunnel secret —— Cloudflare 侧不认这个密钥"
    break
  fi
done

if [[ "$OK" -eq 0 ]]; then
  echo "==> 最近日志"
  docker logs --tail 6 ai4s-tunnel 2>&1 | sed 's/^/    /'
  echo
  echo "结论：token 与账号 $ACC 下的隧道 $TID 不匹配（多半是旧 token / 隧道已重建）。"
  echo "处理：控制台 Tunnels 列表点开 ai4s，在详情页刷新令牌后复制新的安装命令，再跑本脚本。"
  exit 1
fi

echo "==> 连接详情"
docker logs --tail 4 ai4s-tunnel 2>&1 | grep -E "Registered|connIndex" | sed 's/^/    /'
echo
echo "隧道已就绪。剩下只差 Public Hostname：demo.mabokai.site -> http://ai4s-gateway:80"
