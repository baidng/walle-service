# walle 服务端详细设计

> 适用版本：walle-ai v0.1（2026-11-16 原型）
> 运行环境：Ubuntu 24.04 arm64 裸机，EMQX Enterprise 6.3.2 + SRS 7.0.215 + Nginx 门户
> 本文只描述设计与实现细节；安装、配置、上线步骤见 [walle_deployment_require.md](./walle_deployment_require.md)。

---

## 1. 系统定位与范围

walle 是面向树莓派小车的远程控制平台。服务端由三部分组成：

| 层 | 组件 | 职责 |
|---|---|---|
| 接入/门户层 | Nginx（8088） | 静态页面托管、同源反代、注入 EMQX 管理凭据 |
| 消息层 | EMQX Enterprise 6.3.2 | MQTT 连接、主题路由、账号认证（登录校验复用此账号体系） |
| 媒体层 | SRS 7.0.215 | RTMP 接收、HTTP-FLV/HLS/WebRTC 分发、Prometheus 指标 |

服务端**不持有业务数据库**，当前所有业务数据（设备、告警、录像）均为前端占位或直接读 EMQX/SRS 实时接口；持久化属于后续演进项（见第 12 节）。

---

## 2. 总体架构

```text
                    浏览器
                      │ http://<host>:8088
                      ▼
        ┌──────────────────────────────┐
        │  Nginx 门户（托管/手动，8088） │
        │  /            → src/web/*.html│
        │  /api/        → walle-api    │
        │  /emqx-api/   → EMQX +Basic  │
        │  /srs-api/    → SRS API      │
        │  /srs/        → SRS HTTP     │
        │  /metrics     → exporter     │
        └───────┬───────────────┬──────┘
                │               │
       127.0.0.1:18083      127.0.0.1:1985 / 8080 / 9972
                │               │
        ┌───────▼──────┐  ┌─────▼────────┐
        │ EMQX 6.3.2   │  │ SRS 7.0.215  │
        │ systemd 托管 │  │ systemd 托管 │
        └───────▲──────┘  └─────▲────────┘
                │ MQTT          │ RTMP / WebRTC
                └───────┬───────┘
                树莓派小车（设备侧）
```

设计要点：

1. **单一入口**：浏览器只访问 8088，不直连 18083/1985/8080。EMQX 6.x 不返回 CORS 头，直连会因预检失败而不可用，因此门户必须同源反代。
2. **凭据分层**：管理员登录由 `walle_api.py` 调用 EMQX `/api/v5/login` 校验并签发内存 token；查询类接口 `/emqx-api/` 由 Nginx 注入只读 API Key，前端不持有管理凭据。
3. **前后端同源**：所有页面用相对路径请求，换机器/IP 时前端代码无需改动。
4. **无状态前端**：登录态仅存 `sessionStorage`，关闭标签页即失效，服务端无 session。

---

## 3. 端口与对外暴露

| 组件 | 端口 | 协议 | 用途 | 建议暴露面 |
|---|---|---|---|---|
| 门户 Nginx | 8088 | HTTP | 统一入口 | 内网/办公网 |
| EMQX | 18083 | HTTP | Dashboard / REST API | 仅内网 |
| EMQX | 1883 | MQTT | 设备接入 | 按业务 |
| EMQX | 8883 | MQTT/TLS | 设备接入（加密） | 按业务 |
| EMQX | 8083/8084 | WS/WSS | 浏览器 MQTT | 按业务 |
| SRS | 1935 | RTMP | 推流入口 | 按业务 |
| SRS | 1985 | HTTP | 媒体 REST API | 仅内网 |
| SRS | 8080 | HTTP | console / FLV / HLS | 按业务 |
| SRS | 8000 | UDP+TCP | WebRTC（WHEP/WHIP） | 按业务 |
| SRS | 9972 | HTTP | Prometheus exporter | 仅监控网 |

---

## 4. 前端页面与路由设计

页面全部是静态 HTML，位于 `src/web/`，不使用构建工具与前端框架。

