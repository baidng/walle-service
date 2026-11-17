# 裸机部署验证报告（EMQX Enterprise 6.3.2 + SRS 7.0.215 + 门户）

- 主机：`gs@192.168.31.114`（Ubuntu 24.04.5，aarch64，8C/15G）
- 时间：2026-11-15
- 状态：**完成**。EMQX 6.3.2（deb）、SRS 7.0.215（源码）、Nginx 门户（8088）均以裸机方式运行，接口与 FLV 播放实测通过。

---

## 0. 结论与访问入口

| 入口 | 地址 | 说明 |
|---|---|---|
| 统一门户 | http://192.168.31.114:8088/ | EMQX 客户端/订阅 + SRS 流 + FLV 播放 |
| EMQX Dashboard | http://192.168.31.114:18083/ | `admin` / `public`（默认，需改） |
| SRS Console | http://192.168.31.114:8080/console/ | 原生控制台 |
| SRS Player | http://192.168.31.114:8080/players/srs_player.html?app=live&stream=test | 官方播放页 |
| SRS API | http://192.168.31.114:1985/api/v1/ | 流/客户端 |
| SRS Exporter | http://192.168.31.114:9972/metrics | Prometheus |
| 测试流 | `rtmp://192.168.31.114:1935/live/test` | ffmpeg 持续推流 |

关键事实：

1. EMQX 6.3.2 **有 arm64 deb**，但不在 apt 源里，也不在 GitHub Release 的 assets 中；实际下载地址挂在 release 正文，指向 `www.emqx.com/downloads/enterprise/6.3.2/`。
2. 无 license 文件时企业版**可正常启动**，内置 `community/Development` 授权（10,000,000 连接、到期 2029-03-01），并非生产 license。
3. 此前 `assets=0` 的结论源于 GitHub API 只统计上传资产；页面上的 `.deb/.tar.gz` 是外部链接，故 API 为 0。
4. 浏览器直连 EMQX API 仍无 CORS，门户必须同源反代（已实现）。

---

## 1. EMQX 安装与来源

### 1.1 下载地址与校验

```text
页面：https://github.com/emqx/emqx/releases/tag/6.3.2
直链：https://www.emqx.com/downloads/enterprise/6.3.2/emqx-enterprise-6.3.2-ubuntu24.04-arm64.deb
实际 302 → https://packages.emqx.io/emqx-ee/6.3.2/emqx-enterprise-6.3.2-ubuntu24.04-arm64.deb
大小：87,984,724 bytes（84M）
sha256（官方）：ea5252d8f3979ecef714ee1d570558c6f07bad61d18d55d8ebd989243790d717
sha256（实测）：ea5252d8f3979ecef714ee1d570558c6f07bad61d18d55d8ebd989243790d717  ✅ 一致
```

包信息：

```text
Package: emqx-enterprise
Version: 6.3.2
Architecture: arm64
Installed-Size: 132312 KB
Depends: libc6 (>= 2.39), libgcc-s1, libkrb5-3, libodbc2, libsasl2-2,
         libssl3t64 (>= 3.0.3), libstdc++6 (>= 13.1), libtinfo6, zlib1g
```

### 1.2 安装

```bash
cd ~/01_walle_service/walle-service
sudo apt install -y ./emqx-enterprise-6.3.2-ubuntu24.04-arm64.deb
sudo systemctl start emqx       # 安装后服务为 inactive，需手动启动
```

- 服务：`active (running)`，`enabled`（deb 自带 `emqx.service`，未额外编写 unit）。
- 进程：`emqx@127.0.0.1`，beam.smp，内存约 52MB（启动初期）。
- 警告：默认 Erlang cookie 不安全（`WARNING: Default (insecure) Erlang cookie is in use`），需后续在 `/etc/emqx/emqx.conf` 修改。

### 1.3 License 行为（未提供 license）

`GET /api/v5/license` 实际返回：

```json
{"type":"community","max_connections":10000000,"email":"contact@emqx.io",
 "max_sessions":10000000,"start_at":"2025-03-02","customer_type":11,
 "max_tps":"infinity","customer":"Developer","deployment":"Development",
 "expiry":false,"expiry_at":"2029-03-01"}
```

登录响应中 `"license":{"edition":"ee"}`，`/api/v5/nodes` 的 `edition` 为 `Enterprise`。即：**无 license 也可启动，走内置开发/社区授权**；生产需替换为正式 license。

---

## 2. 端口规划（实测监听）

