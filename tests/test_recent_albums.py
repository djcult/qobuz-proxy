"""Album metadata, catalogue paging, and cloud launch acknowledgement."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from qobuz_proxy.auth.api_client import QobuzAPIClient
from qobuz_proxy.connect.protocol import ProtocolCodec
from qobuz_proxy.connect.ws_manager import WsManager
from qobuz_proxy.config import Config
from qobuz_proxy.proto import payload
from qobuz_proxy.webui.speaker_routes import register_speaker_routes


@pytest.mark.asyncio
async def test_album_paging_preserves_order_and_filters_unstreamable():
    api = QobuzAPIClient("app", "secret")
    api._request_signed = AsyncMock(
        side_effect=[
            {"tracks": {"items": [{"id": 1}, {"id": 2, "streamable": False}], "total": 3}},
            {"tracks": {"items": [{"id": 3}], "total": 3}},
        ]
    )
    assert await api.get_album_track_ids("abc") == [1, 3]
    assert api._request_signed.call_args.kwargs["params"]["offset"] == 2


def test_album_load_wire_contains_packed_track_ids():
    codec = ProtocolCodec(bytes(16))
    frame = codec.encode_album_queue([123, 456], 8, 2, b"a" * 16, b"b" * 16)
    decoded = codec.decode_frame(frame)
    batch = codec.decode_qconnect_batch(decoded.payload)
    msg = batch.messages[0]
    assert msg.messageType == 66
    assert list(msg.ctrlSrvrQueueLoadTracks.trackIds) == [123, 456]
    assert msg.ctrlSrvrQueueLoadTracks.queueVersion.major == 8
    assert msg.ctrlSrvrQueueLoadTracks.autoplayReset
    # Field 3, packed fixed32: tag 0x1a, length 8, little-endian IDs.
    assert b"\x1a\x08\x7b\x00\x00\x00\xc8\x01\x00\x00" in msg.SerializeToString()


@pytest.mark.asyncio
async def test_cloud_launch_requires_ack_before_play():
    manager = WsManager(Config())
    manager._is_connected = True
    manager._renderer_active = True
    manager._active_confirmed = True
    manager._ws = Mock()
    manager._ws.send = AsyncMock()
    task = asyncio.create_task(manager.load_album([123, 456], 1, 0))
    await asyncio.sleep(0)
    assert manager._ws.send.await_count == 1
    action, future = manager._album_pending
    reply = payload.QConnectMessage(messageType=91).srvrCtrlQueueTracksLoaded
    reply.actionUuid = action
    reply.queueVersion.major = 2
    reply.tracks.add(queueItemId=77, trackId=123)
    future.set_result(reply)
    assert await task
    assert manager._ws.send.await_count == 2
    frame = manager._ws.send.call_args.args[0]
    decoded = manager._codec.decode_frame(frame)
    msg = manager._codec.decode_qconnect_batch(decoded.payload).messages[0]
    assert msg.ctrlSrvrSetPlayerState.currentQueueItem.id == 77
    assert msg.ctrlSrvrSetPlayerState.currentQueueItem.queueVersion.major == 2
    assert manager._album_pending is None


@pytest.mark.asyncio
async def test_cloud_launch_does_not_play_after_ownership_changes():
    manager = WsManager(Config())
    manager._is_connected = manager._renderer_active = manager._active_confirmed = True
    manager._ws = Mock(send=AsyncMock())
    task = asyncio.create_task(manager.load_album([123], 1, 0))
    await asyncio.sleep(0)
    _, future = manager._album_pending
    manager._ownership_generation += 1
    reply = payload.QConnectMessage(messageType=91).srvrCtrlQueueTracksLoaded
    reply.tracks.add(queueItemId=77, trackId=123)
    future.set_result(reply)
    assert not await task
    assert manager._ws.send.await_count == 1


@pytest.mark.asyncio
async def test_album_api_rejects_offline_and_reports_server_failure():
    app = web.Application()
    app["get_speakers"] = lambda: [{"id": "cdq", "status": "disconnected"}]
    app["on_play_album"] = AsyncMock(side_effect=RuntimeError("No acknowledgement"))
    register_speaker_routes(app)
    async with TestClient(TestServer(app)) as client:
        assert (await client.post("/api/speakers/cdq/albums/abc/play")).status == 409
        app["on_play_album"].assert_not_awaited()
        app["get_speakers"] = lambda: [{"id": "cdq", "status": "playing"}]
        assert (await client.post("/api/speakers/cdq/albums/abc/play")).status == 502
        assert (await client.post("/api/speakers/missing/albums/abc/play")).status == 404


@pytest.mark.asyncio
async def test_timeout_never_starts_local_or_cloud_playback(monkeypatch):
    manager = WsManager(Config())
    manager._is_connected = manager._renderer_active = manager._active_confirmed = True
    manager._ws = Mock(send=AsyncMock())
    wait_for = asyncio.wait_for

    async def short_timeout(future, _timeout):
        return await wait_for(future, 0.01)

    monkeypatch.setattr(asyncio, "wait_for", short_timeout)
    with pytest.raises(RuntimeError, match="acknowledgement"):
        await manager.load_album([123], 1, 0)
    assert manager._ws.send.await_count == 1
    assert manager._album_pending is None


@pytest.mark.asyncio
async def test_album_id_survives_metadata_pipeline():
    from qobuz_proxy.playback.metadata import MetadataService

    api = QobuzAPIClient("app", "secret")
    api._request_signed = AsyncMock(
        return_value={
            "title": "Track",
            "album": {"id": "abc123", "title": "Album"},
        }
    )
    service = MetadataService(api)
    metadata = await service.get_metadata("123")
    assert metadata.to_dict()["album_id"] == "abc123"
