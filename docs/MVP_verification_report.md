# EMQX 6.3.2 + SRS 7.0 最小闭环验证报告（Docker MVP）

- 主机：`gs@192.168.31.114`（Ubuntu 24.04.5，aarch64，8C/15G）
- 时间：2026-11-15
- 结论：**最小闭环跑通**。EMQX 6.3.2、SRS v7.0-a4（7.0.215）、Nginx 门户三容器运行；EMQX REST、SRS HTTP API、SRS Prometheus exporter、HTTP-FLV 播放均实测通过。

---

## 0. 结论与访问入口

| 入口 | 地址 | 说明 |
|---|---|---|
| 统一门户（MVP） | http://192.168.31.114:8088/ | 列出 MQTT 客户端/订阅、SRS 流/客户端，并可播放测试流 |
| EMQX Dashboard | http://192.168.31.114:18083/ | `admin` / `Public123456` |
| SRS Console | http://192.168.31.114:8080/console/ | 原生控制台 |
| SRS Player | http://192.168.31.114:8080/players/srs_player.html?app=live&stream=test | 官方播放页 |
| SRS HTTP API | http://192.168.31.114:1985/api/v1/ | 流/客户端 |
| SRS Exporter | http://192.168.31.114:9972/metrics | Prometheus |
| 测试流 | `rtmp://192.168.31.114:1935/live/test` | ffmpeg 持续推流中 |

关键结论：

1. **不改 EMQX 前端可行**：门户通过 nginx 同源反代调用 EMQX `/api/v5` 与 SRS `/api/v1`，页面能实时展示并播放。
2. **浏览器不能直连 EMQX API**：直连 `18083` 即使带 `Origin` 也**不返回 `Access-Control-Allow-Origin`**，`OPTIONS` 预检返回 500；必须同源反代或自建 BFF。
3. **SRS v7.0-a4 官方镜像可用**（arm64，`ossrs/srs:v7.0-a4`），无需源码构建；版本 `7.0.215`。
4. **SRS API 必须带尾斜杠**：`/api/v1/streams` 返回 301，`/api/v1/streams/` 才是 200。
5. **浏览器自动播放被策略拦截**：需用户点击一次；已加“播放 / 重连”按钮，点击后 `paused=false`、`t=10.90`、画面出现测试图案。
6. **镜像拉取依赖加速**：`registry-1.docker.io`/`download.docker.com` 不可达；已用 `docker.m.daocloud.io`、`docker.1ms.run` 与阿里云 docker-ce 源。

---

## 1. 部署架构

```text
浏览器
  │ http://192.168.31.114:8088
  ▼
mvp-portal (nginx:1.27-alpine)
  ├── /              -> 静态门户 index.html
  ├── /emqx-api/     -> http://emqx:18083/api/v5/   （反代注入 API Key）
  ├── /srs-api/      -> http://srs:1985/api/v1/
  ├── /srs/          -> http://srs:8080/            （console/players/FLV/HLS）
  └── /metrics       -> http://srs:9972/metrics
        ▲                         ▲
        │ docker network mvpnet  │
   mvp-emqx (6.3.2)          mvp-srs (v7.0-a4)
   1883/8083/18083            1935/1985/8080/8000udp/9972
```

compose 文件：`~/01_walle_service/mvp/docker-compose.yml`
nginx：`~/01_walle_service/mvp/nginx/default.conf`
SRS：`~/01_walle_service/mvp/srs/srs.conf`
门户：`~/01_walle_service/mvp/portal/index.html`

---

## 2. 实测通过的接口

### 2.1 EMQX REST（API Key 鉴权）

| 接口 | 结果 | 实际样例（截取） |
|---|---|---|
| `POST /api/v5/login` | 200 | 返回 `token`（dashboard 会话） |
| `POST /api/v5/api_key` | 成功 | 生成 `mvp-portal` key，保存在 `mvp/emqx_api_key.json` |
| `GET /api/v5/clients?limit=2` | 200 | `{"count":1,"data":[{"clientid":"L75fJxt7YPGV2wOT","proto_name":"MQTT","connected":true,"ip_address":"172.18.0.4"}]}` |
| `GET /api/v5/subscriptions?limit=2` | 200 | `{"count":1,"data":[{"clientid":"L75fJxt7YPGV2wOT","topic":"demo/#","qos":1}]}` |
| `GET /api/v5/nodes` | 200 | `[{"node":"emqx@172.18.0.2","version":"6.3.2","node_status":"running","edition":"Enterprise","live_connections":1}]` |
| `GET /api/v5/metrics` | 200 | 113 个数值字段，如 `packets.connect.received=1`、`messages.retained=36` |

浏览器直连测试（同一条 clients 请求）：

```text
GET http://192.168.31.114:18083/api/v5/clients（带 Origin: http://192.168.31.114:8088）
HTTP/1.1 200 OK
（响应头中没有 Access-Control-Allow-Origin）

OPTIONS 同地址 → HTTP/1.1 500
```

