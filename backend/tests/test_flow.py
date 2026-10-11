"""End-to-end flow: two people log in by e-mail code and become a pair."""
from httpx import AsyncClient

from conftest import login as _login


async def login(client: AsyncClient, email: str) -> str:
    return (await _login(client, email))["Authorization"].removeprefix("Bearer ")


async def test_health(client):
    r = await client.get("/health")
    assert r.json()["ok"] is True


async def test_wrong_code_is_rejected(client):
    real = (await client.post("/auth/request-code", json={"email": "a@example.ru"})).json()["dev_code"]
    wrong = f"{(int(real) + 1) % 1_000_000:06d}"
    r = await client.post("/auth/verify", json={"email": "a@example.ru", "code": wrong})
    assert r.status_code == 400
    # После неверной попытки настоящий код всё ещё работает.
    r = await client.post("/auth/verify", json={"email": "a@example.ru", "code": real})
    assert r.status_code == 200
    assert r.json()["is_new"] is True


async def test_me_requires_login(client):
    assert (await client.get("/auth/me")).status_code == 401


async def test_two_people_become_a_pair(client):
    t1 = await login(client, "maxim@example.ru")
    t2 = await login(client, "anya@example.ru")
    h1 = {"Authorization": f"Bearer {t1}"}
    h2 = {"Authorization": f"Bearer {t2}"}

    r = await client.post("/pairs/invite", headers=h1)
    assert r.status_code == 200
    code = r.json()["invite_code"]
    assert len(code) == 8 and r.json()["status"] == "pending"

    r = await client.post("/pairs/join", headers=h2, json={"code": code.lower()})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"

    me1 = (await client.get("/pairs/me", headers=h1)).json()
    me2 = (await client.get("/pairs/me", headers=h2)).json()
    assert me1["pair_id"] == me2["pair_id"]
    assert me1["partner_id"] == (await client.get("/auth/me", headers=h2)).json()["id"]

    # Код одноразовый: третий человек не может им воспользоваться.
    t3 = await login(client, "third@example.ru")
    r = await client.post("/pairs/join", headers={"Authorization": f"Bearer {t3}"}, json={"code": code})
    assert r.status_code == 404
