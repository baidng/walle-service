# walle 服务端详细设计

> 适用版本：walle-ai v0.1（2026-11-19，新增告警持久化）
> 运行环境：Ubuntu 24.04 arm64 裸机，EMQX Enterprise 6.3.2 + SRS 7.0.215 + Nginx 门户
> 本文只描述设计与实现细节；安装、配置、上线步骤见 [walle_deployment_require.md](./walle_deployment_require.md)。

---

## 1. 系统定位与范围

walle 是面向树莓派小车的远程控制平台。服务端由三部分组成：

| 层 | 组件 | 职责 |
|---|---|---|
| 接入/门户层 | Nginx（8088） | 静态页面托管、同源反代、注入 EMQX 管理凭据 |
| 消息层 | EMQX Enterprise 6.3.2 | MQTT 连接、主题路由、账号认证、规则引擎（告警回调） |
| 业务层 | `walle_api.py`（9000） | 登录会话、用户/绑定管理、告警落库与查询 |
| 存储层 | SQLite `src/backend/data/users.db` | 用户角色、设备绑定、运行设置、告警历史 |
| 媒体层 | SRS 7.0.215 | RTMP 接收、HTTP-FLV/HLS/WebRTC 分发、Prometheus 指标 |

服务端自 v0.1 起持有本地 SQLite：角色、设备绑定、推流密钥与**告警历史**均已落库；设备状态、录像索引仍直接读 MQTT/SRS 实时接口，设备与录像的持久化属于后续演进项（见第 13 节）。

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
        │  /api/internal/ → deny       │
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
        └───┬───────▲──┘  └─────▲────────┘
            │ MQTT  │            │ RTMP / WebRTC
            │       │            │
            │   ┌───▼────────────┴──┐
            │   │ 树莓派小车（设备）  │
            │   └───────────────────┘
            │ 规则引擎 HTTP 回调（回环）
            ▼
      ┌──────────────────────┐      ┌──────────────────┐
      │ walle_api 127.0.0.1  │─────▶│ SQLite users.db  │
      │ :9000                │      │ alarms / users   │
      └──────────────────────┘      └──────────────────┘
