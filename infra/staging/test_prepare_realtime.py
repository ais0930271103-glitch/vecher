"""Secrets must survive preparation and must not widen channel permissions."""
import importlib.util
import json
from pathlib import Path
import stat

import pytest

spec = importlib.util.spec_from_file_location("prepare_realtime", Path(__file__).with_name("prepare-realtime.py"))
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


def test_preserves_existing_secrets_and_is_idempotent(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    original = "# Existing settings\nVECHER_DATA_KEYS=s1:do-not-rotate\nVECHER_TURN_SECRET=keep-it\nPOSTGRES_PASSWORD='preserve # symbols'\n"
    (staging / ".env").write_text(original)
    backup = prepare.prepare(staging, tmp_path / "backup", False, owner=None)
    first = (staging / ".env").read_text()
    assert first.startswith(original)
    assert (backup / "staging.env").read_text() == original
    assert stat.S_IMODE((backup / "staging.env").stat().st_mode) == 0o600
    assert stat.S_IMODE((staging / "secrets" / "centrifugo.json").stat().st_mode) == 0o600
    assert prepare.prepare(staging, tmp_path / "backup", False, owner=None) is None
    assert (staging / ".env").read_text() == first
    values = prepare.read_values(first)
    assert values["VECHER_CENTRIFUGO_API_KEY"] != values["VECHER_REALTIME_TOKEN_SECRET"]
    prepare.prepare(staging, tmp_path / "backup", True, owner=None)
    enabled = prepare.read_values((staging / ".env").read_text())
    assert enabled["VECHER_REALTIME_ENABLED"] == "true"
    assert enabled["VECHER_REALTIME_TOKEN_SECRET"] == values["VECHER_REALTIME_TOKEN_SECRET"]
    assert enabled["VECHER_CENTRIFUGO_API_KEY"] == values["VECHER_CENTRIFUGO_API_KEY"]


def test_rejects_invalid_or_duplicate_secrets_without_changes(tmp_path):
    env = tmp_path / ".env"
    original = "VECHER_REALTIME_TOKEN_SECRET=old-invalid\n"
    env.write_text(original)
    with pytest.raises(ValueError, match="existing value was not changed"):
        prepare.prepare(tmp_path, tmp_path / "backup", False, owner=None)
    assert env.read_text() == original
    with pytest.raises(ValueError, match="Duplicate"):
        prepare.make_settings("VECHER_REALTIME_ENABLED=false\nVECHER_REALTIME_ENABLED=true\n", None)


def test_rejects_symlink_env_and_config(tmp_path):
    real = tmp_path / "real"
    real.write_text("VECHER_DATA_KEYS=original\n")
    (tmp_path / ".env").symlink_to(real)
    with pytest.raises(ValueError, match="regular file"):
        prepare.prepare(tmp_path, tmp_path / "backup", False, owner=None)
    (tmp_path / ".env").unlink()
    (tmp_path / ".env").write_text("")
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "centrifugo.json").symlink_to(real)
    with pytest.raises(ValueError, match="regular file"):
        prepare.prepare(tmp_path, tmp_path / "backup", False, owner=None)
    assert real.read_text() == "VECHER_DATA_KEYS=original\n"


def test_only_server_assigned_personal_subscriptions_are_allowed():
    values = prepare.make_settings("", False)
    config = prepare.make_config(values)
    assert config["http_server"]["internal_port"] == "8001"
    assert config["http_api"]["external"] is False
    assert config["admin"]["enabled"] is False
    assert config["client"]["allow_anonymous_connect_without_token"] is False
    assert config["client"]["disallow_anonymous_connection_tokens"] is True
    assert "*" not in config["client"]["allowed_origins"]
    channel = config["channel"]["namespaces"][0]
    assert channel["name"] == "events"
    assert all(channel[k] is False for k in channel if k.startswith("allow_"))
    assert channel["history_size"] == 0
    assert json.loads(json.dumps(config))["http_api"]["key"] == values["VECHER_CENTRIFUGO_API_KEY"]
