# walle 服务端部署文档（裸机）

> 适用环境：Ubuntu 24.04（amd64/x86_64），单机裸机部署；arm64 主机把 deb 包后缀换成 arm64 即可。
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

- Ubuntu 24.04 LTS，amd64（x86_64）。
- 建议 4C/8G 以上；磁盘至少 3GB（EMQX 安装约 130MB，SRS 源码+objs 约 460MB，临时 deb 84MB 安装后可删）。
- 具有 sudo 权限的普通用户（本文以 `gs` 为例）。
- 可访问 `www.emqx.com`、`packages.emqx.io`、`github.com`。
- 端口 8088/18083/1883/8883/8083/8084/1935/1985/8080/8000/9972 未被占用。

---



## 3. 系统依赖

执行下面的命令，安装系统依赖：

```bash
sudo apt-get update
sudo apt-get install -y \
  curl jq git build-essential pkg-config libssl-dev zlib1g-dev \
  automake cmake tcl ffmpeg certbot
```


---


## 4. 安装 Nginx

安装命令：
```bash
sudo apt install -y nginx
```


---


## 5. 安装 SRS 

SRS 没有官方 apt 源/`.deb` 包，因此要自己下载源码，并编译。

### 5.1 编译

```bash
cd <仓库根目录>/thirdparty

git clone --branch v7.0-a4 https://github.com/ossrs/srs.git 

cd srs/trunk
# 配置：
./configure --srt=on
# 编译：
make -j8
#产物：trunk/objs/srs = 30M；trunk/objs = 318M

# 查看版本号：
./objs/srs -V
```

### 5.2 安装

SRS 编译产物是“可执行文件 + 静态资源”的自包含应用，不是共享库，不涉及`ldconfig` ，因此不需要使用类似“ make install ”的安装命令。
也就是说，SRS的编译产物就放在当前仓库的目录下，在使用的时候，加上绝对路径即可，到此，srs的安装已完成。

---

## 6. EMQX Enterprise 6.3.2

官方只提供了 EMQX 的 `.deb` 包，示例以 amd64 为例，实际使用要选择对应的架构包。

### 6.1 下载与校验

```bash
cd ~/Downloads

#  下载地址： https://www.emqx.com/zh/downloads/enterprise
# 以下载 6.3.2 版本为例（amd64 包；arm64 主机替换为 -arm64 后缀）
curl -L -o emqx-enterprise-6.3.2-ubuntu24.04-amd64.deb \
  https://www.emqx.com/downloads/enterprise/6.3.2/emqx-enterprise-6.3.2-ubuntu24.04-amd64.deb

```


### 6.2 安装与启动

```bash
sudo apt install -y ./emqx-enterprise-6.3.2-ubuntu24.04-amd64.deb

# 设置 emqx 服务开机自启
sudo systemctl enable emqx

# 启动 emqx
sudo systemctl start emqx

# 查看 emqx 服务状态
systemctl status emqx --no-pager

# 查看 emqx 服务端口
ss -ltn | grep -E ':(18083|1883|8883|8083|8084)\b'

# 测试 emqx 服务是否启动成功
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18083/
```

> 注意：EMQX 的 deb包，安装完成后，只做了`systemctl enable emqx` ，没做`start`，因此要手动启动。

### 6.3 登录，并修改默认密码

EMQX服务启动成功后，就可以登录EMQX的控制面板（Dashboard：`http://<host>:18083`）。
默认账号 `admin` / `public`（首次部署后请立即修改）。




---

## 7. 生成对应的 conf 配置文件和 systemd 服务文件

前序工作已经准备完成，剩下的工作就是，生成对应的 conf 文件和 service 文件，然后部署到系统中，最后启动服务。

### 7.1 使用脚本自动生成

```bash
cd <仓库根目录>/scripts

# 执行脚本，生成 conf 文件和 service 文件
./auto_gen_conf_service.sh

```

