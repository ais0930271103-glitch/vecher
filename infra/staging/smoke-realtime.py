#!/usr/bin/env python3
"""Check actual WSS + internal publish, without creating users or touching data.

Run inside the API image (the config and uvicorn[standard] dependencies are there):
docker compose exec -T api python - < smoke-realtime.py
"""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import uuid

import httpx
import jwt
from websockets.asyncio.client import connect

from app.config import get_settings


async def receive(socket, command_id=None, event_id=None):
    async with asyncio.timeout(10):
        while True:
            raw = await socket.recv()
            for line in raw.splitlines():
                message = json.loads(line)
                if message == {}:
                    await socket.send("{}")
                elif command_id is not None and message.get("id") == command_id:
                    return message
                elif event_id is not None:
                    publication = message.get("push", {}).get("pub", {}).get("data", {})
                    if publication.get("event_id") == event_id:
                        return message


async def main():
    settings = get_settings()
    if not settings.realtime_enabled:
        raise RuntimeError("REALTIME_DISABLED")
    user_id = str(uuid.uuid4())
    channel = f"events:{user_id}"
    now = datetime.now(timezone.utc)
    token = jwt.encode({
        "sub": user_id, "iat": now, "exp": now + timedelta(seconds=60),
        "channels": [channel],
    }, settings.realtime_token_secret, algorithm="HS256")
    async with connect(settings.realtime_ws_url, open_timeout=10, close_timeout=3, max_size=65536) as socket:
        await socket.send(json.dumps({"id": 1, "connect": {"token": token}}))
        response = await receive(socket, command_id=1)
        if set(response.get("connect", {}).get("subs", {})) != {channel}:
            raise RuntimeError("PERSONAL_SUBSCRIPTION_FAILED")
        await socket.send(json.dumps({"id": 2, "subscribe": {"channel": f"events:{uuid.uuid4()}"}}))
        if (await receive(socket, command_id=2)).get("error", {}).get("code") != 103:
            raise RuntimeError("CHANNEL_ISOLATION_FAILED")
        await socket.send(json.dumps({"id": 3, "publish": {"channel": channel, "data": {"test": True}}}))
        if (await receive(socket, command_id=3)).get("error", {}).get("code") != 103:
            raise RuntimeError("CLIENT_PUBLISH_PERMISSION_FAILED")
        event = {"version": 1, "type": "invalidate", "event_id": str(uuid.uuid4()), "scopes": ["all"]}
        async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as client:
            response = await client.post(
                settings.centrifugo_api_url.rstrip("/") + "/publish",
                headers={"X-API-Key": settings.centrifugo_api_key},
                json={"channel": channel, "data": event},
            )
            response.raise_for_status()
            if response.json().get("error"):
                raise RuntimeError("PUBLISH_FAILED")
        received = await receive(socket, event_id=event["event_id"])
        if received.get("push", {}).get("channel") != channel or received["push"]["pub"]["data"] != event:
            raise RuntimeError("PUBLICATION_MISMATCH")
    print("REALTIME_WSS_OK")
    print("PERSONAL_CHANNEL_ISOLATION_OK")
    print("REALTIME_PUBLISH_OK")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        # Never print the JWT, API key, request headers or settings.
        reason = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        raise SystemExit(f"REALTIME_SMOKE_FAILED: {reason}") from None
