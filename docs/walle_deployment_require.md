# walle 服务端部署文档（裸机）

> 适用环境：Ubuntu 24.04（aarch64/arm64），单机裸机部署。
> 组件：EMQX Enterprise 6.3.2 + SRS 7.0.215 + Nginx 门户（walle-ai）。
> 最后验证：2026-11-15 / 门户登录改造 2026-11-16。
> 架构与业务实现细节见 [walle_detiled_design.md](./walle_detiled_design.md)。

---

## 1. 架构与端口

```text
浏览器
  │ http://<host>:8088
  ▼
Nginx（独立实例，由 walle.service 托管或手动启动）
  ├── /              → 静态门户 src/web/
  ├── /api/          → 127.0.0.1:9000            （用户管理 API，SQLite + EMQX）
  ├── /auth/login    → 127.0.0.1:18083/api/v5/login （用户凭据校验）
  ├── /emqx-api/     → 127.0.0.1:18083/api/v5/   （反代注入 API Key）
  ├── /srs-api/      → 127.0.0.1:1985/api/v1/
  ├── /srs/          → 127.0.0.1:8080/            （console/players/FLV/HLS）
  └── /metrics       → 127.0.0.1:9972/metrics
        ▲                          ▲
        │                          │
  EMQX Enterprise 6.3.2       SRS 7.0.215
  （systemd: emqx.service）   （systemd: srs.service）
```

| 组件 | 端口 | 用途 | 建议对外 |
|---|---|---|---|
| Nginx 门户 | 8088/tcp | 统一入口 | 按需（内网/办公网） |
| EMQX | 18083/tcp | Dashboard / REST API | 仅内网 |
| EMQX | 1883/tcp | MQTT | 按业务 |
| EMQX | 8883/tcp | MQTT over TLS | 按业务 |
| EMQX | 8083/8084/tcp | MQTT over WS/WSS | 按业务 |
| SRS | 1935/tcp | RTMP 推流 | 按业务 |
| SRS | 1985/tcp | HTTP API | 仅内网 |
| SRS | 8080/tcp | HTTP Server / console / FLV / HLS | 按业务 |
| SRS | 8000/udp+tcp | WebRTC | 按业务 |
| SRS | 9972/tcp | Prometheus exporter | 仅监控网 |
| 系统 Nginx | 80/tcp | apt 默认站点（未使用） | 可关闭 |

---

## 2. 前置条件

- Ubuntu 24.04 LTS，arm64（本手册在 24.04.5 aarch64 验证）。
- 建议 4C/8G 以上；磁盘至少 3GB（EMQX 安装约 130MB，SRS 源码+objs 约 460MB，临时 deb 84MB 安装后可删）。
- 具有 sudo 权限的普通用户（本文以 `gs` 为例）。
- 可访问 `www.emqx.com`、`packages.emqx.io`、`github.com`。
- 端口 8088/18083/1883/8883/8083/8084/1935/1985/8080/8000/9972 未被占用。

---

## 3. 目录规划

```text
/home/gs/01_walle_service/walle-service/
├── docs/                              # 文档，部署不需要
│   ├── walle_deployment_require.md    # 本文（部署文档）
│   ├── walle_detiled_design.md        # 架构、登录、MQTT、控制台设计
│   ├── deployment_report.md           # 首次部署验证报告
│   ├── MVP_verification_report.md
│   ├── EMQX_SRS_VM_recon_report.md
│   ├── version_record.md
│   ├── screenshots/                   # 验证截图
│   └── samples/                       # 接口返回样例
├── etc/                               # 运行期配置
│   ├── nginx.conf.example             # 门户反代模板（入库）
│   ├── nginx.conf                     # 由模板复制并填真实 Key（不入库）
│   ├── srs.conf                       # SRS 运行配置（被 srs.service 引用）
│   ├── srs.service                    # SRS 的 systemd 单元源文件
│   ├── walle.conf.example             # 门户保密配置模板（入库）
│   ├── walle.conf                     # 真实密钥，权限 600（不入库）
│   └── emqx_api_key.json              # EMQX API Key（不入库）
├── scripts/
│   ├── start-walle.sh                 # 门户启动/状态脚本
│   ├── stop-walle.sh                  # 门户手动停止脚本
│   ├── deploy.sh                      # 部署脚本：校验 walle.conf → 渲染 walle.service → 安装到系统目录
│   ├── walle.service.example          # systemd 单元模板（入库）
│   └── walle.service                  # 渲染产物，绝对路径随机器而定（不入库）
├── src/
│   ├── web/                           # 门户页面：index/home/admin/user/dashboard.html
│   └── backend/                       # 后端：walle_api.py（用户管理 API）、mqtt_demo_client.py
│       └── data/users.db              # 用户角色库（SQLite，运行后自动生成，不入库）
├── tests/                             # 测试
├── thirdparty/
│   └── srs/                           # SRS 源码（v7.0-a4，约 460MB，不入库，按 7.1 自行 clone）
│       └── trunk/objs/srs             # 编译产物
├── log/                               # 门户日志（access.log/error.log，已 gitignore）
├── .run/nginx/                        # nginx pid 与临时文件（nginx.pid、temp/，已 gitignore）
├── README.md                          # 系统简介（面向用户）
└── LICENSE
```