`auto_gen_conf_service.sh` 脚本的作用：
- 1. 检查 srs，nginx，EMQX是否都安装了，其中nginx，EMQX可以通过判断库中有没有这个对应的包来判断，判断 srs 的时候，输出一个对话框，问用户是否安装了srs，给用户选择y/n，选择y继续，选择n，则提示用户安装srs（参考md文档），并退出。
- 2. 根据 srs.conf.example 和 srs.service.example 生成对应的 srs.conf 和 srs.service 文件，其中生成 srs.conf文件时，提示用户输入公网ip地址，如果不输入，则使用默认探测。
- 3. 根据 nginx.conf.example  生成 nginx.conf， 生成的过程中，需要使用到 EMQX的 `api_key` / `api_secret`，此时，判断emqx是否启动，如果没有启动，则提示用户启动 emqx，如果启动了，让用户输入 admin 的密码，默认密码是pubilc，然后通过脚本自动获取到  EMQX的 `api_key` / `api_secret`，并自动生成 `<BASE64(api_key:api_secret)>` ，最终生成 nginx.conf 文件。
- 4. 根据walle.conf.example 和 walle.service.example 生成对应的  walle.conf 和 walle.service 文件，其中 walle.conf  中的 EMQX_API_KEY 和  EMQX_API_SECRET 与第3部的一样。



### 7.2 手动修改文件

如果不使用上面的脚本自动生成，也可以使用下面的命令配置。

#### 7.2.1 获取 API Key

获取 EMQX的 API Key ，主要用于 Nginx 方向 和 walle 后端调用。

方法：
1. 登录 EMQX Dashboard（`http://<host>:18083`）→ **系统设置（System）→ API 密钥（API Keys）→ 创建（Create）**，按需勾选权限（或者选择管理员角色）。
2. 把新生成的 `api_key` / `api_secret` 保存下来，后面配置Nginx和walle网站会使用到。


#### 7.2.2  生成 `etc/nginx.conf` 文件

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

> 注意，这里不是简单的拷贝复制，生成 `<BASE64(api_key:api_secret)>` 的规则如下：（需要使用 7.2.1 节获取到的 API Key 和 API Secret）：

```bash
KEY=<7.2.1 节获取到的 API Key>
SECRET=<7.2.1 节获取到的 API Secret>
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

#### 7.2.3  生成 `etc/walle.conf` 文件

仓库已提供模板 `etc/walle.conf.example`（入库），复制后将占位符替换为 7.2.1 节获取到的 API Key 和 API Secret：

```bash
cp etc/walle.conf.example etc/walle.conf

vi etc/walle.conf
```

必填项：

| 键 | 说明 |
|---|---|
| `EMQX_API_KEY` / `EMQX_API_SECRET` | 7.2.1 节创建的 API Key |
| `WALLE_INGEST_TOKEN` | EMQX 规则引擎回调 walle_api 的共享 token，见 6.7 节；缺失时告警接入返回 503 |
| `WALLE_ALARM_RETENTION_DAYS` | 可选，告警保留天数，默认 30 |

> 说明：`etc/walle.conf`和`etc/nginx.conf`使用的 API Key 和 API Secret 可以不一样，可以在生成两套 API Key 和 API Secret 后，分别用于门户和 Nginx，比如 Nginx 的权限底一些，门户的权限高一些。这里使用一套。

#### 7.2.4  生成 `etc/walle.service` 文件

仓库已提供模板 `etc/walle.service.example`（入库），复制后**改 4 个字段**（用户名 + 仓库路径）：

```bash
cp etc/walle.service.example etc/walle.service

vi etc/walle.service
```

将占位符：__REPO_ROOT__（仓库绝对路径）、__USER__、__GROUP__（服务运行用户）替换为你的实际用户与仓库路径。

#### 7.2.5  生成 `etc/srs.conf` 文件

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


#### 7.2.6  生成 `etc/srs.service` 文件

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

将上面的 <你的用户名> <仓库路径> 替换为你的实际用户与仓库路径。

---

## 8. 部署并启动服务


## 8.1 部署

使用脚本部署

```bash
cd <仓库根目录>/scripts

