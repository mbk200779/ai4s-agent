#!/usr/bin/env bash
# 演示前自检：30 秒内回答"现在能不能上台"
#
# 用法：
#   bash deploy/demo-check.sh              # 常规自检（不调大模型，零成本）
#   bash deploy/demo-check.sh --with-qa    # 额外跑一次真实问答（会消耗 1 次 LLM 调用）
#   bash deploy/demo-check.sh --quiet      # 只输出结论与失败项
#
# 设计说明：
#   1. 默认不调 LLM。演示前最贵的不是时间，是额度——自检把额度跑没了就没得演示了。
#   2. 公网要测 3 次而不是 1 次。这条链路实测成功率只有 1/3，单次通过说明不了任何事。
#   3. 每项都给出"失败时怎么办"，而不是只报红。台上是没时间查文档的。

set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
export MSYS_NO_PATHCONV=1

DOMAIN="${DEMO_DOMAIN:-demo.mabokai.site}"
QA=0
QUIET=0
for a in "$@"; do
  case "$a" in
    --with-qa) QA=1 ;;
    --quiet)   QUIET=1 ;;
    -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数: $a"; exit 1 ;;
  esac
done

PROBE="./.demo-check.probe"
trap 'rm -f "$PROBE" 2>/dev/null || true' EXIT

FAILS=0
WARNS=0
FAIL_ITEMS=()
WARN_ITEMS=()