说明：

- `docs/` 仅存放文档、验证报告、截图和接口样例，供查阅/审计，**部署不需要**，可整体删除。
- `thirdparty/srs/` 是 SRS 官方源码仓库，含 `.git`、体量大，因此不做版本管理；SRS 运行配置独立放在 `etc/srs.conf`，避免随源码目录一起被忽略。
- 门户日志写入仓库根的 `log/`（`access.log`、`error.log`），pid 与临时文件在 `.run/nginx/`，两者均已加入 `.gitignore`。
- EMQX 安装在系统目录（`/etc/emqx`、`/var/lib/emqx`），SRS 的 systemd 单元安装到 `/etc/systemd/system/srs.service`；这两处不在本仓库内。
- 安装用的 EMQX `.deb` 已在清理时删除，部署时按 6.1 的 URL 重新下载即可。

---

## 4. 上云必改项（换机器先看）

> 本节的值来自当前环境（`192.168.31.114`、用户 `gs`、路径 `/home/gs/...`）。直接复制到云主机前必须逐项核对；不改会影响 WebRTC 播放或直接导致启动失败。

### 4.1 SRS WebRTC 候选地址（必改）

`etc/srs.conf` 当前值：

```hocon
rtc_server {
  enabled on;
  listen 8000;
  candidate 192.168.31.114;
  tcp {
    enabled on;
    listen 8000;
  }
}
```

- **必须改**：`candidate` 改为云主机的公网 IP / EIP（NAT 环境填公网地址，不要填内网地址）。
- **不改的后果**：SDP answer 中携带的是 `192.168.31.114`，公网浏览器 ICE 无法连通，WebRTC（WHEP/WHIP、`/players/rtc_player.html`）会黑屏或超时；HTTP-FLV、HLS、RTMP 推流不受影响。
- **端口**：`listen 8000` 保持不变；安全组/防火墙需放行 **UDP 8000**。当前配置还启用了 `rtc_server.tcp`，因此 **TCP 8000** 也要放行；若只保留 UDP，可把 `tcp { enabled on; }` 改为 `off`。
- **多网卡/多出口**：SRS 支持配置多条 `candidate`，至少保留一条公网地址。

### 4.2 其他写死值（逐条核对）

| 文件 | 当前实际值 | 换机器时怎么改 |
|---|---|---|
| `etc/srs.conf` | `candidate 192.168.31.114;`（第 30 行） | 改为公网 IP/EIP |
| `etc/srs.conf` | `exporter { label ...; tag ...; }` 当前为部署环境标识 | 非必须；多环境时改成可区分名字 |
| `/etc/systemd/system/srs.service`（源文件 `etc/srs.service`） | `User=gs`、`WorkingDirectory=/home/gs/01_walle_service/walle-service/thirdparty/srs/trunk`、`ExecStart=.../thirdparty/srs/trunk/objs/srs -c .../etc/srs.conf` | 用户名或路径不同则三处全部替换，再 `daemon-reload` |
| `etc/nginx.conf`（由 `nginx.conf.example` 复制） | `pid`/`error_log`/`access_log`/`temp`/`root` 均为相对仓库根的路径（`.run/nginx/...`、`log/...`、`src/web`） | 无需修改；仓库整体移动后仍可用 |
| `etc/nginx.conf`（由 `nginx.conf.example` 复制） | `proxy_set_header Authorization "Basic <真实 Key>"`（第 53 行） | 重新生成 API Key 并替换（见 4.3） |
| `scripts/start-walle.sh` | 路径由脚本位置推导 `REPO_ROOT` | 无需修改；仓库整体移动后仍可用 |
| `src/backend/mqtt_demo_client.py` | `HOST = "127.0.0.1"` | 仅测试用；远程测试时改成 broker 地址 |