```

设计要点：

1. **单一入口**：浏览器只访问 8088，不直连 18083/1985/8080。EMQX 6.x 不返回 CORS 头，直连会因预检失败而不可用，因此门户必须同源反代。
2. **凭据分层**：管理员登录由 `walle_api.py` 调用 EMQX `/api/v5/login` 校验并签发内存 token；查询类接口 `/emqx-api/` 由 Nginx 注入只读 API Key，前端不持有管理凭据。
3. **前后端同源**：所有页面用相对路径请求，换机器/IP 时前端代码无需改动。
4. **登录态**：token 仅存 `sessionStorage`，关闭标签页即失效；服务端会话保存在 `walle_api.py` 内存中，进程重启即失效。
5. **告警旁路落库**：EMQX 规则引擎订阅告警主题，通过回环 HTTP 把消息交给 `walle_api.py`，不改变设备原有的 MQTT 发布/订阅链路；规则或 API 故障只影响历史留档，不影响实时告警显示。


### 2.1 目录规划


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
3. **token 的用途**：`/api/users` 等管理接口要求管理员 token；`/api/alarms` 要求任意有效会话 token，并按会话角色限定租户。`/emqx-api/` 查询仍由 Nginx 注入只读 API Key，前端不持有管理凭据。
4. **失败处理**：密码错误 401；角色不符 403（提示来自后端）；EMQX 不可达 502；登录按钮在请求期间 `disabled`，防止重复提交。
5. **已知缺陷**：`/api/login` 与 `/api/admin/login` 未做限流与失败计数，存在暴力破解风险（见第 14 节）；`/api/users` 已要求管理员 token。

---

## 6. Nginx 反代设计

配置模板见 [nginx.conf.example](../etc/nginx.conf.example)；本机运行的 `etc/nginx.conf` 由模板复制并填入真实 API Key，不入版本库。

| location | 上游 | 关键处理 | 目的 |
|---|---|---|---|
| `/` | 本地 portal 目录 | `try_files $uri $uri/ /index.html` | 静态页面 + SPA 式回退 |
| `/api/` | `127.0.0.1:9000` | 反代到 walle_api.py | 登录、用户管理、告警查询 |
| `/api/internal/` | — | `deny all` | 仅允许 EMQX 规则引擎走回环直连 9000 |
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

`src/backend/walle_api.py` 是零依赖的 Python 标准库服务，监听 `127.0.0.1:9000`，由 Nginx 的 `/api/` 反代进来。它不依赖外部数据库服务：密码存于 EMQX，角色/绑定/告警存于本地 SQLite。

### 8.1 数据存储分工

| 数据 | 存储位置 | 说明 |
|---|---|---|
| 用户名 | EMQX 内置数据库 + SQLite | 创建后不可修改，同时作为 EMQX 的 `user_id` |
| 密码 | EMQX 内置数据库 | sha256 + suffix 加盐，walle 不存密码 |
| 角色 | `src/backend/data/users.db`（SQLite） | `客户` / `设备` / `agent` 三选一，单文件自动创建 |
| 超级用户标记 | EMQX | 三种角色均 `is_superuser=false`，权限由 ACL 控制 |
| 告警历史 | `src/backend/data/users.db`（`alarms` 表） | EMQX 规则引擎回调落库，按租户隔离，见第 9 节 |

### 8.2 接口

| 接口 | 方法 | 鉴权 | 用途 |
|---|---|---|---|
| `/api/admin/login` | POST | EMQX Dashboard 账号 | 管理员登录，签发管理 token |
| `/api/login` | POST | MQTT CONNECT 校验 | 客户登录，签发客户会话 token（无管理权限） |
| `/api/users` | GET / POST | `Bearer <token>` | 用户列表 / 新增用户 |
| `/api/users/{用户名}` | PUT / DELETE | `Bearer <token>` | 改密 / 删除用户 |

### 8.3 鉴权与 token

- 管理员在登录页选择「管理员」→ 调用 `POST /api/admin/login` → `walle_api.py` 调 EMQX `/api/v5/login` 校验 Dashboard 账号（非 administrator 拒绝）→ 签发内存 token。
- token 只保存在内存、有效期 12 小时，进程重启即失效。
- 客户登录 `POST /api/login` 签发角色为「客户」的会话 token，只能查询本租户告警，不能访问管理接口（见第 5 节登录流程）。
- 会话记录为 `(username, expires_at, login_at, role)`，`role` 决定管理权限与告警可见范围。
- 所有 `/api/users*` 操作均要求管理员 token。

### 8.4 启停与运维

- 启停合并进门户脚本：`scripts/start-walle.sh` 同时拉起 nginx 与 API，`scripts/stop-walle.sh` 同时停止两者。
- API 日志：`log/walle-api.log`；pid：`.run/nginx/walle-api.pid`。
- SQLite 使用 Python 内置 `sqlite3` 模块，无需安装 MySQL/PostgreSQL；备份用 `sqlite3 src/backend/data/users.db ".backup backup.db"`，热备时不要只拷贝主库文件（WAL 中可能还有未合并数据）。
- 部署/上线步骤见 [walle_deployment_require.md](./walle_deployment_require.md) 第 8 节。

---

## 9. 告警持久化与历史查询设计

### 9.1 数据链路

```text
设备 publish walle/{tenant}/device/{device}/alarm/report
        │
        ▼
EMQX 规则引擎 walle_alarm_persist
  SQL: SELECT topic, payload FROM "walle/+/device/+/alarm/report"
        │
        ▼ HTTP action walle_alarm_hook
   POST http://127.0.0.1:9000/api/internal/alarms
   Header: X-Walle-Ingest: <token>
   Body: {"topic":"${topic}","payload":${payload}}
        │
        ▼
walle_api 校验来源/token → 去重 → INSERT OR IGNORE
        │
        ▼
SQLite alarms 表（保留 30 天）

浏览器 GET /api/alarms?from=&to=&device=...
        │ Authorization: Bearer <会话 token>
        ▼
