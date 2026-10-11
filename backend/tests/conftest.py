"""Общие фикстуры: свежая база на каждый тест, клиент, вход, пара."""
import os

os.environ["VECHER_DATABASE_URL"] = "sqlite+aiosqlite:///./test.db"
os.environ["VECHER_ENV"] = "dev"
os.environ["VECHER_SCHEDULER_ENABLED"] = "0"  # фоновые уведомления в тестах вызываем вручную
os.environ["VECHER_FIREBASE_CREDENTIALS"] = "no-such-key.json"
os.environ["VECHER_OTP_RESEND_SECONDS"] = "0"  # в тестах коды запрашиваются подряд
os.environ["VECHER_JWT_SECRET"] = "test-secret-0123456789abcdef0123456789abcdef"

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
async def fresh_db():
    from app import models  # noqa: F401
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    app.dependency_overrides.clear()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def login(client: AsyncClient, email: str) -> dict:
    code = (await client.post("/auth/request-code", json={"email": email})).json()["dev_code"]
    r = await client.post("/auth/verify", json={"email": email, "code": code})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def make_pair(client: AsyncClient, a: str = "maxim@example.ru", b: str = "anya@example.ru"):
    ha, hb = await login(client, a), await login(client, b)
    code = (await client.post("/pairs/invite", headers=ha)).json()["invite_code"]
    r = await client.post("/pairs/join", headers=hb, json={"code": code})
    assert r.status_code == 200, r.text
    return ha, hb
