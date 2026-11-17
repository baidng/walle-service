#!/usr/bin/env python3
"""Minimal MQTT 3.1.1 demo client: connect + subscribe demo/# + keepalive."""
import socket
import struct
import time

HOST = "127.0.0.1"
PORT = 1883
CLIENT_ID = "srs-mqtt-demo"


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
    sock = socket.create_connection((HOST, PORT), timeout=10)
    variable = enc_str("MQTT") + bytes([4, 0x02]) + struct.pack("!H", 60)
    sock.sendall(packet(0x10, variable + enc_str(CLIENT_ID)))
    sock.recv(4)  # CONNACK
    body = struct.pack("!H", 1) + enc_str("demo/#") + bytes([1])
    sock.sendall(packet(0x82, body))
    sock.recv(5)  # SUBACK
    while True:
        sock.sendall(b"\xc0\x00")  # PINGREQ
        time.sleep(20)
        try:
            sock.recv(1024)
        except socket.timeout:
            pass


if __name__ == "__main__":
    main()
