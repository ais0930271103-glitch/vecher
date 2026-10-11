"""Мягкое приглашение: без «нет», с возможностью предложить другой день."""
from datetime import datetime, timedelta, timezone

from app import push
from conftest import make_pair


def iso(dt: datetime) -> str:
    return dt.isoformat()


async def test_soft_invite_flow(client):
    push.outbox.clear()
    ha, hb = await make_pair(client)
    await client.put("/auth/me", headers=ha, json={"display_name": "Аня"})
    b = (await client.get("/auth/me", headers=hb)).json()["id"]
    tonight = datetime.now(timezone.utc) + timedelta(hours=3)

    r = await client.post("/invites", headers=ha, json={"title": "Ужин вдвоём", "note": "Я готовлю", "at": iso(tonight)})
    assert r.status_code == 201
    inv = r.json()
    assert inv["mine"] is True and inv["status"] == "pending"
    assert "Аня приглашает тебя провести время вместе" in [body for u, _, body, _ in push.outbox if u == b]
    assert all("Ужин" not in body for _, _, body, _ in push.outbox)  # подробности — только в приложении
    # Второе приглашение, пока первое ждёт ответа, — нельзя.
    assert (await client.post("/invites", headers=ha, json={"title": "Кино"})).status_code == 409

    seen = (await client.get("/invites", headers=hb)).json()
    assert len(seen) == 1 and seen[0]["mine"] is False and seen[0]["note"] == "Я готовлю"
    assert (await client.post(f"/invites/{inv['id']}/answer", headers=ha, json={"answer": "yes"})).status_code == 403
    assert (await client.post(f"/invites/{inv['id']}/answer", headers=hb, json={"answer": "no"})).status_code == 422

    friday = tonight + timedelta(days=2)
    r = (await client.post(f"/invites/{inv['id']}/answer", headers=hb,
                           json={"answer": "later", "proposed_at": iso(friday)})).json()
    assert r["status"] == "later" and r["proposed_at"] is not None
    assert (await client.post(f"/invites/{inv['id']}/answer", headers=hb, json={"answer": "yes"})).status_code == 409
    # Пригласившая соглашается на другой день — договорились.
    assert (await client.post(f"/invites/{inv['id']}/agree", headers=hb)).status_code == 409
    r = (await client.post(f"/invites/{inv['id']}/agree", headers=ha)).json()
    assert r["status"] == "yes" and r["at"] == r["proposed_at"]
    assert len((await client.get("/invites", headers=ha)).json()) == 1  # видно до назначенного времени


async def test_not_today_and_cancel(client):
    ha, hb = await make_pair(client)
    inv = (await client.post("/invites", headers=ha, json={"title": "Прогулка"})).json()
    r = (await client.post(f"/invites/{inv['id']}/answer", headers=hb, json={"answer": "not_today"})).json()
    assert r["status"] == "not_today"
    # Можно пригласить снова, а приглашение без ответа — отменить.
    inv2 = (await client.post("/invites", headers=ha, json={"title": "Кино дома"})).json()
    assert (await client.post(f"/invites/{inv2['id']}/cancel", headers=hb)).status_code == 403
    assert (await client.post(f"/invites/{inv2['id']}/cancel", headers=ha)).json()["status"] == "cancelled"
    assert [i["title"] for i in (await client.get("/invites", headers=hb)).json()] == ["Прогулка"]
    past = datetime.now(timezone.utc) - timedelta(hours=2)
    assert (await client.post("/invites", headers=ha, json={"title": "Вчера", "at": past.isoformat()})).status_code == 422
