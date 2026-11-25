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
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(BASE_DIR))  # src/backend -> src -> 仓库根
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "users.db")
CONF_PATH = os.path.join(REPO_ROOT, "etc", "walle.conf")

# 一键 HTTPS：特权脚本（sudo 调用）与它写入的证书状态文件
HTTPS_HELPER = os.path.join(REPO_ROOT, "scripts", "walle_https.sh")
HTTPS_STATE_FILE = "/var/lib/walle/https.json"
HTTPS_SITE_ENABLED = "/etc/nginx/sites-enabled/walle-https.conf"
HTTPS_SUDO_HINT = (
    "需要在服务器上为运行门户的用户配置免密 sudo（把 gs 换成实际运行用户）：\n"
    f"echo 'gs ALL=(root) NOPASSWD: {HTTPS_HELPER}' | sudo tee /etc/sudoers.d/walle-https\n"
    "sudo chmod 440 /etc/sudoers.d/walle-https\n"
    "并确认已安装 certbot：sudo apt-get install -y certbot"
)
DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)([A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$"
)
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}$")

APP_VERSION = "0.1.1"  # 与 docs/version_record.md 保持一致

HOST = "127.0.0.1"
PORT = 9000
EMQX_BASE = "http://127.0.0.1:18083/api/v5"
AUTH_ID = "password_based:built_in_database"
ROLES = ("客户", "设备", "agent")

# 设备绑定生成的 ACL 规则写在 EMQX file 授权源的这一段标记之间，重新生成时整段替换。
ACL_BEGIN = "%% ===== walle bindings (auto-generated, do not edit) ====="
ACL_END = "%% ===== end walle bindings ====="

# 管理员会话（内存保存，进程重启即失效）：token -> (用户名, 过期时间)
SESSION_TTL = 12 * 3600
SESSIONS = {}


def new_session(username, role="administrator"):
    token = secrets.token_urlsafe(32)
    now = time.time()
    SESSIONS[token] = (username, now + SESSION_TTL, now, role)
    return token


def session_record(token):
    record = SESSIONS.get(token)
    if not record:
        return None
    if record[1] < time.time():
        SESSIONS.pop(token, None)
        return None
    return record


def session_user(token):
    record = session_record(token)
    return record[0] if record else None


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