walle_api 按会话角色限定 tenant → 按时间倒序分页返回
```

设计要点：

1. **旁路采集**：设备仍按原主题发布，浏览器仍通过 MQTT 实时接收；规则引擎只做额外转发，不改变业务链路。
2. **故障隔离**：EMQX 规则或 walle_api 异常只影响告警留档，实时告警显示不受影响；落库失败不会阻塞 MQTT 消息。
3. **去重**：EMQX 的 HTTP action 失败会重试，设备也可能重复上报，服务端以内容哈希做唯一约束，重复写入返回 `duplicate=true`。

### 9.2 主题与载荷

实际主题：`walle/{tenant}/device/{device}/alarm/report`。

| 字段 | 必填 | 说明 |
|---|---|---|
| `event` | 否 | 事件名，如 `temperature_high` |
| `level` | 否 | `info` / `warning` / `critical`，缺省 `info` |
| `metric` | 否 | 指标名，如 `temperature`、`battery` |
| `alarm_type` | 否 | 告警类型；兼容旧字段 `type` |
| `message` | 否 | 展示文案，最长 500 字符 |
| `timestamp` | 否 | 设备发生时间，秒/毫秒/微秒时间戳或 ISO 8601；兼容 `ts`、`occurred_at`、`time` |
| `datetime` | 否 | 可直接展示的时间文本，优先使用；缺失时按 `occurred_at` 本地格式化 |
| `hostname` | 否 | 设备展示名 |
| `device_id` | 否 | 设备唯一标识（v1.12 起不再使用 uuid，缺省时取主题中的 device 段） |
| `ip` | 否 | 设备 IP |

时间处理规则：

- 按 `timestamp` → `ts` → `occurred_at` → `time` 顺序取第一个非空值。
- 数字按 epoch 处理；大于 `1e12` 时循环除以 1000，兼容毫秒/微秒/纳秒。
- 字符串支持纯数字或 ISO 8601（含 `Z`）。
- 无法解析、早于 2000 年或晚于当前时间一年以上时，回退为服务端收到时间。
- 服务端统一存 `occurred_at`（epoch 秒）与 `occurred_text`（`YYYY-MM-DD HH:MM:SS`）。

去重键：`tenant|device|event|metric|alarm_type|原始时间|message` 的 SHA-1，作为 `alarms.dedupe` 唯一约束。

### 9.3 表结构

```sql
CREATE TABLE IF NOT EXISTS alarms (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant        TEXT NOT NULL,
    device        TEXT NOT NULL,
    hostname      TEXT,
    device_uuid   TEXT,
    ip            TEXT,
    event         TEXT,
    level         TEXT,
    metric        TEXT,
    alarm_type    TEXT,
    message       TEXT,
    occurred_at   INTEGER NOT NULL,
    occurred_text TEXT,
    received_at   INTEGER NOT NULL,
    detail        TEXT,
    dedupe        TEXT NOT NULL UNIQUE
);

