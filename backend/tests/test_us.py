"""Этап 4: памятка, даты, напоминания."""
from datetime import date, datetime, timedelta, timezone

from app.deps import today_date
from app.main import app
from app.routers.us import next_occurrence
from conftest import login, make_pair

TODAY = date(2026, 10, 12)


def set_day(d: date) -> None:
    app.dependency_overrides[today_date] = lambda: d


def field(sections, key):
    return next(f["value"] for s in sections for f in s["fields"] if f["key"] == key)


async def test_us_requires_active_pair(client):
    h = await login(client, "solo@example.ru")
    for path in ("/person", "/dates", "/reminders"):
        assert (await client.get(path, headers=h)).status_code == 409


async def test_about_me_becomes_partners_memo_notes_stay_private(client):
    ha, hb = await make_pair(client)
    assert (await client.put("/person/me", headers=ha, json={"key": "flowers", "value": " Пионы "})).status_code == 200
    assert (await client.put("/person/me", headers=ha, json={"key": "nope", "value": "x"})).status_code == 422
    await client.put("/person/name", headers=ha, json={"name": "Аня"})

    b = (await client.get("/person", headers=hb)).json()
    assert field(b["partner"], "flowers") == "Пионы"
    assert b["partner_name"] == "Аня" and b["partner_filled"] == 1
    assert field(b["me"], "flowers") is None

    # Очистка пустым значением.
    await client.put("/person/me", headers=ha, json={"key": "flowers", "value": ""})
    assert (await client.get("/person", headers=hb)).json()["partner_filled"] == 0

    # Личная заметка видна только автору.
    r = await client.post("/person/notes", headers=hb, json={"text": "Хочет те серёжки"})
    assert r.status_code == 201
    note_id = r.json()["id"]
    assert [n["text"] for n in (await client.get("/person", headers=hb)).json()["notes"]] == ["Хочет те серёжки"]
    assert (await client.get("/person", headers=ha)).json()["notes"] == []
    assert (await client.delete(f"/person/notes/{note_id}", headers=ha)).status_code == 404
    assert (await client.delete(f"/person/notes/{note_id}", headers=hb)).status_code == 204


def test_next_occurrence():
    assert next_occurrence(date(2020, 10, 20), TODAY, True) == date(2026, 10, 20)
    assert next_occurrence(date(2020, 10, 1), TODAY, True) == date(2027, 10, 1)
    assert next_occurrence(date(2020, 10, 12), TODAY, True) == TODAY
    assert next_occurrence(date(2024, 2, 29), TODAY, True) == date(2027, 2, 28)
    assert next_occurrence(date(2026, 11, 5), TODAY, False) == date(2026, 11, 5)


async def test_dates_together_and_upcoming(client):
    set_day(TODAY)
    ha, hb = await make_pair(client)
    r = await client.post("/dates", headers=ha, json={"title": "Познакомились", "day": "2024-10-12", "kind": "met"})
    assert r.status_code == 201
    await client.post("/dates", headers=hb, json={"title": "ДР Ани", "day": "1998-10-20", "kind": "birthday"})
    await client.post("/dates", headers=hb, json={"title": "Концерт", "day": "2026-10-01", "yearly": False})
    assert (await client.post("/dates", headers=hb, json={"title": "x", "day": "2024-01-01", "kind": "met"})).status_code == 409
    assert (await client.post("/dates", headers=hb, json={"title": "x", "day": "2024-01-01", "kind": "boom"})).status_code == 422

    d = (await client.get("/dates", headers=hb)).json()
    assert d["together_days"] == (TODAY - date(2024, 10, 12)).days
    titles = [(i["title"], i["days_left"], i["years"]) for i in d["items"]]
    assert titles == [("Познакомились", 0, 2), ("ДР Ани", 8, 28), ("Концерт", -11, None)]

    did = d["items"][2]["id"]
    assert (await client.delete(f"/dates/{did}", headers=ha)).status_code == 204
    assert len((await client.get("/dates", headers=ha)).json()["items"]) == 2


async def test_reminders_visibility_done_and_delete(client):
    ha, hb = await make_pair(client)
    due = (datetime.now(timezone(timedelta(hours=3))) + timedelta(hours=2)).replace(microsecond=0)
    me = (await client.post("/reminders", headers=ha, json={"text": "Себе", "target": "me"})).json()
    to_b = (await client.post("/reminders", headers=ha, json={
        "text": "Купить хлеб", "target": "partner", "due_at": due.isoformat()})).json()
    both = (await client.post("/reminders", headers=ha, json={"text": "Забрать сына", "target": "both"})).json()
    assert (await client.post("/reminders", headers=ha, json={"text": "x", "target": "all"})).status_code == 422

    a = {r["text"]: r for r in (await client.get("/reminders", headers=ha)).json()}
    assert set(a) == {"Себе", "Купить хлеб", "Забрать сына"}
    assert (a["Себе"]["for_me"], a["Купить хлеб"]["for_me"], a["Забрать сына"]["for_me"]) == (True, False, True)

    b = {r["text"]: r for r in (await client.get("/reminders", headers=hb)).json()}
    assert set(b) == {"Купить хлеб", "Забрать сына"}  # «себе» партнёра не видно
    assert b["Купить хлеб"]["who"] == "От партнёра" and b["Купить хлеб"]["for_me"] is True
    assert datetime.fromisoformat(b["Купить хлеб"]["due_at"]) == due

    # Чужое «себе» недоступно; отметить может адресат; удалить — только автор.
    assert (await client.post(f"/reminders/{me['id']}/done", headers=hb)).status_code == 404
    assert (await client.post(f"/reminders/{to_b['id']}/done", headers=hb)).json()["done"] is True
    assert (await client.delete(f"/reminders/{both['id']}", headers=hb)).status_code == 403
    assert (await client.delete(f"/reminders/{both['id']}", headers=ha)).status_code == 204

    # Выполненные — в конце списка.
    order = [r["text"] for r in (await client.get("/reminders", headers=ha)).json()]
    assert order[-1] == "Купить хлеб"
