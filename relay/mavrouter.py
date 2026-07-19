#!/usr/bin/env python3
"""Tiny MAVLink UDP fan-out router (pure stdlib).

Receives the vehicle link on a single endpoint and mirrors it to multiple
ground stations (this GCS + QGroundControl), and forwards their packets back
to the vehicle. Enough for one vehicle + several GCS clients — no sysid-aware
routing, just a clean bidirectional fan-out that never loops GCS→GCS.

    # vehicle sends UDP to this Mac:14550; mirror to GCS (14551) + QGC (14552)
    python3 mavrouter.py --master udpin:0.0.0.0:14550 \
        --out 127.0.0.1:14551 --out 127.0.0.1:14552

Endpoint syntax for --master:
    udpin:HOST:PORT    bind & listen (vehicle/bridge sends to us)  [default]
    udpout:HOST:PORT   connect out (a service is listening)
Each --out is a local UDP port a GCS listens on (udpin on their side).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import struct

logging.basicConfig(level=logging.INFO, format="%(asctime)s mavrouter %(message)s")
log = logging.getLogger("mavrouter")


def _crc_accumulate(b: int, crc: int) -> int:
    tmp = b ^ (crc & 0xFF)
    tmp = (tmp ^ (tmp << 4)) & 0xFF
    return ((crc >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4)) & 0xFFFF


def build_heartbeat(seq: int) -> bytes:
    """A valid MAVLink v1 GCS heartbeat (sysid 255). Some datalinks (e.g. SIYI)
    only start streaming once they hear a GCS, so the router emits these."""
    # payload: custom_mode(u32)=0, type=GCS(6), autopilot=INVALID(8), base_mode=0,
    #          system_status=0, mavlink_version=3
    payload = struct.pack("<IBBBBB", 0, 6, 8, 0, 0, 3)
    header = bytes([len(payload), seq & 0xFF, 255, 190, 0])  # len,seq,sysid,compid,msgid=0
    crc = 0xFFFF
    for b in header + payload:
        crc = _crc_accumulate(b, crc)
    crc = _crc_accumulate(50, crc)  # CRC_EXTRA for HEARTBEAT
    return bytes([0xFE]) + header + payload + struct.pack("<H", crc)


class Router:
    def __init__(self) -> None:
        self.master: asyncio.DatagramTransport | None = None
        self.master_peer: tuple[str, int] | None = None  # learned vehicle addr
        self.master_connected = False  # True when master is udpout (fixed peer)
        self.fixed_peer: tuple[str, int] | None = None  # configured vehicle endpoint
        self.outs: list[asyncio.DatagramTransport] = []
        self.n_down = 0
        self.n_up = 0

    def to_ground(self, data: bytes) -> None:
        for t in self.outs:
            t.sendto(data)  # connected sockets — peer implicit
        self.n_down += 1
        if self.n_down % 500 == 0:
            log.info("↓ %d pkts to ground, ↑ %d to vehicle", self.n_down, self.n_up)

    def to_vehicle(self, data: bytes) -> None:
        if self.master is None:
            return
        if self.master_connected:
            self.master.sendto(data)
        else:
            targets = set()
            if self.master_peer is not None:
                targets.add(self.master_peer)
            if self.fixed_peer is not None:
                targets.add(self.fixed_peer)
            for t in targets:
                self.master.sendto(data, t)
        self.n_up += 1


class MasterProto(asyncio.DatagramProtocol):
    def __init__(self, router: Router) -> None:
        self.r = router

    def connection_made(self, transport):  # type: ignore[override]
        self.r.master = transport

    def datagram_received(self, data: bytes, addr):  # type: ignore[override]
        if not self.r.master_connected:
            if self.r.master_peer != addr:
                log.info("vehicle link from %s", addr)
            self.r.master_peer = addr
        self.r.to_ground(data)


class OutProto(asyncio.DatagramProtocol):
    def __init__(self, router: Router) -> None:
        self.r = router

    def datagram_received(self, data: bytes, addr):  # type: ignore[override]
        self.r.to_vehicle(data)


def parse_master(spec: str) -> tuple[str, str, int]:
    kind, rest = spec.split(":", 1)
    host, port = rest.rsplit(":", 1)
    return kind, host, int(port)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--master", default="udpin:0.0.0.0:14550")
    ap.add_argument("--out", action="append", default=[], help="HOST:PORT (repeatable)")
    ap.add_argument("--peer", default=None,
                    help="vehicle endpoint HOST:PORT to send GCS heartbeats to (e.g. SIYI 192.168.144.12:19856)")
    args = ap.parse_args()
    outs = args.out or ["127.0.0.1:14551", "127.0.0.1:14552"]

    loop = asyncio.get_running_loop()
    router = Router()
    if args.peer:
        ph, pp = args.peer.rsplit(":", 1)
        router.fixed_peer = (ph, int(pp))
        log.info("vehicle peer (heartbeats → ): %s:%s", ph, pp)

    kind, host, port = parse_master(args.master)
    if kind == "udpin":
        # The port may still be held by QGC; wait for it to free up.
        warned = False
        while True:
            try:
                await loop.create_datagram_endpoint(
                    lambda: MasterProto(router), local_addr=(host, port)
                )
                break
            except OSError as exc:
                if not warned:
                    log.warning("%s:%d busy (%s) — waiting for it to free up…", host, port, exc)
                    warned = True
                await asyncio.sleep(1.0)
        log.info("master: listening on %s:%d", host, port)
    elif kind == "udpout":
        router.master_connected = True
        await loop.create_datagram_endpoint(
            lambda: MasterProto(router), remote_addr=(host, port)
        )
        log.info("master: connected out to %s:%d", host, port)
    else:
        raise SystemExit(f"unsupported master kind: {kind} (use udpin/udpout)")

    for o in outs:
        h, p = o.rsplit(":", 1)
        t, _ = await loop.create_datagram_endpoint(
            lambda: OutProto(router), remote_addr=(h, int(p))
        )
        router.outs.append(t)
        log.info("out: %s:%s", h, p)

    if router.fixed_peer is not None:
        async def _heartbeat() -> None:
            seq = 0
            while True:
                await asyncio.sleep(1.0)
                if router.master is not None:
                    router.to_vehicle(build_heartbeat(seq))
                    seq += 1
        asyncio.create_task(_heartbeat())

    log.info("router up — relay both directions")
    await asyncio.Event().wait()  # run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