CREATE INDEX idx_alarms_tenant_time ON alarms(tenant, occurred_at DESC);
CREATE INDEX idx_alarms_device_time ON alarms(device, occurred_at DESC);
CREATE INDEX idx_alarms_time        ON alarms(occurred_at DESC);
```

> `alarms.device_uuid` 为历史列名：MQTT v1.12 起报文不再携带 uuid，该列现在存 `device_id`
> （缺省取主题中的 device 段），仅用于兼容已有数据库。

- `detail` 保存完整 payload JSON，便于后续排障与字段扩展。
- SQLite 开启 WAL，连接设置 `busy_timeout=5000`，避免规则回调与页面查询互相阻塞。
- 保留期按 `occurred_at` 计算，默认 30 天；接入请求触发的清理最多每小时执行一次，也可通过 `WALLE_ALARM_RETENTION_DAYS` 调整。

### 9.4 接入接口

`POST /api/internal/alarms`（仅回环地址）

| 项 | 说明 |
|---|---|
| 来源限制 | 仅接受 `127.0.0.1` / `::1`；Nginx 对 `/api/internal/` 配置 `deny all` |
| 鉴权 | 请求头 `X-Walle-Ingest` 必须等于 `etc/walle.conf` 的 `WALLE_INGEST_TOKEN` |
| 请求体 | `{"topic":"walle/{tenant}/device/{device}/alarm/report","payload":{...}}`；`payload` 可为对象或 JSON 字符串 |
| 成功响应 | `200 {"ok":true,"duplicate":false,"id":123}` |
| 重复消息 | `200 {"ok":true,"duplicate":true,"id":...}` |
| 失败响应 | `400` 主题/载荷不合法；`403` 来源或 token 不合法；`503` 未配置 token |

### 9.5 查询接口

`GET /api/alarms`，要求 `Authorization: Bearer <token>`。

| 参数 | 类型 | 说明 |
|---|---|---|
| `from` | epoch 秒或 ISO | `occurred_at >= from`，可空 |
| `to` | epoch 秒或 ISO | `occurred_at <= to`，可空 |
| `device` | string | 精确匹配设备，可空 |
| `level` | string | `info` / `warning` / `critical`，可空 |
| `tenant` | string | 仅管理员可用；客户忽略并强制为本租户 |
| `page` | int | 页码，从 1 开始，默认 1；超出总页数时自动收敛到最后一页 |
| `limit` | int | 每页条数，默认 30，上限 30 |

- 客户只能看到 `tenant = 会话用户名` 的告警；管理员默认看全部，可按 `tenant` 过滤。
- 排序固定为 `occurred_at DESC, id DESC`，分页使用 `LIMIT/OFFSET`，便于前端渲染页码。
- 响应包含 `items`、`total`、`page`、`pages`、`page_size`；`pages = 0` 表示没有匹配数据。
- 单条记录字段与 9.3 表结构一致（不含 `detail`）。

`GET /api/alarms/devices` 返回当前可见范围内的设备聚合：

```json
{"data":[{"device":"car1","hostname":"小车 1 号","count":12,"last_at":1795056654}]}
```

### 9.6 前端交互

普通用户控制台「告警信息」面板分为「当前」「历史」两个子 Tab：

- **当前**：只显示本次页面打开后通过 MQTT 实时收到的 `walle/{tenant}/device/+/alarm/report` 告警，不再限制当前选中设备。内存中保留最近 300 条，每页 30 条；超过一页时底部显示「上一页 / 下一页」。新到的告警在行尾显示未读红点，点击该条后红点消失；存在未读告警时左侧「告警信息」菜单右侧也显示红点，「全部已读」按钮可一次清除全部未读标记。
- **历史**：从 `/api/alarms` 查询数据库，每页固定最多 30 条；超过一页时底部显示「第1页、第2页、…、最后页」，点击页码直接跳转。
- **过滤栏**：仅历史 Tab 显示，包含设备下拉（含「全部设备」）、开始/结束时间（`datetime-local`，未选择时显示「—」，表示不加时间过滤）和「查询」「清除」。点击查询从第 1 页重新检索；切换页码沿用当前过滤条件。
- 进入历史 Tab 的首次操作会加载全部告警（不加时间过滤、全部设备）的第 1 页；之后切换 Tab 保留已查结果，避免重复请求。
- 当前 Tab 收到的告警不影响历史 Tab 的查询结果；实时告警的持久化由 EMQX 规则独立完成，不依赖页面是否打开。
- 告警条目统一为三行：设备名、消息、事件/级别/指标/类型；右侧为发生时间。

### 9.7 EMQX 规则配置

当前环境中的对象（均为 EMQX 6.3.2）：

| 对象 | ID | 关键配置 |
|---|---|---|
| Connector | `http:walle_alarm_http` | `url=http://127.0.0.1:9000`，`pool_size=4` |
| Action | `http:walle_alarm_hook` | `POST /api/internal/alarms`，请求头带 `X-Walle-Ingest` |
| Rule | `walle_alarm_persist` | `SELECT topic, payload FROM "walle/+/device/+/alarm/report"` |

Action body 模板：

```json
{"topic":"${topic}","payload":${payload}}
```

注意：