> 门户页面已全部改为相对路径，`/auth/login` 也由同源反代提供，因此换 IP/域名时**无需修改任何 HTML**。

### 4.3 EMQX API Key（必须重新生成，不要沿用）

`etc/nginx.conf` 由模板 `etc/nginx.conf.example` 复制而来，模板里是占位符；**真实 Key 只存在于本机的 `etc/nginx.conf`，该文件已加入 `.gitignore`，不会入库**。换机器/上线时按下面步骤重建：

1. 登录 EMQX Dashboard（`http://<host>:18083`）→ **Access Control → API Keys → Create**，按需勾选权限（门户读取用 `connections`、`monitoring` 等）。
2. 生成 Basic 值：

```bash
KEY=<新 api_key>
SECRET=<新 api_secret>
printf '%s:%s' "$KEY" "$SECRET" | base64 -w0
```

3. 复制模板（首次部署），把结果替换 `etc/nginx.conf` 中 `Authorization "Basic ..."` 的整段值：

```bash
cp etc/nginx.conf.example etc/nginx.conf
```

4. 重启门户使配置生效：

```bash
./scripts/stop-walle.sh
./scripts/start-walle.sh start
```

> 不要把含真实 Key 的 `nginx.conf` 改名后提交或发给他人；泄露后立即在 Dashboard 删除该 Key 并重建。若误提交了 `nginx.conf`，光删文件不够，必须轮换 Key。

### 4.4 默认口令与 Cookie（必须改）

- Dashboard 默认 `admin` / `public`：首次登录后立即在用户管理里修改密码。门户登录复用的是这套 EMQX 账号，改密后门户同步生效。
- Erlang cookie：`/etc/emqx/emqx.conf` 中的 `node.cookie` 也是默认值，建议改成随机串后 `sudo systemctl restart emqx`。
- 默认口令未改时，任何能访问 18083 的人都能拿到管理权限。

### 4.5 测试进程不在 systemd 内

`ffmpeg` 测试推流与 `mqtt_demo_client.py` 都是手动 `nohup` 启动，主机重启后不会自动恢复；需要按第 9 节命令重新拉起（生产环境应改为 systemd 或不用测试流）。

### 4.6 换机器检查清单

```bash
cd /home/gs/01_walle_service/walle-service

# 1) WebRTC 候选地址
grep -nE 'candidate|listen 8000' etc/srs.conf

# 2) 所有残留内网 IP（应只剩你确认过的项）
grep -Rn '192\.168\.' etc/srs.conf etc/nginx.conf src/web/

# 3) 绝对路径与真实凭据
grep -nE '/home/gs|Authorization' etc/nginx.conf
grep -nE 'User=|WorkingDirectory=|ExecStart=' /etc/systemd/system/srs.service

# 4) 确认公网出口 IP / EIP
curl -s https://ifconfig.me; echo
```

---

## 5. 系统依赖

```bash
sudo apt-get update
sudo apt-get install -y \
  curl jq git nginx build-essential pkg-config libssl-dev zlib1g-dev \
  automake cmake tcl ffmpeg
```

实测版本（Ubuntu 24.04 arm64）：

| 包 | 版本 |
|---|---|
| curl | 8.5.0-2ubuntu10.16 |
| jq | 1.7.1-3ubuntu0.24.04.2 |
| git | 1:2.43.0-1ubuntu7.4 |
| nginx / nginx-common | 1.24.0-2ubuntu7.19 |
| build-essential | 12.10ubuntu1 |
| pkg-config | 1.8.1-2build1 |
| libssl-dev | 3.0.13-0ubuntu3.16 |
| zlib1g-dev | 1:1.3.dfsg-3.1ubuntu2.3 |
| automake | 1:1.16.5-1.3ubuntu1 |
| cmake | 3.28.3-1build7 |
| tcl | 8.6.14build1 |
| ffmpeg | 7:6.1.1-3ubuntu5 |