| 文件 | 路由 | 作用 | 登录态校验 |
|---|---|---|---|
| `index.html` | `/` | 登录页（管理员/普通用户两个 Tab） | 已有 token 则 `replace` 到 `home.html` |
| `home.html` | `/home.html` | 角色路由中转页（加载动画） | 无 token 回 `/`；按角色跳 `admin.html` / `user.html` |
| `admin.html` | `/admin.html` | 管理员控制台 | 无 token 回 `/`；非 admin 跳 `user.html` |
| `user.html` | `/user.html` | 普通用户小车控制台 | 无 token 回 `/`；admin 跳 `admin.html` |
| `dashboard.html` | `/dashboard.html` | 早期 EMQX+SRS 监控 MVP，保留未接入登录 | 无（历史页面） |

路由采用 `location.replace()` 而非 `location.href`，避免用户点“返回”回到登录页或中转页。

### 4.1 sessionStorage 键约定

| 键 | 内容 | 说明 |
|---|---|---|
| `walle_token` | EMQX 返回的 token（缺失时写 `"ok"`） | 仅用于前端判断是否已登录 |
| `walle_role` | `admin` / `user` | 由登录页 Tab 选择，**不来自服务端** |
| `walle_user` | 输入的用户名 | 用于右上角展示 |

退出登录 = 三个键全部 `removeItem` 后跳回 `/`。

---

## 5. 登录认证设计

### 5.1 流程

```text
浏览器 index.html
  ├─ 管理员 Tab → POST /api/admin/login  {username, password}
  │     ▼
  │   walle_api.py
  │     ├─ 调 EMQX /api/v5/login 校验 Dashboard 账号（非 administrator 拒绝）
  │     ├─ 签发内存 token（12h，进程重启失效）→ home.html → admin.html
  │     └─ 401 → 提示“管理员账号或密码错误”
  │   admin.html 调 /api/users 时携带 Authorization: Bearer <token>
  │
  └─ 客户 Tab → POST /api/login  {username, password}
        ▼
      walle_api.py
        ├─ 以 MQTT CONNECT 连 1883 校验密码（等价于设备真实连接）
        ├─ 查 SQLite 角色：仅“客户”可登录，“设备”返回 403
        └─ 200 → { username, role, token } → home.html → user.html
```

对应实现：登录表单在 [index.html](../src/web/index.html)，客户登录在 [walle_api.py](../src/backend/walle_api.py)，反代在 [nginx.conf.example](../etc/nginx.conf.example)。

### 5.2 设计约束

1. **两套认证源**：管理员用 EMQX Dashboard 账号（`/api/admin/login`）；客户用 EMQX 内置数据库里的 MQTT 账号（`/api/login`），也就是「用户管理」页创建的用户名/密码。
2. **客户登录校验角色**：服务端查 SQLite，角色必须是「客户」，「设备」账号（小车）不能登录门户；管理员入口不做该校验。
3. **token 不参与后续鉴权**：门户页面与查询接口均不使用该 token；真正的接口凭据是 Nginx 注入的 API Key。
4. **失败处理**：密码错误 401；角色不符 403（提示来自后端）；EMQX 不可达 502；登录按钮在请求期间 `disabled`，防止重复提交。
5. **已知缺陷**：`/api/login` 与 `/api/admin/login` 未做限流与失败计数，存在暴力破解风险（见第 13 节）；`/api/users` 已要求管理员 token。

---

## 6. Nginx 反代设计

配置模板见 [nginx.conf.example](../etc/nginx.conf.example)；本机运行的 `etc/nginx.conf` 由模板复制并填入真实 API Key，不入版本库。

| location | 上游 | 关键处理 | 目的 |
|---|---|---|---|
| `/` | 本地 portal 目录 | `try_files $uri $uri/ /index.html` | 静态页面 + SPA 式回退 |
| `/api/` | `127.0.0.1:9000` | 反代到 walle_api.py | 登录与用户管理 |
| `/emqx-api/` | `18083/api/v5/` | 注入 `Authorization: Basic <base64(api_key:api_secret)>` | 免前端持有管理凭据 |
| `/srs-api/` | `1985/api/v1/` | `proxy_read_timeout 60s` | 媒体查询接口 |
| `/srs/` | `8080/` | `proxy_buffering off`、`read_timeout 3600s` | 长连接拉流（FLV/HLS/console/players） |
| `= /metrics` | `9972/metrics` | 直通 | Prometheus 抓取 |

设计取舍：