./deploy.sh

```

脚本 deploy.sh 的作用：

- 1. 检查etc/srs.conf、etc/srs.service是否存在，
- 2. 检查etc/nginx.conf 是否存在，
- 3. 检查 etc/walle.conf， etc/walle.service是否存在，
- 4. 将这些文件 拷贝到对应的目录中（`etc/srs.conf` 、`etc/nginx.conf`不拷贝）。
- 5. 给出启动服务的命令。


## 8.2 启动服务

EMQX已经在6.2节中启动了
srs已经还没启动，本节启动
nginx的服务放在walle.service中，启动walle.service即可启动nginx
walle还没有启动，本节启动

```bash
# 启动 srs
sudo systemctl start srs

# 启动walle
sudo systemctl start walle
```

测试：
浏览器输入：http://<公网ip>:8088/，看是否能正常访问。
首次登录时，还没有创建客户，因此，只能用管理员的账号登录，默认账号 `admin` / `public`（首次部署后请立即修改）。

---


## 9. 配置

服务成功启动后，需要做必须的配置，保证walle服务正常运行。


### 9.1 创建内置数据库认证器（必须）

- 1. walle 后端通过 EMQX 内置数据库管理 MQTT 用户，因此，全新安装的 EMQX 没有该认证器，**必须先创建**，否则门户「用户管理」会报 `Authenticator not found in the 'global' scope'`。

执行脚本创建内置数据库认证器

```bash
cd <仓库根目录>/scripts

./emqx_authn_setup.sh
```

验证（返回空列表即正常，NOT_FOUND 说明认证器未创建）：

```bash
KEY=$(grep -E '^EMQX_API_KEY=' etc/walle.conf | tail -n1 | cut -d= -f2-)
SECRET=$(grep -E '^EMQX_API_SECRET=' etc/walle.conf | tail -n1 | cut -d= -f2-)
curl -s -u "$KEY:$SECRET" \
  "http://127.0.0.1:18083/api/v5/authentication/password_based:built_in_database/users?limit=10" \
  | jq .
```

> 也可在 Dashboard 操作：访问控制 → 客户端认证 → 创建 → 认证方式「密码认证」、数据源「内置数据库」、用户 ID 类型「username」、密码哈希算法选择 `sha256`、盐位置 `suffix`。

### 9.2 配置告警落库规则（必须）

- 1. 告警历史依赖 EMQX 规则引擎，把 `walle/{tenant}/device/{device}/alarm/report` 回调给 `walle_api.py`，然后创建 Connector、Action、Rule（EMQX 6.3.2 的 REST 接口）。

执行脚本创建内置数据库认证器

```bash
cd <仓库根目录>/scripts

./emqx_alarm_setup.sh
```

> 也可在 Dashboard 操作：集成 → 连接器（HTTP）→ 动作（HTTP）→ 规则。Action 的 body 模板必须是
> `{"topic":"${topic}","payload":${payload}}`，否则后端无法解析。

验证：

```bash
KEY=$(grep -E '^EMQX_API_KEY=' etc/walle.conf | tail -n1 | cut -d= -f2-)
SECRET=$(grep -E '^EMQX_API_SECRET=' etc/walle.conf | tail -n1 | cut -d= -f2-)
curl -s -u "$KEY:$SECRET" http://127.0.0.1:18083/api/v5/rules/walle_alarm_persist | jq .
```

结果：返回规则且 `enable=true`。


### 9.3 开启一键https

刚配置好的服务器，此时还不能使用https登录，需要开启一键https，开启https之后，srs和mqtt协议走的就是加密协议了。
开启方法：登录管理员账号，点击左边菜单的“系统设置” -- “一键HTTPS”，输入购买的域名，点击“申请证书”。





