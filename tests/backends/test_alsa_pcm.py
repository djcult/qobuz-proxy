"""Tests for lossless PCM container handling without loading libasound."""

from qobuz_proxy.backends.alsa.pcm import AlsaPcm, PcmFormat


def test_24_bit_samples_are_sign_extended_without_value_change() -> None:
    pcm = AlsaPcm("hw:Test,0")
    pcm._format = PcmFormat(192000, 2, 24)  # Exercise the isolated packing primitive.
    pcm._container_bytes = 4
    source = bytes.fromhex("000000 ffff7f 000080 ffffff")
    assert pcm._packed_for_device(source) == bytes.fromhex("00000000 ffff7f00 000080ff ffffffff")


def test_packed_24_bit_samples_are_unchanged() -> None:
    pcm = AlsaPcm("hw:Test,0")
    pcm._format = PcmFormat(96000, 2, 24)
    pcm._container_bytes = 3
    source = bytes.fromhex("010203 fefdfc")
    assert pcm._packed_for_device(source) is source
