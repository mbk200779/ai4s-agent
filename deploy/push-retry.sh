#!/usr/bin/env bash
# 提交截止前的「推送」工具：自动绕开坏 IP，把 main 推上 GitHub。
#
# 背景：本机 DNS 把 github.com 解析到 20.205.243.166，这个 IP 时通时不通；
#       而 140.82.114.3 等其它 GitHub 边缘 IP 是通的。git 默认只用 DNS 给的
#       那一个 IP，所以表现为「Failed to connect to github.com:443 after 21000 ms」，
#       跟账号、凭据、仓库都无关。
#       本脚本先探测哪几个 IP 能用，再用 http.curloptResolve 把连接钉在可用 IP 上推送。
#
# 用法（Git Bash / UCRT64，任意目录）：
#   ./deploy/push-retry.sh            # 探测 + 推送 + 失败自动换 IP，最多 8 轮
#   ./deploy/push-retry.sh 4          # 最多 4 轮
#
# 退出码：0 = 推送成功；1 = 全部 IP 都不通（网络整体断了，或需要开代理）

set -uo pipefail

ROUNDS="${1:-8}"
REPO_URL="https://github.com/mbk200779/ai4s-agent.git"
PROBE_URL="$REPO_URL/info/refs?service=git-receive-pack"

# 候选 GitHub 边缘 IP。401 = 握手成功但需认证（说明通），200 = 通。
CANDIDATES=(140.82.114.3 140.82.112.3 140.82.113.3 140.82.116.3 20.205.243.166 20.27.177.113)

cd "$(dirname "$0")/.." || exit 1
if [ ! -d .git ]; then
  echo "[x] 当前目录不是仓库根：$(pwd)"
  exit 1
fi

# 终端/IDE 注入的代理会干扰，全部清掉走直连；确实要用代理时自行 export 后再跑
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY

BRANCH="$(git rev-parse --abbrev-ref HEAD)"

probe() { # $1=ip → 打印响应码
  curl --noproxy '*' -sS -o /dev/null -w '%{http_code}' -m 6 \
    --resolve "github.com:443:$1" "$PROBE_URL" 2>/dev/null || echo 000
}

pick_ip() { # 输出第一个可用 IP；全不通则返回 1
  for ip in "${CANDIDATES[@]}"; do
    local code; code="$(probe "$ip")"
    if [ "$code" = "401" ] || [ "$code" = "200" ]; then
      echo "$ip"; return 0
    fi
  done
  return 1
}

do_push() { # $1=ip（非空 = 把连接钉在该 IP 上）
  env GIT_TERMINAL_PROMPT=0 git -c "http.curloptResolve=github.com:443:$1" push origin "$BRANCH" 2>&1
}

echo "[i] 仓库：$(pwd)"
echo "[i] 分支：$BRANCH"
PENDING="$(git rev-list --count "origin/$BRANCH..$BRANCH" 2>/dev/null || echo 0)"
echo "[i] 待推送提交：$PENDING 个"
git --no-pager log --oneline "origin/$BRANCH..$BRANCH" 2>/dev/null | sed 's/^/    /'
echo

if [ "$PENDING" = "0" ]; then
  echo "[✓] 远端已经是最新，无需推送。"
  exit 0
fi

for r in $(seq 1 "$ROUNDS"); do
  if ! IP="$(pick_ip)"; then
    echo "[$r/$ROUNDS] 所有候选 IP 都不通，30 秒后重试"
    sleep 30
    continue
  fi
  printf '[%s] 第 %d/%d 轮，用 %s 推送 … ' "$(date +%H:%M:%S)" "$r" "$ROUNDS" "$IP"
  if OUT="$(do_push "$IP")"; then
    echo "成功"
    echo "$OUT" | tail -3 | sed 's/^/    /'
    echo
    echo "[✓] 已推送。核对：https://github.com/mbk200779/ai4s-agent/commits/$BRANCH"
    exit 0
  fi
  echo "失败"
  echo "$OUT" | tail -2 | sed 's/^/    /'
  if echo "$OUT" | grep -qiE "Authentication failed|Permission denied|could not read (Username|Password)|403"; then
    echo
    echo "[x] 凭据问题，重试无用。检查 Windows 凭据管理器里的 git:https://github.com 条目。"
    exit 1
  fi
  sleep 3
done

echo
echo "[x] $ROUNDS 轮都没成功。大概率是本机到 GitHub 整体不通："
echo "    开代理/加速器后重跑本脚本；若代理是系统代理模式，先 export https_proxy=http://127.0.0.1:端口"
exit 1