---

## 10. 测试推流与 MQTT 客户端

```bash
# 推流密钥：登录管理员控制台在「推流密钥」中设置；未设置或与 URL 不一致会被 SRS 拒绝
SECRET=<推流密钥>
nohup ffmpeg -hide_banner -loglevel warning -re \
  -f lavfi -i testsrc2=size=640x360:rate=25 \
  -f lavfi -i sine=frequency=1000 \
  -c:v libx264 -preset veryfast -tune zerolatency -pix_fmt yuv420p -g 50 \
  -c:a aac -b:a 64k -f flv "rtmp://127.0.0.1:1935/live/test?secret=$SECRET" \
  > ffmpeg-push.log 2>&1 &
echo $! > ffmpeg-push.pid
```

无依赖 MQTT 测试客户端（保持连接并订阅指定主题）：

> 9.1 创建内置数据库认证器后，EMQX 不再允许匿名 MQTT 连接。请先在门户创建一个 MQTT 用户，
> 并使用该账号运行客户端；订阅主题必须在该账号的 ACL 范围内（客户账号可用 `walle/<客户名>/#`）。

```bash
MQTT_USERNAME=<MQTT用户名> \
MQTT_PASSWORD=<MQTT密码> \
MQTT_TOPIC='walle/<客户名>#' \
nohup python3 src/backend/mqtt_demo_client.py \
  > mqtt_demo.log 2>&1 &
echo $! > mqtt_demo.pid
```

> 以上命令均在仓库根目录执行，日志与 pid 文件写在仓库根（已由 `.gitignore` 的 `*.log`、`*.pid` 规则忽略）。

---

## 11. 配置文件清单

| 路径 | 作用 | 关键项 |
|---|---|---|
| `/etc/systemd/system/srs.service`（源文件 `etc/srs.service`） | SRS 服务 | `LimitNOFILE=10000`、`Restart=on-failure`、`User=<部署用户>` |
| `/etc/walle/walle.conf`（源文件 `etc/walle.conf`） | 门户保密配置 | `EMQX_API_KEY`/`EMQX_API_SECRET`、`MEDIA_SERVER_SECRET`、`WALLE_INGEST_TOKEN`，权限 600 |
| `/etc/systemd/system/walle.service`（源文件 `etc/walle.service`，由 `scripts/deploy.sh` 安装） | 门户服务 | `Type=forking`、`EnvironmentFile`、8088 |
| `/etc/emqx/emqx.conf` | EMQX 配置（deb 自带） | 默认 dashboard 账号、监听端口 |
| `etc/srs.conf` | SRS 运行配置 | `max_connections 800`、8080/1985/9972、`rtc_server` |
| `etc/nginx.conf`（由模板复制） | 门户反代 | 8088、`/api/`、`/emqx-api/` 注入 Basic、`/srs/` 关闭缓冲 |
| `src/web/index.html` | 登录页 | 角色 Tab、`POST /api/login` 或 `/api/admin/login`、sessionStorage |
| `src/web/home.html` | 角色路由 | 按 `walle_role` 跳 admin/user |
| `src/web/admin.html` | 管理员控制台 | 5s 轮询 EMQX/SRS 接口 |
| `src/web/user.html` | 普通用户控制台 | HTTP-FLV 播放、实时/历史告警、设备与时间过滤 |
| `scripts/start-walle.sh` | 门户启停 | start/stop/status |
| `scripts/deploy.sh` | 门户部署/安装 | 检查 etc/ 下生成的配置与服务文件，安装 `walle.conf` 和 systemd 单元 |
| `etc/emqx_api_key.json`（可选） | 手工保存 API 凭据时使用 | 0600，`api_key`/`api_secret` |
| `/etc/docker/daemon.json` | Docker 守护配置 | 镜像加速、日志轮转（引擎保留） |

---

## 12. 验证步骤

