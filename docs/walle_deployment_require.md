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
  ├── /api/          → 127.0.0.1:9000            （登录与用户管理 API，SQLite + EMQX）
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
<仓库根目录>/walle-service/
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
#  下载地址： https://www.emqx.com/zh/downloads/enterprise

# 以下载 6.3.2 版本为例
curl -L -o emqx-enterprise-6.3.2-ubuntu24.04-arm64.deb \
  https://www.emqx.com/downloads/enterprise/6.3.2/emqx-enterprise-6.3.2-ubuntu24.04-arm64.deb

echo "ea5252d8f3979ecef714ee1d570558c6f07bad61d18d55d8ebd989243790d717  emqx-enterprise-6.3.2-ubuntu24.04-arm64.deb" | sha256sum -c -
```


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

### 6.3 登录，并修改默认密码

EMQX服务启动成功后，就可以登录EMQX的控制面板（Dashboard：`http://<host>:18083`）。
默认账号 `admin` / `public`（首次部署后请立即修改）。


### 6.4 获取 API Key

获取 EMQX的 API Key ，主要用于 Nginx 方向 和 walle 后端调用。

方法：
1. 登录 EMQX Dashboard（`http://<host>:18083`）→ **系统设置（System）→ API 密钥（API Keys）→ 创建（Create）**，按需勾选权限（或者选择管理员角色）。
2. 把新生成的 `api_key` / `api_secret` 保存下来，后面配置Nginx和walle网站会使用到。


### 6.5 创建内置数据库认证器（必须）

walle 后端通过 EMQX 内置数据库管理 MQTT 用户（`src/backend/walle_api.py` 中 `AUTH_ID = "password_based:built_in_database"`）。全新安装的 EMQX 没有该认证器，**必须先创建**，否则门户「用户管理」会报 `Authenticator not found in the 'global' scope'`。

```bash
KEY=$(jq -r .api_key etc/emqx_api_key.json)
SECRET=$(jq -r .api_secret etc/emqx_api_key.json)

curl -s -X POST -u "$KEY:$SECRET" http://127.0.0.1:18083/api/v5/authentication \
  -H 'Content-Type: application/json' \
  -d '{
    "mechanism": "password_based",
    "backend": "built_in_database",
    "user_id_type": "username",
    "password_hash_algorithm": {
      "name": "sha256",
      "salt_position": "suffix"
    }
  }' | jq .
```

参数说明：

- `mechanism` / `backend`：`password_based` + `built_in_database`，与后端 `AUTH_ID` 对应。
- `user_id_type`: `username`，用户以用户名（而非 clientid）登录。
- `password_hash_algorithm`：`sha256` + `suffix`（密码哈希加盐方式，盐追加在密码后），与后端创建用户时的加盐逻辑一致。

验证（返回空列表即正常，NOT_FOUND 说明认证器未创建）：

```bash
curl -s -u "$KEY:$SECRET" \
  "http://127.0.0.1:18083/api/v5/authentication/password_based:built_in_database/users?limit=10" \
  | jq .
```

> 也可在 Dashboard 操作：访问控制 → 客户端认证 → 创建 → 认证方式「密码认证」、数据源「内置数据库」、用户 ID 类型「username」、密码哈希算法选择 `sha256`、盐位置 `suffix`。

### 6.6 License 说明

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
cd <仓库根目录>

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

### 7.2 配置 `etc/srs.conf`

仓库已提供模板 `etc/srs.conf.example`（入库），首次部署复制后**只需改 WebRTC 候选地址一行**：

```bash
cp etc/srs.conf.example etc/srs.conf
```

修改 `etc/srs.conf` 中 `rtc_server` 块的 `candidate`，换成你的公网 IP / EIP：

```nginx
# 修改前（模板默认值，自动探测）：
rtc_server {
    enabled on;
    listen 8000;
    candidate *;
    ...
}

# 修改后：
    candidate <你的公网IP>;
```

- 云主机 / NAT 环境**必须填公网 IP**，否则 SDP answer 携带错误地址，公网浏览器 ICE 无法连通，WebRTC（WHEP/WHIP、`/players/rtc_player.html`）黑屏超时；HTTP-FLV、HLS、RTMP 推流不受影响。
- 安全组放行 **UDP 8000**；本配置启用了 `rtc_server.tcp`，**TCP 8000** 也要放行（若只用 UDP，把 `tcp { enabled on; }` 改为 `off`）。
- `exporter.label` / `exporter.tag` 为部署环境标识，非必须，多环境时改成可区分名字。

换机器检查：

```bash
grep -nE 'candidate|listen 8000' etc/srs.conf
curl -s https://ifconfig.me; echo   # 确认公网出口 IP / EIP
```

### 7.3 修改 `etc/srs.service`

仓库已提供模板 `etc/srs.service.example`（入库），复制后**改 4 个字段**（用户名 + 仓库路径）：

```bash
cp etc/srs.service.example etc/srs.service
```