| 端口 | 用途 | 进程 |
|---|---|---|
| 1883 | EMQX MQTT | emqx |
| 8883 | EMQX MQTT/TLS | emqx |
| 8083/8084 | EMQX MQTT over WS/WSS | emqx |
| 18083 | EMQX Dashboard/REST | emqx |
| 1935 | SRS RTMP | srs |
| 1985 | SRS HTTP API | srs |
| 8080 | SRS HTTP Server/console/FLV | srs |
| 9972 | SRS exporter | srs |
| 8000/udp | SRS WebRTC | srs |
| 8088 | 门户（独立 nginx 实例） | nginx (gs) |
| 80 | 系统 nginx 默认站点 | nginx (system) |

---

## 3. 接口实测

### 3.1 EMQX（API Key 鉴权）

```text
GET /api/v5/clients?limit=2
{"count":1,"data":[{"clientid":"r8HN348LDzLzZtZx","proto_name":"MQTT",
 "connected":true,"ip_address":"127.0.0.1"}]}

GET /api/v5/subscriptions?limit=2
{"count":1,"data":[{"clientid":"r8HN348LDzLzZtZx","topic":"demo/#","qos":1}]}

GET /api/v5/nodes
[{"node":"emqx@127.0.0.1","version":"6.3.2","edition":"Enterprise","node_status":"running"}]

GET /api/v5/metrics
{"node":"emqx@127.0.0.1","fields":113}
```

API Key：本次部署创建（scopes 含 connections/publish/monitoring 等），保存在 `etc/emqx_api_key.json`（0600）。

### 3.2 SRS

```text
GET /api/v1/streams/ → 200
{"total":1,"streams":[{"name":"test","app":"live","clients":2}]}

GET /api/v1/clients/ → 200（fmle-publish + flv-play）

GET /metrics → 200
srs_build_info{... version="7.0.215", code="Kai"} 1

GET /live/test.flv → 200，Content-Type: video/x-flv
```

### 3.3 门户（8088，同源反代）

```text
GET /                         → 200
GET /emqx-api/clients         → 200 {"count":1}
GET /emqx-api/subscriptions   → 200 {"count":1}
GET /emqx-api/nodes           → 200
GET /srs-api/streams/         → 200 {"total":1,...}
GET /srs/live/test.flv        → 200 video/x-flv
```

浏览器实测：EMQX 客户端 1、订阅 1、节点 running；SRS 流 live/test；点击“播放 / 重连”后出现彩条测试图案并持续播放。

截图（已复制到 VM）：

```text
docs/screenshots/srs_play_top.jpg   （EMQX/SRS 概览 + 播放画面）
docs/screenshots/srs_play.jpg       （播放器区域）
```

---

## 4. 与容器版的差异

| 维度 | 容器版 | 裸机版 |
|---|---|---|
| EMQX 安装 | `emqx/emqx:6.3.2` 镜像 | `emqx-enterprise-6.3.2-ubuntu24.04-arm64.deb` |
| EMQX 启动 | compose restart 策略 | systemd `emqx.service`（deb 自带） |
| EMQX 配置 | compose 环境变量 | `/etc/emqx/emqx.conf` + `/var/lib/emqx` |
| SRS 启动 | compose 服务 | `nohup ./objs/srs -c ...`（无 systemd） |
| SRS 依赖 | 镜像内置 | 自行编译（gcc/g++/make、automake、cmake、tcl、libssl-dev、zlib1g-dev、pkg-config） |
| 门户 | `nginx:1.27-alpine` 容器 | 宿主机 nginx 独立实例（自定义 prefix，监听 8088） |
| 端口 | compose 映射 | 直接监听宿主机 |
| 进程守护 | Docker restart | EMQX 有 systemd；SRS/门户/ffmpeg 无守护 |
| 升级 | 拉新镜像 | 重新下载 deb / 重新编译 |

---

## 5. 安装清单（本轮，包名+版本）

| 包 | 版本 | 来源 |
|---|---|---|
| emqx-enterprise | 6.3.2（arm64） | emqx.com → packages.emqx.io（deb） |
| nginx | 1.24.0-2ubuntu7.19 | Ubuntu 官方源 |
| nginx-common | 1.24.0-2ubuntu7.19 | Ubuntu 官方源 |
| build-essential | 12.10ubuntu1 | Ubuntu 官方源（SRS 编译） |
| pkg-config | 1.8.1-2build1 | Ubuntu 官方源 |
| libssl-dev | 3.0.13-0ubuntu3.16 | Ubuntu 官方源 |
| zlib1g-dev | 1:1.3.dfsg-3.1ubuntu2.3 | Ubuntu 官方源 |
| automake | 1:1.16.5-1.3ubuntu1 | Ubuntu 官方源 |
| cmake | 3.28.3-1build7 | Ubuntu 官方源 |
| tcl | 8.6.14build1 | Ubuntu 官方源 |

