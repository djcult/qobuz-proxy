"""The monitor produces bounded analysis packets without touching playback PCM."""
import socket
import struct

from qobuz_proxy.backends.alsa.monitor import HEADER, MAGIC, VERSION, PcmMonitor
from qobuz_proxy.backends.alsa.pcm import PcmFormat


def test_disabled_monitor_is_noop(monkeypatch):
    monkeypatch.delenv("QOBUZ_PCM_MONITOR_SOCKET", raising=False)
    monitor = PcmMonitor()
    assert monitor.sock is None
    monitor.publish(b"\x00" * 4096, PcmFormat(48000, 2, 16))


def test_contiguous_monitor_window(tmp_path):
    path = str(tmp_path / "pcm.sock")
    receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    receiver.bind(path)
    receiver.settimeout(1)
    monitor = PcmMonitor(path, interval=0)
    fmt = PcmFormat(48000, 2, 16)
    frames = int(fmt.sample_rate * 0.18)
    source = b"".join(struct.pack("<hh", i % 20000, -(i % 20000))
                      for i in range(frames))
    try:
        midpoint = len(source) // 2
        monitor.publish(source[:midpoint], fmt)
        monitor.publish(source[midpoint:], fmt)
        packet = receiver.recv(65536)
        magic, version, channels, bits, rate, count, presentation = HEADER.unpack_from(packet)
        assert (magic, version, channels, bits, rate, count) == (
            MAGIC, VERSION, 1, 16, 48000, frames
        )
        assert isinstance(presentation, float)\n        expected = b"".join(struct.pack("<h", i % 20000) for i in range(frames))
        assert packet[HEADER.size:] == expected
    finally:
        monitor.close()
        receiver.close()


def test_missing_listener_drops_packet(tmp_path):
    monitor = PcmMonitor(str(tmp_path / "missing.sock"), interval=0)
    try:
        monitor.publish(b"\x00" * 40000, PcmFormat(48000, 2, 16))
    finally:
        monitor.close()
