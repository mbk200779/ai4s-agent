#!/usr/bin/env bash
# 备用入口：Cloudflare 快速隧道（trycloudflare）
#
# 用途：主域名 demo.mabokai.site 卡住时的临时救场地址。
# 限制：域名每次重建都会变，不能写进提交材料；容器重启后需重跑本脚本取新域名。
#
# 用法（在本机装 Docker 的那台机器上，任意目录均可）：
#   bash deploy/backup-tunnel.sh          # 重建隧道并打印地址
#   bash deploy/backup-tunnel.sh status   # 只看当前地址，不动容器
#   bash deploy/backup-tunnel.sh stop     # 删掉隧道容器
set -u

NAME="ai4s-tunnel-backup"
IMAGE="cloudflare/cloudflared:latest"
ACTION="${1:-start}"

die() { echo "✗ $*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "没有 docker 命令，请先启动 Docker Desktop"

current_url() {
    # 取最后一条：容器若被重启过，日志里会有多个域名，前面那些已经作废
    docker logs "$NAME" 2>&1 | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1
}

probe() {
    # 只看能不能真的拿到 200。快速隧道的域名可能在容器还活着的时候被 Cloudflare 回收，
    # 此时解析直接 NXDOMAIN（curl 0.01 秒返回 000），单纯打印域名会误导人。
    local code
    code="$(curl -s --noproxy '*' -o /dev/null -m 15 -w '%{http_code}' "$1/" 2>/dev/null || true)"
    code="${code: -3}"
    [ -z "$code" ] && code="000"
    echo "$code"
}

if [ "$ACTION" = "status" ]; then
    [ -z "$(docker ps -q -f "name=^${NAME}$")" ] && die "容器 $NAME 未运行，用 start 重建"
    URL="$(current_url)"
    [ -z "$URL" ] && die "容器在跑但没拿到域名，看日志：docker logs $NAME"
    CODE="$(probe "$URL")"
    if [ "$CODE" = "200" ]; then
        echo "$URL"
    else
        echo "$URL   ← HTTP $CODE，域名已失效（容器活着也没用），跑 start 重建" >&2
        exit 2
    fi
    exit 0
fi

if [ "$ACTION" = "stop" ]; then
    docker rm -f "$NAME" >/dev/null 2>&1 && echo "已删除 $NAME（域名随之失效）" || echo "容器本来就不存在"
    exit 0
fi

# 找 compose 生成的项目网络（形如 <目录名>_ai4s-net）
NET="$(docker network ls --format '{{.Name}}' | grep -E '_ai4s-net$' | head -1)"
[ -z "$NET" ] && die "找不到 ai4s 网络，请先在项目目录执行 docker compose up -d"

echo "→ 清理旧容器"
docker rm -f "$NAME" >/dev/null 2>&1

echo "→ 启动隧道（网络 $NET）"
docker run -d --name "$NAME" --network "$NET" --restart unless-stopped \
    "$IMAGE" tunnel --no-autoupdate --url http://gateway:80 >/dev/null \
    || die "容器启动失败"

printf "→ 等待分配域名"
URL=""
for _ in $(seq 1 30); do
    sleep 2
    printf "."
    URL="$(current_url)"
    [ -n "$URL" ] && break
done
echo
[ -z "$URL" ] && die "30 次轮询没拿到域名，看日志：docker logs $NAME"

echo "→ 域名  $URL"

# 快速隧道的新域名大约要 20–40 秒才在各家 DNS 上出现（此前一律 NXDOMAIN），
# 所以自检给到 60 秒，而不是只等十几秒就宣称失败。
for i in $(seq 1 15); do
    sleep 4
    # curl 的 -w 在超时/失败时也会先输出状态码，再以非 0 退出；
    # 若写成 `|| echo 000` 会拼成 000000，probe 里统一取末 3 位。
    CODE="$(probe "$URL")"
    echo "  自检 $i: HTTP $CODE"
    [ "$CODE" = "200" ] && break
done

echo
echo "  页面  $URL"
echo "  接口  $URL/api/v1/health/"
echo "  已在跑可用 bash deploy/backup-tunnel.sh status 随时查回当前地址"
