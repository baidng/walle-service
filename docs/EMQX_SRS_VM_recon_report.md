# EMQX 6.3.2 / SRS 7.0 现场核查报告（Ubuntu VM）

- 核查主机：`gs@192.168.31.114`（hostname `mac-ubuntu`）
- 核查时间：2026-11-15
- 核查方式：SSH 登录 + 只读命令 + 源码浅克隆（全部位于 `~/01_walle_service/recon/`）
- 说明：本报告只陈述可复现证据；标注“推断”的条目未获官方明文确认。

---

## 0. 结论先行

| 问题 | 结论 | 证据强度 |
|---|---|---|
| EMQX 6.3.2 是否存在 | 存在，tag `6.3.2`，commit `6e4ee3d`，提交时间 2026-10-19 | 源码 tag 实测 |
| EMQX 仓库是否含 Dashboard 前端源码 | **不含**。`apps/emqx_dashboard` 只有 Erlang 后端与 `priv/api-spec*.html`，没有 `package.json`/`vite.config`/`www` | 目录清单 + 源码 |
| Dashboard 前端是否公开 | **公开**，独立仓库 `emqx/emqx-dashboard5`，默认分支 `enterprise`，可克隆 | 克隆成功 |
| Dashboard 前端许可证 | **BUSL-1.1**（Business Source License），source-available，不是 OSI 开源 | LICENSE 原文 |
| 前端是否“闭源” | **否**，源码可得；但生产使用受 Additional Use Grant 约束 | LICENSE 原文 |
| EMQX 6.3.2 对应前端版本 | Makefile 固定 `EMQX_DASHBOARD_VERSION ?= 2.3.2-1`，从 `emqx-dashboard5` releases 下载 zip | Makefile + get-dashboard.sh |
| 能否不改前端、仅靠服务端配置加左侧一级菜单 | **不能**。菜单是前端硬编码数组；后端 `apps/emqx_dashboard/src` 中没有任何 menu 相关代码 | grep 空结果 + 前端源码 |
| 官方插件 UI 能否进左侧一级菜单 | **不能**。插件 `index` 只出现在“Plugins → 插件详情”的 iframe Tab，不是左侧导航 | `PluginDetail.vue:65-72` |
| 要加一级菜单的唯一路径 | fork `emqx-dashboard5`，改菜单数组 + 路由 + i18n，重新构建并替换 `priv/www` | 源码结构 |
| SRS 许可证 | MIT（6.0-r3 与 7.0-a4 相同） | LICENSE 原文 |
| SRS console 是否静态目录 | **是**。`trunk/research/console` 由构建脚本复制到 `objs/nginx/html/console`，由 `http_server.dir` 托管；不存在 `console {}` 配置块 | depends.sh:301 + full.conf grep |
| SRS console 6.0-r3 与 7.0-a4 是否一致 | **字节级一致**（`ng_index.html`、`srs.console.js` md5 相同） | md5sum |
| 挂 `/srs/` 后 console 的问题 | 静态资源用相对路径，能加载；但 API 地址是 `schema://host:port`，不认路径前缀，需代理根 `/api/v1`、`/rtc/v1` 或用 query 参数指定 | srs.console.js:692-718 |
| 推流/拉流路径是否受影响 | **不受影响**。RTMP 1935、HTTP-FLV/HLS 8080 与 console 路径无关 | 配置与源码 |

**推荐结论：**

1. 若目标是“左侧一级菜单 + 原生观感”，**只有 fork 前端一条路**，成本主要在长期升级维护。
2. 若目标是“统一门户 + 深度定制后台 UI”，**自建门户 + EMQX REST API** 更稳，且不触碰 BUSL 前端。
3. 若只是“在 EMQX 里能看到 SRS”，用官方插件 `index` iframe（入口在插件详情页）成本最低，但达不到一级菜单。
4. 若想换 broker 以获得“可自由改的官方 UI”，优先评估 ThingsBoard（UI 全开源）与 RabbitMQ（管理 UI 开源），但它们与 EMQX 的 MQTT 能力/规模定位不同。

---

## 1. 环境盘点（实测）

```text
Ubuntu 24.04.5 LTS (Noble Numbat)
Linux mac-ubuntu 7.0.0-42-generic #42~24.04.1-Ubuntu SMP ... aarch64
CPU: 8
Mem: 15Gi total, 12Gi free, 4.0Gi swap
Disk: /dev/sda2 62G, used 11G (19%), avail 48G
```

