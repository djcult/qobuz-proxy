"""Tests for backend-neutral positioned start and URL refresh contracts."""

from qobuz_proxy.backends.base import AudioBackend
from qobuz_proxy.backends.types import BackendTrackMetadata, PlaybackState


class ContractBackend(AudioBackend):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, int]] = []

    async def play(self, url, metadata):  # type: ignore[no-untyped-def]
        self.calls.append(("play", 0))

    async def pause(self) -> None: ...
    async def resume(self) -> bool:
        return True

    async def stop(self, *, next_track_id=None): ...  # type: ignore[no-untyped-def]
    async def seek(self, position_ms: int) -> None:
        self.calls.append(("seek", position_ms))

    async def get_position(self) -> int:
        return 0

    async def set_volume(self, level: int) -> None: ...
    async def get_volume(self) -> int:
        return 100

    async def get_state(self) -> PlaybackState:
        return self._state

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None: ...


async def test_default_play_from_preserves_existing_backends() -> None:
    backend = ContractBackend()
    await backend.play_from("url", BackendTrackMetadata(track_id="1"), 1234)
    assert backend.calls == [("play", 0), ("seek", 1234)]


async def test_url_resolver_supports_normal_and_forced_refresh() -> None:
    backend = ContractBackend()
    calls: list[tuple[str, bool]] = []

    async def resolver(track_id: str, force: bool) -> str:
        calls.append((track_id, force))
        return "fresh"

    backend.set_streaming_url_resolver(resolver)
    assert await backend._resolve_streaming_url("1") == "fresh"
    assert await backend._resolve_streaming_url("1", force=True) == "fresh"
    assert calls == [("1", False), ("1", True)]