- 模板要求 `payload` 本身是合法 JSON 对象；设备侧告警载荷必须是 JSON。
- `X-Walle-Ingest` 必须与 `etc/walle.conf` 中的 `WALLE_INGEST_TOKEN` 完全一致，轮换 token 时需同时更新 Connector/Action 与配置文件。
- 规则只订阅 `walle/{tenant}/device/{device}/alarm/report`，不会把其他主题写入告警表。
- 规则、Action、Connector 的状态可在 EMQX Dashboard「集成 → 规则」中查看，失败重试次数由 Action 的 `max_retries` 控制。

### 9.8 运维与限制

- 备份：停止写入时拷贝 `src/backend/data/users.db`；WAL 模式下热备需同时处理 `users.db-wal` / `users.db-shm`，或使用 `sqlite3 users.db ".backup backup.db"`。
- 容量：单条告警通常小于 1 KB；按 100 台设备、每台每天 100 条估算，30 天约 30 万条，SQLite 可轻松承载。
- 当前未实现告警确认/处理状态、按级别推送、跨租户管理员统计；这些属于后续演进。
- `detail` 保存原始 payload，若设备上报超大字段会放大库体积，设备侧应保持载荷精简。

---

## 10. MQTT 业务约定

### 10.1 主题规范

主题结构：`walle/{tenant}/device/{device}/{resource}`。完整契约以
`walle-ai/doc/MQTT_protocol_convention.md`（v1.13）为准，服务端只依赖以下主题：

| 能力 | 平台 → 设备（请求/命令） | 设备 → 平台（应答/上报） |
|---|---|---|
| 上线/下线 | — | `online`（retained + LWT） |
| 客户端在线 | — | `walle/{tenant}/client/{client_id}/online`（租户级，retained + LWT） |
| 设备发现 | `walle/{tenant}/discovery/query/request` | `discovery/query/response` |
| 基础状态 | `baseState/query/request` | `baseState/query/response` |
| 运行状态 | `runState/query/request` | `runState/query/response` |
| 告警 | — | `alarm/report` |
| 推流 | `video/start/request`、`video/stop/request` | `video/start/response`、`video/stop/response` |
| 手机音频 | `phone/audio/send/request`、`phone/audio/stop/request` | 对应 `response`（Web 不订阅） |
| 录像 | `record/start/request`、`record/stop/request`、`record/delete/request`、`record/list/query/request`、`record/play/start/request`、`record/play/stop/request` | 对应 `response` |
| 车控 | `car/motor/set/cmd`、`car/servo/set/cmd`、`car/servo/status/query/request` | `car/servo/status/query/response` |

约定：

- `tenant` 为客户用户名；主题第 4 段 `device` 为稳定的 `device_id`，不是 MQTT 用户名。
- ACL 按客户（`walle/{tenant}/#`）、设备（`walle/{tenant}/device/${clientid}/#`、
  `walle/{tenant}/discovery/query/request` 与 `walle/{tenant}/client/+/online`）、
  agent（`walle/{tenant}/agents/${clientid}/#` 与本租户 `walle/{tenant}/device/#`）三类授予。
- v1.12 起请求与应答均不再携带 `uuid`，设备身份只使用主题中的 `device_id`。
- `car/motor/set/cmd`、`car/servo/set/cmd` 为无应答的持续控制主题，执行结果只写设备端日志。

### 10.2 设备侧状态

| 数据 | 来源 | 说明 |
|---|---|---|
| 在线状态 | `online` | retained 上线报文 + LWT 遗嘱 |
| 客户端在线 | `client/{client_id}/online` | App/Web retained 上线；设备端订阅 `client/+/online` |
| 基础状态 | `baseState/query/response` | 按需查询，不落盘 |
| 运行状态 | `runState/query/response` | 按需查询推流/音频/录像/电机/舵机/电池/声呐 |
| 告警 | `alarm/report` | 主动上报，由 EMQX 规则落库 |

### 10.3 测试用客户端

`src/backend/mqtt_demo_client.py` 是零依赖 MQTT 客户端，用于验证连接与订阅（订阅 `demo/#`），不属于业务代码。

---

## 11. SRS 集成设计

### 11.1 推拉流链路

```text
小车/ffmpeg ──RTMP 1935──▶ SRS ──┬─ HTTP-FLV 8080  → 门户 /srs/{app}/{stream}.flv
                                 ├─ HLS 8080       → /srs/{app}/{stream}.m3u8
                                 └─ WebRTC 8000    → /srs/players/rtc_player.html
```