| 项 | 结果 |
|---|---|
| Docker | **已安装**：Docker Engine 29.9.3、Compose v5.7.1、containerd 2.4.2；服务 active/enabled；`gs` 不在 docker 组（需 usermod，待确认） |
| Docker Compose | 未安装 |
| git | 2.43.0 |
| curl | 第二轮已安装（8.5.0-2ubuntu10.16） |
| wget / jq / nc / python3 | 可用 |
| 已监听端口 | 22、53（systemd-resolved）、631（cups）、127.0.0.1:30631 |
| 外网 | `wget --spider https://github.com` 成功；`raw.githubusercontent.com` 的裸域名探测失败（无路径时返回 404，属探测方式问题，不代表被墙） |
| `~/01_walle_service` 初始内容 | 空目录 |
| EMQX | 未安装（`command -v emqx` 空，`/opt/emqx`、`/etc/emqx` 不存在，dpkg 无匹配） |
| SRS | 未安装（`command -v srs` 空，`/opt/srs`、`/usr/local/srs` 不存在） |
| sudo | 需要密码（本轮未执行任何 sudo 写操作） |

---

## 2. EMQX 6.3.2：仓库边界与许可证

### 2.1 仓库包含什么

克隆：`git clone --depth 1 --branch 6.3.2 https://github.com/emqx/emqx.git`

顶层目录（实测）：

```text
apps  bin  build  changes  deploy  rel  plugins  scripts
Makefile  mix.exs  mix  env.sh  LICENSE  NOTICE  PLUGIN.md  README.md
```

`apps/emqx_dashboard` 实测：

```text
etc  include  mix.exs  priv  README.md  src  test
```

- `priv/` 只有 `api-spec.html`、`api-spec-login.html`；
- **没有** `www/`、`package.json`、`vite.config.*`、`index.html`、`src/` 前端目录。

`apps/emqx_dashboard/README.md` 原文：

> "Note, at this point EMQX Dashboard itself is an independent frontend project and is integrated through a static file handler. This code here is responsible to provide an HTTP(S) server to give access to it and its underlying API calls."

静态文件处理在 `apps/emqx_dashboard/src/emqx_dashboard.erl:450-451`：

```erlang
{"/", cowboy_static, {priv_file, emqx_dashboard, "www/index.html"}},
{"/static/[...]", cowboy_static, {priv_dir, emqx_dashboard, "www/static"}}
```

说明：**broker 仓库只负责托管 `priv/www`，不包含前端源码。**

### 2.2 前端从哪来：构建链证据

`Makefile` 头部：

```make
# Dashboard version
# from https://github.com/emqx/emqx-dashboard5
export EMQX_DASHBOARD_VERSION ?= 2.3.2-1
```

`scripts/get-dashboard.sh`：

```bash
RELEASE_ASSET_FILE="emqx-enterprise-dashboard-$VERSION.zip"
DASHBOARD_REPO='emqx-dashboard5'
DIRECT_DOWNLOAD_URL="https://github.com/emqx/${DASHBOARD_REPO}/releases/download/${VERSION}/${RELEASE_ASSET_FILE}"
...
unzip -o -q "${RELEASE_ASSET_FILE}" -d "$DASHBOARD_PATH"
rm -rf "$DASHBOARD_PATH/www"
mv "$DASHBOARD_PATH/dist" "$DASHBOARD_PATH/www"
```

结论：EMQX 官方构建时**下载预编译好的 Dashboard zip**，解压后把 `dist` 改名为 `priv/www`。所以：

- 前端源码在独立仓库；
- broker 仓库不含前端源码；
- 想改前端，必须改 `emqx-dashboard5` 并重新产出 zip/dist。

### 2.3 前端仓库与版本

- 仓库：`https://github.com/emqx/emqx-dashboard5`
- 默认分支：`enterprise`
- 克隆成功，`package.json`：`name=emqx-dashboard5`、`version=6.3.0`、`license=BUSL-1.1`
- 最新已发布 tag：`2.3.3`（2026-11-10，据 GitHub Releases API）
- EMQX 6.3.2 的 Makefile 固定的是 `2.3.2-1`
- 4.x 旧前端仓库：`emqx/emqx-dashboard-frontend`，Apache-2.0，最后 tag `v4.4.11`（仅历史参考）

> 注意：dashboard 的 release tag（2.3.x）与 package.json 的 version（6.3.0）不是同一套编号；本报告不推断二者映射，只陈述 Makefile 的固定值。

### 2.4 许可证原文与工程解读

`LICENSE`（EMQX 6.3.2）：

```text
Business Source License 1.1
Licensor: EMQ Technologies Incorporated
Licensed Work: EMQX Version 5.9.0 or later.
Additional Use Grant: a. You may make production use of a single node of the Licensed
                      Work, provided your use does not include offering the Licensed
                      Work to third parties on a hosted or embedded basis.
                      b. Educational users ... and non-profit organizations ...
                      may use the Licensed Work in a production environment without
                      restriction, provided such use is not for commercial profit.
Change Date: Four years from the date the Licensed Work is published.
Change License: Apache License, Version 2.0
```

`emqx-dashboard5/LICENSE`（Dashboard 单独许可）：

```text
Business Source License 1.1
Licensed Work: EMQX Dashboard. This License applies separately to each version ...
Additional Use Grant: You may make production use of the Licensed Work in
                      conjunction with the EMQX broker to manage and monitor that
                      broker, provided the corresponding use of the broker is
                      permitted under the license applicable to that broker ...
Change Date: Four years from the first publicly available distribution ...
Change License: Apache License, Version 2.0
```