def conf_value(key, default=""):
    """读取配置：环境变量优先，其次 etc/walle.conf。"""
    value = os.environ.get(key, "").strip()
    if value:
        return value
    if os.path.isfile(CONF_PATH):
        with open(CONF_PATH, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip()
    return default


def load_alarm_retention_days():
    raw = conf_value("WALLE_ALARM_RETENTION_DAYS", "30")
    try:
        days = int(raw)
    except ValueError:
        return 30
    return days if days > 0 else 30


# 告警接入：EMQX 规则引擎通过回环地址 POST，用共享 token 再做一层校验
ALARM_INGEST_TOKEN = conf_value("WALLE_INGEST_TOKEN")
ALARM_RETENTION_DAYS = load_alarm_retention_days()
ALARM_TABLE_SQL = """CREATE TABLE IF NOT EXISTS alarms (
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
)"""


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


# 浏览器 WHIP 推流凭据：短时效 HMAC 签名，MEDIA_SERVER_SECRET 不出服务端
RTC_TOKEN_TTL = 120
STREAM_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64url_decode(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def rtc_signature(key, payload_b64):
    return _b64url(hmac.new(key.encode(), payload_b64.encode(), hashlib.sha256).digest())


def make_rtc_token(app, stream, ttl=RTC_TOKEN_TTL):
    """签发只对 app/stream 有效、ttl 秒后过期的推流凭据；未配置密钥时返回空串。"""
    key = get_media_secret()
    if not key:
        return ""
    payload = json.dumps(
        {"a": app, "s": stream, "e": int(time.time()) + ttl}, separators=(",", ":")
    )
    body = _b64url(payload.encode())
    return f"{body}.{rtc_signature(key, body)}"


def verify_rtc_token(token, app, stream):
    """校验签名、有效期与 app/stream 绑定，全部通过才返回 True。"""
    key = get_media_secret()
    if not key or "." not in token:
        return False
    body, signature = token.rsplit(".", 1)
    if not hmac.compare_digest(signature, rtc_signature(key, body)):
        return False
    try:
        payload = json.loads(_b64url_decode(body))
    except (ValueError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or payload.get("a") != app or payload.get("s") != stream:
        return False
    try:
        expires_at = int(payload.get("e") or 0)
    except (TypeError, ValueError):
        return False
    return expires_at >= time.time()


def https_state():
    """读取一键 HTTPS 写入的证书状态；文件不存在或无权限时返回空。"""
    try:
        with open(HTTPS_STATE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def https_result_message(message, result):
    """把脚本返回的 EMQX 证书同步结果附加到提示文案。"""
    emqx = result.get("emqx")
    if not isinstance(emqx, dict):
        return message
    if emqx.get("ok"):
        return f"{message}，EMQX 8883/8084 证书已同步"
    detail = str(emqx.get("error") or "").strip()
    return f"{message}；EMQX 未同步" + (f"：{detail}" if detail else "")


def run_https_helper(args, timeout):
    """调用特权脚本（sudo -n），返回脚本最后一行 JSON。"""
    try:
        proc = subprocess.run(
            ["sudo", "-n", HTTPS_HELPER, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return {"ok": False, "error": "未找到 sudo，无法执行 HTTPS 配置脚本"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"操作超时（{timeout} 秒），请在服务器查看 certbot 日志"}
    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload
    detail = (proc.stderr or "").strip() or (proc.stdout or "").strip()
    return {"ok": False, "error": detail[-600:] or f"脚本退出码 {proc.returncode}"}


# ---------- 运行状态 ----------
_status_lock = threading.Lock()


def _cpu_times():
    with open("/proc/stat", encoding="ascii") as fh:
        values = [int(v) for v in fh.readline().split()[1:]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values), idle


def _net_bytes():
    rx = tx = 0
    with open("/proc/net/dev", encoding="ascii") as fh:
        for line in fh.readlines()[2:]:
            name, _, stats = line.partition(":")
            if not stats or name.strip() == "lo":
                continue
            fields = stats.split()
            rx += int(fields[0])
            tx += int(fields[8])
    return rx, tx


def _meminfo():
    values = {}
    with open("/proc/meminfo", encoding="ascii") as fh:
        for line in fh:
            key, _, rest = line.partition(":")
            values[key] = int(rest.strip().split()[0]) * 1024
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", values.get("MemFree", 0))
    return total, max(total - available, 0)


try:
    _status_prev = (time.time(), *_cpu_times(), *_net_bytes())
except OSError:
    _status_prev = None


def _percent(used, total):
    return round(used * 100 / total, 1) if total else 0.0


def system_status():
    """按两次采样之间的增量计算 CPU 与网速，前端轮询即可得到实时值。"""
    global _status_prev
    now = time.time()
    cpu_total, cpu_idle = _cpu_times()
    rx, tx = _net_bytes()
    mem_total, mem_used = _meminfo()
    vfs = os.statvfs("/")
    disk_total = vfs.f_blocks * vfs.f_frsize
    disk_used = disk_total - vfs.f_bavail * vfs.f_frsize

    with _status_lock:
        prev, _status_prev = _status_prev, (now, cpu_total, cpu_idle, rx, tx)

    cpu_percent = 0.0
    rx_kbps = tx_kbps = 0.0
    if prev:
        elapsed = now - prev[0]
        if elapsed > 0:
            delta_total = cpu_total - prev[1]
            if delta_total > 0:
                cpu_percent = 100 * (delta_total - (cpu_idle - prev[2])) / delta_total
            rx_kbps = max(0, rx - prev[3]) * 8 / 1000 / elapsed
            tx_kbps = max(0, tx - prev[4]) * 8 / 1000 / elapsed

    return {
        "version": APP_VERSION,
        "cpu": {
            "percent": round(min(max(cpu_percent, 0), 100), 1),
            "cores": os.cpu_count() or 1,
        },
        "memory": {
            "total": mem_total,
            "used": mem_used,
            "percent": _percent(mem_used, mem_total),
        },
        "disk": {
            "total": disk_total,
            "used": disk_used,
            "percent": _percent(disk_used, disk_total),
        },
        "network": {"rx_kbps": round(rx_kbps, 1), "tx_kbps": round(tx_kbps, 1)},
    }


def _migrate_dev_schema(conn):
    """开发阶段一次性迁移：users 放开 agent 角色，bindings 增加 agent 列。

    不做旧版本兼容，只保证已有数据不丢：旧绑定行的 agent 为空，
    页面显示为「待补 agent」。SQLite 不支持改 CHECK / 加 UNIQUE 列，只能重建表。
    """
    users_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'users'"
    ).fetchone()
    if users_sql and "'agent'" not in (users_sql["sql"] or ""):
        conn.execute("ALTER TABLE users RENAME TO users_old")
        conn.execute(
            """CREATE TABLE users (
                   username   TEXT PRIMARY KEY,
                   role       TEXT NOT NULL CHECK (role IN ('客户', '设备', 'agent')),
                   created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
               )"""
        )
        conn.execute(
            "INSERT INTO users (username, role, created_at) "
            "SELECT username, role, created_at FROM users_old"
        )
        conn.execute("DROP TABLE users_old")

    cols = {row["name"] for row in conn.execute("PRAGMA table_info(bindings)")}
    bsql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'bindings'"
    ).fetchone()
    if "agent" not in cols or "customer   TEXT NOT NULL UNIQUE" not in (bsql["sql"] or ""):
        conn.execute("ALTER TABLE bindings RENAME TO bindings_old")
        conn.execute(
            """CREATE TABLE bindings (
                   device     TEXT PRIMARY KEY,
                   customer   TEXT NOT NULL UNIQUE,
                   agent      TEXT UNIQUE,
                   created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
               )"""
        )
        conn.execute(
            "INSERT INTO bindings (device, customer, created_at) "
            "SELECT device, customer, created_at FROM bindings_old"
        )
        conn.execute("DROP TABLE bindings_old")


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS users (
                   username   TEXT PRIMARY KEY,
                   role       TEXT NOT NULL CHECK (role IN ('客户', '设备', 'agent')),
                   created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
               )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS bindings (
                   device     TEXT PRIMARY KEY,
                   customer   TEXT NOT NULL UNIQUE,
                   agent      TEXT UNIQUE,
                   created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
               )"""
        )
        _migrate_dev_schema(conn)
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
        conn.execute(ALARM_TABLE_SQL)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_alarms_tenant_time "
            "ON alarms(tenant, occurred_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_alarms_device_time "
            "ON alarms(device, occurred_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_alarms_time ON alarms(occurred_at DESC)"
        )
        conn.execute("PRAGMA journal_mode=WAL")


@contextlib.contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------- 告警持久化 ----------
_alarm_cleanup_at = 0.0
_alarm_cleanup_lock = threading.Lock()


def alarm_text(data, key, limit=200):
    value = data.get(key)
    if value is None:
        return ""
    return str(value).strip()[:limit]


def alarm_epoch(value):
    """把秒/毫秒/微秒时间戳或 ISO 时间统一成 epoch 秒；无法识别返回 None。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if re.fullmatch(r"\d+(?:\.\d+)?", text):
            ts = float(text)
        else:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            return int(parsed.timestamp())
    else:
        return None
    while ts > 1e12:
        ts /= 1000
    # 设备时钟异常时（2000 年以前或未来一年以上）按收到时间处理
    if ts < 946684800 or ts > time.time() + 366 * 86400:
        return None
    return int(ts)


def alarm_time_fields(data, received_at):
    """从 payload 提取 occurred_at 与展示文本，缺失时回退到服务端收到时间。"""
    raw = None
    for key in ("timestamp", "ts", "occurred_at", "time"):
        value = data.get(key)
        if value not in (None, ""):
            raw = value
            break
    occurred_at = alarm_epoch(raw) or received_at
    text = data.get("datetime")
    if not isinstance(text, str) or not text.strip():
        try:
            text = datetime.fromtimestamp(occurred_at).strftime("%Y-%m-%d %H:%M:%S")
        except (OSError, OverflowError, ValueError):
            text = ""
    return raw, occurred_at, text.strip()


def cleanup_alarms():
    """按 occurred_at 清理超过保留期的告警，最多每小时执行一次。"""
    global _alarm_cleanup_at
    now = time.time()
    with _alarm_cleanup_lock:
        if now - _alarm_cleanup_at < 3600:
            return
        _alarm_cleanup_at = now
    cutoff = int(now - ALARM_RETENTION_DAYS * 86400)
    with db() as conn:
        conn.execute("DELETE FROM alarms WHERE occurred_at < ?", (cutoff,))


def emqx(method, path, body=None, token=None):
    """调用 EMQX REST API，返回 (status, data)。

    默认用 API Key；Dashboard 用户相关接口必须传 dashboard 会话 token。
    """
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(EMQX_BASE + path, data=data, method=method)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    else:
        basic = base64.b64encode(f"{EMQX_KEY}:{EMQX_SECRET}".encode()).decode()
        req.add_header("Authorization", "Basic " + basic)
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
            "SELECT device, customer, agent, created_at FROM bindings "
            "ORDER BY customer, device"
        ).fetchall()


def managed_acl(bindings):
    """生成 walle 管理的 ACL 段（客户 / 设备 / agent 三元绑定）。

    规则顺序即 EMQX file 授权源的匹配顺序（首次命中生效）：
      1. 客户：本租户全量；
      2. 设备：只读写 walle/{tenant}/device/${clientid}/#，并可订阅租户级发现广播、客户端与 agent 在线状态；
      3. agent：自己的对话通道 walle/{tenant}/agent/${clientid}/# + 本租户所有设备；
      4. 其余账号禁止访问 walle 命名空间。
    """
    lines = [ACL_BEGIN]

    # 1) 客户：按客户去重，一个客户只生成一条规则
    seen_customers = set()
    for row in bindings:
        customer = row["customer"]
        if customer in seen_customers:
            continue
        seen_customers.add(customer)
        lines.append(f'%% 客户 {customer}：本租户全量')
        lines.append(
            f'{{allow, {{username, "{acl_quote(customer)}"}}, all, ["walle/{customer}/#"]}}.'
        )

    # 2) 设备：${clientid} 在“一台设备一个连接”时成立
    for row in bindings:
        device = acl_quote(row["device"])
        tenant = row["customer"]
        lines.append(f'%% 设备 {row["device"]}：只读写自己那棵子树 + 租户级发现广播/客户端与 agent 在线状态')
        lines.append(
            f'{{allow, {{username, "{device}"}}, publish, '
            f'["walle/{tenant}/device/${{clientid}}/#"]}}.'
        )
        lines.append(
            f'{{allow, {{username, "{device}"}}, subscribe, '
            f'["walle/{tenant}/device/${{clientid}}/#", "walle/{tenant}/discovery/query/request", '
            f'"walle/{tenant}/client/+/online", "walle/{tenant}/agent/+/online"]}}.'
        )

    # 3) agent：一个 agent 只绑一台设备（UNIQUE），这里按 agent 再兜底去重
    seen_agents = set()
    for row in bindings:
        agent = row["agent"]
        if not agent or agent in seen_agents:
            continue
        seen_agents.add(agent)
        tenant = row["customer"]
        lines.append(f'%% agent {agent}：自己的对话通道 + 本租户所有设备')
        lines.append(
            f'{{allow, {{username, "{acl_quote(agent)}"}}, all, '
            f'["walle/{tenant}/agent/${{clientid}}/#"]}}.'
        )
        lines.append(
            f'{{allow, {{username, "{acl_quote(agent)}"}}, all, '
            f'["walle/{tenant}/device/#"]}}.'
        )

    lines.append("%% 未绑定设备及其他账号禁止访问 walle 命名空间")
    lines.append('{deny, all, all, ["walle/#"]}.')
    lines.append(ACL_END)
    return "\n".join(lines)


def acl_preview_payload():
    """只读预览：不调用 EMQX，返回将要生成的 ACL 文本与统计。"""
    bindings = binding_rows()
    text = managed_acl(bindings)
    return {
        "text": text,
        "stats": {
            "bindings": len(bindings),
            "customers": len({row["customer"] for row in bindings}),
            "devices": len({row["device"] for row in bindings}),
            "agents": len({row["agent"] for row in bindings if row["agent"]}),
            "lines": len(text.splitlines()),
        },
    }


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
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/health":
            return self._json(200, {"status": "ok"})
        if path == "/api/alarms":
            return self.list_alarms()
        if path == "/api/alarms/devices":
            return self.list_alarm_devices()
        if self.path.startswith("/api/rtc-token"):
            return self.rtc_token()
        if self.path == "/api/users":
            return self.list_users()
        if self.path == "/api/bindings":
            return self.list_bindings()
        if self.path == "/api/acl/preview":
            return self.acl_preview()
        if self.path == "/api/admin/stream-secret":
            return self.get_stream_secret()
        if self.path == "/api/admin/https":
            return self.https_status()
        if self.path == "/api/admin/system-status":
            return self.system_status()
        if self.path == "/api/admin/profile":
            return self.admin_profile()
        self._json(404, {"message": "not found"})

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/internal/alarms":
            return self.ingest_alarm()
        if path == "/api/srs/hook":
            return self.srs_hook()
        if self.path == "/api/admin/stream-secret/regenerate":
            return self.regenerate_stream_secret()
        if self.path == "/api/admin/https/apply":
            return self.https_apply()
        if self.path == "/api/admin/https/renew":
            return self.https_renew()
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
        if self.path == "/api/admin/password":
            return self.change_admin_password()
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
        app = str(body.get("app") or "")
        stream = str(body.get("stream") or "")
        current = get_media_secret()
        if not current:
            self.log_message("SRS 拒绝推流：服务端未配置 MEDIA_SERVER_SECRET")
            return self._json(403, {"code": 403, "message": "secret not configured"})
        # 设备/App 用静态密钥；浏览器 WHIP 用短时效 token，任一通过即放行
        supplied = (params.get("secret") or [""])[0]
        if supplied and hmac.compare_digest(supplied.encode(), current.encode()):
            return self._json(200, 0)
        token = (params.get("token") or [""])[0]
        if token and verify_rtc_token(token, app, stream):
            return self._json(200, 0)
        self.log_message("SRS 拒绝推流：app=%s stream=%s ip=%s token=%s",
                         app, stream, body.get("ip"), "yes" if token else "no")
        return self._json(403, {"code": 403, "message": "invalid credential"})

    # ---------- 告警接入与查询 ----------
    def ingest_alarm(self):
        """EMQX 规则引擎回调：把 walle/{tenant}/device/{device}/alarm/report 落库。

        只接受回环地址并校验共享 token；nginx 另有 /api/internal/ deny 规则。
        """
        if self.client_address[0] not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            return self._json(403, {"message": "forbidden"})
        if not ALARM_INGEST_TOKEN:
            return self._json(503, {"message": "未配置 WALLE_INGEST_TOKEN，拒绝告警接入"})
        supplied = self.headers.get("X-Walle-Ingest") or ""
        if not hmac.compare_digest(supplied.encode(), ALARM_INGEST_TOKEN.encode()):
            return self._json(403, {"message": "invalid ingest token"})
        body = self._body()
        if not isinstance(body, dict):
            return self._json(400, {"message": "请求体不是合法 JSON"})
        topic = str(body.get("topic") or "").strip()
        payload = body.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                return self._json(400, {"message": "payload 不是合法 JSON"})
        if not isinstance(payload, dict):
            return self._json(400, {"message": "payload 必须是 JSON 对象"})
        parts = topic.split("/")
        if (len(parts) != 6 or parts[0] != "walle" or parts[2] != "device"
                or parts[4] != "alarm" or parts[5] != "report"):
            return self._json(
                400, {"message": "topic 不符合 walle/{tenant}/device/{device}/alarm/report"})
        tenant, device = parts[1], parts[3]
        if not valid_tenant(tenant) or not device or any(ord(ch) < 0x20 for ch in device):
            return self._json(400, {"message": "topic 中的租户或设备名不合法"})

        received_at = int(time.time())
        raw_time, occurred_at, occurred_text = alarm_time_fields(payload, received_at)
        event = alarm_text(payload, "event")
        level = alarm_text(payload, "level", 32).lower() or "info"
        metric = alarm_text(payload, "metric")
        alarm_type = alarm_text(payload, "alarm_type") or alarm_text(payload, "type")
        message = alarm_text(payload, "message", 500)
        dedupe = hashlib.sha1(
            "\x1f".join([
                tenant,
                device,
                event,
                metric,
                alarm_type,
                "" if raw_time is None else str(raw_time),
                message,
            ]).encode()
        ).hexdigest()
        with db() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO alarms
                   (tenant, device, hostname, device_uuid, ip, event, level, metric,
                    alarm_type, message, occurred_at, occurred_text, received_at, detail, dedupe)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    tenant,
                    device,
                    alarm_text(payload, "hostname") or None,
                    alarm_text(payload, "device_id") or device or None,
                    alarm_text(payload, "ip", 64) or None,
                    event or None,
                    level,
                    metric or None,
                    alarm_type or None,
                    message or None,
                    occurred_at,
                    occurred_text or None,
                    received_at,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    dedupe,
                ),
            )
            duplicate = cur.rowcount == 0
        try:
            cleanup_alarms()
        except sqlite3.Error as exc:
            self.log_message("清理过期告警失败: %s", exc)
        return self._json(200, {"ok": True, "duplicate": duplicate, "id": cur.lastrowid})

    def _alarm_scope(self):
        """返回 (鉴权记录, tenant 过滤值)；客户只能看自己租户，管理员为 None。"""
        record = self._session()
        if not record:
            return None, None
        if record[3] == "administrator":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            tenant = (query.get("tenant") or [""])[0].strip()
            return record, tenant or None
        return record, record[0]

    def list_alarms(self):
        record, tenant = self._alarm_scope()
        if not record:
            return self._deny()
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)

        def first(key):
            return (query.get(key) or [""])[0].strip()

        where, params = [], []
        if tenant:
            where.append("tenant = ?")
            params.append(tenant)
        device = first("device")
        if device:
            where.append("device = ?")
            params.append(device)
        level = first("level")
        if level:
            where.append("level = ?")
            params.append(level)
        time_from = alarm_epoch(first("from"))
        if time_from is not None:
            where.append("occurred_at >= ?")
            params.append(time_from)
        time_to = alarm_epoch(first("to"))
        if time_to is not None:
            where.append("occurred_at <= ?")
            params.append(time_to)

        try:
            limit = int(first("limit") or 30)
        except ValueError:
            limit = 30
        limit = min(max(limit, 1), 30)

        with db() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM alarms" + (" WHERE " + " AND ".join(where) if where else ""),
                params,
            ).fetchone()[0]
            pages = (total + limit - 1) // limit if total else 0
            try:
                page = int(first("page") or 1)
            except ValueError:
                page = 1
            page = min(max(page, 1), pages or 1)
            rows = conn.execute(
                "SELECT * FROM alarms"
                + (" WHERE " + " AND ".join(where) if where else "")
                + " ORDER BY occurred_at DESC, id DESC LIMIT ? OFFSET ?",
                params + [limit, (page - 1) * limit],
            ).fetchall()

        items = [
            {
                "id": row["id"],
                "tenant": row["tenant"],
                "device": row["device"],
                "hostname": row["hostname"],
                "device_uuid": row["device_uuid"],
                "ip": row["ip"],
                "event": row["event"],
                "level": row["level"],
                "metric": row["metric"],
                "alarm_type": row["alarm_type"],
                "message": row["message"],
                "occurred_at": row["occurred_at"],
                "occurred_text": row["occurred_text"],
                "received_at": row["received_at"],
            }
            for row in rows
        ]
        return self._json(200, {
            "items": items,
            "total": total,
            "page": page,
            "pages": pages,
            "page_size": limit,
        })

    def list_alarm_devices(self):
        record, tenant = self._alarm_scope()
        if not record:
            return self._deny()
        where, params = [], []
        if tenant:
            where.append("tenant = ?")
            params.append(tenant)
        with db() as conn:
            rows = conn.execute(
                "SELECT device, MAX(hostname) AS hostname, COUNT(*) AS count, "
                "MAX(occurred_at) AS last_at FROM alarms"
                + (" WHERE " + " AND ".join(where) if where else "")
                + " GROUP BY device ORDER BY last_at DESC",
                params,
            ).fetchall()
        return self._json(200, {
            "data": [
                {
                    "device": row["device"],
                    "hostname": row["hostname"],
                    "count": row["count"],
                    "last_at": row["last_at"],
                }
                for row in rows
            ]
        })

    # ---------- 流鉴权凭据 ----------
    def rtc_token(self):
        """给已登录用户签发短时效 WHIP 推流凭据（不含 MEDIA_SERVER_SECRET）。"""
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or session_user(auth[7:].strip()) is None:
            return self._json(401, {"message": "未登录或会话已失效，请重新登录"})
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        app = (query.get("app") or ["live"])[0].strip() or "live"
        stream = (query.get("stream") or [""])[0].strip()
        if not STREAM_RE.match(app):
            return self._json(400, {"message": "app 参数不合法"})
        if not STREAM_RE.match(stream):
            return self._json(400, {"message": "stream 参数不合法"})
        token = make_rtc_token(app, stream)
        if not token:
            return self._json(503, {"message": "服务端未配置 MEDIA_SERVER_SECRET，无法签发推流凭据"})
        return self._json(200, {
            "token": token,
            "expires_in": RTC_TOKEN_TTL,
            "app": app,
            "stream": stream,
        })

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

    # ---------- 一键 HTTPS ----------
    def https_status(self):
        if not self._is_admin():
            return self._deny()
        state = https_state()
        domain = str(state.get("domain") or "")
        epoch = state.get("not_after_epoch")
        days_left = None
        if isinstance(epoch, (int, float)):
            days_left = max(0, int((epoch - time.time()) // 86400))
        emqx = state.get("emqx")
        if not isinstance(emqx, dict):
            emqx = {}
        return self._json(200, {
            "enabled": bool(domain) and os.path.exists(HTTPS_SITE_ENABLED),
            "domain": domain,
            "email": state.get("email") or "",
            "not_after": state.get("not_after") or "",
            "days_left": days_left,
            "applied_at": state.get("applied_at") or "",
            "https_url": f"https://{domain}/" if domain else "",
            "emqx": emqx,
            "helper": HTTPS_HELPER,
        })

    def https_apply(self):
        """申请并启用 HTTPS：调用特权脚本申请证书、写 nginx 配置并重载。"""
        if not self._is_admin():
            return self._deny()
        body = self._body()
        if not isinstance(body, dict):
            return self._json(400, {"message": "请求体格式错误"})
        domain = str(body.get("domain") or "").strip().lower().rstrip(".")
        email = str(body.get("email") or "").strip()
        if not DOMAIN_RE.match(domain):
            return self._json(400, {"message": "域名格式不正确，例如 live.example.com"})
        if email and not EMAIL_RE.match(email):
            return self._json(400, {"message": "邮箱格式不正确"})
        result = run_https_helper(["apply", domain, email], timeout=300)
        if not result.get("ok"):
            return self._json(502, {
                "message": result.get("error") or "申请证书失败",
                "hint": HTTPS_SUDO_HINT,
            })
        return self._json(200, {
            "message": https_result_message(f"{domain} 已启用 HTTPS", result),
            "state": result,
        })

    def https_renew(self):
        if not self._is_admin():
            return self._deny()
        state = https_state()
        domain = str(state.get("domain") or "")
        if not domain:
            return self._json(400, {"message": "尚未启用 HTTPS，请先申请证书"})
        result = run_https_helper(
            ["renew", domain, str(state.get("email") or "")], timeout=180
        )
        if not result.get("ok"):
            return self._json(502, {
                "message": result.get("error") or "续期失败",
                "hint": HTTPS_SUDO_HINT,
            })
        return self._json(200, {
            "message": https_result_message("证书已续期", result),
            "state": result,
        })

    # ---------- 运行状态 / 管理员设置 ----------
    def system_status(self):
        if not self._is_admin():
            return self._deny()
        try:
            return self._json(200, system_status())
        except OSError as exc:
            return self._json(500, {"message": f"读取系统状态失败：{exc}"})

    def admin_profile(self):
        record = self._session()
        if not record:
            return self._deny()
        return self._json(200, {
            "username": record[0],
            "role": record[3],
            "login_at": record[2],
            "expires_at": record[1],
        })

    def change_admin_password(self):
        record = self._session()
        if not record:
            return self._deny()
        body = self._body()
        if not isinstance(body, dict):
            return self._json(400, {"message": "请求体格式错误"})
        old = str(body.get("old_password") or "")
        new = str(body.get("new_password") or "")
        if not old or not new:
            return self._json(400, {"message": "当前密码和新密码都不能为空"})
        if len(new) < 6:
            return self._json(400, {"message": "新密码至少 6 位"})
        username = record[0]
        # change_pwd 不接受 API Key，必须先用当前密码换取 Dashboard 会话 token
        status, data = emqx_login(username, old)
        if status == 0:
            return self._json(502, {"message": data.get("message", "EMQX 不可达")})
        if status != 200:
            return self._json(400, {"message": "当前密码不正确"})
        token = (data or {}).get("token")
        if not token:
            return self._json(502, {"message": "EMQX 未返回登录凭据，无法修改密码"})
        status, data = emqx(
            "POST",
            f"/users/{urllib.parse.quote(username, safe='')}/change_pwd",
            {"old_pwd": old, "new_pwd": new},
            token=token,
        )
        if status in (200, 204):
            return self._json(200, {"message": "管理员密码已更新"})
        if status == 0:
            message = data.get("message") if isinstance(data, dict) else ""
            return self._json(502, {"message": message or "EMQX 不可达"})
        if status in (401, 403):
            return self._json(502, {"message": "EMQX 拒绝修改密码，请重新登录后再试"})
        message = data.get("message") if isinstance(data, dict) else ""
        return self._json(400, {"message": message or "密码修改失败，请检查当前密码"})

    def _session(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        return session_record(auth[7:].strip())

    def _is_admin(self):
        record = self._session()
        return record is not None and record[3] == "administrator"

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
        return self._json(200, {
            "username": username,
            "token": new_session(username, "administrator"),
            "role": "administrator",
        })

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
        return self._json(200, {
            "username": username,
            "role": row["role"],
            "token": new_session(username, row["role"]),
        })

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
        order = {"客户": 0, "agent": 1, "设备": 2}
        users.sort(key=lambda u: (order.get(u["role"], 3), u["username"]))
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
            return self._json(400, {"message": "角色只能是 客户、设备 或 agent"})
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
            if row is not None and row["role"] == "agent":
                count = conn.execute(
                    "SELECT COUNT(*) FROM bindings WHERE agent = ?", (username,)
                ).fetchone()[0]
                if count:
                    return self._json(
                        409,
                        {"message": f"agent {username} 仍绑定 {count} 台设备，请先解除绑定"},
                    )
            binding = conn.execute(
                "SELECT device, customer, agent, created_at FROM bindings WHERE device = ?",
                (username,),
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

    # ---------- 设备绑定（客户 + 设备 + agent，三者 1:1） ----------
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
        agent = (body.get("agent") or "").strip()
        if not device or not customer or not agent:
            return self._json(400, {"message": "设备、客户、agent 三者都必须选择"})
        if any(ord(ch) < 0x20 for ch in device):
            return self._json(400, {"message": "设备名不能包含控制字符"})
        if not valid_tenant(customer):
            return self._json(400, {"message": "客户名不能包含 / + # 等 MQTT 特殊字符"})
        if not valid_tenant(agent):
            return self._json(400, {"message": "agent 名不能包含 / + # 等 MQTT 特殊字符"})
        with db() as conn:
            dev = conn.execute("SELECT role FROM users WHERE username = ?", (device,)).fetchone()
            cus = conn.execute("SELECT role FROM users WHERE username = ?", (customer,)).fetchone()
            agt = conn.execute("SELECT role FROM users WHERE username = ?", (agent,)).fetchone()
            if dev is None or dev["role"] != "设备":
                return self._json(400, {"message": f"{device} 不是角色为「设备」的用户"})
            if cus is None or cus["role"] != "客户":
                return self._json(400, {"message": f"{customer} 不是角色为「客户」的用户"})
            if agt is None or agt["role"] != "agent":
                return self._json(400, {"message": f"{agent} 不是角色为「agent」的用户"})
            exists = conn.execute(
                "SELECT customer, agent FROM bindings WHERE device = ?", (device,)
            ).fetchone()
            if exists is not None:
                return self._json(
                    409, {"message": f"设备 {device} 已绑定（客户 {exists['customer']}），请先解除绑定"}
                )
            agent_used = conn.execute(
                "SELECT device FROM bindings WHERE agent = ?", (agent,)
            ).fetchone()
            if agent_used is not None:
                return self._json(
                    409, {"message": f"agent {agent} 已绑定设备 {agent_used['device']}，agent 与设备只能 1:1"}
                )
            customer_used = conn.execute(
                "SELECT device FROM bindings WHERE customer = ?", (customer,)
            ).fetchone()
            if customer_used is not None:
                return self._json(
                    409, {"message": f"客户 {customer} 已绑定设备 {customer_used['device']}，客户与设备只能 1:1"}
                )
            conn.execute(
                "INSERT INTO bindings (device, customer, agent) VALUES (?, ?, ?)",
                (device, customer, agent),
            )
        try:
            sync_bindings_acl()
        except RuntimeError as exc:
            with db() as conn:
                conn.execute("DELETE FROM bindings WHERE device = ?", (device,))
            return self._json(502, {"message": f"绑定失败，ACL 未写入: {exc}"})
        return self._json(201, {"device": device, "customer": customer, "agent": agent})

    def delete_binding(self, device):
        if not self._is_admin():
            return self._deny()
        with db() as conn:
            row = conn.execute(
                "SELECT device, customer, agent, created_at FROM bindings WHERE device = ?",
                (device,),
            ).fetchone()
            if row is None:
                return self._json(404, {"message": f"设备 {device} 没有绑定记录"})
            conn.execute("DELETE FROM bindings WHERE device = ?", (device,))
        try:
            sync_bindings_acl()
        except RuntimeError as exc:
            with db() as conn:
                conn.execute(
                    "INSERT INTO bindings (device, customer, agent, created_at) VALUES (?, ?, ?, ?)",
                    (row["device"], row["customer"], row["agent"], row["created_at"]),
                )
            return self._json(502, {"message": f"解除绑定失败，ACL 未更新: {exc}"})
        return self._json(200, {"device": device, "message": "已解除绑定并删除 ACL"})

    def acl_preview(self):
        if not self._is_admin():
            return self._deny()
        return self._json(200, acl_preview_payload())


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
