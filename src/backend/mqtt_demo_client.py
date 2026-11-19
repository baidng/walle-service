#!/usr/bin/env python3
"""Minimal MQTT 3.1.1 demo client: connect + subscribe + keepalive.

可选环境变量：
  MQTT_HOST       默认 127.0.0.1
  MQTT_PORT       默认 1883
  MQTT_CLIENT_ID  默认 srs-mqtt-demo
  MQTT_USERNAME   MQTT 用户名（创建内置数据库认证器后必填）
  MQTT_PASSWORD   MQTT 密码
  MQTT_TOPIC      订阅主题，默认 demo/#
"""
import os
import socket
import struct
import time

HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
PORT = int(os.environ.get("MQTT_PORT", "1883"))
CLIENT_ID = os.environ.get("MQTT_CLIENT_ID", "srs-mqtt-demo")
USERNAME = os.environ.get("MQTT_USERNAME", "")
PASSWORD = os.environ.get("MQTT_PASSWORD", "")
TOPIC = os.environ.get("MQTT_TOPIC", "demo/#")


def enc_str(value: str) -> bytes:
    raw = value.encode()
    return struct.pack("!H", len(raw)) + raw


def rem_len(length: int) -> bytes:
    out = b""
    while True:
        digit = length % 128
        length //= 128
        out += bytes([digit | (0x80 if length > 0 else 0)])
        if length == 0:
            return out


def packet(first: int, body: bytes) -> bytes:
    return bytes([first]) + rem_len(len(body)) + body


def main() -> None:
    if PASSWORD and not USERNAME:
        raise SystemExit("MQTT_PASSWORD 需要同时设置 MQTT_USERNAME")

    flags = 0x02  # Clean Session
    payload = enc_str(CLIENT_ID)
    if USERNAME:
        flags |= 0x80
        payload += enc_str(USERNAME)
    if PASSWORD:
        flags |= 0x40
        payload += enc_str(PASSWORD)

    sock = socket.create_connection((HOST, PORT), timeout=10)
    variable = enc_str("MQTT") + bytes([4, flags]) + struct.pack("!H", 60)
    sock.sendall(packet(0x10, variable + payload))

    connack = sock.recv(4)
    if len(connack) < 4 or connack[3] != 0:
        code = connack[3] if len(connack) >= 4 else "unknown"
        raise SystemExit(f"MQTT CONNECT 失败，返回码 {code}（4=用户名或密码错误，5=未授权）")

    body = struct.pack("!H", 1) + enc_str(TOPIC) + bytes([1])
    sock.sendall(packet(0x82, body))
    sock.recv(5)  # SUBACK

    print(f"connected: client_id={CLIENT_ID} username={USERNAME or '-'} topic={TOPIC}", flush=True)
    while True:
        sock.sendall(b"\xc0\x00")  # PINGREQ
        time.sleep(20)
        try:
            sock.recv(1024)
        except socket.timeout:
            pass


if __name__ == "__main__":
    main()