工程解读（**不构成法律意见**）：

| 场景 | 结论 |
|---|---|
| 自己改前端、内部单节点生产使用 | 许可文本授予 copy/modify/derivative works 权利；单节点生产在 Additional Use Grant 内。可行，但需保留许可声明。 |
| 内部多节点生产 | 超出“single node” grant，需要商业授权（按 EMQX 商业条款）。 |
| 对外分发修改后的前端 | BSL 允许 redistribute，但接收方生产使用仍受 grant 限制；把 EMQX/Dashboard 以 hosted 或 embedded 方式提供给第三方，需要商业授权。 |
| 四年后 | 每个版本按 Change Date 转为 Apache-2.0（按版本分别计算）。 |

> 关键点：**“源码公开”不等于“开源”**。BUSL 是 source-available；改代码本身不被禁止，但“生产使用范围”和“对外分发形态”受限制。涉及对外商业化分发时务必走法务/商务确认。

### 2.5 是否存在服务端菜单注册点

- `grep -rIn -i "menu" apps/emqx_dashboard/src` → **无结果**
- `grep -rIl --include=*.erl -iE "dashboard.*menu|menu.*dashboard" apps/` → **无结果**
- 菜单/路由全部在前端：
  - `src/hooks/useMenus.ts`：静态 `totalMenuList` 数组（monitoring/auth/ruleengine/.../plugins/management/diagnose/system）
  - `src/views/Base/LeftBar.vue`：消费 `useMenus()` 渲染左侧栏
  - `src/router/index.ts`：静态路由表
  - `src/common/featureGate.ts`：`FEATURE_ROUTE_MAP` 只把 feature gate 映射到**已存在**的路由，用于显示/隐藏，不能新增菜单

服务端唯一的“影响面”是 feature gate 列表（`GET /api/v5/features`）决定哪些已有菜单可见，**没有下发菜单项的 API**。

---

## 3. 加一级菜单：改动点与构建链

### 3.1 必须改动的位置（fork 前端）

| 文件 | 改动 |
|---|---|
| `src/hooks/useMenus.ts` | 在 `totalMenuList` 增加一项，如 `{ title: 'srs', icon: Video, path: '/srs' }` |
| `src/router/index.ts` | 增加 `/srs` 路由与组件（`component: () => import('@/views/Srs/Index.vue')`） |
| `src/i18n/components.ts` | 增加 `srs` 菜单文案（模板用 `components.${title}` 取词条） |
| `src/views/Srs/Index.vue` | 新页面：iframe SRS console/player，或调 BFF 渲染流/客户端表格 |
| `src/common/featureGate.ts` | 可选，若希望该菜单受服务端 feature 控制 |
| 图标 | `lucide-vue-next` 引入图标（现有菜单均来自该库） |

### 3.2 构建链

实测 `vite.config.js`：`base: './'`、`build.assetsDir: 'static'`、`server.port: 7001`、开发代理 `/api/v5`。

```bash
# 在 emqx-dashboard5 中
pnpm install
pnpm build          # vite build -> dist/
# 打包为 EMQX 期望的资产名
zip -r emqx-enterprise-dashboard-<version>.zip dist
```

然后两种落地方式：

1. **官方构建方式**：把 zip 放到 `emqx-dashboard5` 的 release（或本地覆盖 `EMQX_DASHBOARD_VERSION` 下载源），重新构建 EMQX，使 `get-dashboard.sh` 解压到 `apps/emqx_dashboard/priv/www`。
2. **替换已安装产物**：直接把 `dist/*` 覆盖到 `$EMQX/lib/emqx_dashboard-*/priv/www/`（**非官方**，升级会被覆盖）。

### 3.3 升级维护成本

- 每次 EMQX 升级（尤其 minor）都要 rebase 你的 fork；Dashboard 的 API 客户端、路由、组件会变。
- 需要跟 `EMQX_DASHBOARD_VERSION` 对齐，否则 `priv/www/version` 校验会触发重新下载。
- BUSL 声明必须保留；对外分发要重新评估授权。
- 需要前端构建环境（Node/pnpm）与回归测试，CI 成本持续存在。

### 3.4 插件机制能否替代

不能实现“左侧一级菜单”。官方插件 UI 的证据：

`src/views/Plugins/PluginDetail.vue:65-72`：

```vue
<el-tab-pane v-if="hasPluginUI" :label="tl('pluginUI')" name="ui" :lazy="true">
  <iframe :src="pluginUIUrl" frameborder="0" class="w-full iframe-container" />
</el-tab-pane>
```

`:219`：

```ts
() => `${windowLocation}${API_BASE_URL}/plugin_api/${pluginName.value}${pluginInfo.value?.index}`
```

结论：插件 `index` 是“插件详情页里的一个 Tab”，不是导航菜单；后端 `/plugin_api/:plugin/[...]` 也只是 API 网关，不参与菜单渲染。

