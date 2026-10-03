"""Direct, integer-PCM ALSA audio backend."""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from qobuz_proxy.backends.base import AudioBackend
from qobuz_proxy.backends.types import BackendInfo, BackendTrackMetadata, PlaybackState

from .decoder import DecoderError, FlacProcessDecoder
from .pcm import AlsaPcm, PcmDevice, PcmFormat

logger = logging.getLogger(__name__)


class Decoder(Protocol):
    def available(self) -> bool: ...
    async def decode(self, url: str, *, skip_ms: int, on_format, on_pcm): ...
    async def cancel(self) -> None: ...


@dataclass(frozen=True)
class AlsaTelemetry:
    frames_submitted: int
    delay_frames: int | None
    position_ms: int
    sample_rate: int
    xruns: int = 0


class AlsaAudioBackend(AudioBackend):
    """Streams FLAC as bit-perfect integer PCM to an explicit ALSA ``hw:`` device."""

    def __init__(
        self,
        device: str = "hw:0,0",
        latency_us: int = 500_000,
        name: str = "ALSA Audio",
        *,
        pcm_factory: Optional[Callable[[], PcmDevice]] = None,
        decoder_factory: Optional[Callable[[], Decoder]] = None,
    ) -> None:
        super().__init__(name)
        if not device.startswith("hw:") or device.startswith("plughw:"):
            raise ValueError("ALSA backend requires an explicit hw: device")
        self._device = device
        self._latency_us = latency_us
        self._pcm_factory = pcm_factory or (lambda: AlsaPcm(device, latency_us))
        self._decoder_factory = decoder_factory or FlacProcessDecoder
        self._pcm: PcmDevice | None = None
        self._decoder: Decoder | None = None
        self._task: asyncio.Task[None] | None = None
        self._generation = 0
        self._metadata: BackendTrackMetadata | None = None
        self._format: PcmFormat | None = None
        self._start_frame = 0
        self._frames_submitted = 0
        self._last_delay: int | None = None
        self._paused_position_ms = 0

    @property
    def supports_gapless(self) -> bool:
        return False

    async def connect(self) -> bool:
        if sys.platform != "linux":
            logger.error("The ALSA backend is only available on Linux")
            return False
        decoder = self._decoder_factory()
        if not decoder.available():
            logger.error("The ALSA backend requires the 'flac' executable in PATH")
            return False
        try:
            # Constructing the PCM validates the device spelling without opening it.
            self._pcm_factory()
        except (OSError, ValueError) as exc:
            logger.error(f"Invalid ALSA configuration: {exc}")
            return False
        self._is_connected = True
        self.name = f"ALSA: {self._device}"
        return True

    async def disconnect(self) -> None:
        await self.stop()
        self._is_connected = False

    async def play(self, url: str, metadata: BackendTrackMetadata) -> None:
        await self.play_from(url, metadata, 0)

    async def play_from(
        self, url: str, metadata: BackendTrackMetadata, position_ms: int = 0
    ) -> None:
        await self._cancel_pipeline()
        self._metadata = metadata
        self._paused_position_ms = max(0, position_ms)
        self._notify_state_change(PlaybackState.LOADING)
        await self._start_pipeline(url, self._paused_position_ms)

    async def _start_pipeline(self, url: str, position_ms: int) -> None:
        self._generation += 1
        generation = self._generation
        self._format = None
        self._frames_submitted = 0
        self._last_delay = None
        self._decoder = self._decoder_factory()
        self._pcm = self._pcm_factory()
        pcm = self._pcm
        started: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        async def on_format(audio_format: PcmFormat) -> None:
            if audio_format.sample_rate not in (44100, 48000, 88200, 96000, 176400, 192000):
                raise RuntimeError(f"Unsupported source sample rate: {audio_format.sample_rate} Hz")
            self._format = audio_format
            self._start_frame = position_ms * audio_format.sample_rate // 1000
            await asyncio.to_thread(pcm.open, audio_format)

        async def on_pcm(data: bytes) -> None:
            if generation != self._generation or self._pcm is None:
                raise asyncio.CancelledError
            frames = await asyncio.to_thread(self._pcm.write, data)
            self._frames_submitted += frames
            delay = await asyncio.to_thread(self._pcm.delay_frames)
            if delay is not None:
                self._last_delay = delay
            self._notify_position_update(self._position_ms())
            if not started.done():
                started.set_result(None)

        async def run() -> None:
            try:
                assert self._decoder is not None
                try:
                    await self._decoder.decode(
                        url,
                        skip_ms=max(0, position_ms),
                        on_format=on_format,
                        on_pcm=on_pcm,
                    )
                except DecoderError as exc:
                    if (
                        "Signed streaming URL rejected" not in str(exc)
                        or generation != self._generation
                    ):
                        raise
                    assert self._metadata is not None
                    fresh_url = await self._resolve_streaming_url(
                        self._metadata.track_id, force=True
                    )
                    self._decoder = self._decoder_factory()
                    await self._decoder.decode(
                        fresh_url,
                        skip_ms=max(0, position_ms),
                        on_format=on_format,
                        on_pcm=on_pcm,
                    )
                if not started.done():
                    raise DecoderError(
                        "FLAC decoder produced no PCM at the requested start position"
                    )
                if generation != self._generation or self._pcm is None:
                    return
                await asyncio.to_thread(self._pcm.drain)
                if generation != self._generation:
                    return
                self._paused_position_ms = self._position_ms()
                self._notify_state_change(PlaybackState.STOPPED)
                self._notify_track_ended()
            except asyncio.CancelledError:
                if not started.done():
                    started.cancel()
                raise
            except Exception as exc:
                if not started.done():
                    started.set_exception(exc)
                elif generation == self._generation:
                    logger.exception("ALSA playback failed (%s): %r", type(exc).__name__, exc)
                    self._notify_state_change(PlaybackState.ERROR)
                    self._notify_playback_error(str(exc))
            finally:
                if generation == self._generation and self._pcm is not None:
                    await asyncio.to_thread(self._pcm.close)

        self._task = asyncio.create_task(run())
        try:
            await started
        except Exception:
            await self._cancel_pipeline()
            raise
        self._notify_state_change(PlaybackState.PLAYING)

    def _position_ms(self) -> int:
        if self._format is None:
            return self._paused_position_ms
        delay = self._last_delay or 0
        audible = self._start_frame + max(0, self._frames_submitted - delay)
        if self._format.total_samples is not None:
            audible = min(audible, self._format.total_samples)
        return audible * 1000 // self._format.sample_rate

    async def _cancel_pipeline(self) -> None:
        self._generation += 1
        decoder, pcm, task = self._decoder, self._pcm, self._task
        self._decoder = None
        self._pcm = None
        self._task = None
        if pcm is not None:
            # drop() is specifically used to interrupt blocked write/drain calls.
            await asyncio.to_thread(pcm.drop)
        if decoder is not None:
            await decoder.cancel()
        if task is not None and not task.done():
            try:
                # drop() interrupts libasound write/drain and decoder.cancel()
                # interrupts pipe I/O. Let their worker calls actually return
                # before closing native handles; cancellation of to_thread()
                # alone would not stop its underlying thread.
                await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            except (DecoderError, OSError):
                pass
        if pcm is not None:
            await asyncio.to_thread(pcm.close)

    async def pause(self) -> None:
        if self._state != PlaybackState.PLAYING:
            return
        if self._pcm is not None:
            delay = await asyncio.to_thread(self._pcm.delay_frames)
            if delay is not None:
                self._last_delay = delay
        self._paused_position_ms = self._position_ms()
        await self._cancel_pipeline()
        self._notify_state_change(PlaybackState.PAUSED)

    async def resume(self) -> bool:
        if self._state != PlaybackState.PAUSED or self._metadata is None:
            return False
        try:
            url = await self._resolve_streaming_url(self._metadata.track_id)
            self._notify_state_change(PlaybackState.LOADING)
            await self._start_pipeline(url, self._paused_position_ms)
            self._notify_state_change(PlaybackState.PLAYING)
            return True
        except Exception as exc:
            self._notify_state_change(PlaybackState.ERROR)
            self._notify_playback_error(str(exc))
            return False

    async def stop(self, *, next_track_id: Optional[str] = None) -> None:
        await self._cancel_pipeline()
        self._metadata = None
        self._format = None
        self._frames_submitted = 0
        self._paused_position_ms = 0
        self._notify_state_change(PlaybackState.STOPPED)

    async def seek(self, position_ms: int) -> None:
        if self._metadata is None:
            return
        target = max(0, position_ms)
        was_paused = self._state == PlaybackState.PAUSED
        await self._cancel_pipeline()
        self._paused_position_ms = target
        if was_paused:
            self._notify_position_update(target)
            return
        url = await self._resolve_streaming_url(self._metadata.track_id)
        self._notify_state_change(PlaybackState.LOADING)
        await self._start_pipeline(url, target)
        self._notify_state_change(PlaybackState.PLAYING)

    async def get_position(self) -> int:
        if self._state == PlaybackState.PLAYING and self._pcm is not None:
            delay = await asyncio.to_thread(self._pcm.delay_frames)
            if delay is not None:
                self._last_delay = delay
        return self._position_ms()

    async def set_volume(self, level: int) -> None:
        # The player runs this backend in existing fixed-volume mode. Never alter PCM.
        return None

    async def get_volume(self) -> int:
        return 100

    async def get_state(self) -> PlaybackState:
        return self._state

    def telemetry(self) -> AlsaTelemetry:
        """Return an observational snapshot; playback never consumes it."""
        return AlsaTelemetry(
            frames_submitted=self._frames_submitted,
            delay_frames=self._last_delay,
            position_ms=self._position_ms(),
            sample_rate=self._format.sample_rate if self._format else 0,
        )

    def get_info(self) -> BackendInfo:
        return BackendInfo(backend_type="alsa", name=self.name, device_id=f"alsa-{self._device}")
