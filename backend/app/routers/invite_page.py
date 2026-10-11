"""Страница ссылки-приглашения: https://<сервер>/i/КОД.

Открывается в браузере у того, кого пригласили: «Аня приглашает тебя в «Вечер»»,
кнопки «Открыть в приложении» (если уже установлено) и «Скачать приложение», и код на всякий случай.
"""
import html
import re
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_session
from ..models import Pair, PairMember, User

router = APIRouter(tags=["invite-page"])

PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<title>Приглашение в «Вечер»</title>
<style>
body{{margin:0;min-height:100vh;background:#121220;color:#F4EEE6;font-family:-apple-system,"Segoe UI",Roboto,sans-serif;
display:flex;align-items:center;justify-content:center}}
main{{max-width:380px;padding:32px 20px;text-align:center}}
h1{{font-weight:400;color:#F6DDB4;font-size:28px;margin:14px 0 8px;line-height:1.25}}
p{{color:#D8D2DE;line-height:1.5}} small{{color:#A9A5B5}}
.code{{font-size:30px;letter-spacing:4px;color:#F6DDB4;font-weight:700;margin:4px 0 22px}}
.btn{{display:block;padding:16px;border-radius:28px;text-decoration:none;font-weight:700;margin:10px 0;font-size:16px}}
.gold{{background:linear-gradient(90deg,#F3C98B,#E3A55E);color:#1A1433}}
.ghost{{border:1px solid rgba(243,201,139,.45);color:#F6DDB4}}
ol{{text-align:left;color:#D8D2DE;line-height:1.6;padding-left:20px}}
</style></head><body><main>
<div style="font-size:48px">🌙</div>
<h1>{title}</h1>
<p>«Вечер» — пространство только для вас двоих: вопросы, совпадения желаний, планы и ваши даты.</p>
{body}
</main></body></html>"""

OK = """<p><small>Код приглашения</small></p><div class="code">{pretty}</div>
<a class="btn ghost" href="vecher://open/i/{code}">У меня уже есть «Вечер» — открыть</a>
{download}
<ol><li>Скачай и установи «Вечер» (телефон спросит разрешение на установку — разреши).</li>
<li>Войди по почте.</li><li>Нажми «У меня есть код» и введи <b>{pretty}</b> — или открой эту ссылку ещё раз.</li></ol>"""

GONE = """<p>Это приглашение уже использовано или отменено. Попроси партнёра прислать новое.</p>"""


@router.get("/i/{code}", response_class=HTMLResponse, include_in_schema=False)
async def invite_page(code: str, request: Request, session: AsyncSession = Depends(get_session)):
    clean = re.sub(r"[^A-Za-z0-9]", "", code).upper()[:16]
    pair = (await session.execute(select(Pair).where(Pair.invite_code == clean))).scalar_one_or_none()
    if pair is None or pair.status != "pending":
        return HTMLResponse(PAGE.format(title="Приглашение не найдено", body=GONE), status_code=404)
    inviter = (await session.execute(
        select(User).join(PairMember, PairMember.user_id == User.id).where(PairMember.pair_id == pair.id)
    )).scalars().first()
    name = html.escape(inviter.display_name) if inviter and inviter.display_name else "Тебя"
    title = f"{name} приглашает тебя в «Вечер»" if name != "Тебя" else "Тебя пригласили в «Вечер»"
    has_apk = (Path(get_settings().downloads_dir) / "vecher.apk").exists()
    download = (f'<a class="btn gold" href="{request.url_for("download_apk")}">Скачать приложение</a>'
                if has_apk else '<p><small>Скоро «Вечер» можно будет скачать в RuStore.</small></p>')
    pretty = f"{clean[:4]}-{clean[4:]}" if len(clean) == 8 else clean
    return HTMLResponse(PAGE.format(title=title, body=OK.format(code=clean, pretty=pretty, download=download)))
