"""Hardware-free lifecycle tests for AlsaAudioBackend."""

import asyncio
import threading

from qobuz_proxy.backends.alsa.backend import AlsaAudioBackend
from qobuz_proxy.backends.alsa.pcm import PcmFormat
from qobuz_proxy.backends.types import BackendTrackMetadata, PlaybackState


class FakePcm:
    def __init__(self) -> None:
        self.format = None
        self.writes: list[bytes] = []
        self.delay = 0
        self.dropped = False
        self.drained = False
        self.closed = False

    def open(self, audio_format: PcmFormat) -> None:
        self.format = audio_format

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        assert self.format is not None
        return len(data) // (self.format.channels * (self.format.bits_per_sample // 8))

    def delay_frames(self) -> int:
        return self.delay

    def drain(self) -> None:
        self.drained = True

    def drop(self) -> None:
        self.dropped = True

    def close(self) -> None:
        self.closed = True


class FakeDecoder:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.skip_ms = -1
        self.cancelled = False

    def available(self) -> bool:
        return True

    async def decode(self, url, *, skip_ms, on_format, on_pcm):  # type: ignore[no-untyped-def]
        self.skip_ms = skip_ms
        await on_format(PcmFormat(48000, 2, 24, 480000))
        await on_pcm(b"\x01\x02\x03\x04\x05\x06" * 480)
        self.started.set()
        await self.release.wait()

    async def cancel(self) -> None:
        self.cancelled = True
        self.release.set()


def _metadata() -> BackendTrackMetadata:
    return BackendTrackMetadata(track_id="42", duration_ms=10_000, sample_rate=48000, bit_depth=24)


async def test_play_from_is_atomic_and_uses_integer_pcm() -> None:
    pcm = FakePcm()
    decoder = FakeDecoder()
    backend = AlsaAudioBackend(
        device="hw:Test,0", pcm_factory=lambda: pcm, decoder_factory=lambda: decoder
    )
    assert await backend.connect()

    await backend.play_from("https://cdn/track.flac", _metadata(), 2500)
    await decoder.started.wait()

    assert decoder.skip_ms == 2500
    assert pcm.format == PcmFormat(48000, 2, 24, 480000)
    assert pcm.writes == [b"\x01\x02\x03\x04\x05\x06" * 480]
    assert await backend.get_state() == PlaybackState.PLAYING
    assert await backend.get_position() == 2510
    await backend.stop()
    assert pcm.dropped and pcm.closed and decoder.cancelled


async def test_play_from_waits_until_first_retained_pcm_is_written() -> None:
    class DelayedPcmDecoder(FakeDecoder):
        def __init__(self) -> None:
            super().__init__()
            self.format_sent = asyncio.Event()
            self.send_pcm = asyncio.Event()

        async def decode(self, url, *, skip_ms, on_format, on_pcm):  # type: ignore[no-untyped-def]
            self.skip_ms = skip_ms
            await on_format(PcmFormat(48000, 2, 24, 480000))
            self.format_sent.set()
            await self.send_pcm.wait()
            await on_pcm(b"\x01\x02\x03\x04\x05\x06" * 480)
            self.started.set()
            await self.release.wait()

    pcm = FakePcm()
    decoder = DelayedPcmDecoder()
    backend = AlsaAudioBackend(
        device="hw:Test,0", pcm_factory=lambda: pcm, decoder_factory=lambda: decoder
    )
    assert await backend.connect()

    play = asyncio.create_task(backend.play_from("https://cdn/track.flac", _metadata(), 2500))
    await decoder.format_sent.wait()
    assert not play.done()
    assert await backend.get_state() == PlaybackState.LOADING

    decoder.send_pcm.set()
    await play
    assert pcm.writes
    assert await backend.get_state() == PlaybackState.PLAYING
    await backend.stop()


async def test_pause_and_resume_restart_at_audible_position_with_fresh_url() -> None:
    pcms: list[FakePcm] = []
    decoders: list[FakeDecoder] = []

    def pcm_factory() -> FakePcm:
        pcm = FakePcm()
        pcms.append(pcm)
        return pcm

    def decoder_factory() -> FakeDecoder:
        decoder = FakeDecoder()
        decoders.append(decoder)
        return decoder

    backend = AlsaAudioBackend(
        device="hw:Test,0", pcm_factory=pcm_factory, decoder_factory=decoder_factory
    )
    urls: list[tuple[str, bool]] = []

    async def resolve(track_id: str, force: bool) -> str:
        urls.append((track_id, force))
        return "https://cdn/fresh.flac"

    backend.set_streaming_url_resolver(resolve)
    assert await backend.connect()
    await backend.play("https://cdn/old.flac", _metadata())
    await decoders[-1].started.wait()
    pcms[-1].delay = 240

    await backend.pause()
    assert await backend.get_state() == PlaybackState.PAUSED
    assert await backend.get_position() == 5

    assert await backend.resume()
    assert urls == [("42", False)]
    assert decoders[-1].skip_ms == 5
    assert await backend.get_state() == PlaybackState.PLAYING
    await backend.stop()


async def test_seek_restarts_through_shared_nonzero_start_pipeline() -> None:
    decoders: list[FakeDecoder] = []

    def decoder_factory() -> FakeDecoder:
        decoder = FakeDecoder()
        decoders.append(decoder)
        return decoder

    backend = AlsaAudioBackend(
        device="hw:Test,0", pcm_factory=FakePcm, decoder_factory=decoder_factory
    )
    backend.set_streaming_url_resolver(
        lambda _track_id, _force: asyncio.sleep(0, result="https://cdn/fresh.flac")
    )
    assert await backend.connect()
    await backend.play("https://cdn/old.flac", _metadata())

    await backend.seek(3750)

    assert decoders[-1].skip_ms == 3750
    assert await backend.get_state() == PlaybackState.PLAYING
    await backend.stop()


async def test_fixed_volume_never_changes_pcm() -> None:
    backend = AlsaAudioBackend(device="hw:Test,0", pcm_factory=FakePcm, decoder_factory=FakeDecoder)
    await backend.set_volume(0)
    assert await backend.get_volume() == 100
    assert backend.supports_gapless is False


async def test_stop_interrupts_blocked_drain_before_closing_pcm() -> None:
    class BlockingDrainPcm(FakePcm):
        def __init__(self) -> None:
            super().__init__()
            self.in_drain = threading.Event()
            self.release_drain = threading.Event()

        def drain(self) -> None:
            self.in_drain.set()
            self.release_drain.wait(timeout=2)
            super().drain()

        def drop(self) -> None:
            self.release_drain.set()
            super().drop()

    class FinishingDecoder(FakeDecoder):
        async def decode(self, url, *, skip_ms, on_format, on_pcm):  # type: ignore[no-untyped-def]
            await on_format(PcmFormat(48000, 2, 24, 480))
            await on_pcm(b"\0" * 6)

    pcm = BlockingDrainPcm()
    backend = AlsaAudioBackend(
        device="hw:Test,0", pcm_factory=lambda: pcm, decoder_factory=FinishingDecoder
    )
    assert await backend.connect()
    await backend.play("url", _metadata())
    await asyncio.to_thread(pcm.in_drain.wait, 1)

    await asyncio.wait_for(backend.stop(), timeout=1)

    assert pcm.dropped and pcm.drained and pcm.closed
    assert await backend.get_state() == PlaybackState.STOPPED