修改 `etc/srs.service`，把模板中的 `gs` 和 `/home/gs/01_walle_service/...` 替换为你的实际用户与仓库路径：

```ini
[Service]
User=<你的用户名>
Group=<你的用户名>
WorkingDirectory=<仓库路径>/thirdparty/srs/trunk
ExecStart=<仓库路径>/thirdparty/srs/trunk/objs/srs -c <仓库路径>/etc/srs.conf
```

> `User` 不存在时 systemd 启动会报 `status=217/USER`。`max_connections` 必须小于 `LimitNOFILE`（本配置 800 / 10000）。

### 7.4 安装运行

```bash
sudo install -m 0644 etc/srs.service /etc/systemd/system/srs.service
sudo systemctl daemon-reload
sudo systemctl enable --now srs
systemctl status srs --no-pager
```

### 7.5 验证

```bash
curl http://127.0.0.1:1985/api/v1/streams/
```

---

## 8. 门户 Nginx（8088）

### 8.1 配置 `etc/nginx.conf`

仓库已提供模板 `etc/nginx.conf.example`（入库），复制后**只需把 `/emqx-api/` location 中的 `<BASE64(api_key:api_secret)>` 占位符替换为真实值**：

```bash
cp etc/nginx.conf.example etc/nginx.conf
```

修改 `etc/nginx.conf` 第 67 行（`/emqx-api/` location）：

```nginx
# 修改前：
    proxy_set_header Authorization "Basic <BASE64(api_key:api_secret)>";

# 修改后（把占位符替换为实际生成的 base64 值）：
    proxy_set_header Authorization "Basic <base64 编码后的 api_key:api_secret>";
```

> 注意，这里不是简单的拷贝复制，生成 `<BASE64(api_key:api_secret)>` 的规则如下：（需要使用 6.4 节获取到的 API Key 和 API Secret）：

```bash
KEY=<6.4 节获取到的 API Key>
SECRET=<6.4 节获取到的 API Secret>
BASIC=$(printf '%s:%s' "$KEY" "$SECRET" | base64 -w0)
echo "生成的 Basic 值: $BASIC"

# 把 etc/nginx.conf 中 <BASE64(api_key:api_secret)> 替换为真实值
sed -i "s|<BASE64(api_key:api_secret)>|$BASIC|" etc/nginx.conf

# 验证替换成功（应输出带真实 base64 的整行，不再含 <BASE64...>）
grep -n 'Authorization "Basic' etc/nginx.conf
```


验证 EMQX API 调用正常：

```bash
curl -s -u "$KEY:$SECRET" 'http://127.0.0.1:18083/api/v5/nodes' | jq -c '[.[]|{node,edition,node_status}]'
# 期望：[{"node":"emqx@127.0.0.1","edition":"Enterprise","node_status":"running"}]
```


### 8.2 配置 `etc/walle.conf`

仓库已提供模板 `etc/walle.conf.example`（入库），复制后将占位符替换为 6.4 节获取到的 API Key 和 API Secret：

```bash
cp etc/walle.conf.example etc/walle.conf

vi etc/walle.conf
```

> 说明：`etc/walle.conf`和`etc/nginx.conf`使用的 API Key 和 API Secret 可以不一样，可以在生成两套 API Key 和 API Secret 后，分别用于门户和 Nginx，比如 Nginx 的权限底一些，门户的权限高一些。这里使用一套。


### 8.3 配置 `etc/walle.service`

仓库已提供模板 `etc/walle.service.example`（入库），复制后**改 4 个字段**（用户名 + 仓库路径）：

```bash
cp etc/walle.service.example etc/walle.service

vi etc/walle.service
```


### 8.4 部署脚本

`scripts/deploy.sh` 依次完成 3 件事：

1. 校验 `etc/walle.conf` 是否存在、非空，且 `EMQX_API_KEY` / `EMQX_API_SECRET` 已填入真实值（拒绝占位符）。
2. 以 `scripts/walle.service.example` 为模板，渲染出 `scripts/walle.service`（替换仓库路径与服务用户）。
3. 用 `sudo` 把 `walle.conf` 安装到 `/etc/walle/walle.conf`（权限 600）、`walle.service` 安装到 `/etc/systemd/system/walle.service`（权限 644），并执行 `systemctl daemon-reload`，最后打印关键信息。

用法：

```bash
./scripts/deploy.sh
sudo systemctl enable --now walle
```


### 8.5 启动和停止服务脚本


使用方法：

```bash
chmod +x scripts/start-walle.sh scripts/stop-walle.sh
./scripts/start-walle.sh start
./scripts/start-walle.sh status
./scripts/stop-walle.sh
```

