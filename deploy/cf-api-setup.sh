#!/usr/bin/env bash
# Cloudflare 全自动接入（走 API，完全不需要打开 dash 后台）
#
# 为什么需要这个脚本：dash.cloudflare.com 在本机出口 IP 上一直被 Cloudflare
# 人机验证拦住（403 + "正在进行安全验证"），但 api.cloudflare.com 不受影响，
# 返回的是标准 JSON。所以把「添加站点 / 建隧道 / 配 Public Hostname」全部改为
# 走 API，绕开浏览器。
#
# 用法：
#   export CF_EMAIL="登录 Cloudflare 的邮箱"
#   export CF_KEY="Global API Key"          # 有 CF_EMAIL 时按 Global Key 认证
#   # 或者只给受限 Token（没有 CF_EMAIL 时按 Bearer 认证）：
#   export CF_KEY="scoped-api-token"
#
#   bash deploy/cf-api-setup.sh check
#   bash deploy/cf-api-setup.sh zone   --zone mabokai.site
#   bash deploy/cf-api-setup.sh tunnel --zone mabokai.site --host demo --gateway ai4s-gateway:80
#   bash deploy/cf-api-setup.sh all    --zone mabokai.site --host demo
#   bash deploy/cf-api-setup.sh status --zone mabokai.site --host demo

set -uo pipefail

API="https://api.cloudflare.com/client/v4"
NET="${AI4S_NET:-2026-10-21-16-54-36_ai4s-net}"
CONTAINER="${AI4S_TUNNEL_NAME:-ai4s-tunnel}"
TUNNEL_NAME="${AI4S_TUNNEL_LABEL:-ai4s}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESP="$SCRIPT_DIR/.cf_resp.json"
CODE=""

# Git Bash 会把以 / 开头的参数当路径转换
export MSYS_NO_PATHCONV=1

usage() {
  sed -n '9,23p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 1
}

ZONE=""
HOST=""
GW="ai4s-gateway:80"
CMD="${1:-}"; shift || true

while [[ $# -gt 0 ]]; do
  case "$1" in
    --zone)    ZONE="${2:-}"; shift 2 ;;
    --host)    HOST="${2:-}"; shift 2 ;;
    --gateway) GW="${2:-}"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "未知参数: $1"; usage ;;
  esac
done

[[ -z "$CMD" ]] && usage
: "${CF_KEY:?请先设置环境变量 CF_KEY（Global API Key 或受限 Token）}"

if [[ -n "${CF_EMAIL:-}" ]]; then
  AUTH=(-H "X-Auth-Email: ${CF_EMAIL}" -H "X-Auth-Key: ${CF_KEY}")
  AUTH_MODE="Global API Key"
else
  AUTH=(-H "Authorization: Bearer ${CF_KEY}")
  AUTH_MODE="API Token"
fi

# ---------- 基础设施 ----------

api() { # api METHOD PATH [JSON_BODY] -> 结果写入 $RESP，退出码存 $CODE
  local method="$1" path="$2" body="${3:-}"
  local args=(-s -m 40 --noproxy '*' -X "$method" "${API}${path}" "${AUTH[@]}"
              -H "Content-Type: application/json" -o "$RESP" -w '%{http_code}')
  if [[ -n "$body" ]]; then
    CODE=$(curl "${args[@]}" --data-binary "$body" 2>/dev/null) || CODE="000"
  else
    CODE=$(curl "${args[@]}" 2>/dev/null) || CODE="000"
  fi
}

pick() { # pick dot.path  -> 从 $RESP 取值，空则输出空
  python - "$1" "$RESP" <<'PY'
import sys, json
path, f = sys.argv[1], sys.argv[2]
try:
    d = json.load(open(f, encoding='utf-8'))
except Exception:
    print(''); sys.exit(0)
for k in [x for x in path.split('.') if x]:
    if isinstance(d, list):
        try: d = d[int(k)]
        except Exception: print(''); sys.exit(0)
    elif isinstance(d, dict):
        d = d.get(k)
    else:
        print(''); sys.exit(0)
    if d is None:
        print(''); sys.exit(0)
print(json.dumps(d, ensure_ascii=False) if isinstance(d, (dict, list)) else d)
PY
}

die() {
  echo "✗ $1" >&2
  local msg
  msg=$(pick 'errors.0.message')
  [[ -n "$msg" ]] && echo "  API 返回: [$CODE] $msg" >&2
  [[ -n "${2:-}" ]] && echo "  $2" >&2
  rm -f "$RESP"
  exit 1
}