- `/srs/` 必须关闭缓冲，否则 FLV 直播会被 Nginx 攒包导致延迟累积；超时放宽到 1 小时以容忍长时间观看。
- `/emqx-api/` 前缀映射到 `/api/v5/`，前端只需 `fetch("/emqx-api/nodes")`，不感知版本号。
- 门户可由 `walle.service` 托管（`scripts/deploy.sh` 渲染并安装单元），也可用 `scripts/start-walle.sh` 手动 start/stop/status；以非特权端口 8088 运行，避免与系统 `/etc/nginx` 实例冲突。

---

## 7. EMQX 集成设计

### 7.1 使用的接口

| 接口 | 使用方 | 取用字段 |
|---|---|---|
| `POST /api/v5/login` | 登录页 | `token` |
| `GET /api/v5/nodes` | 管理员控制台 | `node_status`、`version`、`edition` |
| `GET /api/v5/clients?limit=1` | 管理员控制台 | `meta.count`（在线客户端数） |
| `GET /api/v5/subscriptions?limit=1` | 管理员控制台 | `meta.count`（活跃订阅数） |
| `GET /api/v5/metrics` | 管理员控制台 | `fields.incoming_messages_received`、`fields.outgoing_messages_sent` |

### 7.2 API Key 机制

- 通过 Dashboard 的 **Access Control → API Keys** 创建，得到 `api_key` / `api_secret`，落盘 `etc/emqx_api_key.json`（0600）。
- 使用时 `base64(api_key:api_secret)` 写入 Nginx 的 `Authorization` 头。
- 轮换方式：新建 Key → 重算 base64 → 替换 `etc/nginx.conf` → `scripts/stop-walle.sh` 后 `scripts/start-walle.sh start`。

### 7.3 License

未导入正式 license 时使用内置开发授权（`community` / `Development`，1000 万连接，到期 2029-03-01），`/api/v5/nodes` 仍返回 `edition: Enterprise`。生产必须导入正式 license。

---

## 8. 用户管理 API 设计

`src/backend/walle_api.py` 是零依赖的 Python 标准库服务，监听 `127.0.0.1:9000`，由 Nginx 的 `/api/` 反代进来。它不依赖外部数据库服务：密码存于 EMQX，角色存于本地 SQLite。

### 8.1 数据存储分工

| 数据 | 存储位置 | 说明 |
|---|---|---|
| 用户名 | EMQX 内置数据库 + SQLite | 创建后不可修改，同时作为 EMQX 的 `user_id` |
| 密码 | EMQX 内置数据库 | sha256 + suffix 加盐，walle 不存密码 |
| 角色 | `src/backend/data/users.db`（SQLite） | `客户` / `设备` 二选一，单文件自动创建 |
| 超级用户标记 | EMQX | 两种角色均 `is_superuser=false`，权限由 ACL 控制 |

### 8.2 接口

| 接口 | 方法 | 鉴权 | 用途 |
|---|---|---|---|
| `/api/admin/login` | POST | EMQX Dashboard 账号 | 管理员登录，签发管理 token |
| `/api/login` | POST | MQTT CONNECT 校验 | 客户登录（不签发管理权限） |
| `/api/users` | GET / POST | `Bearer <token>` | 用户列表 / 新增用户 |
| `/api/users/{用户名}` | PUT / DELETE | `Bearer <token>` | 改密 / 删除用户 |

### 8.3 鉴权与 token

- 管理员在登录页选择「管理员」→ 调用 `POST /api/admin/login` → `walle_api.py` 调 EMQX `/api/v5/login` 校验 Dashboard 账号（非 administrator 拒绝）→ 签发内存 token。
- token 只保存在内存、有效期 12 小时，进程重启即失效。
- 客户登录 `POST /api/login` 不签发管理权限（见第 5 节登录流程）。
- 所有 `/api/users*` 操作均要求请求头 `Authorization: Bearer <token>`。

### 8.4 启停与运维

- 启停合并进门户脚本：`scripts/start-walle.sh` 同时拉起 nginx 与 API，`scripts/stop-walle.sh` 同时停止两者。
- API 日志：`log/walle-api.log`；pid：`.run/nginx/walle-api.pid`。
- SQLite 使用 Python 内置 `sqlite3` 模块，无需安装 MySQL/PostgreSQL；备份只需拷贝 `src/backend/data/users.db`。
- 部署/上线步骤见 [walle_deployment_require.md](./walle_deployment_require.md) 第 8 节。

---

## 9. MQTT 业务约定

### 9.1 主题规范

