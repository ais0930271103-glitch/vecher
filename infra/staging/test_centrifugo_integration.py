"""Optional real-server contract tests; CI supplies the pinned Centrifugo binary."""
import importlib.util
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import uuid

import httpx
import jwt
import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosedError

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "backend"))


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prepare = load_module("realtime_prepare_integration", "prepare-realtime.py")
smoke = load_module("realtime_smoke_integration", "smoke-realtime.py")
BINARY = os.environ.get("VECHER_CENTRIFUGO_BIN")
pytestmark = pytest.mark.skipif(not BINARY, reason="Set VECHER_CENTRIFUGO_BIN for real-server tests")


@pytest.fixture
def server(tmp_path):
    from app.config import Settings

    def free_port():
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    external = free_port()
    internal = free_port()
    while internal == external:
        internal = free_port()
    values = prepare.make_settings("", True)
    config = prepare.make_config(values)
    config["http_server"] = {
        "address": "127.0.0.1", "port": external,
        "internal_address": "127.0.0.1", "internal_port": str(internal),
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    subprocess.run([BINARY, "checkconfig", "--strict", f"--config={path}"], check=True)
    process = subprocess.Popen([BINARY, f"--config={path}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    settings = Settings(
        _env_file=None, env="dev", realtime_enabled=True,
        realtime_ws_url=f"ws://127.0.0.1:{external}/connection/websocket",
        centrifugo_api_url=f"http://127.0.0.1:{internal}/api",
        centrifugo_api_key=values["VECHER_CENTRIFUGO_API_KEY"],
        realtime_token_secret=values["VECHER_REALTIME_TOKEN_SECRET"],
    )
    try:
        for _ in range(100):
            if process.poll() is not None:
                pytest.fail("Centrifugo exited before readiness")
            try:
                if httpx.get(f"http://127.0.0.1:{internal}/health", timeout=0.2, trust_env=False).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        else:
            pytest.fail("Centrifugo did not become ready")
        yield settings
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


@pytest.mark.asyncio
async def test_public_websocket_private_channel_and_actual_backend_publisher(server, monkeypatch):
    from app import realtime

    monkeypatch.setattr(smoke, "get_settings", lambda: server)
    monkeypatch.setattr(realtime, "get_settings", lambda: server)
    await smoke.main()  # Public WS, forbidden foreign subscription and client publishing.
    user_id = str(uuid.uuid4())
    session = realtime.session_credentials(user_id)
    async with connect(session["url"]) as client:
        await client.send(json.dumps({"id": 1, "connect": {"token": session["token"]}}))
        response = await smoke.receive(client, command_id=1)
        assert set(response["connect"]["subs"]) == {session["channel"]}
        event = {"version": 1, "type": "invalidate", "event_id": str(uuid.uuid4()), "scopes": ["chat"]}
        # Exercise the real application's publisher, including v6 idempotency_key.
        await realtime.publish_invalidation(user_id, event)
        received = await smoke.receive(client, event_id=event["event_id"])
        assert received["push"]["pub"]["data"] == event


@pytest.mark.asyncio
async def test_internal_api_is_not_on_public_listener_and_forged_tokens_fail(server):
    public = server.realtime_ws_url.replace("ws://", "http://").split("/connection/")[0]
    async with httpx.AsyncClient(trust_env=False) as client:
        response = await client.post(public + "/api/info", headers={"X-API-Key": server.centrifugo_api_key}, json={})
        assert response.status_code == 404
        assert (await client.get(public + "/health")).status_code == 404
        denied = await client.post(server.centrifugo_api_url + "/info", json={})
        assert denied.status_code in {401, 403}
    token = jwt.encode({"sub": str(uuid.uuid4()), "exp": int(time.time()) + 60}, secrets.token_hex(32), algorithm="HS256")
    async with connect(server.realtime_ws_url) as client:
        await client.send(json.dumps({"id": 1, "connect": {"token": token}}))
        with pytest.raises(ConnectionClosedError) as closed:
            await smoke.receive(client, command_id=1)
        assert closed.value.rcvd.code == 3500  # Centrifugo: invalid token.
