"""GCS-IP beacon — makes Outrider's video transport IP-INDEPENDENT.

Outrider (the Jetson) PUSHES its OAK-D RGB stream (H.264/MPEG-TS over UDP) to
THIS GCS host. The Mac's DHCP IP can change at any time, which would otherwise
break the feed (the Jetson would keep pushing at the stale address).

This task periodically sends a tiny UDP "beacon" to the Jetson. The Jetson's
`outrider-discovery` daemon reads the beacon packet's SOURCE address — which is
exactly the source IP the OS will also use for the inbound video push (same route
to the same host) — and re-points the push there, restarting its stream only when
the IP actually changes. So when the Mac's IP changes, the next beacon teaches the
Jetson the new address and the feed recovers on its own.

The beacon payload is `GCS_BEACON` (the source IP is what matters). Fully guarded
and gated on OUTRIDER_JETSON_HOST being set, so it can never affect the backend.
"""
from __future__ import annotations

import asyncio
import logging
import socket

from .config import settings

log = logging.getLogger("gcs.beacon")


async def gcs_beacon_loop() -> None:
    host = (settings.outrider_jetson_host or "").strip()
    interval = settings.outrider_beacon_interval_s
    port = settings.outrider_beacon_port
    if not host or interval <= 0 or port <= 0:
        log.info("GCS beacon disabled (host=%r interval=%s port=%s)", host, interval, port)
        return

    payload = b"GCS_BEACON"
    log.info("GCS beacon -> %s:%d every %.1fs (Outrider video IP-discovery)", host, port, interval)

    def _new_socket() -> socket.socket:
        return socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    sock = _new_socket()
    try:
        while True:
            try:
                sock.sendto(payload, (host, port))
            except OSError as e:
                # A cached socket can be pinned to a now-invalid source route after
                # the host's IP changes — the very event this beacon exists to
                # handle (M7). Recreate the socket so the OS re-picks the current
                # source route on the next send; otherwise sendto would keep failing
                # forever and the Jetson would never learn the new GCS address.
                log.debug("GCS beacon send failed: %s — recreating socket", e)
                try:
                    sock.close()
                except OSError:
                    pass
                try:
                    sock = _new_socket()
                except OSError as e2:
                    log.debug("GCS beacon socket recreate failed: %s", e2)
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        raise
    finally:
        try:
            sock.close()
        except OSError:
            pass