| Topic | 方向 | 用途 | QoS |
|---|---|---|---|
| `walle/{device_id}/cmd` | 平台 → 小车 | 运动、录像、模式、鸣笛、灯光等指令 | 1 |
| `walle/{device_id}/state` | 小车 → 平台 | 位置、电量、传感器状态上报 | 1 |
| `walle/{device_id}/alarm` | 小车 → 平台 | 碰撞、低电量、离线等告警事件 | 1 |
| `walle/{device_id}/heartbeat` | 小车 → 平台 | 心跳，默认 30s | 0 |

约定：

- 一条设备对应一个 `device_id`，主题前缀即权限边界，后续 ACL 按 `walle/{device_id}/#` 授予。
- 指令载荷建议 JSON：`{"method":"move","value":"forward","seq":123,"ts":...}`，`seq` 用于设备端去重。
- 运动指令采用“按下发、松开停”语义，前端在 `mouseup`/`mouseleave`/`touchend` 时发 `stop`，防止失控。

### 9.2 设备侧状态

| 事件 | 上报主题 | 预期周期 |
|---|---|---|
| 心跳 | `heartbeat` | 30s；平台超过 3min 无心跳判离线 |
| 状态 | `state` | 1s（位置/速度）、30s（电量） |
| 告警 | `alarm` | 事件触发即上报 |

### 9.3 测试用客户端

`src/backend/mqtt_demo_client.py` 是零依赖 MQTT 客户端，用于验证连接与订阅（订阅 `demo/#`），不属于业务代码。

---

## 10. SRS 集成设计

### 10.1 推拉流链路

```text
小车/ffmpeg ──RTMP 1935──▶ SRS ──┬─ HTTP-FLV 8080  → 门户 /srs/{app}/{stream}.flv
                                 ├─ HLS 8080       → /srs/{app}/{stream}.m3u8
                                 └─ WebRTC 8000    → /srs/players/rtc_player.html
```

- 推流地址：`rtmp://<host>:1935/live/{stream}`。
- 播放地址（同源反代后）：`http://<host>:8088/srs/live/{stream}.flv`。
- 门户实时监控页使用 mpegts.js 播 HTTP-FLV，`isLive: true`、`hasAudio: false`（当前测试流带音频但页面静音播放，规避浏览器自动播放限制）。

### 10.2 媒体侧关键配置

| 配置项 | 值 | 说明 |
|---|---|---|
| `http_remux.mount` | `[vhost]/[app]/[stream].flv` | 开 HTTP-FLV，延迟最低 |
| `hls` | 分片 2s / 窗口 10s | 兼容 iOS 与回看 |
| `rtc.rtmp_to_rtc` | `on` | RTMP 推入即可 WebRTC 播放 |
| `rtc_server.candidate` | 主机 IP | 必须是浏览器可达地址，换机器必改 |
| `max_connections` | 800 | 必须小于 systemd `LimitNOFILE=10000` |
| `exporter` | 9972 | Prometheus 指标 |

### 10.3 使用的接口

| 接口 | 用途 |
|---|---|
| `GET /api/v1/streams/` | 活动流列表、`kbps.recv_30s` / `send_30s` 带宽 |
| `GET /api/v1/clients/` | 观看客户端数 |
| `GET /api/v1/versions/` | SRS 版本号 |
| `GET /metrics` | Prometheus 指标（经门户 `/metrics`） |

---

## 11. 控制台功能设计

### 11.1 管理员控制台（admin.html）

左侧菜单为“平铺 + 可折叠分组”，面板通过 `data-panel` 切换，**同一页面内切换不刷新**。数据加载：进入页面全量拉一次，之后每 5s 轮询 EMQX 与 SRS。

| 面板 | 数据来源 | 状态 |
|---|---|---|
| 基本信息 | `/emqx-api/nodes`、`/emqx-api/clients`、`/srs-api/streams/`、`/srs-api/versions/` | 已实现 |
| 服务状态表 | 同上接口按 5 个端口逐项探活 | 已实现（探活是接口级，非端口级） |
| MQTT · 概览 | `/emqx-api/metrics` 字段 + 订阅数 | 已实现 |
| MQTT · 用户管理 | `/api/users`（walle_api.py → EMQX 内置数据库） | 已实现：新增/删除/改密码，角色客户/设备 |
| MQTT · 主题配置 | 静态主题规范表 | 已实现（文档性质） |
| MQTT · 规则引擎 | — | 规划：alarm → 存储/Webhook |
| SRS · 概览 | `streams` + `clients` 聚合带宽 | 已实现 |
| SRS · 流管理 | `/srs-api/streams/` 明细表 | 已实现 |
| SRS · 服务配置 | 静态配置展示 + `candidate` 展示 | 已实现（静态） |
| 系统设置 | 版本、时区、license 类型；重启按钮 | 部分实现：重启为演示弹窗，需宿主机执行 |
| 普通用户管理 | — | 规划：用户表 + 设备绑定 |
| 日志 | — | 规划：EMQX/SRS/登录日志汇聚 |

