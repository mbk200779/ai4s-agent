# cpolar 国内隧道接入指南

> 为什么要加这条通路：Cloudflare 隧道在国内晚高峰走美西（LAX），回源那一段丢包到
> 60~75%——实测连 231 字节的 `/icon.svg` 都只有一半能传完，而同一时刻从这台机器
> 下载 `cloudflare.com` 的 1.3 MB 首页 5/5 都能成功。也就是说瓶颈不在本地、不在应用、
> 不在客户端到 CF 边缘这一段，而在 cloudflared ↔ CF 边缘之间，且不受我们配置影响：
> 换 CF 边缘 IP、换 IPv6、重启隧道、把连接数从 4 加到 8，实测都没有改善。

cpolar 走国内节点，不经过国际出口。两条隧道可以并存，随时切。

---

## 一、三步接入

### 1. 注册并复制 authtoken

打开 https://www.cpolar.com/ 注册（邮箱即可）→ 登录后台 → 左侧 **验证** →
复制那串 **Authtoken**。

### 2. 填进 `.env`

```bash
# 文件末尾，把 authtoken 粘在等号后面
CPOLAR_AUTHTOKEN=你的authtoken
```

> `.env` 已在 `.gitignore` 里，token 不会进仓库。
> `deploy/cpolar.yml` 里只有占位符 `__CPOLAR_TOKEN__`，启动时由容器内 sed 替换到 `/tmp`。

### 3. 启动

```bash
docker compose --profile cpolar up -d cpolar
docker logs -f ai4s-cpolar     # 日志里会出现 https://xxxx.cpolar.top
```

日志里那一行 `Forwarding https://xxxxx.cpolar.top -> http://gateway:80` 就是公网地址，
直接发给别人。

---

## 二、免费版的三个限制

| 限制 | 影响 | 建议 |
| --- | --- | --- |
| **1 Mbps 带宽** | 首屏 4.9 KB 没问题；PDF 预览、大图会明显慢 | 演示讲评够用；要流畅看图升级基础版 |
| **域名 24 小时变化** | 链接第二天就失效，不能写进提交材料 | 短期演示无所谓；要固定域名需付费（基础版约 99 元/年） |
| **4 条隧道** | 我们只用 1 条 | 无影响 |

如果只是这周给评委看，免费版足够：演示当天启动，把当时的链接发出去。

---

## 三、验证

```bash
# 1. 隧道是否在线
docker logs ai4s-cpolar 2>&1 | grep -E "Forwarding|online|Tunnel Status"

# 2. 公网地址是否真的通（把 URL 换成日志里那条）
for i in 1 2 3; do
  curl -s -o /dev/null -w "%{http_code} %{size_download}B %{time_total}s\n" https://xxxx.cpolar.top/
done

# 3. 长耗时接口（综述生成要 40~90 秒，隧道默认超时可能截断）
curl -s --max-time 120 -X POST https://xxxx.cpolar.top/api/v1/review/generate \
  -H "Content-Type: application/json" \
  -d '{"topic":"Transformer 自注意力机制","top_k":6}' -o /dev/null -w "%{http_code} %{time_total}s\n"
```

第 3 条最容易出问题：免费版对单连接有超时，综述这类 60 秒以上的请求可能被断。
如果被断，演示时改用问答页或提前生成好综述再展示。

---

## 四、和 Cloudflare 隧道的关系

两条隧道**互不冲突**，同时跑也可以：

| 入口 | 走哪里 | 适用 |
| --- | --- | --- |
| `demo.mabokai.site`（cloudflared） | 国际出口 → LAX | 国内非高峰、海外访问 |
| `xxx.cpolar.top`（cpolar） | 国内节点 | 国内晚高峰、评委在国内 |

只停 Cloudflare 那条：

```bash
docker compose --profile tunnel stop tunnel
```

回到 Cloudflare：

```bash
docker compose --profile cpolar stop cpolar
docker compose --profile tunnel up -d tunnel
```

---

## 五、排障

**日志一直刷 `authToken is null`**
`.env` 里的 `CPOLAR_AUTHTOKEN` 没读到。确认没有多余空格，改完执行
`docker compose --profile cpolar up -d --force-recreate cpolar`。

**日志一直刷 `Waiting N seconds before reconnecting`**
token 错误或服务器连不上。先确认 `nc -zv cpolard.cpolar.com 4443` 通，
再去后台核对 token 是否为当前账号的。

**日志出现地址但访问 502**
隧道到 `gateway:80` 不通。进容器验证：
`docker exec ai4s-cpolar sh -c "getent hosts gateway"` 应该有输出。
容器必须挂在 `ai4s-net` 上——compose 里已经配好，手工 `docker run` 时容易漏。