---

## 4. 替代 MQTT Broker 选型

> 说明：许可证与能力以官方仓库/文档为准；“UI 可改”指源码可获取并允许修改（仍需遵守各自许可证）。

| Broker | 许可证 | 自带 Web 管理界面 | 界面源码可改 | MQTT 5.0 | REST/HTTP API | 集群 | 部署复杂度 |
|---|---|---|---|---|---|---|---|
| EMQX 6.3.2 | BUSL-1.1（source-available） | 有（Dashboard） | 可改（BUSL，生产受 grant 约束） | 是 | `/api/v5` 覆盖最全 | 是 | 中 |
| VerneMQ | Apache-2.0 | 有（vmq_webadmin，HTTP listener 8888） | 可改（Apache-2.0） | 是 | 管理 HTTP API | 是（Erlang 分布式） | 中高 |
| Eclipse Mosquitto | EPL-2.0 / EDL | **无** | 不适用 | 是 | 无官方 REST | 无（仅 bridge） | 低 |
| NanoMQ | MIT | **无官方 Web UI**（HTTP API 8081） | 可自建 | 是 | HTTP API | 无（边缘/桥接） | 低 |
| HiveMQ CE | Apache-2.0 | **无**（Control Center 为商业版） | 不适用 | 是 | CE 无完整管理 REST | 无（商业版支持） | 中 |
| Apache BifroMQ | Apache-2.0 | **明确不提供**（FAQ） | 需自建 | 是（推断，官方未逐项列出） | 控制 API + 插件 | 是（分布式） | 高 |
| RabbitMQ + MQTT 插件 | MPL-2.0 | 有（management 插件） | 可改（MPL-2.0） | 是（3.13+ 多数特性） | Management REST API | 是 | 中 |
| ThingsBoard | Apache-2.0 | 有（完整 IoT 平台 UI） | 可改（Apache-2.0） | 是（v3.1/v3.1.1/v5.0） | REST API | 是（有集群部署文档；CE 能力按版本确认） | 中高 |

### 推荐排序（针对“自建统一门户 + 深度定制后台 UI”）

1. **保留 EMQX，自建门户**：broker 能力、REST API 覆盖、集群最成熟；UI 完全自己做，不受 Dashboard 限制。
2. **ThingsBoard**：如果产品核心是设备管理/可视化，UI 全开源可直接改；但它是 IoT 平台，MQTT 吞吐/会话语义与 EMQX 定位不同。
3. **RabbitMQ**：管理 UI 开源可改、插件生态强、REST 完整；MQTT 是插件能力，海量 IoT 连接场景需压测。
4. **VerneMQ**：Apache-2.0、webadmin 可改、Erlang 集群强；UI 功能较基础。
5. **BifroMQ**：Apache-2.0、多租户/分布式强；无 UI，必须自建。
6. **NanoMQ**：MIT、边缘轻量；无 UI，仅适合边缘节点。
7. **HiveMQ CE**：Apache-2.0、MQTT 5.0 完整；无 UI、无集群，扩展受限。
8. **Mosquitto**：最轻量；无 UI、无 REST、无集群，只适合小规模/边缘。

---

## 5. 自建门户方案

### 5.1 架构

```text
Browser
  │ HTTPS
  ▼
Nginx（门户域名，TLS，静态前端）
  │ /            -> 门户 SPA
  │ /api/        -> 门户 BFF
  ▼
门户 BFF（Go/Node/Java）
  ├── EMQX REST API  /api/v5/*      （API Key，服务端持有）
  ├── SRS HTTP API  :1985/api/v1/*  （Basic/网络 ACL）
  ├── 数据库（用户、角色、审计、流元数据）
  └── WebSocket/SSE -> 浏览器实时推送
        ▲
        │ HTTP 回调（on_publish/on_play/on_unpublish/on_stop）
      SRS ── RTMP/SRT/WHIP ── 推流端
        └── HLS/HTTP-FLV/WHEP ── 播放端
```

要点：

- 浏览器**永远不直接持有** EMQX API Key 或 SRS 管理凭据，全部走 BFF。
- EMQX 18083/1985 不直接暴露公网；只暴露门户 443 与媒体端口。
- SRS 回调是同步的，BFF 必须快速返回 `200 {"code":0}`，重活异步入库。

### 5.2 EMQX REST API 覆盖（源码实测模块）