ok() { echo "✓ $1"; }

# ---------- 步骤 ----------

step_check() {
  echo "认证方式: $AUTH_MODE"
  api GET /user
  [[ "$CODE" != "200" ]] && die "凭据无效或没有权限"
  local email
  email=$(pick 'result.email')
  ok "凭据有效，账号邮箱: ${email:-（受限 Token，无邮箱字段）}"

  api GET "/accounts?per_page=50"
  [[ "$CODE" != "200" ]] && die "无法读取账号列表"
  local acct_count
  acct_count=$(python - "$RESP" <<'PY'
import sys, json
d = json.load(open(sys.argv[1], encoding='utf-8'))
r = d.get('result') or []
for a in r:
    print(f"  · {a.get('name')}  id={a.get('id')}")
print(f"__COUNT__{len(r)}")
PY
)
  echo "$acct_count" | grep -v '^__COUNT__' | head -10
  local n
  n=$(echo "$acct_count" | grep '^__COUNT__' | sed 's/__COUNT__//')
  [[ "${n:-0}" -lt 1 ]] && die "该凭据下没有任何账号，请确认权限范围（需 Account 级权限）"
  ok "共 ${n} 个账号"
}

# 确保 zone 已添加到账号，输出 nameservers
ensure_zone() {
  [[ -z "$ZONE" ]] && die "请指定 --zone，例如 --zone mabokai.site"

  api GET "/zones?name=${ZONE}&per_page=5"
  local zid
  if [[ "$CODE" == "200" ]]; then
    zid=$(pick 'result.0.id')
  fi

  if [[ -z "$zid" ]]; then
    local aid
    aid=$(account_id)
    api POST /zones "{\"name\":\"${ZONE}\",\"account\":{\"id\":\"${aid}\"},\"type\":\"full\"}"
    if [[ "$CODE" != "200" ]]; then
      # 1061 = 域名已存在于某账号（可能就是本账号，重新查一次）
      if [[ "$(pick 'errors.0.code')" == "1061" ]]; then
        api GET "/zones?name=${ZONE}"
        zid=$(pick 'result.0.id')
      fi
      [[ -z "$zid" ]] && die "添加站点失败" "若域名已被其他 Cloudflare 账号占用，需先在那个账号里删除。"
    else
      zid=$(pick 'result.id')
      ok "已添加站点 ${ZONE}"
    fi
  else
    ok "站点 ${ZONE} 已在账号内（zone id: ${zid}）"
  fi

  ZONE_ID="$zid"
  api GET "/zones/${zid}"
  NS1=$(pick 'result.name_servers.0')
  NS2=$(pick 'result.name_servers.1')
  STATUS=$(pick 'result.status')
  echo
  echo "  站点状态 : ${STATUS:-unknown}"
  echo "  NS 记录  : ${NS1:-?}"
  echo "             ${NS2:-?}"
  echo
}

account_id() {
  api GET "/accounts?per_page=50"
  local aid
  aid=$(pick 'result.0.id')
  [[ -z "$aid" ]] && die "无法获取账号 ID"
  echo "$aid"
}

step_zone() {
  echo "── 步骤 1/2：添加站点到 Cloudflare ──"
  ensure_zone
  cat <<EOF
下一步（必须做，且只能在腾讯云做）：
  腾讯云控制台 → 域名注册 → 我的域名 → ${ZONE} → 管理 → DNS 修改 → 自定义
  填入上面两个 NS，保存。

改完后 Cloudflare 侧从 pending 变 active 通常 10 分钟到数小时。
可以先执行 tunnel 子命令建隧道（不依赖 NS 生效），NS 一转正立即连通。
EOF
}

find_tunnel() { # 输出 tunnel id
  api GET "/accounts/${1}/cfd_tunnel?name=${TUNNEL_NAME}&is_deleted=false"
  [[ "$CODE" == "200" ]] || return 0
  pick 'result.0.id'
}