结论：**CORS 未开启且预检不可用，浏览器必须走同源反代/BFF**。本方案由 nginx 在 `/emqx-api/` 注入 `Authorization: Basic <API_KEY:SECRET>`，浏览器只访问 8088。

### 2.2 SRS HTTP API 与 Exporter

| 接口 | 结果 | 实际样例（截取） |
|---|---|---|
| `GET /api/v1/streams/` | 200 | `{"code":0,"total":1,"streams":[{"name":"test","app":"live","clients":6,"kbps":{"recv_30s":1186,"send_30s":5948},"video":{"codec":"H264","width":640,"height":360}}]}` |
| `GET /api/v1/clients/` | 200 | `{"code":0,"total":6,"clients":[{"id":"lt2q41d4","ip":"172.18.0.1","type":"fmle-publish","name":"test","alive":223.52}, ... 5 个 flv-play]}` |
| `GET /api/v1/streams`（无尾斜杠） | 301 | `Redirect to /api/v1/streams/` |
| `GET /api/v1/versions/` | 200 | `{"code":0,"server":"vid-3m0p19m"}`，`version` 为 null（见“异常”） |
| `GET :9972/metrics` | 200 | `srs_build_info{... version="7.0.215", code="Kai", label="mvp", tag="mvp"} 1` |

### 2.3 门户与播放

| 检查 | 结果 |
|---|---|
| `GET http://192.168.31.114:8088/` | 200，8107 bytes，标题 `EMQX + SRS 统一门户 (MVP)` |
| `/emqx-api/clients`、`/emqx-api/subscriptions`、`/emqx-api/nodes` | 200 |
| `/srs-api/streams/`、`/srs-api/clients/` | 200 |
| `/srs/live/test.flv` | 200，`Content-Type: video/x-flv`，8 秒读取 1,260,422 bytes |
| `/srs/players/js/mpegts-1.7.3.min.js` | 200，217,829 bytes |
| `/metrics` | 200 |

播放验证：

```text
ffprobe http://127.0.0.1:8088/srs/live/test.flv
streams: aac (0/0), h264 640x360 @ 25/1
```

浏览器点击播放后：

```text
[debug] readyState=4 t=10.90 paused=false err=- h264=true
```

截图（已复制到 VM）：

- `~/01_walle_service/mvp/screenshots/emqx_srs_portal_full.jpg`（页面全貌：客户端 1、订阅 1、SRS 流 live/test）
- `~/01_walle_service/mvp/screenshots/emqx_srs_after_click.jpg`（点击后出现彩色测试图案）

---

## 3. 失败/异常与处理

| 问题 | 现象 | 处理 |
|---|---|---|
| Docker Hub 直连 | `registry-1.docker.io` 超时，`hub.docker.com` API 超时 | `daemon.json` 配 `docker.m.daocloud.io`、`docker.1ms.run` |
| Docker 官方 apt 源 | `download.docker.com` curl 35 reset，`NO_PUBKEY` | 改用阿里云 docker-ce 镜像（上一轮） |
| SRS 启动失败 | `max_connections 1000` 超过容器 ulimit 1024 | compose 增加 `ulimits.nofile=10000`，重启成功 |
| CORS | 直连 18083 无 `Access-Control-Allow-Origin`，OPTIONS 500 | 同源 nginx 反代；门户不直连 |
| SRS API 尾斜杠 | `/api/v1/streams` 301 | 门户统一请求 `/srs-api/streams/`、`/clients/` |
| `/api/v1/versions/` | `version: null`（SRS 7 该字段未填充） | 版本以 exporter `srs_build_info` 为准 |
| 自动播放 | `paused=true`、画面黑屏 | 页面加“播放 / 重连”按钮；点击后正常 |
| ffmpeg 容器镜像 | `linuxserver/ffmpeg` 拉取超时 | 改用宿主机 apt 安装 ffmpeg 推流 |
| EMQX 首次 pull | 管道取 rc 误判，镜像未落盘 | 重试并直接检查 `docker images`，成功 |

---

## 4. 安全与已知限制（MVP）

1. **API Key 暴露在 nginx 配置中**（Basic Base64），仅用于验证；生产必须由 BFF 持有，前端只调 BFF。
2. EMQX Dashboard 使用默认密码 `Public123456`，且 18083 对 `0.0.0.0` 暴露；生产需改密、限源 IP 或只开内网。
3. 无 TLS：全链路 HTTP；生产用 443 + 证书。
4. SRS 1985/9972 对 `0.0.0.0` 暴露；建议只在内网/监控网开放。
5. SRS 未配置 DVR/录制持久化；容器重建会丢录制文件（当前未启用录制）。
6. 门户为只读展示 + 播放；未实现踢客户端、鉴权用户增删、规则管理等写操作。
7. 测试推流为**宿主机后台 ffmpeg**（PID 文件 `mvp/ffmpeg-push.pid`），重启虚拟机后不会自动恢复；正式化应改为 compose 服务。

---

## 5. 本轮 apt 安装清单

本轮仅安装 **ffmpeg 及其依赖**（此前 Docker 已安装）：

```text
ffmpeg 7:6.1.1-3ubuntu5
```

