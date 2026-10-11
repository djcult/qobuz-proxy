"""PCM monitor is optional and must not affect playback."""
import socket

from qobuz_proxy.backends.alsa.monitor import HEADER, MAGIC, VERSION, PcmMonitor
from qobuz_proxy.backends.alsa.pcm import PcmFormat


def test_disabled_monitor_is_noop(monkeypatch):
    monkeypatch.delenv("QOBUZ_PCM_MONITOR_SOCKET", raising=False)
    monitor = PcmMonitor()
    assert monitor.sock is None
    monitor.publish(b"\x00" * 4096, PcmFormat(48000, 2, 16))


def test_monitor_packet(tmp_path):
    path = str(tmp_path / "pcm.sock")
    receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    receiver.bind(path)
    receiver.settimeout(1)
    monitor = PcmMonitor(path, interval=0)
    try:
        source = bytes(range(256)) * 16
        monitor.publish(source, PcmFormat(96000, 2, 24))
        packet = receiver.recv(16384)
        magic, version, channels, bits, rate, frames = HEADER.unpack_from(packet)
        assert (magic, version, channels, bits, rate, frames) == (
            MAGIC, VERSION, 2, 24, 682,  # overwritten below
        )
    finally:
        monitor.close()
        receiver.close()


def test_missing_listener_drops_packet(tmp_path):
    monitor = PcmMonitor(str(tmp_path / "missing.sock"), interval=0)
    try:
        monitor.publish(b"\x00" * 4096, PcmFormat(44100, 2, 16))
    finally:
        monitor.close()
