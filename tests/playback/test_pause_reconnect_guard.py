"""Regression tests for PAUSED-at-zero and reconnect snapshots."""
import time

import pytest

from qobuz_proxy.backends import PlaybackState
from tests.playback.test_player_serialization import _make_player


@pytest.mark.asyncio
async def test_normal_pause_at_zero_is_not_discarded_or_rewound():
    player, backend = _make_player()
    await player.play_track(queue_item_id=1, track_id="42")
    player._set_position(14_544)
    position_before = player.current_position_ms

    await player.apply_remote_state(
        track_id="42", queue_item_id=1, position_ms=0, playing_state=3
    )

    assert player.state == PlaybackState.PAUSED
    assert player.current_position_ms >= position_before - 100


@pytest.mark.asyncio
async def test_reconnect_stale_pause_is_suppressed_once():
    player, backend = _make_player()
    await player.play_track(queue_item_id=1, track_id="42")
    player._set_position(14_544)
    player.note_websocket_reconnected()

    await player.apply_remote_state(
        track_id="42", queue_item_id=1, position_ms=0, playing_state=3
    )
    assert player.state == PlaybackState.PLAYING

    # The very next explicit pause must not be swallowed by the guard.
    await player.apply_remote_state(
        track_id="42", queue_item_id=1, position_ms=0, playing_state=3
    )
    assert player.state == PlaybackState.PAUSED


@pytest.mark.asyncio
async def test_expired_reconnect_guard_allows_pause():
    player, backend = _make_player()
    await player.play_track(queue_item_id=1, track_id="42")
    player._set_position(14_544)
    player.note_websocket_reconnected()
    player._reconnect_snapshot_deadline = time.monotonic() - 1

    await player.apply_remote_state(
        track_id="42", queue_item_id=1, position_ms=0, playing_state=3
    )
    assert player.state == PlaybackState.PAUSED