| 管理动作 | 端点/模块 | 证据 |
|---|---|---|
| 客户端列表/详情/踢下线 | `GET/DELETE /api/v5/clients/:clientid` | `apps/emqx_management/src/emqx_mgmt_api_clients.erl:136` |
| 代订阅/退订 | `POST /clients/:clientid/subscribe`、`/unsubscribe`（含 bulk） | 同上 139-142 |
| 订阅查询 | `GET /api/v5/subscriptions` | `emqx_mgmt_api_subscriptions.erl:51` |
| 认证用户增删改查 | `/api/v5/authentication/:id/users`、`/users/:user_id` | `apps/emqx_auth/src/emqx_authn/emqx_authn_api.erl:110-111, 384+` |
| 规则管理 | `/api/v5/rules` CRUD | `apps/emqx_rule_engine/src/emqx_rule_engine_api.erl` |
| 指标查询 | `GET /api/v5/metrics` | `emqx_mgmt_api_metrics.erl:48` |
| 告警 | `GET/DELETE /api/v5/alarms`、`POST /alarms/force_deactivate` | `emqx_mgmt_api_alarms.erl:38-39` |
| 禁用客户端 | `/api/v5/banned` | `emqx_mgmt_api_banned.erl` |

### 5.3 SRS HTTP API 覆盖（v7.0-a4 源码）

| 动作 | 端点 | 证据 |
|---|---|---|
| 流列表/详情 | `GET /api/v1/streams/`、`/streams/:id` | `srs_app_http_api.cpp:798` 注释 |
| 客户端列表/详情 | `GET /api/v1/clients/`、`/clients/:id` | 同上 868 |
| 踢客户端（踢流） | `DELETE /api/v1/clients/:id` → `client->conn_->expire()` | `srs_app_http_api.cpp:915-926` |
| 概览/版本/Vhost/配置 | `/api/v1/summaries`、`/versions`、`/vhosts`、`/configs` | 源码路由 |
| 录制（DVR）查询 | **无 HTTP API**；只有 `dvr` feature 标记，文件落盘 | grep 仅见 `srs_app_dvr.hpp` 与 feature 标记 |

> 录制文件需要门户自己扫盘/建索引，或通过 Nginx 静态目录提供下载。

### 5.4 技术栈建议

| 层 | 建议 | 理由 |
|---|---|---|
| 前端 | Vue 3 + Vite + Element Plus（或 React + Ant Design） | 与 Dashboard 技术栈接近，组件成熟 |
| BFF | Go（Gin/Echo）或 Node（NestJS） | 高并发、易做 API 聚合与 RBAC |
| 数据库 | PostgreSQL（+ Redis 可选） | 用户/角色/审计/流元数据 |
| 实时 | WebSocket/SSE + MQTT over WebSocket（可选） | 流状态、告警推送 |
| 部署 | Docker Compose + Nginx | 与现有运维一致 |

### 5.5 目录结构（Compose）

```text
~/01_walle_service/portal/
├── docker-compose.yml
├── .env.example
├── nginx/
│   ├── nginx.conf
│   └── conf.d/portal.conf
├── frontend/                 # Vue3 源码，构建产物由 nginx 托管
│   ├── Dockerfile
│   └── src/
├── backend/                  # BFF
│   ├── Dockerfile
│   └── internal/
│       ├── api/              # 门户 REST
│       ├── emqx/             # /api/v5 客户端
│       ├── srs/              # /api/v1 客户端 + 回调
│       └── rbac/             # 角色/权限
└── data/                     # 持久化（pg 数据、DVR 索引）
```

`docker-compose.yml` 服务：`nginx`、`portal-frontend`（构建产物）、`portal-backend`、`postgres`、（可选）`redis`。

### 5.6 权限设计落在哪一层

| 层 | 职责 |
|---|---|
| Nginx | TLS、限流、IP ACL；**不做业务鉴权** |
| 门户 BFF | 登录态（Session/JWT）、RBAC、端点白名单、审计日志；**唯一持有 EMQX/SRS 凭据的层** |
| EMQX | 两套 API Key：只读 key（仅 GET）与管理 key（含 DELETE/POST）；按角色选用；18083 不公网暴露 |
| SRS | `http_api.auth` Basic + 网络 ACL；推拉流鉴权走 `on_publish/on_play` token |
| 数据层 | 用户、角色、审计、流元数据；不存明文第三方密钥 |

只读用户：BFF 只放行 `GET /clients`、`GET /streams`、`GET /metrics`、`GET /alarms` 等。
管理员：额外放行 `DELETE /clients/:id`、`POST /subscribe`、`/authentication/:id/users` 写操作、`/rules` 写操作。

---

## 6. SRS 控制台子路径问题

### 6.1 console 是静态目录，不是可配置路由

- 源码目录：`trunk/research/console/`（`index.html`、`en_index.html`、`ng_index.html`、`js/`、`views/`）
- 构建复制：`trunk/auto/depends.sh:301`

```bash
rm -rf ${SRS_OBJS}/nginx/html/console &&
cp -rf $SRS_WORKDIR/research/console ${SRS_OBJS}/nginx/html/ &&
```

- `conf/full.conf` 无 `console {}` 配置块；console 由 `http_server.dir`（默认 `./objs/nginx/html`）托管：

```hocon
http_server {
    enabled on;
    listen 8080;
    dir ./objs/nginx/html;
    crossdomain on;
}
```