---

## 6. EMQX Enterprise 6.3.2

### 6.1 下载与校验

```bash
mkdir -p /tmp/emqx-deb
cd /tmp/emqx-deb

curl -L -o emqx-enterprise-6.3.2-ubuntu24.04-arm64.deb \
  https://www.emqx.com/downloads/enterprise/6.3.2/emqx-enterprise-6.3.2-ubuntu24.04-arm64.deb

echo "ea5252d8f3979ecef714ee1d570558c6f07bad61d18d55d8ebd989243790d717  emqx-enterprise-6.3.2-ubuntu24.04-arm64.deb" | sha256sum -c -
```

> 直链会 302 到 `https://packages.emqx.io/emqx-ee/6.3.2/...`；文件约 84MB。
>
> 安装包不长期保留：当前环境已在清理时删除该 deb（目录树中也没有它）。重复部署时按上面的 URL 重新下载并校验 sha256；安装完成后可直接删除 deb 释放约 84MB。

### 6.2 安装与启动

```bash
cd /tmp/emqx-deb
sudo apt install -y ./emqx-enterprise-6.3.2-ubuntu24.04-arm64.deb
sudo systemctl enable --now emqx
systemctl status emqx --no-pager
ss -ltn | grep -E ':(18083|1883|8883|8083|8084)\b'
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18083/
```

> 注意：deb 的 postinst 只 `enable`，不会自动 `start`，必须显式启动。

### 6.3 登录与 API Key

默认账号 `admin` / `public`（首次部署后请立即修改）。

```bash
cd /home/gs/01_walle_service/walle-service

TOKEN=$(curl -s -X POST http://127.0.0.1:18083/api/v5/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"public"}' | jq -r .token)

curl -s -X POST http://127.0.0.1:18083/api/v5/api_key \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"portal","desc":"portal readonly","enable":true}' \
  | tee etc/emqx_api_key.json

chmod 600 etc/emqx_api_key.json
```

文件中的 `api_key` / `api_secret` 用于 Nginx 注入（见第 8 节）。

### 6.4 License 说明

未安装 license 时服务可正常启动，内置开发/社区授权：

```json
{"type":"community","max_connections":10000000,"max_sessions":10000000,
 "customer":"Developer","deployment":"Development","expiry":false,
 "expiry_at":"2029-03-01"}
```

`/api/v5/nodes` 返回 `edition: Enterprise`。生产环境需替换为正式 license。

---

## 7. SRS 7.0.215

### 7.1 编译

```bash
cd /home/gs/01_walle_service/walle-service

git clone --depth 1 --branch v7.0-a4 https://github.com/ossrs/srs.git \
  thirdparty/srs

cd thirdparty/srs/trunk
./configure --srt=on
make -j"$(nproc)"
./objs/srs -v
```

产物：

```text
trunk/objs/srs                 约 30MB
trunk/objs/                    约 318MB（含 console/players）
trunk/objs/nginx/html/console  原生控制台
```

### 7.2 运行配置 `etc/srs.conf`

> SRS 源码目录 `thirdparty/srs/` 只放源码与编译产物；运行配置独立放在 `etc/srs.conf`，由 systemd 的 `-c` 参数指定。

```hocon
listen 1935;
max_connections 800;
daemon off;
srs_log_tank console;
srs_log_level trace;

http_api {
  enabled on;
  listen 1985;
  crossdomain on;
}

http_server {
  enabled on;
  listen 8080;
  dir ./objs/nginx/html;
  crossdomain on;
}

exporter {
  enabled on;
  listen 9972;
  label <环境标识>;
  tag <环境标识>;
}

rtc_server {
  enabled on;
  listen 8000;
  candidate 192.168.31.114;
  tcp {
    enabled on;
    listen 8000;
  }
}

vhost __defaultVhost__ {
  http_remux {
    enabled on;
    mount [vhost]/[app]/[stream].flv;
  }
  hls {
    enabled on;
    hls_fragment 2;
    hls_window 10;
  }
  rtc {
    enabled on;
    rtmp_to_rtc on;
  }
}
```

