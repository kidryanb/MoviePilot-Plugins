# quark-relay：夸克 STRM 服务器中转

让「网盘订阅助手（改版）」生成的夸克 STRM 播放流量走服务器，不占用家里 NAS 的上行。

```
播放器 ──> 服务器 quark-relay ──(取直链，几 KB)──> NAS 上的 MP 插件
               │
               └──(带 Cookie 分段拉流)──> 夸克 ──> 播放器
```

- STRM 内容形如 `https://你的域名/q/quark/<file_id>/<签名>?/文件名.mkv`。签名是 HMAC，没有密钥就无法伪造，也改不了链接里的文件。
- 服务器只向 MP 要直链和请求头，并按文件缓存：有效期取直链里的过期时间减去 60 秒，读不到时缓存 10 分钟。夸克拒绝（403/410）时自动作废并重新取一次，播放途中签名过期也能续上。夸克 Cookie 只在 MP 和你的服务器之间传，不会交给播放器。
- 可以另配一个内网地址 `MP_INTERNAL_URL` 专门用来取直链，不用绕公网，第一次打开更快。
- 支持 Range 请求，播放时可以拖动进度。只依赖 Python 标准库。

## 1. 在服务器上运行

先生成一串密钥，插件和服务器都要填这一串：

```bash
openssl rand -hex 24
```

不用构建镜像，直接用官方 Python 镜像运行：

```bash
mkdir -p /opt/quark-relay
curl -fsSL -o /opt/quark-relay/relay.py \
  https://raw.githubusercontent.com/kidryanb/MoviePilot-Plugins/main/tools/quark-relay/relay.py

docker run -d --name quark-relay --restart unless-stopped \
  -p 127.0.0.1:8080:8080 \
  -v /opt/quark-relay/relay.py:/app/relay.py:ro \
  -e RELAY_SECRET=<上面生成的密钥> \
  -e MP_URL=<服务器能访问到的 MP 地址，例如 http://127.0.0.1:3000 或 https://mp.example.com> \
  -e MP_INTERNAL_URL=<可选：服务器经内网/穿透直达 MP 的地址，例如 http://127.0.0.1:3000> \
  -e MP_APIKEY=<MP 的 API 令牌：设定 → 安全 → API 令牌> \
  -e PATH_PREFIX=/q \
  python:3.12-alpine python /app/relay.py
```

| 变量 | 说明 |
| --- | --- |
| `RELAY_SECRET` | 必填，和插件「服务器中转密钥」相同 |
| `MP_URL` | 服务器访问 NAS 上 MP 的地址，例如 `https://mp.example.com`。和 `MP_INTERNAL_URL` 至少填一个 |
| `MP_INTERNAL_URL` | 可选。取直链优先走的内网或回源地址，例如 frp 在服务器本机的端口 `http://127.0.0.1:3000`。连不上时自动退回 `MP_URL`，之后 60 秒内直接走 `MP_URL`。不填就和以前一样只用 `MP_URL` |
| `MP_APIKEY` | 必填，MP 的 API 令牌 |
| `PATH_PREFIX` | 挂在域名子路径时填写，例如 `/q`；单独用一个域名时留空 |
| `LISTEN_PORT` | 默认 8080 |
| `LINK_TTL` | 直链里读不到过期时间时的缓存秒数，默认 600 |
| `LINK_EXPIRY_MARGIN` | 直链里读得到过期时间时，提前这么多秒作废，默认 60 |
| `CHUNK_MB` | 每段向夸克拉取的大小，默认 4 |
| `LOG_LEVEL` | 默认 INFO。设为 `DEBUG` 时每个播放请求记一行，见下方 |

## 2. 反向代理（HTTPS）

Nginx：

```nginx
location /q/ {
    proxy_pass http://127.0.0.1:8080;   # 不要在末尾加 /，保留 /q 前缀
    proxy_buffering off;
    proxy_request_buffering off;
    proxy_http_version 1.1;
    proxy_set_header Range $http_range;
    proxy_set_header If-Range $http_if_range;
    proxy_read_timeout 3600s;
}
```

Caddy：

```
mp.example.com {
    handle /q/* {
        reverse_proxy 127.0.0.1:8080 {
            flush_interval -1
        }
    }
    # 其他路径照旧（例如转发到 MP）
}
```

如果这个域名原来整个转发到 NAS 上的 MP，就要把 `/q/` 这一段单独交给 quark-relay，否则流量还是会绕回 NAS。

## 3. 插件设置

在插件「转存设置 → STRM 与元数据」中：

- 转存后直接生成 STRM：开
- 服务器中转地址（夸克）：`https://mp.example.com/q`
- 服务器中转密钥：和 `RELAY_SECRET` 相同

保存后，「播放代理与 STRM 示例」里会显示夸克 STRM 的地址。使用服务器中转时，不需要再开本机的「STRM 播放代理」。只有之后新生成的夸克 STRM 会指向服务器；之前生成的 STRM 还是旧地址。

## 4. 验证

1. 打开一个夸克 `.strm` 文件，复制里面的地址，用 VLC 打开，确认能播放、能拖动进度。
2. 看服务器上的 `docker logs quark-relay` 和 MP 插件日志：每次开始播放，MP 日志都应出现「服务器中转取链」。
3. 外网播放时，Emby 客户端要设为直接播放。如果由 Emby 服务端转码或代理，流量仍会经过 NAS。

## 5. 排查慢：逐请求日志

加 `-e LOG_LEVEL=DEBUG` 重建容器后，`docker logs -f quark-relay` 里每个请求一行：

```
请求 GET quark/0a1b…9f8e range=bytes=0- status=206 cache=miss link=212ms ttfb=468ms bytes=4194304 total=1530ms renewed=0
```

- `cache`：直链是否命中缓存（`hit` / `miss`）
- `link`：取直链用时（命中缓存时接近 0）
- `ttfb`：从收到请求到发出第一个字节
- `bytes`：这次传给播放器的字节数；`renewed=1` 表示途中直链过期、已自动续上

日志里不会出现 Cookie、MP 令牌、链接签名或夸克直链，文件标识只保留首尾 4 位。

## 6. 预取（可选）

`GET` 或 `POST https://你的域名/q/prefetch/quark/<file_id>/<签名>`：只把直链取进缓存，不拉流，成功返回 204。路径就是 STRM 里的地址在 `/q/` 后面插一段 `prefetch/`，签名不对返回 403。可以在 Emby 打开详情页时由脚本或 Webhook 调用，让第一次播放直接命中缓存。

## 7. 多台中转（预留，暂未在配置页显示）

插件 0.5.12 起，配置里有两个新字段，默认都为空，留空时和以前完全一样：

- `strm_relay_urls`：额外的中转地址，每行 `名称|地区|URL`（也可以只写 `URL`）
- `strm_relay_choice`：填名称或地区，生成 STRM 时用这一台；没填或没匹配上时用原来的「服务器中转地址」

所有中转共用同一个 `RELAY_SECRET`。改了选择后，只有之后新生成的 STRM 会指向新地址。

## 已知限制

- 夸克直链如果绑定了取链时的 IP，服务器用 NAS 取到的链接会被拒绝（日志里是「上游未返回分段响应：HTTP 403」）。遇到这种情况请反馈，届时改为由服务器自己用 Cookie 取链。
- 本文件与插件同为 GPL-3.0。
