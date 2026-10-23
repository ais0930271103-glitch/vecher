"""Guard tests and a real local process rehearsal, without Docker/live services.

  /tmp/vecher-venv/bin/python -m pytest -q infra/staging/release14/test_media_rehearsal.py

The integration child uses an empty disposable SQLite DB, ASGI transport, real
FFmpeg, a distinct worker without DATA_KEYS/JWT, and the actual operator CLI.
The release runner/CI must also exercise PostgreSQL and built images on its
isolated internal Docker network; this is not evidence of a live deployment.
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys

import pytest

DIRECTORY = Path(__file__).resolve().parent
BACKEND = DIRECTORY.parents[2] / "backend"
HELPER = DIRECTORY / "media_rehearsal.py"
spec = importlib.util.spec_from_file_location("release14_media_rehearsal", HELPER)
rehearsal = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = rehearsal
spec.loader.exec_module(rehearsal)
DATABASE = "vecher_release14_smoke_012345abcdef"
BASE = "http://127.0.0.1:8000"


def safe_environment(tmp_path):
    return {
        "VECHER_RELEASE14_SYNTHETIC_REHEARSAL": "1",
        "VECHER_ENV": "staging",
        "VECHER_BLOGS_ENABLED": "true",
        "VECHER_DATABASE_URL": "postgresql+asyncpg://synthetic:only@isolated-db:5432/" + DATABASE,
        "VECHER_JWT_SECRET": "synthetic-test-secret-" * 3,
        "VECHER_DATA_KEYS": "k1:" + base64.b64encode(bytes(range(32))).decode(),
        "VECHER_SMTP_HOST": "",
        "VECHER_SCHEDULER_ENABLED": "false",
        "VECHER_OUTBOX_ENABLED": "false",
        "VECHER_REALTIME_ENABLED": "false",
        "VECHER_FIREBASE_CREDENTIALS": str(tmp_path / "disabled-push.json"),
    }


def test_guard_accepts_only_explicit_synthetic_environment(tmp_path):
    rehearsal.validate_environment(safe_environment(tmp_path), DATABASE, BASE)
    rehearsal.validate_environment(safe_environment(tmp_path), DATABASE, "http://[::1]:8000/")


@pytest.mark.parametrize("change,expected_code", [
    ({"VECHER_RELEASE14_SYNTHETIC_REHEARSAL": "0"}, "synthetic_guard_missing"),
    ({"VECHER_ENV": "prod"}, "staging_required"),
    ({"VECHER_BLOGS_ENABLED": "false"}, "blogs_must_be_enabled"),
    ({"VECHER_SCHEDULER_ENABLED": "true"}, "delivery_must_be_disabled"),
    ({"VECHER_OUTBOX_ENABLED": ""}, "delivery_must_be_disabled"),
    ({"VECHER_REALTIME_ENABLED": "true"}, "delivery_must_be_disabled"),
    ({"VECHER_SMTP_HOST": "mail.example.invalid"}, "smtp_must_be_disabled"),
    ({"VECHER_SMTP_PASSWORD": "do-not-print-this"}, "smtp_credentials_not_allowed"),
    ({"VECHER_DATA_KEYS": ""}, "synthetic_encryption_key_required"),
    ({"VECHER_JWT_SECRET": "short"}, "synthetic_jwt_required"),
    ({"VECHER_FIREBASE_CREDENTIALS": "firebase-key.json"}, "firebase_must_be_unconfigured"),
    ({"VECHER_DATABASE_URL": "postgresql+asyncpg://user:secret@db/vecher"}, "database_url_not_synthetic"),
    ({"VECHER_DATABASE_URL": "sqlite+aiosqlite:///synthetic.db"}, "database_url_not_synthetic"),
    ({"VECHER_DATABASE_URL": "postgresql+asyncpg://db/" + DATABASE + "?host=other"}, "database_url_not_synthetic"),
])
def test_guard_refuses_unsafe_environment_before_app_import(tmp_path, change, expected_code):
    environment = safe_environment(tmp_path) | change
    with pytest.raises(rehearsal.RehearsalStopped) as stopped:
        rehearsal.validate_environment(environment, DATABASE, BASE)
    assert stopped.value.code == expected_code
    assert "do-not-print-this" not in str(stopped.value)


@pytest.mark.parametrize("url", [
    "https://127.0.0.1:8000", "http://localhost:8000", "http://public.example:8000",
    "http://127.0.0.1:8001", "http://token:secret@127.0.0.1:8000",
    BASE + "/other", BASE + "?target=other", BASE + "#fragment",
])
def test_guard_rejects_external_ambiguous_or_credentialed_http(tmp_path, url):
    with pytest.raises(rehearsal.RehearsalStopped, match="loopback_api_required"):
        rehearsal.validate_environment(safe_environment(tmp_path), DATABASE, url)


def test_guard_rejects_existing_push_credentials_and_database_name(tmp_path):
    environment = safe_environment(tmp_path)
    Path(environment["VECHER_FIREBASE_CREDENTIALS"]).write_text("synthetic-secret")
    with pytest.raises(rehearsal.RehearsalStopped, match="firebase_must_be_unconfigured"):
        rehearsal.validate_environment(environment, DATABASE, BASE)
    with pytest.raises(rehearsal.RehearsalStopped, match="database_name_not_synthetic"):
        rehearsal.validate_environment(environment, "vecher", BASE)


def fixture_bytes():
    return b"\x00\x00\x00\x18ftypisom" + rehearsal.SOURCE_MARKER


def test_fixture_is_confined_bounded_and_requires_metadata_marker(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    path = media / rehearsal.FIXTURE_NAME
    path.write_bytes(fixture_bytes())
    assert rehearsal.read_fixture(path, media).data == fixture_bytes()
    with pytest.raises(rehearsal.RehearsalStopped, match="fixture_path_not_isolated"):
        rehearsal.read_fixture(tmp_path / "arbitrary.mp4", media)
    path.write_bytes(b"ftyp-but-no-test-metadata")
    with pytest.raises(rehearsal.RehearsalStopped, match="fixture_marker_missing"):
        rehearsal.read_fixture(path, media)
    with path.open("wb") as stream:
        stream.truncate(rehearsal.MAX_FIXTURE_BYTES + 1)
    with pytest.raises(rehearsal.RehearsalStopped, match="fixture_size_or_type_invalid"):
        rehearsal.read_fixture(path, media)


def test_fixture_rejects_symlink_directory_file_and_hardlink(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    other = tmp_path / "other.mp4"
    other.write_bytes(fixture_bytes())
    path = media / rehearsal.FIXTURE_NAME
    path.symlink_to(other)
    with pytest.raises(rehearsal.RehearsalStopped, match="fixture_unavailable"):
        rehearsal.read_fixture(path, media)
    path.unlink()
    os.link(other, path)
    with pytest.raises(rehearsal.RehearsalStopped, match="fixture_size_or_type_invalid"):
        rehearsal.read_fixture(path, media)
    root_link = tmp_path / "media-link"
    root_link.symlink_to(media, target_is_directory=True)
    with pytest.raises(rehearsal.RehearsalStopped, match="unsafe_media_directory"):
        rehearsal.read_fixture(root_link / rehearsal.FIXTURE_NAME, root_link)


def test_cli_fails_before_app_import_and_never_prints_secret_input(tmp_path):
    # Deliberately no PYTHONPATH: importing app here would itself fail. The
    # missing sentinel must reject the request first with its stable code.
    result = subprocess.run([sys.executable, str(HELPER), "--base-url", "http://secret:never-print@host:8000",
                             "--fixture", "/not-used.mp4", "--expected-database", DATABASE],
                            cwd=tmp_path, env={"PATH": os.defpath}, capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "RELEASE14_MEDIA_REHEARSAL_STOPPED=synthetic_guard_missing\n"


def test_real_upload_keyless_worker_review_follow_and_withdrawal(tmp_path):
    missing = [name for name in ("ffmpeg", "ffprobe") if not shutil.which(name)]
    if missing:
        if os.environ.get("VECHER_REQUIRE_MEDIA_TOOLS") == "1":
            pytest.fail("Required real media tools are missing")
        pytest.skip("Real media tools are not installed")
    environment = {key: value for key, value in os.environ.items() if not key.startswith("VECHER_")}
    environment.update(safe_environment(tmp_path))
    environment.update({
        "PYTHONPATH": str(BACKEND),
        "VECHER_JWT_SECRET": secrets.token_urlsafe(48),
        "VECHER_DATA_KEYS": "k1:" + base64.b64encode(secrets.token_bytes(32)).decode(),
        "VECHER_DATABASE_URL": "sqlite+aiosqlite:///" + str(tmp_path / "synthetic.db"),
        "VECHER_BLOG_MEDIA_DIR": str(tmp_path / "media"),
        "VECHER_BLOG_MEDIA_MIN_FREE_BYTES": "0",
        "VECHER_BLOG_MEDIA_WORKER_POLL_SECONDS": "0.1",
        "VECHER_UPLOADS_DIR": str(tmp_path / "unused-private-uploads"),
        "VECHER_DOWNLOADS_DIR": str(tmp_path / "unused-downloads"),
    })
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--exercise-child"],
                            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=150)
    assert result.returncode == 0, result.stdout + result.stderr[-6000:]
    lines = result.stdout.splitlines()
    report = json.loads(next(line for line in lines if line.startswith("{")))
    assert "RELEASE14_LOCAL_PROCESS_REHEARSAL_OK" in lines
    assert report["profile_encrypted"] and report["worker_completed"] and report["operator_exact_pins"]
    assert report["joint_consent_verified"] and report["withdrawal_verified"]
    assert report["reader_range_verified"] and report["private_follows_verified"]
    assert report["old_client_compatible"] and report["upload_idempotent"]
    assert report["synthetic_users"] == 3 and report["http_requests"] >= 40
    assert report["schema"] == rehearsal.SCHEMA_HEAD and report["version"] == rehearsal.VERSION
    assert environment["VECHER_JWT_SECRET"] not in result.stdout + result.stderr
    assert environment["VECHER_DATA_KEYS"] not in result.stdout + result.stderr


async def exercise_child():
    """Fresh interpreter avoids app settings/engine caches from another test."""
    import httpx
    from sqlalchemy import text
    from app.db import SessionLocal, engine
    from app.main import app
    from app.migrate import upgrade
    from app.models import User

    root = Path(os.environ["VECHER_BLOG_MEDIA_DIR"])
    root.mkdir(mode=0o700)
    path = root / rehearsal.FIXTURE_NAME
    subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
        "color=c=blue:s=64x48:r=10:d=0.6", "-f", "lavfi", "-i",
        "sine=frequency=440:sample_rate=48000:duration=0.6", "-c:v", "libx264", "-threads", "1",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", "-metadata",
        "comment=source-metadata-must-be-removed", str(path),
    ], stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=30)
    fixture = rehearsal.read_fixture(path, root)
    assert await upgrade(engine) == rehearsal.SCHEMA_HEAD
    await rehearsal.require_empty_database(engine)
    # The empty guard must also refuse unrelated application data, before API
    # writes. Roll back a tiny synthetic row, then prove the DB is empty again.
    async with SessionLocal() as session:
        session.add(User(email="must-block@synthetic.invalid"))
        await session.commit()
    try:
        await rehearsal.require_empty_database(engine)
    except rehearsal.RehearsalStopped as error:
        assert error.code == "database_not_empty"
    else:
        raise AssertionError("Nonempty DB was accepted")
    async with engine.begin() as connection:
        await connection.execute(text("DELETE FROM users"))
    await rehearsal.require_empty_database(engine)
    worker_environment = {key: value for key, value in os.environ.items()
                          if not key.startswith("VECHER_") and key not in {"SMTP_PASSWORD", "DATA_KEYS"}}
    for name in ("ENV", "DATABASE_URL", "BLOGS_ENABLED", "BLOG_MEDIA_DIR", "BLOG_MEDIA_MIN_FREE_BYTES",
                 "BLOG_MEDIA_WORKER_POLL_SECONDS"):
        worker_environment["VECHER_" + name] = os.environ["VECHER_" + name]
    assert "VECHER_DATA_KEYS" not in worker_environment and "VECHER_JWT_SECRET" not in worker_environment
    assert not any(key.startswith("VECHER_SMTP") for key in worker_environment)
    worker = subprocess.Popen([sys.executable, "-m", "app.blog_media_worker"],
                              env=worker_environment, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE,
                                         trust_env=False, follow_redirects=False) as client:
                async with asyncio.timeout(100):
                    report = await rehearsal.exercise(client, fixture, ready_timeout=60)
        assert worker.poll() is None, "Real worker exited unexpectedly"
        try:
            await rehearsal.require_empty_database(engine)
        except rehearsal.RehearsalStopped as error:
            assert error.code == "database_not_empty"
        else:
            raise AssertionError("An exercised DB was accepted for a second run")
        print(json.dumps(report, sort_keys=True))
        print("RELEASE14_LOCAL_PROCESS_REHEARSAL_OK")
    finally:
        if worker.poll() is None:
            worker.terminate()
        try:
            _, stderr = worker.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            worker.kill()
            _, stderr = worker.communicate(timeout=10)
        await engine.dispose()
        if b"Traceback" in stderr:
            raise AssertionError("Worker process failed; no secret output included")


if __name__ == "__main__":
    if sys.argv[1:] != ["--exercise-child"]:
        raise SystemExit("Only the isolated test child is supported")
    asyncio.run(exercise_child())