### 7.3 systemd 单元 `/etc/systemd/system/srs.service`

```ini
[Unit]
Description=SRS 7.0.215 media server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=gs
Group=gs
WorkingDirectory=/home/gs/01_walle_service/walle-service/thirdparty/srs/trunk
ExecStart=/home/gs/01_walle_service/walle-service/thirdparty/srs/trunk/objs/srs -c /home/gs/01_walle_service/walle-service/etc/srs.conf
Restart=on-failure
RestartSec=5
LimitNOFILE=10000

[Install]
WantedBy=multi-user.target
```

安装：

```bash
sudo install -m 0644 etc/srs.service /etc/systemd/system/srs.service
sudo systemctl daemon-reload
sudo systemctl enable --now srs
systemctl status srs --no-pager
```

> `max_connections` 必须小于 `LimitNOFILE`；本配置为 800 / 10000。

---

## 8. 门户 Nginx（8088）

### 8.1 配置 `etc/nginx.conf`

仓库里只提交模板 `etc/nginx.conf.example`，首次部署先复制并填入真实 Key：

```bash
cp etc/nginx.conf.example etc/nginx.conf
```

模板内容如下（`Authorization` 行为占位符）：
```nginx
worker_processes 1;
pid .run/nginx/nginx.pid;
error_log log/error.log warn;

events { worker_connections 1024; }

http {
    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    access_log log/access.log;

    client_body_temp_path .run/nginx/temp/client_body;
    proxy_temp_path       .run/nginx/temp/proxy;
    fastcgi_temp_path     .run/nginx/temp/fastcgi;
    uwsgi_temp_path       .run/nginx/temp/uwsgi;
    scgi_temp_path        .run/nginx/temp/scgi;

    server {
        listen 8088;
        server_name _;

        root src/web;
        index index.html;

        location / { try_files $uri $uri/ /index.html; }

        location /api/ {
            proxy_pass http://127.0.0.1:9000;
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_read_timeout 30s;
        }

        location = /auth/login {
            proxy_pass http://127.0.0.1:18083/api/v5/login;
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_set_header Content-Type application/json;
            proxy_read_timeout 60s;
        }

        location /emqx-api/ {
            proxy_pass http://127.0.0.1:18083/api/v5/;
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_set_header Authorization "Basic <BASE64(api_key:api_secret)>";
            proxy_read_timeout 60s;
        }

        location /srs-api/ {
            proxy_pass http://127.0.0.1:1985/api/v1/;
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_read_timeout 60s;
        }

        location /srs/ {
            proxy_pass http://127.0.0.1:8080/;
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_buffering off;
            proxy_read_timeout 3600s;
        }

        location = /metrics {
            proxy_pass http://127.0.0.1:9972/metrics;
        }
    }
}
```

生成 `Authorization` 值：

```bash
KEY=$(jq -r .api_key etc/emqx_api_key.json)
SECRET=$(jq -r .api_secret etc/emqx_api_key.json)
printf '%s:%s' "$KEY" "$SECRET" | base64 -w0
```

把结果替换 `<BASE64(api_key:api_secret)>`。

> `/auth/login` 不能注入 API Key，否则 EMQX 会用管理凭据而不是用户提交的凭据做校验；也不要给它加认证 Bypass 之外的缓存。

### 8.2 启动脚本 `scripts/start-walle.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

# 路径全部从脚本自身位置推导；仓库整体移动后无需修改
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$BASE_DIR/.." && pwd)"
CONF="$REPO_ROOT/etc/nginx.conf"
RUN_DIR="$REPO_ROOT/.run/nginx"
LOG_DIR="$REPO_ROOT/log"
PID_FILE="$RUN_DIR/nginx.pid"
PORTAL_URL="http://127.0.0.1:8088/"