- 推流地址：`rtmp://<host>:1935/live/{stream}`。
- 播放地址（同源反代后）：`http://<host>:8088/srs/live/{stream}.flv`。
- 门户实时监控页使用 mpegts.js 播 HTTP-FLV，`isLive: true`、`hasAudio: false`（当前测试流带音频但页面静音播放，规避浏览器自动播放限制）。

### 11.2 媒体侧关键配置

| 配置项 | 值 | 说明 |
|---|---|---|
| `http_remux.mount` | `[vhost]/[app]/[stream].flv` | 开 HTTP-FLV，延迟最低 |
| `hls` | 分片 2s / 窗口 10s | 兼容 iOS 与回看 |
| `rtc.rtmp_to_rtc` | `on` | RTMP 推入即可 WebRTC 播放 |
| `rtc_server.candidate` | 主机 IP | 必须是浏览器可达地址，换机器必改 |
| `max_connections` | 800 | 必须小于 systemd `LimitNOFILE=10000` |
| `exporter` | 9972 | Prometheus 指标 |

### 11.3 使用的接口

| 接口 | 用途 |
|---|---|
| `GET /api/v1/streams/` | 活动流列表、`kbps.recv_30s` / `send_30s` 带宽 |
| `GET /api/v1/clients/` | 观看客户端数 |
| `GET /api/v1/versions/` | SRS 版本号 |
| `GET /metrics` | Prometheus 指标（经门户 `/metrics`） |

---

## 12. 控制台功能设计

### 12.1 管理员控制台（admin.html）

左侧菜单为“平铺 + 可折叠分组”，面板通过 `data-panel` 切换，**同一页面内切换不刷新**。数据加载：进入页面全量拉一次，之后每 5s 轮询 EMQX 与 SRS。

| 面板 | 数据来源 | 状态 |
|---|---|---|
| 基本信息 | `/emqx-api/nodes`、`/emqx-api/clients`、`/srs-api/streams/`、`/srs-api/versions/` | 已实现 |
| 服务状态表 | 同上接口按 5 个端口逐项探活 | 已实现（探活是接口级，非端口级） |
| MQTT · 概览 | `/emqx-api/metrics` 字段 + 订阅数 | 已实现 |
| MQTT · 用户管理 | `/api/users`（walle_api.py → EMQX 内置数据库） | 已实现：新增/删除/改密码，角色客户/设备/agent |
| MQTT · 设备绑定 | `/api/bindings`、`/api/acl/preview` | 已实现：客户/设备/agent 三元 1:1 绑定、绑定即写 ACL、只读预览 |
| MQTT · 规则引擎 | EMQX `walle_alarm_persist` | 已实现：alarm → HTTP 回调 → SQLite |
| SRS · 概览 | `streams` + `clients` 聚合带宽 | 已实现 |
| SRS · 流管理 | `/srs-api/streams/` 明细表 | 已实现 |
| SRS · 服务配置 | 静态配置展示 + `candidate` 展示 | 已实现（静态） |
| 系统设置 | 版本、时区、license 类型；重启按钮 | 部分实现：重启为演示弹窗，需宿主机执行 |
| 普通用户管理 | — | 规划：用户表 + 设备绑定 |
| 日志 | — | 规划：EMQX/SRS/登录日志汇聚 |

### 12.2 普通用户控制台（user.html）

| 面板 | 数据来源 | 状态 |
|---|---|---|
| 设备列表 | 前端常量 `DEVICES`（3 台示例） | 占位，待接入后端设备接口 |
| 设备信息 | 选中设备对象 | 已实现（基于占位数据） |
| 实时监控 | `/srs/{stream}.flv` + mpegts.js | 已实现播放；控制指令为占位 |
| 视频录像 | — | 规划：SRS DVR + 索引检索 |
| 告警 | MQTT 实时 + `/api/alarms` | 已实现：全部设备实时告警、历史查询、设备/时间过滤、分页 |

运动控制指令当前只在页面“最近指令”日志中回显，真实实现需增加服务端发布接口（见 12.3）。

