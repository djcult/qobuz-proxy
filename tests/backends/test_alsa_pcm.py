"""Tests for lossless PCM container handling without loading libasound."""

import pytest

from qobuz_proxy.backends.alsa.pcm import AlsaPcm, PcmFormat


@pytest.mark.parametrize(
    ("bits_per_sample", "expected_name", "expected_container_bytes"),
    [(16, b"S16_LE", 2), (24, b"S32_LE", 4)],
)
def test_source_bit_depth_selects_exact_device_format(
    bits_per_sample: int, expected_name: bytes, expected_container_bytes: int
) -> None:
    class FakeAlsa:
        def __init__(self) -> None:
            self.selected_names: list[bytes] = []

        def snd_pcm_open(self, handle, device, stream, mode):  # type: ignore[no-untyped-def]
            return 0

        def snd_pcm_format_value(self, name: bytes) -> int:
            self.selected_names.append(name)
            return 1

        def snd_pcm_set_params(self, *args) -> int:  # type: ignore[no-untyped-def]
            return 0

    pcm = AlsaPcm("hw:Test,0")
    fake_alsa = FakeAlsa()
    pcm._load = lambda: fake_alsa  # type: ignore[method-assign]
    pcm._parameters_are_exact = lambda *args: True  # type: ignore[method-assign]

    pcm.open(PcmFormat(192000, 2, bits_per_sample))

    assert fake_alsa.selected_names == [expected_name]
    assert pcm._container_bytes == expected_container_bytes


def test_24_bit_samples_are_left_aligned_without_losing_significant_bits() -> None:
    pcm = AlsaPcm("hw:Test,0")
    pcm._format = PcmFormat(192000, 2, 24)  # Exercise the isolated packing primitive.
    pcm._container_bytes = 4
    source = bytes.fromhex("000000 ffff7f 000080 ffffff")
    assert pcm._packed_for_device(source) == bytes.fromhex("00000000 00ffff7f 00000080 00ffffff")