### 11.2 普通用户控制台（user.html）

| 面板 | 数据来源 | 状态 |
|---|---|---|
| 设备列表 | 前端常量 `DEVICES`（3 台示例） | 占位，待接入后端设备接口 |
| 设备信息 | 选中设备对象 | 已实现（基于占位数据） |
| 实时监控 | `/srs/{stream}.flv` + mpegts.js | 已实现播放；控制指令为占位 |
| 视频录像 | — | 规划：SRS DVR + 索引检索 |
| 告警 | 前端常量 `ALARMS` | 占位，待订阅 `walle/+/alarm` |

运动控制指令当前只在页面“最近指令”日志中回显，真实实现需增加服务端发布接口（见 10.3）。

### 11.3 待补齐的服务端能力

前端已按下列接口约定留出调用位置，但服务端尚未实现：

| 能力 | 建议接口 | 实现方式 |
|---|---|---|
| 设备列表/详情 | `GET /walle-api/devices` | 后端读设备表或 EMQX 在线状态 |
| 指令下发 | `POST /walle-api/devices/{id}/cmd` | 后端用 MQTT 客户端 publish 到 `walle/{id}/cmd` |
| 告警查询 | `GET /walle-api/alarms` | 规则引擎落库后查询 |
| 录像检索 | `GET /walle-api/recordings` | SRS DVR 产出文件索引 |
| 重启/运维动作 | `POST /walle-api/ops/{action}` | 后端受限执行 systemctl，禁止前端直调 |

这类写操作必须经服务端（BFF）鉴权后执行，不能沿用当前“Nginx 注入管理 Key”的只读模式。

---

## 12. 数据模型规划

当前无数据库。规划中的最小模型：

```text
user      (id, username, password_hash, role, created_at)
device    (id, name, owner_user_id, location, stream_app, stream_name, last_seen_at)
alarm     (id, device_id, level, type, message, occurred_at, status)
recording (id, device_id, path, start_at, end_at, size, trigger)
```

约定：

- `device.id` 即 MQTT 主题中的 `{device_id}`。
- 每台设备归属 0..1 个普通用户；管理员可见全部。
- 用户与 MQTT 凭据一一映射（`username = user_{id}`），ACL 限定 `walle/{device_id}/#`。

---

## 13. 安全设计与已知风险

| 风险 | 现状 | 处置建议 |
|---|---|---|
| 管理 Key 暴露面 | `/emqx-api/` 对 8088 所有访问者开放，等同可读 EMQX 管理数据 | 引入 BFF 做会话鉴权；或限制 8088 来源 IP |
| 角色可伪造 | `walle_role` 存于前端 | 角色判定下沉到 BFF，登录后签发服务端会话 |
| 暴力破解 | `/api/login`、`/api/admin/login` 无限流 | Nginx `limit_req` + 失败锁定 |
| 默认凭据 | EMQX `admin/public`、默认 Erlang cookie | 部署时立即修改 |
| 明文传输 | 8088 为 HTTP | 全链路 HTTPS |
| 传输凭据 | 登录密码明文经 Nginx 转 EMQX（内网回环） | 保持回环；对外启用 TLS |
| 设备侧鉴权 | 暂无 ACL | 按 `walle/{device_id}/#` 下发 ACL，一设备一凭据 |

---

## 14. 演进路线

1. 增加 BFF 层：服务端会话、角色校验、指令下发、运维动作。
2. 接入数据库，落地设备/告警/录像模型（用户角色已用 SQLite 落地）。
3. 设备侧 MQTT 凭据与 ACL 自动化下发。
4. 管理员控制台补齐规则引擎、日志检索。
5. 普通用户控制台接入真实设备与告警订阅，录像回看。
6. 全链路 HTTPS、限流、审计。
7. 门户与测试推流 systemd 化，去掉手工启停。