`apt` 共 `Setting up` 78 个包（含依赖）：

```text
libgme0 libchromaprint1 libssh-gcrypt-4 libhwy1t64 libudfread0 libnorm1t64
librabbitmq4 libsodium23 libshine3 libx264-164 libmbedcrypto7t64 libgsm1
libsoxr0 libzix-0-0 libcodec2-1.2 libmysofa1 libsvtav1enc1d1 libcjson1
libxvidcore4 librav1e0 libsnappy1v5 librist4 libpgm-5.3-0t64 libblas3
libplacebo338 libva2 libdc1394-25 libunibreak5 libaacs0 libjxl0.7
pocketsphinx-en-us libfftw3-double3 libgfortran5 libx265-199 librubberband2
libsndio7.0 libbdplus0 libjack-jackd2-0 libvidstab1.1 libsrt1.5-gnutls
libflite1 libdav1d7 libva-drm2 ocl-icd-libopencl1 libvdpau1 libbs2b0
libzimg2 libopenal-data libxss1 mesa-va-drivers libbluray2 libsdl2-2.0-0
libva-x11-2 libopenmpt0t64 libzvbi-common libserd-0-0 mesa-vdpau-drivers
liblapack3 libzmq5 libzvbi0t64 libavutil58 libopenal1 libsphinxbase3t64
libass9 libswresample4 va-driver-all libavcodec60 vdpau-driver-all libsord-0-0
libpostproc57 libsratom-0-0 liblilv-0-0 libswscale7 libavformat60
libpocketsphinx3 libavfilter9 libavdevice60 ffmpeg
```

完整日志：`~/01_walle_service/mvp/apt-ffmpeg.log`

---

## 6. 文件与变更清单

均在 `~/01_walle_service/` 内：

```text
~/01_walle_service/mvp/
├── docker-compose.yml          # 新建：emqx/srs/portal 三服务
├── daemon.json                 # 新建：镜像加速（已安装到 /etc/docker/daemon.json）
├── srs/srs.conf                # 新建：http_api/http_server/exporter/rtc
├── nginx/default.conf          # 新建：同源反代 + API Key 注入
├── portal/index.html           # 新建：门户页（列表 + FLV 播放）
├── emqx_api_key.json           # 新建：API key/secret（敏感）
├── ffmpeg-push.log / .pid      # 新建：推流日志与 PID
├── samples/                    # 新建：接口返回样例
├── screenshots/                # 新建：两张页面截图
├── apt-ffmpeg.log              # 新建
├── pull-emqx.log / pull-mosquitto.log  # 新建
└── MVP_verification_report.md  # 本报告
```

系统级变更（均在已授权范围）：

- `usermod -aG docker gs`（已生效：新会话无需 sudo 可执行 docker）
- `/etc/docker/daemon.json`（registry-mirrors、日志轮转）
- apt 安装 ffmpeg
- Docker 容器/网络/卷：`mvp-emqx`、`mvp-srs`、`mvp-portal`、`mvp-mqtt-sub`（eclipse-mosquitto 订阅端）、`mvpnet`、`mvp_emqx-data`

未修改其他 `/etc` 配置、未改防火墙、未安装宿主机 nginx。

---

## 7. 待确认的 root 操作

1. 防火墙限源：18083/1985/9972 仅内网，8088 按需开放（当前均 0.0.0.0）。
2. TLS：容器 nginx 挂证书或前置 443 反代（本轮未做）。
3. 轮换 EMQX Dashboard 密码与 API Key，删除 `emqx_api_key.json` 中的明文密钥。
4. 是否保留宿主机 ffmpeg 推流进程（当前 PID 记录在 `ffmpeg-push.pid`）；建议改为 compose 服务。
5. SRS 录制/DVR 持久化（如需要，增加 volume 与 `dvr` 配置）。
6. Docker 镜像加速已配置；如需私有 registry，另行确认。

---

## 8. 复现与运维命令

```bash
cd ~/01_walle_service/mvp
docker compose up -d            # 启动 emqx/srs/portal
docker compose ps
docker compose logs -f srs

# 推流（宿主机，当前已在跑）
ffmpeg -re -f lavfi -i testsrc2=size=640x360:rate=25 -f lavfi -i sine=frequency=1000 \
  -c:v libx264 -preset veryfast -tune zerolatency -pix_fmt yuv420p -g 50 \
  -c:a aac -b:a 64k -f flv rtmp://127.0.0.1:1935/live/test

# 验证
curl -s http://127.0.0.1:1985/api/v1/streams/ | jq
curl -s -u "$KEY:$SECRET" http://127.0.0.1:18083/api/v5/clients | jq
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8088/
```

停止：`docker compose down`（保留 `emqx-data` 卷；如需彻底清理再加 `-v`）。

---

## 9. 证据文件

- 接口样例：`~/01_walle_service/mvp/samples/*.json`
- 页面截图：`~/01_walle_service/mvp/screenshots/*.jpg`
- 推流日志：`~/01_walle_service/mvp/ffmpeg-push.log`
- 容器日志：`docker logs mvp-emqx | mvp-srs | mvp-portal`
