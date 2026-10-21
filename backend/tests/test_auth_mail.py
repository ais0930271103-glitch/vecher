"""Login delivery must not expose OTPs or recipient addresses through logs."""
import asyncio
import logging
import smtplib

import pytest
from sqlalchemy import select

from app import mailer
from app.config import get_settings
from app.db import SessionLocal
from app.models import LoginCode
from app.routers import auth
from app.security import hash_otp


EMAIL = "mail-privacy@example.ru"
CODE = "583204"


@pytest.fixture
def mail_config(monkeypatch, caplog):
    settings = get_settings()
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.invalid")
    monkeypatch.setattr(settings, "smtp_user", "sender@example.ru")
    monkeypatch.setattr(settings, "smtp_password", "synthetic-test-password")
    monkeypatch.setattr(auth, "new_otp", lambda: CODE)
    caplog.set_level(logging.DEBUG, logger="vecher.auth")
    caplog.set_level(logging.DEBUG, logger="vecher.mail")
    return settings


async def finish_mail_tasks():
    tasks = tuple(auth._mail_tasks)
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)


def assert_private_logs(caplog):
    assert EMAIL not in caplog.text
    assert CODE not in caplog.text
    assert f"{CODE[:3]} {CODE[3:]}" not in caplog.text


@pytest.mark.parametrize("environment,configured", [
    ("dev", False), ("dev", True), ("staging", True), ("prod", True),
])
async def test_login_code_only_returned_in_dev_and_never_logged(
    client, monkeypatch, caplog, mail_config, environment, configured,
):
    monkeypatch.setattr(mail_config, "env", environment)
    if not configured:
        monkeypatch.setattr(mail_config, "smtp_host", "")
    sent = []

    def capture_message(to, subject, text, html):
        sent.append((to, subject, text, html))

    monkeypatch.setattr(mailer, "_send", capture_message)
    response = await client.post("/auth/request-code", json={"email": EMAIL})
    await finish_mail_tasks()
    assert response.status_code == 200
    expected = {"sent": True, "dev_code": CODE} if environment == "dev" else {"sent": True}
    assert response.json() == expected
    assert len(sent) == int(configured)
    if configured:
        assert sent[0][0] == EMAIL and f"{CODE[:3]} {CODE[3:]}" in sent[0][2]
    async with SessionLocal() as session:
        row = (await session.execute(select(LoginCode))).scalar_one()
        assert row.email == EMAIL and row.code_hash == hash_otp(EMAIL, CODE)
    assert_private_logs(caplog)


@pytest.mark.parametrize("environment", ["staging", "prod"])
@pytest.mark.parametrize("missing", ["smtp_host", "smtp_user", "smtp_password"])
async def test_missing_smtp_rejects_before_creating_an_unusable_code(
    client, monkeypatch, caplog, mail_config, environment, missing,
):
    monkeypatch.setattr(mail_config, "env", environment)
    monkeypatch.setattr(mail_config, missing, "")

    def must_not_generate_code():
        raise AssertionError("SMTP precondition must be checked before generating a code")

    monkeypatch.setattr(auth, "new_otp", must_not_generate_code)
    response = await client.post("/auth/request-code", json={"email": EMAIL})
    assert response.status_code == 503
    assert "не настроена" in response.json()["detail"]
    assert "dev_code" not in response.json()
    async with SessionLocal() as session:
        assert (await session.execute(select(LoginCode))).first() is None
    assert not auth._mail_tasks
    assert_private_logs(caplog)


@pytest.mark.parametrize("environment", ["dev", "staging", "prod"])
async def test_smtp_exception_logs_only_its_class(
    client, monkeypatch, caplog, mail_config, environment,
):
    monkeypatch.setattr(mail_config, "env", environment)
    private_provider_response = f"private-provider-body: {EMAIL}, code={CODE}"

    def fail_delivery(*_args):
        raise smtplib.SMTPRecipientsRefused({EMAIL: (550, private_provider_response.encode())})

    monkeypatch.setattr(mailer, "_send", fail_delivery)
    response = await client.post("/auth/request-code", json={"email": EMAIL})
    await finish_mail_tasks()
    assert response.status_code == (503 if environment == "prod" else 200)
    if environment != "dev":
        assert "dev_code" not in response.json() and CODE not in response.text
    records = [record for record in caplog.records if record.name == "vecher.mail"]
    assert len(records) == 1
    assert records[0].getMessage() == "Не удалось отправить письмо с кодом входа (SMTPRecipientsRefused)"
    assert records[0].exc_info is None
    assert "private-provider-body" not in caplog.text
    assert_private_logs(caplog)


async def test_configured_staging_still_responds_before_background_mail_finishes(
    client, monkeypatch, mail_config,
):
    monkeypatch.setattr(mail_config, "env", "staging")
    started, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def slow_delivery(to, code):
        assert to == EMAIL and code == CODE
        started.set()
        await release.wait()
        completed.set()
        return True

    monkeypatch.setattr(auth, "send_login_code", slow_delivery)
    try:
        response = await asyncio.wait_for(
            client.post("/auth/request-code", json={"email": EMAIL}), timeout=2,
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        assert response.status_code == 200 and response.json() == {"sent": True}
        assert not completed.is_set()
    finally:
        release.set()
        await finish_mail_tasks()
    assert completed.is_set()