case "${1:-start}" in
  start)
    if [ ! -f "$CONF" ]; then
      echo "missing $CONF, copy etc/nginx.conf.example to etc/nginx.conf first" >&2
      exit 1
    fi
    mkdir -p "$LOG_DIR" "$RUN_DIR/temp/client_body" "$RUN_DIR/temp/proxy" "$RUN_DIR/temp/fastcgi" "$RUN_DIR/temp/uwsgi" "$RUN_DIR/temp/scgi"
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "portal already running (pid $(cat "$PID_FILE"))"
      exit 0
    fi
    rm -f "$PID_FILE"
    nginx -p "$REPO_ROOT" -c "$CONF"
    echo "portal started (pid $(cat "$PID_FILE")), $PORTAL_URL"
    ;;
  stop)
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      PID="$(cat "$PID_FILE")"
      kill -QUIT "$PID"
      for _ in $(seq 1 25); do
        kill -0 "$PID" 2>/dev/null || break
        sleep 0.2
      done
      rm -f "$PID_FILE"
      echo "portal stopped"
    else
      rm -f "$PID_FILE"
      echo "portal not running"
    fi
    ;;
  status)
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$PORTAL_URL" || true)
    echo "portal http=$code"
    ;;
  *)
    echo "usage: $0 {start|stop|status}"
    exit 1
    ;;
esac
```

```bash
chmod +x scripts/start-walle.sh scripts/stop-walle.sh
./scripts/start-walle.sh start
./scripts/start-walle.sh status
./scripts/stop-walle.sh
```

> 门户既可用 `scripts/start-walle.sh` 手动启停，也可由 `walle.service` 托管（见 8.3）；Nginx 以 `gs` 用户运行，监听 8088（非特权端口）。

### 8.3 walle.conf 与 walle.service（部署脚本）

`etc/walle.conf` 存放门户保密配置（`EMQX_API_KEY` / `EMQX_API_SECRET`），权限 600，已加入 `.gitignore`，**不入库**。首次部署从模板复制并填写：

```bash
cp etc/walle.conf.example etc/walle.conf
chmod 600 etc/walle.conf
# 编辑 etc/walle.conf，把占位值替换为真实 api_key / api_secret
```

`scripts/deploy.sh` 依次完成 3 件事：

1. 校验 `etc/walle.conf` 是否存在、非空，且 `EMQX_API_KEY` / `EMQX_API_SECRET` 已填入真实值（拒绝占位符）。
2. 以 `scripts/walle.service.example` 为模板，渲染出 `scripts/walle.service`（替换仓库路径与服务用户）。
3. 用 `sudo` 把 `walle.conf` 安装到 `/etc/walle/walle.conf`（权限 600）、`walle.service` 安装到 `/etc/systemd/system/walle.service`（权限 644），并执行 `systemctl daemon-reload`，最后打印关键信息。

用法：

```bash
./scripts/deploy.sh
sudo systemctl enable --now walle
```

生成的 `walle.service` 为 `Type=forking`，`EnvironmentFile=/etc/walle/walle.conf`，`ExecStart` 调用仓库内的 `scripts/start-walle.sh start`，`PIDFile` 指向仓库根的 `.run/nginx/nginx.pid`。该服务只托管门户（nginx），SRS 仍由 `srs.service` 管理。

常用操作：

```bash
systemctl status walle --no-pager
sudo systemctl stop walle
sudo journalctl -u walle -n 50 --no-pager
```

> 门户可以由 `walle.service` 托管，也可以直接手动执行 `./scripts/start-walle.sh start|status` 与 `./scripts/stop-walle.sh`（例如调试时）。

### 8.4 用户管理 API（SQLite + EMQX）

`src/backend/walle_api.py` 是零依赖的 Python 标准库服务，监听 `127.0.0.1:9000`，由 nginx 的 `/api/` 反代进来。

- **用户名**：创建后不可修改，同时作为 EMQX 内置数据库的 `user_id`。
- **密码**：只保存在 EMQX（sha256 + suffix 加盐），walle 不存密码。
- **角色**：`客户` / `设备` 二选一，保存在 `src/backend/data/users.db`（SQLite 单文件，自动创建）；两种角色在 EMQX 侧都不勾选「是否为超级用户」（`is_superuser=false`），权限由 ACL 控制。
- **接口**：`GET/POST /api/users`、`PUT/DELETE /api/users/{用户名}`。

启停已合并进门户脚本：`start-walle.sh` 会同时拉起 nginx 与 API，`stop-walle.sh` 会同时停止两者；API 日志在 `log/walle-api.log`，pid 在 `.run/nginx/walle-api.pid`。

> SQLite 直接使用 Python 内置模块，不需要安装 MySQL/PostgreSQL；备份只需拷贝 `src/backend/data/users.db`。

---

## 9. 测试推流与 MQTT 客户端

```bash
nohup ffmpeg -hide_banner -loglevel warning -re \
  -f lavfi -i testsrc2=size=640x360:rate=25 \
  -f lavfi -i sine=frequency=1000 \
  -c:v libx264 -preset veryfast -tune zerolatency -pix_fmt yuv420p -g 50 \
  -c:a aac -b:a 64k -f flv rtmp://127.0.0.1:1935/live/test \
  > ffmpeg-push.log 2>&1 &