### 12.3 待补齐的服务端能力

前端已按下列接口约定留出调用位置，但服务端尚未实现：

| 能力 | 建议接口 | 实现方式 |
|---|---|---|
| 设备列表/详情 | `GET /walle-api/devices` | 后端读设备表或 EMQX 在线状态 |
| 指令下发 | `POST /walle-api/devices/{id}/cmd` | 后端用 MQTT 客户端 publish 到 `walle/{tenant}/device/{device}/car/motor/set/cmd` |
| 录像检索 | `GET /walle-api/recordings` | SRS DVR 产出文件索引 |
| 重启/运维动作 | `POST /walle-api/ops/{action}` | 后端受限执行 systemctl，禁止前端直调 |

这类写操作必须经服务端（BFF）鉴权后执行，不能沿用当前“Nginx 注入管理 Key”的只读模式。

---

## 13. 数据模型

`src/backend/data/users.db` 中已落地的表：

```text
users    (username, role, created_at)
bindings (device, customer, created_at)
settings (key, value)
alarms   (id, tenant, device, hostname, device_uuid, ip, event, level, metric,
          alarm_type, message, occurred_at, occurred_text, received_at,
          detail, dedupe)
```

后续规划（尚未落库）：

```text
device    (id, name, owner_user_id, location, stream_app, stream_name, last_seen_at)
recording (id, device_id, path, start_at, end_at, size, trigger)
```

约定：

- `device` 为稳定的 `device_id`，位于主题固定段 `device/` 之后；`tenant` 为客户用户名。
- 客户、设备、agent 三者 1:1（`bindings(device 主键, customer UNIQUE, agent UNIQUE)`），管理员可见全部。
- 用户与 MQTT 凭据一一映射，ACL 按客户 / 设备 / agent 三类生成：客户 `walle/{tenant}/#`；设备 `walle/{tenant}/device/${clientid}/#`、`walle/{tenant}/discovery/query/request` 与 `walle/{tenant}/client/+/online`；agent `walle/{tenant}/agents/${clientid}/#` 与本租户 `walle/{tenant}/device/#`。
- 告警按 `tenant` 隔离，客户查询时由服务端强制追加租户条件。

---

## 14. 安全设计与已知风险

| 风险 | 现状 | 处置建议 |
|---|---|---|
| 管理 Key 暴露面 | `/emqx-api/` 对 8088 所有访问者开放，等同可读 EMQX 管理数据 | 引入 BFF 做会话鉴权；或限制 8088 来源 IP |
| 角色可伪造 | `walle_role` 仍存于前端，但仅影响页面跳转 | 管理接口与告警查询均按服务端会话 `role` 校验，前端角色无法提权 |
| 告警越权查询 | 客户令牌可能被用于查询其他租户 | 查询强制 `tenant = 会话用户名`，管理员才可跨租户 |
| 告警接入伪造 | `/api/internal/alarms` 可写库 | 仅回环地址 + `X-Walle-Ingest` 共享 token；Nginx `deny all` |
| 暴力破解 | `/api/login`、`/api/admin/login` 无限流 | Nginx `limit_req` + 失败锁定 |
| 默认凭据 | EMQX `admin/public`、默认 Erlang cookie | 部署时立即修改 |
| 明文传输 | 8088 为 HTTP | 全链路 HTTPS |
| 传输凭据 | 登录密码明文经 Nginx 转 EMQX（内网回环） | 保持回环；对外启用 TLS |
| 设备侧鉴权 | 已按租户/设备生成 EMQX ACL | 设备凭据轮换与自动下发仍需完善 |

---

## 15. 演进路线

1. BFF 能力补全：设备列表、指令下发、运维动作（服务端会话与角色校验已具备）。
2. 告警增强：确认/处理状态、分级订阅推送、非 JSON 载荷兜底。
3. 落地设备与录像模型（用户、绑定、设置、告警已用 SQLite 落地）。
4. 设备侧 MQTT 凭据与 ACL 自动化下发。
5. 管理员控制台补齐日志检索与告警总览。
6. 全链路 HTTPS、限流、审计。
7. 门户与测试推流 systemd 化，去掉手工启停。