- `ng_index.html` 使用相对资源（`js/...`），**没有 `<base>` 标签**，AngularJS hash 路由（`#/summaries`）。因此挂到 `/srs/console/` 时静态资源通常能正常加载，不需要改 base href。

### 6.2 API 地址拼接的具体问题

`trunk/research/console/js/srs.console.js`：

```js
schema: "http",
host: null,
port: 1985,
baseurl: function(){
    return self.schema + "://" + self.host + (self.port === 80? "": ":" + self.port);
},
jsonp: function(url){ return self.baseurl() + url + "?callback=JSON_CALLBACK" + self.sstq(); },
```

`init`（692-718）优先读 query，否则用 `$location`：

```js
if ($location.search().schema) self.schema = $location.search().schema;
else self.schema = $location.protocol();

if ($location.search().host) self.host = $location.search().host;
else self.host = $location.host();

if ($location.search().port) self.port = $location.search().port;
else self.port = $location.port();
```

结论：

- API 基址是 `schema://host:port`，**不感知 `/srs/` 路径前缀**；
- 页面挂在 `https://portal.example.com/srs/console/` 时，默认会请求 `https://portal.example.com:443/api/v1/...`（根路径），不会带 `/srs/`；
- 若同域只代理 `/srs/` 而不代理根 `/api/v1/`，console 会 404；
- 若 SRS 在别的端口/子域，必须用 `?host=&port=&schema=` 显式指定。

### 6.3 Nginx 解决方式（推荐）

```nginx
# SRS console / players / HLS / FLV
location /srs/ {
    proxy_pass http://127.0.0.1:8080/;   # 末尾 / 去掉 /srs 前缀
    proxy_set_header Host $host;
    proxy_http_version 1.1;
    proxy_buffering off;
}

# console 默认会打根路径 /api/v1，这里同源代理到 1985
location /api/v1/ {
    proxy_pass http://127.0.0.1:1985;
    proxy_set_header Host $host;
    proxy_http_version 1.1;
}

# WebRTC 信令 WHEP/WHIP
location /rtc/v1/ {
    proxy_pass http://127.0.0.1:1985;
    proxy_set_header Host $host;
    proxy_http_version 1.1;
    proxy_read_timeout 3600s;
}
```

访问链接（显式带参数最稳）：

```text
https://portal.example.com/srs/console/ng_index.html#/summaries?host=portal.example.com&port=443&schema=https
```

如果希望 console 的“连接 SRS”页默认值正确，可用 query 参数；若不想暴露 1985，就保持根 `/api/v1/`、`/rtc/v1/` 反向代理。

### 6.4 推流/拉流路径是否受影响

**不受影响。**

- RTMP 推流：`rtmp://host:1935/live/stream`（1935 与 console 无关）。
- HTTP-FLV/HLS：`http://host:8080/live/stream.flv`、`/live/stream.m3u8`（由 `http_server.listen` 提供）。
- WHEP：`https://host:1985/rtc/v1/whep/...`（信令）与 UDP 8000（媒体）。
- 只有“console/player 页面里生成的 URL”可能受路径前缀影响；媒体服务本身的路径不变。

---

## 7. “改 EMQX 前端” vs “自建门户”成本对照

| 维度 | 改 EMQX 前端（fork dashboard5） | 自建门户 |
|---|---|---|
| 初期投入 | 中：fork + 菜单/路由/页面 + 构建链 | 中高：登录/RBAC/BFF/UI/部署 |
| 视觉原生度 | 最高（就是原生菜单） | 取决于自研 UI；可 iframe 嵌 Dashboard 保留原生页 |
| 升级维护 | 高：每次 EMQX 升级需 rebase、回归 | 低中：只依赖稳定 REST API |
| 许可证 | BUSL：修改允许，生产/分发受 grant 约束 | 不触碰 BUSL 前端；EMQX 后端仍受 BSL |
| 功能覆盖 | 直接复用 Dashboard 全部功能 | 需按需实现，工作量大 |
| 风险 | 前端 API 契约漂移、替换 `priv/www` 被升级覆盖 | 自研 Bug、权限模型需自己保证 |
| 适用 | 必须“左侧一级菜单 + 原生观感” | 需要深度定制、长期可控 |

**建议：**

- 若“一级菜单”是硬需求且能接受长期维护：fork 前端。
- 若“统一门户/深度定制”是硬需求：自建门户，EMQX Dashboard 只作为只读 iframe 或跳转。
- 不建议用 `sub_filter` 注入菜单、或直接改编译后 JS（无源码保障、升级即碎）。

---

## 8. 待用户确认的 root 操作（本轮未执行）

1. 安装 Docker Engine + Compose 插件（`apt install docker.io docker-compose-v2` 或 Docker 官方源）。
2. 安装 Nginx 并写入 `/etc/nginx/conf.d/*.conf`、配置 TLS 证书。
3. 若容器化部署：创建 compose 项目、数据卷、`/etc/docker/daemon.json`（如需镜像加速）。
4. 防火墙/安全组放行：443、18083（仅内网）、1985（仅内网）、8080、1935、8000/udp、9972（仅监控网）。
5. 如需系统级服务：创建 systemd unit（EMQX/SRS/门户）。
6. 可选：`apt install curl`（当前缺失，本轮用 wget 替代）。

