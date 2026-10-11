"""Отправка писем (код входа) через SMTP — почта на рег.ру.

Без настроек SMTP (разработка, тесты) письмо не уходит: код пишется в лог сервера.
"""
import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from .config import get_settings

log = logging.getLogger("vecher.mail")


def smtp_configured() -> bool:
    s = get_settings()
    return bool(s.smtp_host and s.smtp_user and s.smtp_password)


def _send(to: str, subject: str, text: str, html: str) -> None:
    s = get_settings()
    msg = EmailMessage()
    msg["From"] = formataddr(("Вечер", s.smtp_from or s.smtp_user))
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    ctx = ssl.create_default_context()
    if s.smtp_port == 465:
        with smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, context=ctx, timeout=8) as smtp:
            smtp.login(s.smtp_user, s.smtp_password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=8) as smtp:
            smtp.starttls(context=ctx)
            smtp.login(s.smtp_user, s.smtp_password)
            smtp.send_message(msg)


async def send_login_code(to: str, code: str) -> bool:
    """True — письмо отправлено; False — SMTP не настроен или ошибка (код смотреть в логе)."""
    if not smtp_configured():
        return False
    pretty = f"{code[:3]} {code[3:]}"
    text = (f"Твой код для входа в «Вечер»: {pretty}\n\n"
            f"Код действует {get_settings().otp_ttl_minutes} минут. Если это был не ты — просто проигнорируй письмо.")
    html = f"""<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:420px;margin:0 auto;padding:24px;
background:#121220;color:#F4EEE6;border-radius:16px">
<div style="font-size:22px;color:#F6DDB4;margin-bottom:12px">🌙 Вечер</div>
<div style="color:#D8D2DE">Твой код для входа:</div>
<div style="font-size:34px;letter-spacing:6px;font-weight:700;color:#F6DDB4;margin:12px 0">{pretty}</div>
<div style="color:#A9A5B5;font-size:13px">Код действует {get_settings().otp_ttl_minutes} минут.
Если это был не ты — просто проигнорируй письмо.</div></div>"""
    try:
        await asyncio.to_thread(_send, to, f"Код для входа: {pretty}", text, html)
        return True
    except Exception as e:  # noqa: BLE001
        log.error("Не удалось отправить письмо на %s: %s", to, e)
        return False
