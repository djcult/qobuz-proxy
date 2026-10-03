"""Tests for the narrow incremental FLAC decoder helpers."""

import pytest

from qobuz_proxy.backends.alsa.decoder import DecoderError, parse_streaminfo


def _streaminfo(rate: int, channels: int, bits: int, total: int) -> bytes:
    packed = (rate << 44) | ((channels - 1) << 41) | ((bits - 1) << 36) | total
    body = b"\x00" * 10 + packed.to_bytes(8, "big") + b"\x00" * 16
    return b"fLaC" + bytes((0x80, 0, 0, 34)) + body


@pytest.mark.parametrize("rate", [44100, 48000, 88200, 96000, 176400, 192000])
def test_parse_streaminfo_preserves_source_format(rate: int) -> None:
    parsed = parse_streaminfo(_streaminfo(rate, 2, 24, rate * 60))
    assert parsed.sample_rate == rate
    assert parsed.channels == 2
    assert parsed.bits_per_sample == 24
    assert parsed.total_samples == rate * 60


def test_parse_streaminfo_rejects_non_flac() -> None:
    with pytest.raises(DecoderError, match="not a native FLAC"):
        parse_streaminfo(b"not flac".ljust(42, b"\0"))
