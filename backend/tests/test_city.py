"""Город: при регистрации, смена в настройках, «сейчас в другом городе» на время поездки."""
from datetime import date, timedelta

from app.deps import today_date
from app.main import app
from conftest import login

DAY = date(2026, 10, 12)


async def test_city_choose_change_and_travel(client):
    app.dependency_overrides[today_date] = lambda: DAY
    h = await login(client, "city@example.ru")
    assert (await client.get("/auth/me", headers=h)).json()["has_city"] is False
    assert (await client.get("/me/city", headers=h)).json()["current_city"] is None

    found = (await client.get("/cities", params={"q": "архан"})).json()
    assert found[0]["name"] == "Архангельск" and found[0]["region"] == "Архангельская область"
    assert len((await client.get("/cities")).json()) == 20
    # Поехать в другой город, не указав домашний, нельзя.
    assert (await client.put("/me/city", headers=h, json={"travel_city": "Москва"})).status_code == 422

    r = (await client.put("/me/city", headers=h, json={"home_city": "  архангельск "})).json()
    assert r == {"home_city": "Архангельск", "travel_city": None, "travel_until": None, "current_city": "Архангельск"}
    assert (await client.get("/auth/me", headers=h)).json()["has_city"] is True

    until = DAY + timedelta(days=3)
    r = (await client.put("/me/city", headers=h, json={"travel_city": "москва", "travel_until": until.isoformat()})).json()
    assert r["current_city"] == "Москва" and r["home_city"] == "Архангельск"
    assert (await client.put("/me/city", headers=h, json={"travel_city": "Сочи",
                                                          "travel_until": (DAY - timedelta(days=1)).isoformat()})).status_code == 422
    # Поездка закончилась — снова дома, без лишних действий.
    app.dependency_overrides[today_date] = lambda: until + timedelta(days=1)
    r = (await client.get("/me/city", headers=h)).json()
    assert r["current_city"] == "Архангельск" and r["travel_city"] is None

    # Своего города нет в списке — можно вписать; «вернулся» — пустая строка.
    app.dependency_overrides[today_date] = lambda: DAY
    r = (await client.put("/me/city", headers=h, json={"travel_city": "Онега"})).json()
    assert r["current_city"] == "Онега" and r["travel_until"] is None
    r = (await client.put("/me/city", headers=h, json={"travel_city": ""})).json()
    assert r["current_city"] == "Архангельск"