---

## 9. 本轮安装与变更清单

**apt 安装：无。**

**在虚拟机上创建的文件/目录（全部位于 `~/01_walle_service/`）：**

```text
~/01_walle_service/recon/
├── clone.log
├── emqx/                 # git clone --depth 1 --branch 6.3.2
├── srs-7.0-a4/           # git clone --depth 1 --branch v7.0-a4
├── srs-6.0-r3/           # git clone --depth 1 --branch v6.0-r3
├── emqx-dashboard5/      # git clone --depth 1 --branch enterprise
└── EMQX_SRS_VM_recon_report.md   # 本报告
```

**修改：无。** 未改动 `/etc`、未安装/启停系统服务、未改 Docker（未安装）、未执行 `rm -rf`。

---

## 10. 来源链接

**EMQX：**

- LICENSE（6.3.2）：https://github.com/emqx/emqx/blob/6.3.2/LICENSE
- Dashboard 静态托管：https://github.com/emqx/emqx/blob/6.3.2/apps/emqx_dashboard/src/emqx_dashboard.erl
- Dashboard README（独立前端项目）：https://github.com/emqx/emqx/blob/6.3.2/apps/emqx_dashboard/README.md
- 前端下载脚本：https://github.com/emqx/emqx/blob/6.3.2/scripts/get-dashboard.sh
- Makefile（Dashboard 版本）：https://github.com/emqx/emqx/blob/6.3.2/Makefile
- Dashboard 前端仓库：https://github.com/emqx/emqx-dashboard5
- 菜单定义：https://github.com/emqx/emqx-dashboard5/blob/enterprise/src/hooks/useMenus.ts
- 路由表：https://github.com/emqx/emqx-dashboard5/blob/enterprise/src/router/index.ts
- 左侧栏：https://github.com/emqx/emqx-dashboard5/blob/enterprise/src/views/Base/LeftBar.vue
- 插件 UI iframe：https://github.com/emqx/emqx-dashboard5/blob/enterprise/src/views/Plugins/PluginDetail.vue
- 构建配置：https://github.com/emqx/emqx-dashboard5/blob/enterprise/vite.config.js
- 4.x 旧前端（Apache-2.0）：https://github.com/emqx/emqx-dashboard-frontend

**SRS：**

- LICENSE（v7.0-a4）：https://github.com/ossrs/srs/blob/v7.0-a4/LICENSE
- console 复制脚本：https://github.com/ossrs/srs/blob/v7.0-a4/trunk/auto/depends.sh
- 配置样例：https://github.com/ossrs/srs/blob/v7.0-a4/trunk/conf/full.conf
- console API 拼接：https://github.com/ossrs/srs/blob/v7.0-a4/trunk/research/console/js/srs.console.js
- RTC player 端口逻辑：https://github.com/ossrs/srs/blob/v7.0-a4/trunk/research/players/rtc_player.html
- HTTP API：https://github.com/ossrs/srs/blob/v7.0-a4/trunk/src/app/srs_app_http_api.cpp

**替代 Broker：**

- VerneMQ：https://github.com/vernemq/vernemq ；HTTP listener：https://docs.vernemq.com/configuring-vernemq/http-listeners
- Mosquitto：https://mosquitto.org/
- NanoMQ：https://github.com/nanomq/nanomq ；HTTP API：https://nanomq.io/docs/en/latest/api/introduction.html
- HiveMQ CE：https://github.com/hivemq/hivemq-community-edition ；CE vs 商业版：https://www.hivemq.com/community/open-source/
- Apache BifroMQ：https://bifromq.apache.org/docs/development/get_started/faq/
- RabbitMQ：https://github.com/rabbitmq/rabbitmq-server ；MQTT 插件：https://www.rabbitmq.com/docs/mqtt
- ThingsBoard：https://github.com/thingsboard/thingsboard ；MQTT API：https://thingsboard.io/docs/reference/mqtt-api/getting-connected/

---

## 11. 证据附录（关键命令输出摘录）

```text
$ git -C emqx describe --tags
6.3.2

$ ls apps/emqx_dashboard
etc  include  mix.exs  priv  README.md  src  test

$ ls apps/emqx_dashboard/priv
api-spec.html  api-spec-login.html

$ grep -rIn -i "menu" apps/emqx_dashboard/src
（无输出）

$ md5sum srs-7.0-a4/trunk/research/console/ng_index.html srs-6.0-r3/trunk/research/console/ng_index.html
cbd35d1b87af0be9df0db1fc4d57c493  .../srs-7.0-a4/.../ng_index.html
cbd35d1b87af0be9df0db1fc4d57c493  .../srs-6.0-r3/.../ng_index.html
```