step_tunnel() {
  [[ -z "$ZONE" ]] && die "请指定 --zone，例如 --zone mabokai.site"
  [[ -z "$HOST" ]] && die "请指定 --host，例如 --host demo"
  echo "── 步骤 2/2：创建隧道并绑定 ${HOST}.${ZONE} ──"

  local aid tid
  aid=$(account_id)
  ok "账号 ID: ${aid}"

  tid=$(find_tunnel "$aid")
  if [[ -n "$tid" ]]; then
    ok "复用已有隧道 ${TUNNEL_NAME}（id: ${tid}）"
  else
    api POST "/accounts/${aid}/cfd_tunnel" \
      "{\"name\":\"${TUNNEL_NAME}\",\"config_src\":\"cloudflare\"}"
    [[ "$CODE" != "200" ]] && die "创建隧道失败"
    tid=$(pick 'result.id')
    ok "已创建隧道 ${TUNNEL_NAME}（id: ${tid}）"
  fi

  local fqdn="${HOST}.${ZONE}"
  api PUT "/accounts/${aid}/cfd_tunnel/${tid}/configurations" \
    "{\"config\":{\"ingress\":[{\"hostname\":\"${fqdn}\",\"service\":\"http://${GW}\"},{\"service\":\"http_status:404\"}]}}"
  [[ "$CODE" != "200" ]] && die "配置 ingress 失败"
  ok "Public Hostname 已绑定：${fqdn} → http://${GW}"

  api GET "/accounts/${aid}/cfd_tunnel/${tid}/token"
  [[ "$CODE" != "200" ]] && die "获取隧道 token 失败"
  local token
  token=$(pick 'result')
  [[ -z "$token" ]] && die "隧道 token 为空"

  echo "$token" > "$SCRIPT_DIR/.tunnel_token"
  chmod 600 "$SCRIPT_DIR/.tunnel_token" 2>/dev/null || true
  ok "隧道 token 已保存到 deploy/.tunnel_token（仅本地，勿提交）"

  # 替换容器并启动
  if docker network ls --format '{{.Name}}' | grep -qx "$NET"; then
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    docker run -d --name "$CONTAINER" --network "$NET" --restart unless-stopped \
      cloudflare/cloudflared:latest tunnel --no-autoupdate run --token "$token" >/dev/null \
      && ok "容器 ${CONTAINER} 已启动（网络 ${NET}）" \
      || die "容器启动失败"
  else
    die "找不到 Docker 网络 ${NET}" "先启动整套服务：docker compose up -d"
  fi

  echo
  echo "等待隧道注册（最多 40 秒）…"
  local i
  for i in $(seq 1 20); do
    if docker logs "$CONTAINER" 2>&1 | grep -q "Registered tunnel connection"; then
      ok "隧道已注册到 Cloudflare 边缘"
      break
    fi
    sleep 2
    [[ "$i" -eq 20 ]] && echo "  仍未看到 Registered，最近日志："
  done
  docker logs "$CONTAINER" 2>&1 | grep -E "Registered tunnel connection|ERR|error|Unauthorized" | tail -3 || true

  echo
  echo "地址：https://${fqdn}"
  echo "注意：需要 ${ZONE} 的 NS 已切到 Cloudflare 且状态 active 才能访问。"
  echo "用 'bash deploy/cf-api-setup.sh status --zone ${ZONE} --host ${HOST}' 查看状态。"
}

step_status() {
  [[ -z "$ZONE" ]] && die "请指定 --zone"
  api GET "/zones?name=${ZONE}"
  local zid status
  zid=$(pick 'result.0.id')
  status=$(pick 'result.0.status')
  echo "站点 ${ZONE}: ${status:-未添加}"
  [[ -n "$zid" ]] && echo "  NS: $(pick 'result.0.name_servers.0') / $(pick 'result.0.name_servers.1')"

  # 本机 DNS 实际解析
  echo
  if [[ -n "$HOST" ]]; then
    local fqdn="${HOST}.${ZONE}"
    echo "本机 DNS 解析 ${fqdn}:"
    nslookup "${fqdn}" 223.5.5.5 2>&1 | iconv -f gbk -t utf-8 2>/dev/null | grep -iE "Address|Non-existent|NXDOMAIN|can't find" | head -4
    printf "  HTTP 探测: "
    curl -s -m 15 --noproxy '*' -o /dev/null -w "%{http_code}\n" "https://${fqdn}/" 2>&1 || echo "fail"
  fi

  echo
  echo "容器 ${CONTAINER}: $(docker ps --filter "name=${CONTAINER}" --format '{{.Status}}' 2>/dev/null | head -1)"
}

case "$CMD" in
  check)  step_check ;;
  zone)   step_zone ;;
  tunnel) step_tunnel ;;
  all)    step_zone; echo; step_tunnel ;;
  status) step_status ;;
  *)      usage ;;
esac

rm -f "$RESP"
