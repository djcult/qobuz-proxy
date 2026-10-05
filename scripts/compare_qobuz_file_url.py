#!/usr/bin/env python3
"""Compare Qobuz legacy track/getFileUrl with modern file/url metadata.

Diagnostic only: no audio is downloaded or decrypted and media URLs/keys/blobs
are never printed.

Usage:
    python scripts/compare_qobuz_file_url.py 246992807
    python scripts/compare_qobuz_file_url.py 87729785 246992807 197057813
"""

import argparse
import asyncio
import hashlib
import json
import time
from typing import Any
from urllib.parse import urlencode

import aiohttp

from qobuz_proxy.auth.api_client import QobuzAPIClient
from qobuz_proxy.auth.credentials import load_user_token
from qobuz_proxy.auth.oauth import OAUTH_APP_ID, OAUTH_APP_SECRET

API_BASE = "https://www.qobuz.com/api.json/0.2"


def _signature(endpoint: str, params: dict[str, str], request_ts: str) -> str:
    """Build the Qobuz signed-request digest for an object/action endpoint."""
    object_name, action = endpoint.split("/", 1)
    raw = object_name + action
    for key in sorted(params):
        raw += key + params[key]
    raw += request_ts + OAUTH_APP_SECRET
    return hashlib.md5(raw.encode()).hexdigest()


def _summary(data: dict[str, Any]) -> dict[str, Any]:
    """Return only safe rendition metadata; deliberately omit URL/key/blob."""
    fields = (
        "format_id",
        "bit_depth",
        "sampling_rate",
        "mime_type",
        "file_type",
        "n_segments",
        "duration",
        "n_samples",
        "sample",
        "restrictions",
    )
    return {key: data.get(key) for key in fields if key in data}


async def _modern_file_url(
    session: aiohttp.ClientSession,
    track_id: str,
    session_id: str,
    auth_token: str,
    format_id: int,
) -> tuple[int, dict[str, Any]]:
    params = {
        "format_id": str(format_id),
        "intent": "stream",
        "track_id": str(track_id),
    }
    request_ts = f"{time.time():.6f}"
    query = {
        **params,
        "request_ts": request_ts,
        "request_sig": _signature("file/url", params, request_ts),
    }
    headers = {
        "X-App-Id": OAUTH_APP_ID,
        "X-User-Auth-Token": auth_token,
        "X-Session-Id": session_id,
    }
    async with session.get(
        f"{API_BASE}/file/url?{urlencode(query)}", headers=headers
    ) as response:
        text = await response.text()
        try:
            body = json.loads(text)
        except json.JSONDecodeError:
            body = {"error": text[:500]}
        return response.status, body


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare Qobuz legacy and modern stream rendition metadata."
    )
    parser.add_argument("track_ids", nargs="+", help="Qobuz track ID(s)")
    parser.add_argument(
        "--format-id",
        type=int,
        default=27,
        choices=(5, 6, 7, 27),
        help="Requested Qobuz format tier (default: 27)",
    )
    args = parser.parse_args()

    creds = load_user_token()
    if not creds:
        raise SystemExit(
            "No cached Qobuz credentials found. Authenticate qobuz-proxy first."
        )

    client = QobuzAPIClient(OAUTH_APP_ID, OAUTH_APP_SECRET)
    if not await client.login_with_token(
        creds["user_id"], creds["user_auth_token"]
    ):
        raise SystemExit("Cached Qobuz credentials were rejected.")

    if not await client.start_session() or not client.x_session_id:
        raise SystemExit("Could not start Qobuz qbz-1 playback session.")

    auth_token = client.user_auth_token or creds["user_auth_token"]

    async with aiohttp.ClientSession() as session:
        for track_id in args.track_ids:
            print(f"\nTrack {track_id} — requested format {args.format_id}")

            legacy = await client.get_track_url(track_id, args.format_id)
            if legacy:
                print(
                    "  track/getFileUrl:",
                    json.dumps(_summary(legacy), sort_keys=True),
                )
            else:
                print("  track/getFileUrl: FAILED")

            status, modern = await _modern_file_url(
                session,
                track_id,
                client.x_session_id,
                auth_token,
                args.format_id,
            )
            if status == 200:
                print(
                    "  file/url:         ",
                    json.dumps(_summary(modern), sort_keys=True),
                )
            else:
                safe_error = modern.get("message") or modern.get("error") or modern.get("code")
                print(f"  file/url:          HTTP {status}: {safe_error!r}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