echo $! > ffmpeg-push.pid
```

无依赖 MQTT 测试客户端（保持连接并订阅 `demo/#`）：

```bash
nohup python3 src/backend/mqtt_demo_client.py \
  > mqtt_demo.log 2>&1 &
echo $! > mqtt_demo.pid
```

> 以上命令均在仓库根目录执行，日志与 pid 文件写在仓库根（已由 `.gitignore` 的 `*.log`、`*.pid` 规则忽略）。

---

## 10. 配置文件清单

| 路径 | 作用 | 关键项 |
|---|---|---|
| `/etc/systemd/system/srs.service`（源文件 `etc/srs.service`） | SRS 服务 | `LimitNOFILE=10000`、`Restart=on-failure`、`User=gs` |
| `/etc/walle/walle.conf`（源文件 `etc/walle.conf`） | 门户保密配置 | `EMQX_API_KEY`/`EMQX_API_SECRET`，权限 600 |
| `/etc/systemd/system/walle.service`（由 `scripts/walle.service` 安装） | 门户服务 | `Type=forking`、`EnvironmentFile`、8088 |
| `/etc/emqx/emqx.conf` | EMQX 配置（deb 自带） | 默认 dashboard 账号、监听端口 |
| `etc/srs.conf` | SRS 运行配置 | `max_connections 800`、8080/1985/9972、`rtc_server` |
| `etc/nginx.conf`（由模板复制） | 门户反代 | 8088、`/auth/login`、`/emqx-api/` 注入 Basic、`/srs/` 关闭缓冲 |
| `src/web/index.html` | 登录页 | 角色 Tab、`POST /auth/login`、sessionStorage |
| `src/web/home.html` | 角色路由 | 按 `walle_role` 跳 admin/user |
| `src/web/admin.html` | 管理员控制台 | 5s 轮询 EMQX/SRS 接口 |
| `src/web/user.html` | 普通用户控制台 | HTTP-FLV 播放、指令占位 |
| `scripts/start-walle.sh` | 门户启停 | start/stop/status |
| `scripts/deploy.sh` | 门户部署/安装 | 校验 walle.conf、渲染并安装 walle.service |
| `etc/emqx_api_key.json` | API 凭据 | 0600，`api_key`/`api_secret` |
| `/etc/docker/daemon.json` | Docker 守护配置 | 镜像加速、日志轮转（引擎保留） |

---

## 11. 验证步骤

```bash
cd /home/gs/01_walle_service/walle-service
KEY=$(jq -r .api_key etc/emqx_api_key.json)
SECRET=$(jq -r .api_secret etc/emqx_api_key.json)

# EMQX
curl -s -u "$KEY:$SECRET" 'http://127.0.0.1:18083/api/v5/clients?limit=2' | jq -c '{count:.meta.count}'
curl -s -u "$KEY:$SECRET" 'http://127.0.0.1:18083/api/v5/subscriptions?limit=2' | jq -c '{count:.meta.count}'
curl -s -u "$KEY:$SECRET" 'http://127.0.0.1:18083/api/v5/nodes' | jq -c '[.[]|{node,version,edition,node_status}]'
curl -s -u "$KEY:$SECRET" 'http://127.0.0.1:18083/api/v5/metrics' | jq -c '.[0]|{node,fields:(keys|length)}'

# SRS
curl -sL http://127.0.0.1:1985/api/v1/streams/ | jq -c '{total,streams:[.streams[]|{name,app,clients:(.clients|length)}]}'
curl -sL http://127.0.0.1:1985/api/v1/clients/ | jq -c '{total}'
curl -s -o /dev/null -w 'exporter=%{http_code}\n' http://127.0.0.1:9972/metrics

# 门户
curl -s -o /dev/null -w 'portal=%{http_code}\n' http://127.0.0.1:8088/
curl -s -o /dev/null -w 'flv=%{http_code} type=%{content_type}\n' --max-time 5 http://127.0.0.1:8088/srs/live/test.flv

# 登录（正确密码 200，错误密码 401）
curl -s -o /dev/null -w 'login=%{http_code}\n' -X POST http://127.0.0.1:8088/auth/login \
  -H 'Content-Type: application/json' -d '{"username":"admin","password":"public"}'
curl -s -o /dev/null -w 'bad-login=%{http_code}\n' -X POST http://127.0.0.1:8088/auth/login \
  -H 'Content-Type: application/json' -d '{"username":"admin","password":"wrong"}'
```

