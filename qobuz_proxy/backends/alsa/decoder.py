"""Incremental FLAC decoding through the reference ``flac`` executable."""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import aiohttp

from .pcm import PcmFormat

PCMConsumer = Callable[[bytes], Awaitable[None]]
FormatConsumer = Callable[[PcmFormat], Awaitable[None]]


class DecoderError(RuntimeError):
    """The HTTP source or FLAC decoder failed."""


@dataclass(frozen=True)
class DecoderResult:
    compressed_bytes: int
    pcm_bytes: int


def parse_streaminfo(prefix: bytes) -> PcmFormat:
    """Parse the mandatory first native-FLAC STREAMINFO metadata block."""
    if len(prefix) < 42 or prefix[:4] != b"fLaC":
        raise DecoderError("Input is not a native FLAC stream")
    block_type = prefix[4] & 0x7F
    length = int.from_bytes(prefix[5:8], "big")
    if block_type != 0 or length != 34:
        raise DecoderError("FLAC STREAMINFO is missing or malformed")
    packed = int.from_bytes(prefix[18:26], "big")
    sample_rate = (packed >> 44) & 0xFFFFF
    channels = ((packed >> 41) & 0x7) + 1
    bits_per_sample = ((packed >> 36) & 0x1F) + 1
    total_samples = packed & ((1 << 36) - 1)
    if not sample_rate or not channels or not bits_per_sample:
        raise DecoderError("FLAC STREAMINFO contains an invalid audio format")
    return PcmFormat(sample_rate, channels, bits_per_sample, total_samples or None)


class FlacProcessDecoder:
    """Streams a URL through ``flac`` and emits bounded raw PCM chunks."""

    def __init__(self, executable: str = "flac", chunk_size: int = 64 * 1024) -> None:
        self.executable = executable
        self.chunk_size = chunk_size
        self._process: Optional[asyncio.subprocess.Process] = None
        self._response: Optional[aiohttp.ClientResponse] = None

    def available(self) -> bool:
        return shutil.which(self.executable) is not None

    async def decode(
        self,
        url: str,
        *,
        skip_ms: int,
        on_format: FormatConsumer,
        on_pcm: PCMConsumer,
    ) -> DecoderResult:
        timeout = aiohttp.ClientTimeout(total=None, connect=20, sock_read=30)
        compressed_bytes = 0
        pcm_bytes = 0
        stderr_tail = b""
        process: Optional[asyncio.subprocess.Process] = None
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    self._response = response
                    if response.status in (401, 403):
                        raise DecoderError(
                            f"Signed streaming URL rejected with HTTP {response.status}"
                        )
                    response.raise_for_status()
                    prefix = await response.content.readexactly(42)
                    compressed_bytes += len(prefix)
                    audio_format = parse_streaminfo(prefix)
                    await on_format(audio_format)

                    command = [
                        self.executable,
                        "--decode",
                        "--stdout",
                        "--force-raw-format",
                        "--endian=little",
                        "--sign=signed",
                        "--silent",
                    ]
                    skip_samples = max(0, skip_ms) * audio_format.sample_rate // 1000
                    if skip_samples:
                        command.append(f"--skip={skip_samples}")
                    command.append("-")
                    self._process = await asyncio.create_subprocess_exec(
                        *command,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    process = self._process
                    stdin = process.stdin
                    stdout = process.stdout
                    stderr = process.stderr
                    assert stdin is not None and stdout is not None and stderr is not None

                    async def feed() -> None:
                        nonlocal compressed_bytes
                        stdin.write(prefix)
                        await stdin.drain()
                        async for chunk in response.content.iter_chunked(self.chunk_size):
                            compressed_bytes += len(chunk)
                            stdin.write(chunk)
                            await stdin.drain()
                        stdin.close()
                        await stdin.wait_closed()

                    async def consume() -> None:
                        nonlocal pcm_bytes
                        frame_bytes = audio_format.channels * (
                            (audio_format.bits_per_sample + 7) // 8
                        )
                        remainder = b""
                        while chunk := await stdout.read(self.chunk_size):
                            combined = remainder + chunk
                            aligned = len(combined) - (len(combined) % frame_bytes)
                            if aligned:
                                pcm = combined[:aligned]
                                pcm_bytes += len(pcm)
                                await on_pcm(pcm)
                            remainder = combined[aligned:]
                        if remainder:
                            raise DecoderError("flac produced an incomplete PCM frame")

                    async def read_stderr() -> None:
                        nonlocal stderr_tail
                        while chunk := await stderr.read(4096):
                            stderr_tail = (stderr_tail + chunk)[-16_384:]

                    await asyncio.gather(feed(), consume(), read_stderr())
                    rc = await process.wait()
                    if rc:
                        detail = stderr_tail.decode(errors="replace").strip()
                        raise DecoderError(f"flac exited with status {rc}: {detail}")
            return DecoderResult(compressed_bytes, pcm_bytes)
        except asyncio.IncompleteReadError as exc:
            raise DecoderError("Truncated FLAC stream before STREAMINFO") from exc
        finally:
            if process is not None and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            self._response = None
            self._process = None

    async def cancel(self) -> None:
        if self._response is not None:
            self._response.close()
        process = self._process
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