```bash
cd <仓库根目录>
KEY=$(grep -E '^EMQX_API_KEY=' etc/walle.conf | tail -n1 | cut -d= -f2-)
SECRET=$(grep -E '^EMQX_API_SECRET=' etc/walle.conf | tail -n1 | cut -d= -f2-)

# EMQX
curl -s -u "$KEY:$SECRET" 'http://127.0.0.1:18083/api/v5/clients?limit=2' | jq -c '{count:.meta.count}'
curl -s -u "$KEY:$SECRET" 'http://127.0.0.1:18083/api/v5/subscriptions?limit=2' | jq -c '{count:.meta.count}'
curl -s -u "$KEY:$SECRET" 'http://127.0.0.1:18083/api/v5/nodes' | jq -c '[.[]|{node,version,edition,node_status}]'
curl -s -u "$KEY:$SECRET" 'http://127.0.0.1:18083/api/v5/metrics' | jq -c '.[0]|{node,fields:(keys|length)}'
# 内置数据库认证器（应返回 data 数组，NOT_FOUND 说明 9.1 节未执行）
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
curl -s -o /dev/null -w 'bad-login=%{http_code}\n' -X POST http://127.0.0.1:8088/api/admin/login \
  -H 'Content-Type: application/json' -d '{"username":"<Dashboard账号>","password":"wrong"}'
# 未带 token 访问用户接口应为 401
curl -s -o /dev/null -w 'users=%{http_code}\n' http://127.0.0.1:8088/api/users

# 告警规则与内部接入：规则应 enable=true，外部访问 /api/internal/ 应为 403
curl -s -u "$KEY:$SECRET" http://127.0.0.1:18083/api/v5/rules/walle_alarm_persist \
  | jq -c '{id,enable,actions}'
curl -s -o /dev/null -w 'internal=%{http_code}\n' -X POST http://127.0.0.1:8088/api/internal/alarms \
  -H 'Content-Type: application/json' -d '{}'
```

期望（有推流与 MQTT 测试客户端时）：

```text
count=1；count=1；edition=Enterprise / version=6.3.2 / running；fields=113；authenticator count=0（或 N，无用户时为 0）
total=1（live/test）；total>=1；exporter=200
portal=200；flv=200 type=video/x-flv
login=200；bad-login=401；internal=403；规则 enable=true
```

浏览器侧：`http://<host>:8088/` 输入 EMQX 账号登录 → 管理员进入控制台、普通用户进入小车控制台 → 退出后回到登录页。

---



## 13. 运行注意事项

1. **EMQX 无 CORS**：浏览器直连 18083 不会返回 `Access-Control-Allow-Origin`，`OPTIONS` 预检 500；必须经门户同源反代。新增接口时也要走 `/emqx-api/`。
2. **SRS ulimit**：`max_connections` 必须小于 `LimitNOFILE`，否则启动报错；systemd 已设 10000。
3. **自动播放**：浏览器会拦截有声自动播放，监控页需点击“播放 / 重连”。
4. **门户托管**：可由 `walle.service` 托管并开机自启（`sudo systemctl enable --now walle`）；未启用时重启主机后需手动执行 `./scripts/start-walle.sh start`，ffmpeg 测试推流同理。
5. **默认凭据**：EMQX `admin/public`、默认 Erlang cookie 均不安全，必须修改。
6. **API Key 注入风险**：能访问 8088 的人即可调用只读管理接口，生产应加 BFF 鉴权或限制来源 IP。
7. **SRS 无录制持久化**：当前未启用 DVR；重建主机不会保留录制文件。
8. **告警保留期**：默认保留 30 天，按设备上报时间 `occurred_at` 清理；可通过 `WALLE_ALARM_RETENTION_DAYS` 调整。规则/Connector/Action 与 `WALLE_INGEST_TOKEN` 任一变更后需同步另一端，否则告警接入返回 403。

其余安全分析与演进项见 [walle_detiled_design.md](./walle_detiled_design.md) 第 13、14 节。

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
