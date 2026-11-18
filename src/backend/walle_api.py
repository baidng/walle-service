#!/usr/bin/env python3
"""walle 用户管理后端

职责：
  1. 用户角色（客户 / 设备）存在本机 SQLite；
  2. 用户名与密码的增删改最终都调用 EMQX 内置数据库（built_in_database）；
  3. 密码只存在 EMQX（sha256），walle 不保存任何密码。

仅依赖 Python 标准库，直接 `python3 walle_api.py` 启动，监听 127.0.0.1:9000，
由 nginx 把 /api/ 反代过来。
"""
import base64
import contextlib
import hmac
import json
import os
import secrets
import socket
import sqlite3
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(BASE_DIR))  # src/backend -> src -> 仓库根
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "users.db")
CONF_PATH = os.path.join(REPO_ROOT, "etc", "walle.conf")

HOST = "127.0.0.1"
PORT = 9000
EMQX_BASE = "http://127.0.0.1:18083/api/v5"
AUTH_ID = "password_based:built_in_database"
ROLES = ("客户", "设备")

# 设备绑定生成的 ACL 规则写在 EMQX file 授权源的这一段标记之间，重新生成时整段替换。
ACL_BEGIN = "%% ===== walle bindings (auto-generated, do not edit) ====="
ACL_END = "%% ===== end walle bindings ====="

# 管理员会话（内存保存，进程重启即失效）：token -> (用户名, 过期时间)
SESSION_TTL = 12 * 3600
SESSIONS = {}


def new_session(username):
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = (username, time.time() + SESSION_TTL)
    return token


def session_user(token):
    record = SESSIONS.get(token)
    if not record:
        return None
    username, expires_at = record
    if expires_at < time.time():
        SESSIONS.pop(token, None)
        return None
    return username


