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
import json
import os
import socket
import sqlite3
import struct
import sys
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


def emqx_users():
    status, data = emqx("GET", f"/authentication/{AUTH_ID}/users?limit=1000")
    if status != 200 or not isinstance(data, dict):
        raise RuntimeError(f"读取 EMQX 用户失败: {data}")
    return {u["user_id"]: u for u in data.get("data", [])}


def emqx_user_path(username):
    return f"/authentication/{AUTH_ID}/users/{urllib.parse.quote(username, safe='')}"


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
        self._json(404, {"message": "not found"})

    def do_POST(self):
        if self.path == "/api/login":
            return self.login()
        if self.path == "/api/users":
            return self.create_user()
        self._json(404, {"message": "not found"})

    def do_PUT(self):
        if self.path.startswith("/api/users/"):
            return self.update_user(self._username())
        self._json(404, {"message": "not found"})

    def do_DELETE(self):
        if self.path.startswith("/api/users/"):
            return self.delete_user(self._username())
        self._json(404, {"message": "not found"})

    def _username(self):
        return urllib.parse.unquote(self.path.rsplit("/", 1)[-1])

    # ---------- 业务 ----------
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
        status, data = emqx("DELETE", emqx_user_path(username))
        if status not in (200, 204):
            return self._json(status or 502, {"message": f"EMQX 删除用户失败: {data}"})
        with db() as conn:
            conn.execute("DELETE FROM users WHERE username = ?", (username,))
        return self._json(200, {"username": username, "message": "已删除"})


def main():
    init_db()
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