期望（有推流与 MQTT 测试客户端时）：

```text
count=1；count=1；edition=Enterprise / version=6.3.2 / running；fields=113
total=1（live/test）；total>=1；exporter=200
portal=200；flv=200 type=video/x-flv
login=200；bad-login=401
```

浏览器侧：`http://<host>:8088/` 输入 EMQX 账号登录 → 管理员进入控制台、普通用户进入小车控制台 → 退出后回到登录页。

---

## 12. Docker 说明

Docker 引擎保留，但**镜像已全部清空**；`/etc/docker/daemon.json` 内容：

```json
{
  "registry-mirrors": [
    "https://docker.m.daocloud.io",
    "https://docker.1ms.run"
  ],
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "50m",
    "max-file": "3"
  }
}
```

用途：加速镜像拉取（直连 Docker Hub 不可用时）、限制容器日志大小。本方案不需要 Docker 运行任何服务。

清理记录：6 个镜像（10 个 tag，约 922.4MB）→ 0；容器 0；保留 6 个本地卷（约 331KB）。

---

## 13. 运行注意事项

1. **EMQX 无 CORS**：浏览器直连 18083 不会返回 `Access-Control-Allow-Origin`，`OPTIONS` 预检 500；必须经门户同源反代。新增接口时也要走 `/emqx-api/`。
2. **SRS ulimit**：`max_connections` 必须小于 `LimitNOFILE`，否则启动报错；systemd 已设 10000。
3. **自动播放**：浏览器会拦截有声自动播放，监控页需点击“播放 / 重连”。
4. **门户托管**：可由 `walle.service` 托管并开机自启（`sudo systemctl enable --now walle`）；未启用时重启主机后需手动执行 `./scripts/start-walle.sh start`，ffmpeg 测试推流同理。
5. **默认凭据**：EMQX `admin/public`、默认 Erlang cookie 均不安全，必须修改。
6. **API Key 注入风险**：能访问 8088 的人即可调用只读管理接口，生产应加 BFF 鉴权或限制来源 IP。
7. **SRS 无录制持久化**：当前未启用 DVR；重建主机不会保留录制文件。

其余安全分析与演进项见 [walle_detiled_design.md](./walle_detiled_design.md) 第 12、13 节。

---

## 14. 附录：首次部署验证结果（2026-11-15）

```text
systemctl is-active emqx  → active
systemctl is-active srs   → active
systemctl restart srs     → active，/api/v1/streams/ 仍 200

EMQX /api/v5/clients        → {"count":1,"ids":["srs-mqtt-demo"]}
EMQX /api/v5/subscriptions  → {"count":1,"topics":["demo/#"]}
EMQX /api/v5/nodes          → [{"node":"emqx@127.0.0.1","version":"6.3.2","edition":"Enterprise","node_status":"running"}]
EMQX /api/v5/metrics        → {"node":"emqx@127.0.0.1","fields":113}

SRS /api/v1/streams/        → {"total":1,"streams":[{"name":"test","app":"live","clients":1}]}
SRS /api/v1/clients/        → {"total":1}
SRS /metrics                → 200（srs_build_info version="7.0.215"）

门户 /                       → 200
门户 /srs/live/test.flv      → 200，video/x-flv
浏览器                        → 点击播放后出现彩条测试图案
```

截图：`docs/screenshots/srs_play_top.jpg`、`docs/screenshots/srs_play.jpg`（另有容器版 `mvp_*.jpg`）。
