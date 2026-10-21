#!/usr/bin/env python3
"""Prepare staging Centrifugo secrets without running services or changing data keys.

Only Python's standard library is required. Existing secrets are reused; every
changed .env/config is backed up privately before atomic replacement.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import tempfile

DOMAIN = "api-test.xn--b1aga8b7a.online"
MANAGED = {
    "VECHER_REALTIME_ENABLED",
    "VECHER_REALTIME_WS_URL",
    "VECHER_CENTRIFUGO_API_URL",
    "VECHER_CENTRIFUGO_API_KEY",
    "VECHER_REALTIME_TOKEN_SECRET",
    "VECHER_REALTIME_TOKEN_TTL_SECONDS",
}


def regular_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Expected a regular file: {path}")


def read_values(text: str) -> dict[str, str]:
    result = {}
    for line in text.splitlines():
        match = re.fullmatch(r"\s*([A-Z][A-Z0-9_]*)\s*=\s*(.*?)\s*", line)
        if not match or match[1] not in MANAGED:
            continue
        key, value = match.groups()
        if key in result:
            raise ValueError(f"Duplicate setting: {key}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        result[key] = value
    return result


def make_settings(text: str, enabled: bool | None) -> dict[str, str]:
    old = read_values(text)
    values = {
        "VECHER_REALTIME_ENABLED": old.get("VECHER_REALTIME_ENABLED", "false"),
        "VECHER_REALTIME_WS_URL": f"wss://{DOMAIN}/connection/websocket",
        "VECHER_CENTRIFUGO_API_URL": "http://centrifugo:8001/api",
        "VECHER_REALTIME_TOKEN_TTL_SECONDS": "300",
    }
    if enabled is not None:
        values["VECHER_REALTIME_ENABLED"] = str(enabled).lower()
    if values["VECHER_REALTIME_ENABLED"] not in {"true", "false"}:
        raise ValueError("VECHER_REALTIME_ENABLED must be true or false")
    for key in ("VECHER_CENTRIFUGO_API_KEY", "VECHER_REALTIME_TOKEN_SECRET"):
        value = old.get(key) or secrets.token_hex(32)
        if not re.fullmatch(r"[0-9a-fA-F]{64,128}", value):
            raise ValueError(f"{key} must be 64–128 hexadecimal characters; existing value was not changed")
        values[key] = value
    if values["VECHER_CENTRIFUGO_API_KEY"] == values["VECHER_REALTIME_TOKEN_SECRET"]:
        raise ValueError("Centrifugo API key and JWT secret must be different")
    return values


def merge_env(text: str, values: dict[str, str]) -> str:
    # Keep unrelated lines byte-for-byte, including DATA_KEYS/Firebase/TURN.
    lines = text.splitlines(keepends=True)
    seen = set()
    output = []
    for line in lines:
        match = re.match(r"\s*([A-Z][A-Z0-9_]*)\s*=", line)
        if match and match[1] in values:
            key = match[1]
            output.append(f"{key}={values[key]}\n")
            seen.add(key)
        else:
            output.append(line)
    if output and not output[-1].endswith("\n"):
        output[-1] += "\n"
    for key, value in values.items():
        if key not in seen:
            output.append(f"{key}={value}\n")
    return "".join(output)


def make_config(values: dict[str, str]) -> dict:
    return {
        "http_server": {"port": 8000, "internal_port": "8001"},
        "client": {
            "token": {"hmac_secret_key": values["VECHER_REALTIME_TOKEN_SECRET"]},
            "allowed_origins": [f"https://{DOMAIN}"],
            "allow_anonymous_connect_without_token": False,
            "disallow_anonymous_connection_tokens": True,
        },
        "http_api": {"key": values["VECHER_CENTRIFUGO_API_KEY"], "external": False},
        "admin": {"enabled": False},
        "health": {"enabled": True},
        "channel": {
            "namespaces": [{
                "name": "events",
                "allow_subscribe_for_client": False,
                "allow_publish_for_client": False,
                "allow_publish_for_subscriber": False,
                "allow_history_for_client": False,
                "allow_presence_for_client": False,
                "presence": False,
                "history_size": 0,
            }],
        },
        "log": {"level": "warn"},
    }


def atomic_write(path: Path, data: bytes, owner: tuple[int, int] | None) -> None:
    if path.is_symlink():
        raise ValueError(f"Refusing symlink: {path}")
    fd, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        if owner is not None:
            os.fchown(fd, *owner)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def prepare(staging: Path, backup_root: Path, enabled: bool | None,
            owner: tuple[int, int] | None = (10001, 10001)) -> Path | None:
    env = staging / ".env"
    regular_file(env)
    secret_dir = staging / "secrets"
    if secret_dir.is_symlink():
        raise ValueError("secrets must not be a symlink")
    secret_dir.mkdir(mode=0o700, exist_ok=True)
    config_file = secret_dir / "centrifugo.json"
    if config_file.exists() or config_file.is_symlink():
        regular_file(config_file)
    original = env.read_bytes()
    values = make_settings(original.decode("utf-8"), enabled)
    updated = merge_env(original.decode("utf-8"), values).encode()
    config = (json.dumps(make_config(values), indent=2) + "\n").encode()
    if original == updated and config_file.is_file() and config_file.read_bytes() == config:
        os.chmod(secret_dir, 0o700)
        os.chmod(env, 0o600)
        os.chmod(config_file, 0o600)
        if owner is not None:
            os.chown(secret_dir, *owner)
            os.chown(config_file, *owner)
        return None
    if backup_root.is_symlink():
        raise ValueError("backup directory must not be a symlink")
    backup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup = Path(tempfile.mkdtemp(
        prefix=f"realtime-{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}-",
        dir=backup_root,
    ))
    shutil.copyfile(env, backup / "staging.env")
    os.chmod(backup / "staging.env", 0o600)
    if config_file.exists():
        shutil.copyfile(config_file, backup / "centrifugo.json")
        os.chmod(backup / "centrifugo.json", 0o600)
    # Write config before enabling the backend. On failure, .env is still old.
    if owner is not None:
        os.chown(secret_dir, *owner)
    os.chmod(secret_dir, 0o700)
    atomic_write(config_file, config, owner)
    atomic_write(env, updated, None)
    return backup


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staging-dir", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--backup-dir", type=Path, default=Path("/root/backups/vecher-test"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--enable", action="store_true")
    mode.add_argument("--disable", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run as root on the staging server")
    enabled = True if args.enable else False if args.disable else None
    try:
        backup = prepare(args.staging_dir, args.backup_dir, enabled)
    except (ValueError, OSError) as error:
        raise SystemExit(f"REALTIME_PREPARE_STOPPED: {error}") from error
    if backup:
        print(f"REALTIME_CONFIG_BACKUP={backup}")
    print("REALTIME_CONFIG_READY: secrets saved locally; no services started")


if __name__ == "__main__":
    main()
