#!/usr/bin/env bash
# 提交截止前的「推送重试」小工具。
#
# 用途：本机到 github.com:443 时不时连不上（DNS 正常解析到 20.205.243.166，
#       但 TCP 握手超时）。GitHub 不通报错是「Could not connect to server」
#       或「Connection was reset」，跟账号/凭据无关，网络通了就能推。
#
# 用法（在 Git Bash 或 UCRT64 里，先 cd 到项目根目录）：
#   ./deploy/push-retry.sh              # 每 30 秒试一次，最多 40 次（约 20 分钟）
#   ./deploy/push-retry.sh 10 60        # 每 10 次？不，是：间隔 10 秒、最多 60 次
#
# 退出码：0 = 推送成功；1 = 次数用尽仍失败

set -uo pipefail

INTERVAL="${1:-30}"
MAX_TRIES="${2:-40}"

cd "$(dirname "$0")/.." || exit 1

if [ ! -d .git ]; then
  echo "[x] 当前目录不是仓库根：$(pwd)"
  exit 1
fi

# 清掉 IDE/终端注入的代理变量，git 走直连；本机有可用代理时请自行 export
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
echo "[i] 仓库：$(pwd)"
echo "[i] 分支：$BRANCH"
echo "[i] 待推送提交："
git --no-pager log --oneline "@{u}..HEAD" 2>/dev/null || git --no-pager log --oneline origin/"$BRANCH"..HEAD 2>/dev/null || echo "    (无法判断，继续尝试)"
echo

for i in $(seq 1 "$MAX_TRIES"); do
  printf '[%s] 第 %d/%d 次推送 … ' "$(date +%H:%M:%S)" "$i" "$MAX_TRIES"
  if OUT="$(git push origin "$BRANCH" 2>&1)"; then
    echo "成功"
    echo "$OUT"
    echo
    echo "[✓] 已推送。到 https://github.com/mbk200779/ai4s-agent/commits/$BRANCH 核对最新提交。"
    exit 0
  fi
  echo "失败"
  echo "$OUT" | tail -2 | sed 's/^/    /'
  # 凭据类错误不必重试
  if echo "$OUT" | grep -qiE "Authentication failed|Permission denied|403"; then
    echo
    echo "[x] 这是凭据问题，不是网络问题，重试无用。请检查 Windows 凭据管理器里的 git:https://github.com"
    exit 1
  fi
  [ "$i" -lt "$MAX_TRIES" ] && sleep "$INTERVAL"
done

echo
echo "[x] $MAX_TRIES 次仍失败。确认代理/加速器已开启，或稍后重跑本脚本。"
exit 1
