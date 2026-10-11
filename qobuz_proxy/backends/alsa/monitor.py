"""Optional, best-effort audio analysis tap.

A bounded rolling window of source PCM is downsampled ONLY for monitoring.
The original PCM sent to ALSA is never altered. Socket sends are nonblocking.
"""
from __future__ import annotations

import os
import socket
import struct
import time

from .pcm import PcmFormat

MAGIC = b"QPCM"
VERSION = 1
HEADER = struct.Struct("<4sBBBIH")


class PcmMonitor:
    def __init__(self, path: str | None = None, *, interval: float = 0.08):
        self.path = path if path is not None else os.environ.get("QOBUZ_PCM_MONITOR_SOCKET", "")
        self.interval = interval
        self.next_send = 0.0
        self.sock: socket.socket | None = None
        self._buffer = bytearray()
        self._format: PcmFormat | None = None
        if self.path:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            self.sock.setblocking(False)

    def publish(self, data: bytes, fmt: PcmFormat) -> None:
        sock = self.sock
        if sock is None:
            return
        # Keep a continuous 180 ms rolling window across decoder chunks.
        if self._format != fmt:
            self._buffer.clear()
            self._format = fmt
            self.next_send = 0.0
        width = (fmt.bits_per_sample + 7) // 8
        frame_bytes = width * fmt.channels
        window_frames = max(1, int(fmt.sample_rate * 0.18))
        max_bytes = window_frames * frame_bytes
        self._buffer.extend(data)
        if len(self._buffer) > max_bytes:
            del self._buffer[:len(self._buffer) - max_bytes]
        now = time.monotonic()
        if now < self.next_send or len(self._buffer) < max_bytes:
            return
        self.next_send = now + self.interval
        # Convert to signed mono 16-bit at <=48 kHz for the monitor only.
        # One channel avoids phase cancellation in stereo recordings.
        stride = max(1, (fmt.sample_rate + 47999) // 48000)
        count = min(65535, window_frames // stride)
        output = bytearray(count * 2)
        try:
            start = len(self._buffer) - max_bytes
            for i in range(count):
                offset = start + i * stride * frame_bytes
                value = int.from_bytes(
                    self._buffer[offset:offset + width], "little", signed=True
                )
                sample = value >> max(0, fmt.bits_per_sample - 16)
                struct.pack_into("<h", output, i * 2, sample)
            packet = HEADER.pack(MAGIC, VERSION, 1, 16,
                                 fmt.sample_rate // stride, count) + output
            sock.sendto(packet, self.path)
        except (OSError, ValueError, struct.error):
            # A missing or slow receiver must never interrupt playback.
            pass

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None
        self._buffer.clear()