日志：`apt-emqx-install.log`、`apt-build-deps*.log` 等（首次部署时的历史记录）。

---

## 6. SRS 编译与运行（裸机）

```text
源码：~/01_walle_service/walle-service/thirdparty/srs（v7.0-a4）
配置：cd trunk && ./configure --srt=on
编译：make -j8（make.rc=0）
产物：trunk/objs/srs = 30M；trunk/objs = 318M
版本：7.0.215
运行：ulimit -n 10000；nohup ./objs/srs -c ~/01_walle_service/walle-service/etc/srs.conf &
PID：73637（日志 srs.log）
```

`max_connections 800`，避免再次触发容器版遇到的 `ulimit 1024` 限制。

---

## 7. 文件/进程

```text
~/01_walle_service/walle-service/
├── emqx-enterprise-6.3.2-ubuntu24.04-arm64.deb   # 84M，sha256 已校验（部署后已删除）
├── etc/emqx_api_key.json                          # 0600
├── thirdparty/srs/                                # 源码 + objs
├── etc/srs.conf
├── etc/nginx.conf                                 # 独立实例，8088
├── src/web/index.html                             # 适配后的门户
├── log/{error.log,access.log}                     # 门户日志
├── .run/nginx/{nginx.pid,temp/}
├── docs/screenshots/{srs_play_top.jpg,srs_play.jpg}
├── configure.log / make.log / make.rc
├── srs.log / srs.pid
├── apt-emqx-install.log
└── docs/deployment_report.md
```

进程：

```text
EMQX：systemd emqx.service（emqx@127.0.0.1）
SRS：73637  ./objs/srs -c .../srs.conf
ffmpeg：73747  推流 rtmp://127.0.0.1:1935/live/test
门户 nginx：80260  nginx -p ~/01_walle_service/walle-service -c ~/01_walle_service/walle-service/etc/nginx.conf
系统 nginx：49881（80 默认站点）
```

---

## 8. 失败项/坑与处理

| 问题 | 说明 | 处理 |
|---|---|---|
| apt 源无 6.x | `packages.emqx.com` 的 noble/jammy/focal 索引最高 5.8.9，无 6.x | 改用 emqx.com 下载页的 deb |
| GitHub API assets=0 | Release 资产是正文里的外部链接，不是上传资产 | 从 release 页面解析直链 |
| deb 安装后服务未启动 | postinst 只 enable，不 start | `systemctl start emqx` |
| 默认 Erlang cookie 警告 | 启动日志提示不安全 | 待改 `/etc/emqx/emqx.conf` |
| 无 license | 不阻断启动，内置 community/Development 授权 | 生产需正式 license |
| 浏览器 CORS | 直连 18083 无 ACAO | 门户同源反代（8088） |
| 自动播放 | 浏览器拦截，需点击 | 页面“播放 / 重连”按钮 |
| SRS ulimit | 上次容器因 1024 限制失败 | 启动前 `ulimit -n 10000` |

---

## 9. 待确认项

1. SRS / 门户 nginx / ffmpeg 的 systemd 守护（需 root，当前为 nohup/后台进程）。
2. 防火墙限源：18083/1985/9972/8088 仅内网。
3. TLS：门户与 EMQX/SRS API 走 HTTPS。
4. 修改 EMQX 默认密码 `public`、Erlang cookie、轮换 API Key。
5. 生产 license 导入（当前为 Development 授权）。
6. 是否保留 `bare-mqtt-sub` 测试容器（host 网络订阅端）。

---

## 10. 收尾变更（2026-11-15 晚）

- SRS 由 nohup 改为 `srs.service`（`User=gs`、`LimitNOFILE=10000`、`Restart=on-failure`），`enable --now` 成功；`systemctl restart srs` 后 `/api/v1/streams/` 仍 200。
- 门户改为手动脚本 `scripts/start-walle.sh`（start/stop/status），实测 stop→200 失败、start→200 成功；当时未做 systemd。
- Docker 镜像全部删除：清理前 6 个镜像（10 个 tag，约 922.4MB），清理后 0 镜像、0 容器；保留 6 个本地卷（约 331KB）与 Docker 引擎。
- 新增无依赖 MQTT 测试客户端 `src/backend/mqtt_demo_client.py`（替代已删除的 mosquitto 容器），保持 1 个客户端与 `demo/#` 订阅。
- 新增 `~/01_walle_service/walle-service/README.md`（541 行，14 章），面向新机器从零部署。
