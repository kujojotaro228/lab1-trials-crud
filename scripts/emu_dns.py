#!/usr/bin/env python3
"""DNS-сервер стенда-емуляції: аналог вбудованого DNS Docker (127.0.0.11).

Відповідає A-записами на імена з файлу hosts.json ({"web": ["127.0.1.1", ...]}),
файл перечитується на кожен запит — так --scale змінює відповідь одразу.
Інші імена пересилаються на перший nameserver з /etc/resolv.conf.

    python scripts/emu_dns.py --listen 127.0.0.11 --hosts /path/hosts.json
"""
from __future__ import annotations

import argparse
import json
import socket
import struct
import threading


def parse_qname(pkt: bytes, off: int = 12) -> tuple[str, int]:
    labels = []
    while True:
        ln = pkt[off]
        off += 1
        if ln == 0:
            break
        labels.append(pkt[off:off + ln].decode("ascii", "replace"))
        off += ln
    return ".".join(labels), off


def answer(pkt: bytes, ips: list[str], qend: int) -> bytes:
    tid = pkt[:2]
    flags = 0x8180                                  # відповідь, RD, RA, NOERROR
    qtype = struct.unpack("!H", pkt[qend:qend + 2])[0]
    q = pkt[12:qend + 4]
    recs = b""
    n = 0
    if qtype in (1, 255):                           # A або ANY
        for ip in ips:
            recs += struct.pack("!HHHIH", 0xC00C, 1, 1, 0, 4) + socket.inet_aton(ip)
            n += 1
    return tid + struct.pack("!HHHHH", flags, 1, n, 0, 0) + q + recs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", default="127.0.0.11")
    ap.add_argument("--port", type=int, default=53)
    ap.add_argument("--hosts", required=True)
    ap.add_argument("--upstream", default=None)
    a = ap.parse_args()
    upstream = a.upstream
    if upstream is None:
        for line in open("/etc/resolv.conf"):
            if line.startswith("nameserver") and "127.0.0.11" not in line:
                upstream = line.split()[1]
                break
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((a.listen, a.port))

    def forward(pkt, addr):
        try:
            u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            u.settimeout(3)
            u.sendto(pkt, (upstream, 53))
            sock.sendto(u.recv(4096), addr)
        except OSError:
            pass

    while True:
        pkt, addr = sock.recvfrom(4096)
        try:
            name, qend = parse_qname(pkt)
            try:
                hosts = json.load(open(a.hosts))
            except (OSError, ValueError):
                hosts = {}
            key = name.lower().rstrip(".")
            if key in hosts:
                sock.sendto(answer(pkt, hosts[key], qend), addr)
            elif upstream:
                threading.Thread(target=forward, args=(pkt, addr), daemon=True).start()
        except Exception:  # noqa: BLE001
            continue


if __name__ == "__main__":
    main()