> 说明：本报告中“SRS 7.0 与 6.0-r3 console 一致”的结论基于上述 md5；配置项差异未逐项穷举，仅覆盖 console/API 路径与 `http_server.dir`。

---

## 12. 第二轮：Docker 安装记录（2026-11-15）

> 本节为新增授权（安装 Docker）后的执行记录；第 2–6 节的源码核查结论不受影响。

### 12.1 安装结果

| 项 | 结果 |
|---|---|
| Docker Engine | 29.9.3（API 1.56，linux/arm64） |
| Docker Compose | v5.7.1（compose 插件） |
| containerd | 2.4.2 |
| buildx | 0.39.0 |
| 服务状态 | `systemctl is-active docker` = active；`is-enabled` = enabled |
| 服务端连通 | `sudo docker info` 返回 ServerVersion 29.9.3，存储驱动 overlayfs |
| `gs` 是否在 docker 组 | **否**。`getent group docker` = `docker:x:984:`（无成员）；`id -nG gs` 无 docker |
| 普通用户直接执行 | `docker ps` → `permission denied ... /var/run/docker.sock` |

### 12.2 安装过程与网络问题

1. 首次按官方源 `https://download.docker.com/linux/ubuntu` 安装失败：
   - `curl: (35) Recv failure: Connection reset by peer`
   - GPG key 下载为空 → `gpg: no valid OpenPGP data found`
   - apt 报 `NO_PUBKEY 7EA0A9C3F273FCD8`，`docker-ce` 无安装候选。
2. 改用**阿里云 docker-ce 镜像**（仍是 Docker 官方包，仅换下载源）：
   - key：`https://mirrors.aliyun.com/docker-ce/linux/ubuntu/gpg`
   - repo：`deb [arch=arm64 signed-by=/etc/apt/keyrings/docker.gpg] https://mirrors.aliyun.com/docker-ce/linux/ubuntu noble stable`
3. 安装成功，日志：
   - `~/01_walle_service/recon/docker-install.log`（失败尝试）
   - `~/01_walle_service/recon/docker-install-2.log`（成功尝试）

### 12.3 本轮 apt 安装清单（新增）

```text
curl                          8.5.0-2ubuntu10.16
containerd.io                 2.4.2-4~ubuntu.24.04~noble
docker-buildx-plugin          0.39.0-1~ubuntu.24.04~noble
docker-ce                     5:29.9.3-1~ubuntu.24.04~noble
docker-ce-cli                 5:29.9.3-1~ubuntu.24.04~noble
docker-ce-rootless-extras     5:29.9.3-1~ubuntu.24.04~noble
docker-compose-plugin         5.7.1-1~ubuntu.24.04~noble
pigz                          2.8-1
```

`ca-certificates`、`gnupg` 安装前已存在，未新增。

### 12.4 创建/修改的文件路径

**仅在 `~/01_walle_service` 内：**

```text
~/01_walle_service/recon/docker-install.log      # 新建（失败尝试日志）
~/01_walle_service/recon/docker-install-2.log    # 新建（成功日志）
~/01_walle_service/recon/EMQX_SRS_VM_recon_report.md  # 修改（本报告）
```

**Docker 安装授权范围内的系统级变更：**

```text
/etc/apt/keyrings/docker.gpg                     # 新建（Docker GPG key）
/etc/apt/sources.list.d/docker.list              # 新建（指向阿里云 docker-ce 镜像）
/usr/bin/docker 等包文件                          # apt 安装
/lib/systemd/system/docker.service               # 包自带
/lib/systemd/system/containerd.service           # 包自带
```

未创建任何容器、镜像、网络或数据卷；未修改其他 `/etc` 配置。

### 12.5 待确认的 root 操作（本轮未执行）

1. `usermod -aG docker gs`（让 `gs` 免 sudo 使用 Docker；需重新登录生效）。
2. 安装 Nginx 并写入 `/etc/nginx/conf.d/*.conf`（门户/反代）。
3. 防火墙/安全组放行：443、18083（内网）、1985（内网）、8080、1935、8000/udp、9972（监控网）。
4. 如需镜像加速：修改 `/etc/docker/daemon.json` 并 `systemctl restart docker`。
5. 部署 EMQX/SRS/门户容器（`docker compose up`）与 systemd 单元。

### 12.6 原核查结论复核

- EMQX 6.3.2 `LICENSE`：BUSL-1.1，Licensed Work 为 EMQX 5.9.0+；单节点生产可用，多节点/对外托管需商业授权。
- `apps/emqx_dashboard` 不含前端源码；前端在 `emqx/emqx-dashboard5`（BUSL-1.1）。
- 菜单由 `src/hooks/useMenus.ts` 硬编码，后端无菜单注册点；插件 `index` 仅出现在插件详情 iframe。
- SRS console 为静态目录（`trunk/research/console` → `objs/nginx/html/console`），API 基址为 `schema://host:port`，挂子路径需代理根 `/api/v1`、`/rtc/v1` 或用 query 参数。

