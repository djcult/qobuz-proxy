"""Tests for the narrow incremental FLAC decoder helpers."""

import asyncio
import shutil
import struct
import subprocess
import wave
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from aiohttp import web

from qobuz_proxy.backends.alsa.decoder import DecoderError, FlacProcessDecoder, parse_streaminfo


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


@asynccontextmanager
async def _served(data: bytes):  # type: ignore[no-untyped-def]
    async def stream(_request: web.Request) -> web.Response:
        return web.Response(body=data, content_type="audio/flac")

    app = web.Application()
    app.router.add_get("/track.flac", stream)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    server = site._server
    assert server is not None
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/track.flac"
    finally:
        await runner.cleanup()


async def test_decoder_discards_pcm_instead_of_seeking_its_stdin(tmp_path: Path) -> None:
    executable = tmp_path / "fake-flac"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "assert not any(arg.startswith('--skip=') for arg in sys.argv)\n"
        "sys.stdin.buffer.read()\n"
        "sys.stdout.buffer.write(b''.join(i.to_bytes(2, 'little', signed=True) for i in range(10)))\n"
    )
    executable.chmod(0o755)
    source = _streaminfo(1000, 1, 16, 10) + b"compressed payload"
    chunks: list[bytes] = []

    async with _served(source) as url:
        result = await FlacProcessDecoder(str(executable), chunk_size=7).decode(
            url,
            skip_ms=3,
            on_format=lambda _format: asyncio.sleep(0),
            on_pcm=lambda data: asyncio.sleep(0, result=chunks.append(data)),
        )

    expected = b"".join(i.to_bytes(2, "little", signed=True) for i in range(3, 10))
    assert b"".join(chunks) == expected
    assert result.pcm_bytes == len(expected)


@pytest.mark.skipif(shutil.which("flac") is None, reason="reference flac executable unavailable")
async def test_reference_flac_nonzero_start_is_decoded_sequentially(tmp_path: Path) -> None:
    samples = tuple(range(-10, 10))
    wav_path = tmp_path / "source.wav"
    flac_path = tmp_path / "source.flac"
    with wave.open(str(wav_path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(1000)
        output.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    subprocess.run(
        ["flac", "--silent", "--force", "--output-name", str(flac_path), str(wav_path)],
        check=True,
    )
    chunks: list[bytes] = []

    async with _served(flac_path.read_bytes()) as url:
        await FlacProcessDecoder(chunk_size=7).decode(
            url,
            skip_ms=5,
            on_format=lambda _format: asyncio.sleep(0),
            on_pcm=lambda data: asyncio.sleep(0, result=chunks.append(data)),
        )

    assert b"".join(chunks) == struct.pack(f"<{len(samples) - 5}h", *samples[5:])
