"""Приглашение в пару по ссылке: ссылка в ответе и экран «Аня приглашает тебя»."""
from conftest import login


async def test_invite_link_and_preview(client):
    ha = await login(client, "anya@example.ru")
    await client.put("/auth/me", headers=ha, json={"display_name": "Аня"})
    inv = (await client.post("/pairs/invite", headers=ha)).json()
    code = inv["invite_code"]
    assert inv["invite_url"].endswith(f"/i/{code}")

    hb = await login(client, "max@example.ru")
    pretty = f"{code[:4]}-{code[4:]}".lower()  # из ссылки или продиктованный — регистр и дефис не важны
    assert (await client.get(f"/pairs/invite/{pretty}", headers=hb)).json() == {"inviter_name": "Аня"}
    assert (await client.get(f"/pairs/invite/{code}", headers=ha)).status_code == 409  # своё приглашение
    assert (await client.get("/pairs/invite/NOPE0000", headers=hb)).status_code == 404

    assert (await client.post("/pairs/join", headers=hb, json={"code": pretty})).status_code == 200
    # Использованная ссылка больше не работает.
    assert (await client.get(f"/pairs/invite/{code}", headers=hb)).status_code == 404
    assert (await client.get("/pairs/me", headers=ha)).json()["invite_url"] is None


async def test_invite_page_in_browser(client):
    ha = await login(client, "anya2@example.ru")
    await client.put("/auth/me", headers=ha, json={"display_name": "Аня"})
    code = (await client.post("/pairs/invite", headers=ha)).json()["invite_code"]
    page = await client.get(f"/i/{code}")
    assert page.status_code == 200 and "Аня приглашает тебя" in page.text
    assert f"vecher://open/i/{code}" in page.text
    assert (await client.get("/i/NOPE0000")).status_code == 404
