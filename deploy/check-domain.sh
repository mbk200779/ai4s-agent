#!/usr/bin/env bash
# 域名接入状态检测（不需要任何凭据）
#
# 用法：
#   bash deploy/check-domain.sh                 # 查一次
#   bash deploy/check-domain.sh --watch         # 每 60 秒查一次，发现生效就自动停
#   bash deploy/check-domain.sh --watch 30      # 每 30 秒查一次
#
# 判断依据：mabokai.site 的 NS 是否已从 dnspod 换成 *.ns.cloudflare.com

set -uo pipefail
export MSYS_NO_PATHCONV=1

ZONE="${ZONE:-mabokai.site}"
HOST="${HOST:-demo}"
FQDN="${HOST}.${ZONE}"
DNS_LIST=(223.5.5.5 119.29.29.29 1.1.1.1 8.8.8.8)

WATCH=0
INTERVAL=60
case "${1:-}" in
  --watch) WATCH=1; [[ -n "${2:-}" ]] && INTERVAL="$2" ;;
  -h|--help) sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
esac

ns_of() { # ns_of <resolver> -> 输出该解析器看到的 NS，空格分隔
  local out
  out=$(nslookup -type=ns "$ZONE" "$1" 2>&1 | iconv -f gbk -t utf-8 2>/dev/null)
  if echo "$out" | grep -qiE "Non-existent|NXDOMAIN"; then
    echo "NXDOMAIN"; return
  fi
  echo "$out" | grep -iE "nameserver = " | sed 's/.*nameserver = //' | tr -d '\r' | sort | tr '\n' ' '
}

# 直接向顶级域权威服务器查询，绕开所有递归缓存。
# 注册局的权威数据不缓存，所以这里返回的就是"注册商到底提交了没有"的真相。
registry_ns() {
  local tld="${ZONE#*.}"
  local servers
  servers=$(nslookup -type=ns "$tld" 223.5.5.5 2>&1 | iconv -f gbk -t utf-8 2>/dev/null \
            | grep -i "nameserver = " | sed 's/.*nameserver = //' | tr -d '\r')
  [[ -z "$servers" ]] && { echo ""; return; }
  local s ns
  for s in $servers; do
    ns=$(nslookup -type=ns "$ZONE" "$s" 2>&1 | iconv -f gbk -t utf-8 2>/dev/null \
         | grep -i "nameserver = " | sed 's/.*nameserver = //' | tr -d '\r' | sort | tr '\n' ' ')
    [[ -n "$ns" ]] && { echo "$ns"; return; }
  done
  echo ""
}

check_once() {
  local stamp cf_count total reg
  stamp=$(date '+%H:%M:%S')
  echo "──── $stamp ────"
  cf_count=0
  total=0

  reg=$(registry_ns)
  if [[ -z "$reg" ]]; then
    echo "  注册局权威: 查询失败（网络或限流）"
  elif echo "$reg" | grep -qi "ns.cloudflare.com"; then
    echo "  注册局权威: ✓ $reg"
    echo "              → 注册局已接受变更，剩余只是公共 DNS 缓存过期"
  else
    echo "  注册局权威: × $reg"
    echo "              → 注册局仍是旧 NS：腾讯云还没把变更推上去，继续等公共 DNS 没有意义"
  fi
  echo
  for d in "${DNS_LIST[@]}"; do
    local ns
    ns=$(ns_of "$d")
    total=$((total + 1))
    if echo "$ns" | grep -qi "ns.cloudflare.com"; then
      printf '  %-16s ✓ %s\n' "$d" "$ns"
      cf_count=$((cf_count + 1))
    elif [[ "$ns" == "NXDOMAIN" ]]; then
      printf '  %-16s ! NXDOMAIN（尚无记录）\n' "$d"
    elif [[ -z "$ns" ]]; then
      printf '  %-16s - 无响应（可能被屏蔽）\n' "$d"
    else
      printf '  %-16s × %s\n' "$d" "$ns"
    fi
  done

  if [[ "$cf_count" -ge 2 ]]; then
    echo
    echo "  NS 已切换到 Cloudflare（$cf_count/$total 个解析器确认）"

    local a
    a=$(nslookup "$FQDN" 223.5.5.5 2>&1 | iconv -f gbk -t utf-8 2>/dev/null \
        | grep -iE "^Address|Addresses" | grep -v "223.5.5.5" | head -2 | sed 's/.*: *//' | tr '\n' ' ')
    if [[ -n "$a" ]]; then
      echo "  $FQDN -> $a"
      printf '  HTTP 探测: '
      curl -s -m 20 --noproxy '*' -o /dev/null -w '%{http_code}\n' "https://$FQDN/" 2>/dev/null || echo fail
    else
      echo "  $FQDN 还没有解析记录"
      echo "  → 去 Cloudflare 隧道里确认已添加 Public Hostname：$FQDN → HTTP ai4s-gateway:80"
    fi
    return 0
  fi

  echo "  尚未切换（Cloudflare NS 确认 $cf_count/$total）"
  return 1
}

if [[ "$WATCH" -eq 0 ]]; then
  check_once && exit 0 || exit 1
fi

echo "监控 $ZONE 的 NS 变更，每 ${INTERVAL}s 一次，Ctrl+C 停止"
while true; do
  if check_once; then
    echo
    echo "已完成切换，退出监控。"
    exit 0
  fi
  sleep "$INTERVAL"
done
