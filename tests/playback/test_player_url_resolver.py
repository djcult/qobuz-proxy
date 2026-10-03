"""Tests for backend URL refresh wiring."""

from unittest.mock import AsyncMock, MagicMock

from qobuz_proxy.backends.base import AudioBackend
from qobuz_proxy.backends.types import BackendTrackMetadata, PlaybackState
from qobuz_proxy.playback.player import QobuzPlayer
from qobuz_proxy.playback.queue import QueueTrack


class ResolverBackend(AudioBackend):
    async def play(self, url: str, metadata: BackendTrackMetadata) -> None: ...
    async def pause(self) -> None: ...
    async def resume(self) -> bool:
        return True

    async def stop(self, *, next_track_id=None): ...  # type: ignore[no-untyped-def]
    async def seek(self, position_ms: int) -> None: ...
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


async def test_player_resolver_refreshes_metadata_and_queue_url() -> None:
    queue = MagicMock()
    queue.set_url_callback = MagicMock()
    queue.set_metadata_callback = MagicMock()
    metadata = MagicMock()
    metadata.get_streaming_url = AsyncMock(return_value="https://cdn/current")
    metadata.refresh_streaming_url = AsyncMock(return_value="https://cdn/forced")
    backend = ResolverBackend()
    player = QobuzPlayer(queue, metadata, backend)
    track = QueueTrack(queue_item_id=7, track_id="42")
    player._current_track = track

    assert await backend._resolve_streaming_url("42") == "https://cdn/current"
    assert track.streaming_url == "https://cdn/current"
    assert await backend._resolve_streaming_url("42", force=True) == "https://cdn/forced"
    assert track.streaming_url == "https://cdn/forced"
    metadata.refresh_streaming_url.assert_awaited_once_with("42")


async def test_player_resolver_rejects_inactive_track() -> None:
    queue = MagicMock()
    metadata = MagicMock()
    backend = ResolverBackend()
    QobuzPlayer(queue, metadata, backend)
    assert await backend._streaming_url_resolver("old", False) is None  # type: ignore[misc]