say() { [[ "$QUIET" -eq 1 ]] || printf '%s\n' "$1"; }
ok()  { [[ "$QUIET" -eq 1 ]] || printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad() { printf '  \033[31m✗\033[0m %s\n' "$1"; FAILS=$((FAILS+1)); FAIL_ITEMS+=("$1"); }
warn(){ printf '  \033[33m!\033[0m %s\n' "$1"; WARNS=$((WARNS+1)); WARN_ITEMS+=("$1"); }
hint(){ [[ "$QUIET" -eq 1 ]] || printf '      %s\n' "$1"; }

code_of() { # url timeout -> http code（000 表示失败）
  # 坑：curl 超时时会先写出 http_code（比如 200）再以非 0 退出，
  # 若写成 `|| echo 000` 就会拼成 "200000"。所以只取输出的后 3 位。
  local out
  out=$(curl -sS --noproxy '*' -m "$2" -o "$PROBE" -w '%{http_code}' "$1" 2>/dev/null)
  [[ ${#out} -ge 3 ]] && printf '%s' "${out: -3}" || printf '000'
}

say "=============================================="
say " 演示自检  $(date '+%Y-%m-%d %H:%M:%S')"
say " 目标域名  https://$DOMAIN"
say "=============================================="
say ""

# ---------- 1. Docker 引擎 ----------
say "==> Docker 引擎"
if docker info >/dev/null 2>&1; then
  ok "引擎正常"
else
  bad "Docker 引擎没运行"
  hint "从开始菜单打开 Docker Desktop，等鲸鱼图标停止转动，再重跑本脚本"
  echo ""
  echo "结论：不可演示（引擎未启动，后面全部无从检查）"
  exit 1
fi

# ---------- 2. 容器健康 ----------
say ""
say "==> 容器健康"
REQUIRED=(ai4s-qdrant ai4s-minio ai4s-redis ai4s-api ai4s-worker ai4s-web ai4s-gateway ai4s-tunnel)
for c in "${REQUIRED[@]}"; do
  st=$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || echo missing)
  hl=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}-{{end}}' "$c" 2>/dev/null || echo -)
  if [[ "$st" == "running" && ( "$hl" == "healthy" || "$hl" == "-" ) ]]; then
    ok "$c  running $hl"
  elif [[ "$st" == "missing" ]]; then
    bad "$c  不存在"
    hint "docker compose --profile tunnel up -d 重建"
  elif [[ "$hl" == "starting" ]]; then
    warn "$c  启动中（健康检查还没过）"
  else
    bad "$c  status=$st health=$hl"
    hint "看日志：docker logs --tail 30 $c"
  fi
done

# ---------- 3. 本地链路 ----------
say ""
say "==> 本地链路（不经公网，演示最稳的一条路）"
for p in / /qa /library; do
  code=$(code_of "http://localhost:8080$p" 20)
  [[ "$code" == "200" ]] && ok "gateway$p -> 200" || bad "gateway$p -> $code"
done
body=$(curl -sS --noproxy '*' -m 15 "http://localhost:8080/api/v1/health/" 2>/dev/null || echo "")
[[ "$body" == *'"status":"ok"'* ]] && ok "API 健康检查 $body" || bad "API 健康检查异常：${body:0:80}"

# ---------- 4. 检索底座（不调 LLM） ----------
say ""
say "==> 检索底座"
Q=$(curl -sS -m 8 "http://localhost:6333/collections" 2>/dev/null || echo "")
if [[ -n "$Q" ]]; then
  # 只关心业务集合：探针集合（_probe*）与历史坏档（*.broken-*）不参与判断，
  # 列出来只会让人以为有问题。
  cols=$(printf '%s' "$Q" | python -c "
import sys, json
names = [c['name'] for c in json.load(sys.stdin)['result']['collections']]
biz = [n for n in names if n.startswith('ai4s_') and '.broken' not in n]
print(len(names), ','.join(biz))
" 2>/dev/null || echo "?")
  set -- $cols
  ok "Qdrant 可读（共 $1 个集合，业务集合：${2:-无}）"
else
  bad "Qdrant 不通（:6333 无响应）"
  hint "docker restart ai4s-qdrant，等 20 秒再跑一次"
fi
for c in ai4s_papers ai4s_paper_meta; do
  pc=$(curl -sS -m 8 "http://localhost:6333/collections/$c" 2>/dev/null \
        | python -c "import sys,json;print(json.load(sys.stdin)['result']['points_count'])" 2>/dev/null || echo "")
  if [[ -n "$pc" ]]; then
    [[ "$pc" -gt 0 ]] && ok "$c  $pc 个点" || warn "$c 是空的（0 个点）——库里没论文，问答会判「答不了」"
  else
    bad "$c 读不到"
  fi
done

# 写入通道：Qdrant 1.11.0 的稀疏倒排索引有 panic（issue #5231），一旦触发，
# 检索照常、写入全 500 —— 现场表现就是"导入必失败、删论文也删不掉"，而问答一切正常，
# 极易误判成解析器问题。这里只读检查，不写探测点。
qv=$(curl -sS -m 8 "http://localhost:6333/" 2>/dev/null \
      | python -c "import sys,json;print(json.load(sys.stdin).get('version',''))" 2>/dev/null || echo "")
if [[ -n "$qv" ]]; then
  panic=$(docker logs ai4s-qdrant --since 24h 2>&1 | grep -c "inverted_index_ram" || true)
  panic=${panic:-0}
  case "$qv" in
    1.11.*)
      bad "Qdrant $qv 有已知稀疏索引 panic（issue #5231），写入随时会挂"
      hint "docker compose up -d qdrant —— 镜像已改 v1.12.4，重启即生效" ;;
    *)
      if [[ "$panic" -gt 0 ]]; then
        bad "Qdrant $qv 近 24h 出现 $panic 次 inverted_index_ram panic（导入会失败）"
        hint "见 docs/2.25「写入通道是否坏了」；重启只短期有效，先确认镜像版本"
      else
        ok "写入通道（Qdrant $qv，近 24h 无稀疏索引 panic）"
      fi ;;
  esac
fi

# ---------- 5. 入库队列 ----------
say ""
say "==> 入库队列"
qlen=$(docker compose exec -T redis redis-cli -n 0 llen celery 2>/dev/null | tr -d '\r' || echo "?")
if [[ "$qlen" =~ ^[0-9]+$ ]]; then
  [[ "$qlen" -eq 0 ]] && ok "队列空（没有积压任务）" || warn "队列积压 $qlen 个任务，worker 可能在慢慢跑"
else
  warn "取不到队列长度（不影响演示）"
fi

# ---------- 6. 磁盘与内存 ----------
say ""
say "==> 资源余量"
avail=$(df -Pk /d 2>/dev/null | awk 'NR==2{print int($4/1024/1024)}')
if [[ -n "${avail:-}" ]]; then
  [[ "$avail" -ge 10 ]] && ok "D 盘剩余 ${avail}G" || bad "D 盘只剩 ${avail}G，入库/日志可能写满"
fi
mem=$(docker info --format '{{.MemTotal}}' 2>/dev/null || echo 0)
used=$(docker stats --no-stream --format '{{.MemUsage}}' 2>/dev/null | awk -F'/' '
  {
    v = $1; gsub(/[ \t]/, "", v)
    if (v ~ /GiB$/)      { sub(/GiB$/, "", v); s += v }
    else if (v ~ /MiB$/) { sub(/MiB$/, "", v); s += v / 1024 }
    else if (v ~ /KiB$/) { sub(/KiB$/, "", v); s += v / 1024 / 1024 }
    else if (v ~ /B$/)   { sub(/B$/,   "", v); s += v / 1024 / 1024 / 1024 }
  }
  END { printf "%.1f", s }')
if [[ -n "${used:-}" && "${used:-0}" != "0.0" ]]; then
  total=$(awk -v m="$mem" 'BEGIN{printf "%.1f", m/1024/1024/1024}')
  ok "容器共占 ${used}G / ${total}G"
fi
dify=$(docker ps --format '{{.Names}}' 2>/dev/null | grep -c '^docker-' || true)
if [[ "${dify:-0}" -gt 0 ]]; then
  warn "另有 $dify 个 Dify 容器在跑，与 AI4S 共用 Docker 内存"
  hint "演示期间若跑大论文入库，可临时停掉：docker stop \$(docker ps -q --filter name=docker-)"
fi

# ---------- 7. 公网（3 次，看成功率） ----------
say ""
say "==> 公网 https://$DOMAIN（测 3 次，看成功率）"
POK=0
for i in 1 2 3; do
  t0=$(date +%s%N 2>/dev/null || echo 0)
  code=$(code_of "https://$DOMAIN/" 25)
  t1=$(date +%s%N 2>/dev/null || echo 0)
  ms=$(( (t1 - t0) / 1000000 ))
  if [[ "$code" == "200" ]]; then
    POK=$((POK+1)); ok "第 $i 次 -> 200  ${ms}ms"
  else
    # 单次失败不单独计为"不通过"，成功率在下面统一判定——
    # 否则 1/3 通会显示成两条红项，掩盖了"链路整体不稳"这个真正的结论。
    say "  · 第 $i 次 -> $code  ${ms}ms"
  fi
done
if [[ "$POK" -eq 3 ]]; then
  ok "公网 3/3 通"
elif [[ "$POK" -ge 2 ]]; then
  warn "公网 $POK/3 通 —— 能演示，但评委扫码可能失败"
  hint "上台优先用 localhost:8080，或局域网地址（ipconfig 查 IPv4，端口 8080）"
elif [[ "$POK" -ge 1 ]]; then
  warn "公网 $POK/3 通 —— 大概率打不开，别把二维码写在 PPT 上"
  hint "改成现场演示本机，或演示前重启隧道：docker restart ai4s-tunnel"
else
  bad "公网 0/3 通"
  hint "先确认本地 8080 能开；本地能开而公网全红 = 隧道/CF 边缘问题：docker logs --tail 30 ai4s-tunnel"
fi

# ---------- 8. 可选：真实问答 ----------
if [[ "$QA" -eq 1 ]]; then
  say ""
  say "==> 真实问答（消耗 1 次 LLM 调用）"
  ans=$(curl -sS --noproxy '*' -m 120 -X POST "http://localhost:8080/api/v1/qa/ask" \
        -H 'Content-Type: application/json' \
        -d '{"question":"注意力机制的计算复杂度是多少？"}' 2>/dev/null || echo "")
  if [[ "$ans" == *'"answer"'* ]]; then
    ok "问答链路通：${ans:0:100}"
  else
    bad "问答失败：${ans:0:160}"
    hint "额度耗尽会报 Free quota exhausted，去 Dashscope 充值或换模型"
  fi
fi

# ---------- 结论 ----------
echo ""
echo "=============================================="
if [[ "$FAILS" -eq 0 && "$WARNS" -eq 0 ]]; then
  echo " 结论：可以演示。本地与公网都正常。"
elif [[ "$FAILS" -eq 0 ]]; then
  echo " 结论：可以演示，但有 $WARNS 项需要注意："
  for w in "${WARN_ITEMS[@]}"; do echo "   · $w"; done
else
  echo " 结论：还有 $FAILS 项不通过，先修："
  for f in "${FAIL_ITEMS[@]}"; do echo "   · $f"; done
  [[ "$WARNS" -gt 0 ]] && echo " 另有 $WARNS 项提醒，见上方 ! 行。"
fi
echo "=============================================="
[[ "$FAILS" -eq 0 ]] && exit 0 || exit 1