def load_credentials():
    """优先读环境变量（systemd EnvironmentFile），否则读 etc/walle.conf。"""
    key = os.environ.get("EMQX_API_KEY", "")
    secret = os.environ.get("EMQX_API_SECRET", "")
    if key and secret:
        return key, secret
    if os.path.isfile(CONF_PATH):
        with open(CONF_PATH, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("EMQX_API_KEY="):
                    key = line.split("=", 1)[1].strip()
                elif line.startswith("EMQX_API_SECRET="):
                    secret = line.split("=", 1)[1].strip()
    if not key or not secret:
        sys.exit("缺少 EMQX_API_KEY / EMQX_API_SECRET，请检查环境变量或 etc/walle.conf")
    return key, secret


EMQX_KEY, EMQX_SECRET = load_credentials()


def load_media_secret():
    """SRS 推流鉴权密钥：优先环境变量，其次 etc/walle.conf；空值表示未配置。"""
    secret = os.environ.get("MEDIA_SERVER_SECRET", "").strip()
    if secret:
        return secret
    if os.path.isfile(CONF_PATH):
        with open(CONF_PATH, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("MEDIA_SERVER_SECRET="):
                    secret = line.split("=", 1)[1].strip()
    return secret


MEDIA_SECRET = load_media_secret()


def get_media_secret():
    """当前生效的推流密钥：以数据库为准，未写入时回退到启动配置。"""
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = 'media_secret'").fetchone()
    return (row["value"] if row else "") or MEDIA_SECRET or ""


def set_media_secret(value):
    with db() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES ('media_secret', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (value,),
        )


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS users (
                   username   TEXT PRIMARY KEY,
                   role       TEXT NOT NULL CHECK (role IN ('客户', '设备')),
                   created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
               )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS bindings (
                   device     TEXT PRIMARY KEY,
                   customer   TEXT NOT NULL,
                   created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
               )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS settings (
                   key   TEXT PRIMARY KEY,
                   value TEXT NOT NULL
               )"""
        )
        if MEDIA_SECRET:
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES ('media_secret', ?)",
                (MEDIA_SECRET,),
            )


@contextlib.contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def emqx(method, path, body=None):
    """调用 EMQX REST API，返回 (status, data)。"""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(EMQX_BASE + path, data=data, method=method)
    token = base64.b64encode(f"{EMQX_KEY}:{EMQX_SECRET}".encode()).decode()
    req.add_header("Authorization", "Basic " + token)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw
    except urllib.error.URLError as exc:
        return 0, {"message": f"EMQX 不可达: {exc.reason}"}


def _mqtt_remaining(length):
    out = bytearray()
    while True:
        byte = length % 128
        length //= 128
        if length:
            byte |= 0x80
        out.append(byte)
        if not length:
            return bytes(out)


def mqtt_auth(username, password, host="127.0.0.1", port=1883, timeout=3):
    """用 MQTT CONNECT 校验账号密码，等价于设备实际连上 broker 的过程。"""
    client_id = "walle-check-" + os.urandom(4).hex()
    payload = b""
    for part in (client_id, username, password):
        raw = part.encode()
        payload += struct.pack("!H", len(raw)) + raw
    variable = struct.pack("!H", 4) + b"MQTT" + bytes([4, 0xC2]) + struct.pack("!H", 30)
    body = variable + payload
    packet = bytes([0x10]) + _mqtt_remaining(len(body)) + body
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(packet)
        resp = sock.recv(4)
    return len(resp) >= 4 and resp[0] == 0x20 and resp[3] == 0


def emqx_login(username, password):
    """用用户自己的账号密码校验 EMQX Dashboard 账号。"""
    data = json.dumps({"username": username, "password": password}).encode()
    req = urllib.request.Request(EMQX_BASE + "/login", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except urllib.error.URLError as exc:
        return 0, {"message": f"EMQX 不可达: {exc.reason}"}


def emqx_users():
    status, data = emqx("GET", f"/authentication/{AUTH_ID}/users?limit=1000")
    if status != 200 or not isinstance(data, dict):
        raise RuntimeError(f"读取 EMQX 用户失败: {data}")
    return {u["user_id"]: u for u in data.get("data", [])}


def emqx_user_path(username):
    return f"/authentication/{AUTH_ID}/users/{urllib.parse.quote(username, safe='')}"


def acl_quote(value):
    """转义 ACL 规则里的字符串字面量。"""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def valid_tenant(value):
    """客户名会作为 MQTT 主题层级，不能包含分隔符、通配符或控制字符。"""
    return (
        bool(value)
        and not any(ch in value for ch in "/+#\x00")
        and all(ord(ch) >= 0x20 for ch in value)
    )


def binding_rows():
    with db() as conn:
        return conn.execute(
            "SELECT device, customer, created_at FROM bindings ORDER BY customer, device"
        ).fetchall()


def managed_acl(bindings):
    """生成 walle 管理的 ACL 段。

    规则顺序即 EMQX file 授权源的匹配顺序（首次命中生效）：
      1. 客户可读写自己租户的整棵子树；
      2. 设备只读写「绑定客户 + 自己 clientid」的子树，并可订阅发现查询；
      3. 其余账号（含未绑定设备）禁止访问 walle 命名空间。
    """
    lines = [ACL_BEGIN]
    for row in bindings:
        tenant = acl_quote(row["customer"])
        lines.append(f'%% 客户 {row["customer"]}：读写本租户整棵子树')
        lines.append(f'{{allow, {{username, "{tenant}"}}, all, ["walle/{row["customer"]}/#"]}}.')
    for row in bindings:
        device = acl_quote(row["device"])
        tenant = row["customer"]
        lines.append(f'%% 设备 {row["device"]}：只读写绑定客户 {tenant} 下自己的子树')
        lines.append(
            f'{{allow, {{username, "{device}"}}, publish, ["walle/{tenant}/${{clientid}}/#"]}}.'
        )
        lines.append(
            f'{{allow, {{username, "{device}"}}, subscribe, '
            f'["walle/{tenant}/${{clientid}}/#", "walle/{tenant}/v1/discovery/query"]}}.'
        )
    lines.append("%% 未绑定设备及其他账号禁止访问 walle 命名空间")
    lines.append('{deny, all, all, ["walle/#"]}.')
    lines.append(ACL_END)
    return "\n".join(lines)


def strip_managed_acl(rules):
    out, skipping = [], False
    for line in rules.splitlines():
        stripped = line.strip()
        if stripped == ACL_BEGIN:
            skipping = True
            continue
        if stripped == ACL_END:
            skipping = False
            continue
        if not skipping:
            out.append(line)
    return "\n".join(out).strip("\n")


def sync_bindings_acl():
    """按 bindings 表重新生成 EMQX file 授权源中的 walle ACL 段。"""
    status, data = emqx("GET", "/authorization/sources/file")
    if status != 200 or not isinstance(data, dict) or "rules" not in data:
        raise RuntimeError(f"读取 EMQX ACL 失败: {data}")
    base = strip_managed_acl(data["rules"])
    rules = managed_acl(binding_rows())
    if base:
        rules += "\n\n" + base
    status, data = emqx(
        "PUT",
        "/authorization/sources/file",
        {"type": "file", "enable": True, "rules": rules},
    )
    if status not in (200, 204):
        raise RuntimeError(f"写入 EMQX ACL 失败: {data}")
    # 清掉授权缓存，让绑定/解绑立即生效（否则最长 1 分钟后才刷新）
    emqx("DELETE", "/authorization/cache")


class Handler(BaseHTTPRequestHandler):
    server_version = "walle-api/0.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, code, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode())
        except json.JSONDecodeError:
            return None

    # ---------- 路由 ----------
    def do_GET(self):
        if self.path == "/api/health":
            return self._json(200, {"status": "ok"})
        if self.path == "/api/users":
            return self.list_users()
        if self.path == "/api/bindings":
            return self.list_bindings()
        if self.path == "/api/admin/stream-secret":
            return self.get_stream_secret()
        self._json(404, {"message": "not found"})

    def do_POST(self):
        if self.path == "/api/srs/hook":
            return self.srs_hook()
        if self.path == "/api/admin/stream-secret/regenerate":
            return self.regenerate_stream_secret()
        if self.path == "/api/admin/login":
            return self.admin_login()
        if self.path == "/api/login":
            return self.login()
        if self.path == "/api/users":
            return self.create_user()
        if self.path == "/api/bindings":
            return self.create_binding()
        self._json(404, {"message": "not found"})

    def do_PUT(self):
        if self.path == "/api/admin/stream-secret":
            return self.update_stream_secret()
        if self.path.startswith("/api/users/"):
            return self.update_user(self._username())
        self._json(404, {"message": "not found"})

    def do_DELETE(self):
        if self.path.startswith("/api/users/"):
            return self.delete_user(self._username())
        if self.path.startswith("/api/bindings/"):
            return self.delete_binding(self._username())
        self._json(404, {"message": "not found"})

    def _username(self):
        return urllib.parse.unquote(self.path.rsplit("/", 1)[-1])

    # ---------- 业务 ----------
    def srs_hook(self):
        """SRS http_hooks 回调：P0 只校验推流密钥，播放暂不拦截。"""
        # 只允许本机 SRS 直接回调；nginx 侧另有 deny 规则挡住外部访问
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            return self._json(403, {"code": 403, "message": "forbidden"})
        body = self._body()
        if not isinstance(body, dict):
            return self._json(400, {"code": 400, "message": "invalid json"})
        if str(body.get("action") or "") != "on_publish":
            return self._json(200, 0)
        params = urllib.parse.parse_qs(str(body.get("param") or "").lstrip("?"))
        supplied = (params.get("secret") or [""])[0]
        current = get_media_secret()
        if not current:
            self.log_message("SRS 拒绝推流：服务端未配置 MEDIA_SERVER_SECRET")
            return self._json(403, {"code": 403, "message": "secret not configured"})
        if not hmac.compare_digest(supplied.encode(), current.encode()):
            self.log_message("SRS 拒绝推流：app=%s stream=%s ip=%s",
                             body.get("app"), body.get("stream"), body.get("ip"))
            return self._json(403, {"code": 403, "message": "invalid secret"})
        return self._json(200, 0)

    # ---------- 流鉴权密钥 ----------
    def get_stream_secret(self):
        if not self._is_admin():
            return self._deny()
        return self._json(200, {"secret": get_media_secret()})

    def update_stream_secret(self):
        if not self._is_admin():
            return self._deny()
        body = self._body()
        if not isinstance(body, dict):
            return self._json(400, {"message": "请求体格式错误"})
        value = str(body.get("secret") or "").strip()
        if not 8 <= len(value) <= 128 or not all(
            (c.isascii() and c.isalnum()) or c in "-_" for c in value
        ):
            return self._json(400, {"message": "密钥需为 8-128 位字母、数字、下划线或短横线"})
        set_media_secret(value)
        return self._json(200, {"secret": value})

    def regenerate_stream_secret(self):
        if not self._is_admin():
            return self._deny()
        value = secrets.token_hex(16)
        set_media_secret(value)
        return self._json(200, {"secret": value})

    def _is_admin(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        return session_user(auth[7:].strip()) is not None

    def _deny(self):
        return self._json(401, {"message": "未登录或会话已失效，请以管理员身份登录"})

    def admin_login(self):
        """管理员登录：校验 EMQX Dashboard 账号，成功后签发 walle 会话 token。"""
        body = self._body() or {}
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        if not username or not password:
            return self._json(400, {"message": "用户名和密码不能为空"})
        status, data = emqx_login(username, password)
        if status == 0:
            return self._json(502, {"message": data.get("message", "EMQX 不可达")})
        if status != 200:
            return self._json(401, {"message": "管理员账号或密码错误"})
        role = (data or {}).get("role")
        if role and role != "administrator":
            return self._json(403, {"message": "该 Dashboard 账号不是管理员，不能管理用户"})
        return self._json(200, {"username": username, "token": new_session(username), "role": "administrator"})

    def login(self):
        """客户登录：密码交给 EMQX 校验，角色必须是「客户」。"""
        body = self._body() or {}
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        if not username or not password:
            return self._json(400, {"message": "用户名和密码不能为空"})
        try:
            ok = mqtt_auth(username, password)
        except OSError:
            return self._json(502, {"message": "无法连接 EMQX（1883），请检查服务状态"})
        if not ok:
            return self._json(401, {"message": "用户名或密码错误"})
        with db() as conn:
            row = conn.execute("SELECT role FROM users WHERE username = ?", (username,)).fetchone()
        if row is None:
            return self._json(403, {"message": "该账号未在 walle 登记角色，不能登录"})
        if row["role"] != "客户":
            return self._json(403, {"message": "该账号角色为「设备」，不能登录"})
        return self._json(200, {"username": username, "role": row["role"], "token": "ok"})

    def list_users(self):
        if not self._is_admin():
            return self._deny()
        try:
            remote = emqx_users()
        except RuntimeError as exc:
            return self._json(502, {"message": str(exc)})
        with db() as conn:
            rows = conn.execute("SELECT username, role, created_at FROM users ORDER BY created_at").fetchall()
        local = {r["username"]: r for r in rows}
        # 以 EMQX 为准列出用户；角色缺失时标记为未登记
        users = [
            {
                "username": name,
                "role": local[name]["role"] if name in local else None,
                "superuser": bool(info.get("is_superuser")),
                "created_at": local[name]["created_at"] if name in local else None,
            }
            for name, info in remote.items()
        ]
        users.sort(key=lambda u: (u["role"] != "客户", u["username"]))
        return self._json(200, {"data": users})

    def create_user(self):
        if not self._is_admin():
            return self._deny()
        body = self._body()
        if body is None:
            return self._json(400, {"message": "请求体不是合法 JSON"})
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        role = body.get("role") or ""
        if not username or not password:
            return self._json(400, {"message": "用户名和密码不能为空"})
        if role not in ROLES:
            return self._json(400, {"message": "角色只能是 客户 或 设备"})
        status, data = emqx(
            "POST",
            f"/authentication/{AUTH_ID}/users",
            {"user_id": username, "password": password, "is_superuser": False},
        )
        if status not in (200, 201, 204):
            return self._json(status or 502, {"message": f"EMQX 创建用户失败: {data}"})
        try:
            with db() as conn:
                conn.execute("INSERT INTO users (username, role) VALUES (?, ?)", (username, role))
        except sqlite3.IntegrityError:
            # 本地已存在同名记录，回滚 EMQX 侧避免状态不一致
            emqx("DELETE", emqx_user_path(username))
            return self._json(409, {"message": f"用户 {username} 已存在"})
        return self._json(201, {"username": username, "role": role})

    def update_user(self, username):
        if not self._is_admin():
            return self._deny()
        body = self._body() or {}
        password = body.get("password") or ""
        if not password:
            return self._json(400, {"message": "新密码不能为空"})
        with db() as conn:
            row = conn.execute("SELECT role FROM users WHERE username = ?", (username,)).fetchone()
        if row is None:
            return self._json(404, {"message": f"本地没有用户 {username} 的角色记录"})
        status, data = emqx(
            "PUT",
            emqx_user_path(username),
            {"password": password, "is_superuser": False},
        )
        if status not in (200, 201, 204):
            return self._json(status or 502, {"message": f"EMQX 修改密码失败: {data}"})
        return self._json(200, {"username": username, "message": "密码已更新"})

    def delete_user(self, username):
        if not self._is_admin():
            return self._deny()
        with db() as conn:
            row = conn.execute("SELECT role FROM users WHERE username = ?", (username,)).fetchone()
            if row is not None and row["role"] == "客户":
                count = conn.execute(
                    "SELECT COUNT(*) FROM bindings WHERE customer = ?", (username,)
                ).fetchone()[0]
                if count:
                    return self._json(
                        409,
                        {"message": f"客户 {username} 仍绑定 {count} 台设备，请先解除绑定"},
                    )
            binding = conn.execute(
                "SELECT device, customer, created_at FROM bindings WHERE device = ?", (username,)
            ).fetchone()
        status, data = emqx("DELETE", emqx_user_path(username))
        if status not in (200, 204):
            return self._json(status or 502, {"message": f"EMQX 删除用户失败: {data}"})
        with db() as conn:
            conn.execute("DELETE FROM users WHERE username = ?", (username,))
            conn.execute("DELETE FROM bindings WHERE device = ?", (username,))
        if binding is not None:
            try:
                sync_bindings_acl()
            except RuntimeError as exc:
                return self._json(
                    200,
                    {"username": username, "message": f"已删除，但 ACL 同步失败: {exc}"},
                )
        return self._json(200, {"username": username, "message": "已删除"})

    # ---------- 设备绑定 ----------
    def list_bindings(self):
        if not self._is_admin():
            return self._deny()
        return self._json(200, {"data": [dict(row) for row in binding_rows()]})

    def create_binding(self):
        if not self._is_admin():
            return self._deny()
        body = self._body()
        if body is None:
            return self._json(400, {"message": "请求体不是合法 JSON"})
        device = (body.get("device") or "").strip()
        customer = (body.get("customer") or "").strip()
        if not device or not customer:
            return self._json(400, {"message": "设备和客户都不能为空"})
        if any(ord(ch) < 0x20 for ch in device):
            return self._json(400, {"message": "设备名不能包含控制字符"})
        if not valid_tenant(customer):
            return self._json(400, {"message": "客户名不能包含 / + # 等 MQTT 特殊字符"})
        with db() as conn:
            dev = conn.execute("SELECT role FROM users WHERE username = ?", (device,)).fetchone()
            cus = conn.execute("SELECT role FROM users WHERE username = ?", (customer,)).fetchone()
            if dev is None or dev["role"] != "设备":
                return self._json(400, {"message": f"{device} 不是角色为「设备」的用户"})
            if cus is None or cus["role"] != "客户":
                return self._json(400, {"message": f"{customer} 不是角色为「客户」的用户"})
            exists = conn.execute(
                "SELECT customer FROM bindings WHERE device = ?", (device,)
            ).fetchone()
            if exists is not None:
                return self._json(
                    409, {"message": f"设备 {device} 已绑定到 {exists['customer']}，请先解除绑定"}
                )
            conn.execute(
                "INSERT INTO bindings (device, customer) VALUES (?, ?)", (device, customer)
            )
        try:
            sync_bindings_acl()
        except RuntimeError as exc:
            with db() as conn:
                conn.execute("DELETE FROM bindings WHERE device = ?", (device,))
            return self._json(502, {"message": f"绑定失败，ACL 未写入: {exc}"})
        return self._json(201, {"device": device, "customer": customer})

    def delete_binding(self, device):
        if not self._is_admin():
            return self._deny()
        with db() as conn:
            row = conn.execute(
                "SELECT device, customer, created_at FROM bindings WHERE device = ?", (device,)
            ).fetchone()
            if row is None:
                return self._json(404, {"message": f"设备 {device} 没有绑定记录"})
            conn.execute("DELETE FROM bindings WHERE device = ?", (device,))
        try:
            sync_bindings_acl()
        except RuntimeError as exc:
            with db() as conn:
                conn.execute(
                    "INSERT INTO bindings (device, customer, created_at) VALUES (?, ?, ?)",
                    (row["device"], row["customer"], row["created_at"]),
                )
            return self._json(502, {"message": f"解除绑定失败，ACL 未更新: {exc}"})
        return self._json(200, {"device": device, "message": "已解除绑定并删除 ACL"})


def main():
    init_db()
    try:
        sync_bindings_acl()
    except RuntimeError as exc:
        print(f"warning: {exc}", file=sys.stderr, flush=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"walle-api listening on http://{HOST}:{PORT} (db: {DB_PATH})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
