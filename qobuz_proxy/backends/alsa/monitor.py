"""Best-effort PCM telemetry, never on the ALSA write path.

Set QOBUZ_PCM_MONITOR_SOCKET to a local AF_UNIX datagram listener path.
Packets contain original signed little-endian source PCM (not ALSA's
24-in-32 container), with a compact header. No listener is required.
"""
from __future__ import annotations

import os
import socket
import struct
import time

from .pcm import PcmFormat

MAGIC = b"QPCM"
VERSION = 1
# magic, version, channels, source bits, sample rate, sample count
HEADER = struct.Struct("<4sBBBIH")


class PcmMonitor:
    def __init__(self, path: str | None = None, *, interval: float = 1 / 25):
        self.path = path if path is not None else os.environ.get("QOBUZ_PCM_MONITOR_SOCKET", "")
        self.interval = interval
        self.next_send = 0.0
        self.sock: socket.socket | None = None
        if self.path:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            self.sock.setblocking(False)

    def publish(self, data: bytes, fmt: PcmFormat) -> None:
        sock = self.sock
        if sock is None:
            return
        now = time.monotonic()
        if now < self.next_send:
            return
        self.next_send = now + self.interval
        frame_bytes = fmt.channels * ((fmt.bits_per_sample + 7) // 8)
        count = min(1024, len(data) // frame_bytes)
        if count == 0:
            return
        try:
            packet = HEADER.pack(MAGIC, VERSION, fmt.channels, fmt.bits_per_sample,
                                 fmt.sample_rate, count) + data[:count * frame_bytes]
            sock.sendto(packet, self.path)
        except (OSError, ValueError):
            # Missing/full listener or oversized datagram: drop monitor sample.
            # Playback must never wait for a monitor consumer.
            pass

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None