> 门户既可用 `scripts/start-walle.sh` 手动启停，也可由 `walle.service` 托管。



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
| `/etc/walle/walle.conf`（源文件 `etc/walle.conf`） | 门户保密配置 | `EMQX_API_KEY`/`EMQX_API_SECRET`、`MEDIA_SERVER_SECRET`（与设备/App 一致），权限 600 |
| `/etc/systemd/system/walle.service`（由 `scripts/walle.service` 安装） | 门户服务 | `Type=forking`、`EnvironmentFile`、8088 |
| `/etc/emqx/emqx.conf` | EMQX 配置（deb 自带） | 默认 dashboard 账号、监听端口 |
| `etc/srs.conf` | SRS 运行配置 | `max_connections 800`、8080/1985/9972、`rtc_server` |
| `etc/nginx.conf`（由模板复制） | 门户反代 | 8088、`/api/`、`/emqx-api/` 注入 Basic、`/srs/` 关闭缓冲 |
| `src/web/index.html` | 登录页 | 角色 Tab、`POST /api/login` 或 `/api/admin/login`、sessionStorage |
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
cd <仓库根目录>
KEY=$(jq -r .api_key etc/emqx_api_key.json)
SECRET=$(jq -r .api_secret etc/emqx_api_key.json)

# EMQX
curl -s -u "$KEY:$SECRET" 'http://127.0.0.1:18083/api/v5/clients?limit=2' | jq -c '{count:.meta.count}'
curl -s -u "$KEY:$SECRET" 'http://127.0.0.1:18083/api/v5/subscriptions?limit=2' | jq -c '{count:.meta.count}'
curl -s -u "$KEY:$SECRET" 'http://127.0.0.1:18083/api/v5/nodes' | jq -c '[.[]|{node,version,edition,node_status}]'
curl -s -u "$KEY:$SECRET" 'http://127.0.0.1:18083/api/v5/metrics' | jq -c '.[0]|{node,fields:(keys|length)}'
# 内置数据库认证器（应返回 data 数组，NOT_FOUND 说明 6.4 节未执行）
curl -s -u "$KEY:$SECRET" 'http://127.0.0.1:18083/api/v5/authentication/password_based:built_in_database/users?limit=2' | jq -c '{count:.meta.count}'

# SRS
curl -sL http://127.0.0.1:1985/api/v1/streams/ | jq -c '{total,streams:[.streams[]|{name,app,clients:(.clients|length)}]}'
curl -sL http://127.0.0.1:1985/api/v1/clients/ | jq -c '{total}'
curl -s -o /dev/null -w 'exporter=%{http_code}\n' http://127.0.0.1:9972/metrics

# 门户
curl -s -o /dev/null -w 'portal=%{http_code}\n' http://127.0.0.1:8088/
curl -s -o /dev/null -w 'flv=%{http_code} type=%{content_type}\n' --max-time 5 http://127.0.0.1:8088/srs/live/test.flv

# 管理员登录（正确密码 200 并返回 token，错误密码 401）
curl -s -o /dev/null -w 'admin-login=%{http_code}\n' -X POST http://127.0.0.1:8088/api/admin/login \
  -H 'Content-Type: application/json' -d '{"username":"<Dashboard账号>","password":"<密码>"}'
# 未带 token 访问用户接口应为 401
curl -s -o /dev/null -w 'users=%{http_code}\n' http://127.0.0.1:8088/api/users
```

期望（有推流与 MQTT 测试客户端时）：

```text
count=1；count=1；edition=Enterprise / version=6.3.2 / running；fields=113；authenticator count=0（或 N，无用户时为 0）
total=1（live/test）；total>=1；exporter=200
portal=200；flv=200 type=video/x-flv
login=200；bad-login=401
```

浏览器侧：`http://<host>:8088/` 输入 EMQX 账号登录 → 管理员进入控制台、普通用户进入小车控制台 → 退出后回到登录页。

---



## 12. 运行注意事项

1. **EMQX 无 CORS**：浏览器直连 18083 不会返回 `Access-Control-Allow-Origin`，`OPTIONS` 预检 500；必须经门户同源反代。新增接口时也要走 `/emqx-api/`。
2. **SRS ulimit**：`max_connections` 必须小于 `LimitNOFILE`，否则启动报错；systemd 已设 10000。
3. **自动播放**：浏览器会拦截有声自动播放，监控页需点击“播放 / 重连”。
4. **门户托管**：可由 `walle.service` 托管并开机自启（`sudo systemctl enable --now walle`）；未启用时重启主机后需手动执行 `./scripts/start-walle.sh start`，ffmpeg 测试推流同理。
5. **默认凭据**：EMQX `admin/public`、默认 Erlang cookie 均不安全，必须修改。
6. **API Key 注入风险**：能访问 8088 的人即可调用只读管理接口，生产应加 BFF 鉴权或限制来源 IP。
7. **SRS 无录制持久化**：当前未启用 DVR；重建主机不会保留录制文件。

其余安全分析与演进项见 [walle_detiled_design.md](./walle_detiled_design.md) 第 13、14 节。

---

## 13. 附录：首次部署验证结果（2026-11-15）

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
