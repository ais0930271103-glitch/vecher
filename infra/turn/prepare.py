#!/usr/bin/env python3
"""Validate a real TURN host and prepare its local config; never starts services.

Run on the intended TURN host. `--check-only` writes nothing. Normal mode needs
root to make the config/certificate readable by coturn's uid/gid 65534.
The shared secret is generated once, kept in a root-only file and never printed.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile


def read_env(path: Path) -> dict[str, str]:
    settings: dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"Строка {number}: ожидается NAME=value")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in settings:
            raise ValueError(f"Строка {number}: неверное или повторное имя параметра")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        # This is a data parser, not a shell. No source/eval or variable expansion.
        settings[key] = value
    return settings


def required(settings: dict[str, str], key: str) -> str:
    value = settings.get(key, "")
    if not value:
        raise ValueError(f"Заполните {key} фактическим значением")
    if any(ch in value for ch in "\r\n\x00"):
        raise ValueError(f"Неверный формат {key}")
    return value


def validate(settings: dict[str, str]) -> dict[str, object]:
    domain = required(settings, "TURN_DOMAIN").encode("idna").decode("ascii").lower()
    labels = domain.split(".")
    if len(labels) < 2 or len(domain) > 253 or any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in labels
    ):
        raise ValueError("TURN_DOMAIN должен быть действительным DNS-именем")
    if any(domain == x or domain.endswith("." + x) for x in (
        "example.com", "example.org", "example.net", "invalid", "localhost", "example"
    )):
        raise ValueError("В TURN_DOMAIN осталось учебное имя")
    public_ip = ipaddress.IPv4Address(required(settings, "TURN_PUBLIC_IP"))
    local_ip = ipaddress.IPv4Address(required(settings, "TURN_LOCAL_IP"))
    if not public_ip.is_global or public_ip.is_multicast:
        raise ValueError("TURN_PUBLIC_IP должен быть публичным IPv4")
    if local_ip.is_unspecified or local_ip.is_loopback or local_ip.is_multicast or local_ip.is_link_local:
        raise ValueError("TURN_LOCAL_IP должен быть адресом интерфейса TURN-хоста")
    ports = {}
    for key, default in (
        ("TURN_PORT", 3478), ("TURN_TLS_PORT", 5349),
        ("TURN_MIN_PORT", 49160), ("TURN_MAX_PORT", 49200),
    ):
        port = int(settings.get(key, str(default)))
        if not 1024 <= port <= 65535:
            raise ValueError(f"{key}: для общего staging разрешены порты 1024..65535")
        ports[key] = port
    if ports["TURN_PORT"] == ports["TURN_TLS_PORT"]:
        raise ValueError("Обычный и TLS-порт должны различаться")
    if ports["TURN_MAX_PORT"] < ports["TURN_MIN_PORT"] + 15:
        raise ValueError("Нужен диапазон хотя бы из 16 relay-портов")
    if any(ports["TURN_MIN_PORT"] <= p <= ports["TURN_MAX_PORT"] for p in (
        ports["TURN_PORT"], ports["TURN_TLS_PORT"]
    )):
        raise ValueError("Relay-диапазон пересекает порт слушателя")
    state = Path(settings.get("TURN_STATE_DIR", "/opt/vecher-turn"))
    if (not state.is_absolute() or len(state.parts) < 3 or state.resolve() != state
            or state in {Path("/var/tmp"), Path("/var/lib"), Path("/var/run")}):
        raise ValueError("TURN_STATE_DIR должен быть отдельным абсолютным каталогом")
    if state.exists():
        if not state.is_dir():
            raise ValueError("TURN_STATE_DIR должен быть каталогом")
        own_files = {"tls", "turn-secret", "turnserver.conf", "backend-turn.env"}
        if any(item.name not in own_files and not item.name.startswith(".prepare-") for item in state.iterdir()):
            raise ValueError("TURN_STATE_DIR содержит посторонние файлы; выберите отдельный пустой каталог")
    cert = Path(required(settings, "TURN_CERT_FILE"))
    key = Path(required(settings, "TURN_KEY_FILE"))
    if not cert.is_absolute() or not key.is_absolute() or not cert.is_file() or not key.is_file():
        raise ValueError("TURN_CERT_FILE и TURN_KEY_FILE: нужны существующие PEM-файлы по абсолютным путям")
    return dict(domain=domain, public_ip=public_ip, local_ip=local_ip,
                state=state, cert=cert, key=key, **ports)


def openssl(*args: str) -> bytes:
    result = subprocess.run(["openssl", *args], capture_output=True, timeout=15, check=False)
    if result.returncode:
        # Do not echo arguments, private-key content, or generated credentials.
        raise ValueError("Проверка TLS через OpenSSL не пройдена: проверьте имя, срок, цепочку CA и ключ")
    return result.stdout


def verify_tls(config: dict[str, object]) -> None:
    cert, key, domain = str(config["cert"]), str(config["key"]), str(config["domain"])
    openssl("x509", "-in", cert, "-noout", "-checkend", "604800")
    names = openssl("x509", "-in", cert, "-noout", "-subject", "-issuer", "-nameopt", "RFC2253").decode().splitlines()
    values = [line.split("=", 1)[-1].strip() for line in names]
    if len(values) != 2 or values[0] == values[1]:
        raise ValueError("Самоподписанный сертификат для TURN TLS не принимается")
    openssl("verify", "-purpose", "sslserver", "-verify_hostname", domain, "-untrusted", cert, cert)
    certificate_key = openssl("x509", "-in", cert, "-pubkey", "-noout")
    private_key = openssl("pkey", "-in", key, "-passin", "pass:", "-pubout")
    if certificate_key.strip() != private_key.strip():
        raise ValueError("TLS-сертификат и приватный ключ не совпадают")


def atomic_write(path: Path, data: bytes, mode: int, group: int = 0) -> None:
    if path.is_symlink():
        raise ValueError(f"Отказ от замены символической ссылки: {path.name}")
    fd, temp = tempfile.mkstemp(prefix=".prepare-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
            os.fchmod(target.fileno(), mode)
            os.fchown(target.fileno(), 0, group)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def render(config: dict[str, object], secret: str) -> str:
    lines = [
        "# Generated by infra/turn/prepare.py; contains a shared secret. Do not commit.",
        f"listening-ip={config['local_ip']}", f"relay-ip={config['local_ip']}",
        f"listening-port={config['TURN_PORT']}", f"tls-listening-port={config['TURN_TLS_PORT']}",
        f"min-port={config['TURN_MIN_PORT']}", f"max-port={config['TURN_MAX_PORT']}",
        f"realm={config['domain']}", "fingerprint", "use-auth-secret",
        f"static-auth-secret={secret}", "stale-nonce=600",
        "cert=/etc/coturn/tls/fullchain.pem", "pkey=/etc/coturn/tls/privkey.pem",
        # coturn 4.18 uses opt-in flags; the old no-tlsv1/no-dtls names no longer exist.
        "tlsv1=0", "tlsv1_1=0", "dtls=0", "no-tcp-relay", "cli=0",
        "no-multicast-peers", "user-quota=12", "total-quota=24",
        "max-bps=625000", "bps-capacity=2500000", "relay-threads=1",
        "log-file=stdout", "simple-log", "pidfile=/tmp/turnserver.pid",
        "denied-peer-ip=0.0.0.0-0.255.255.255",
        "denied-peer-ip=10.0.0.0-10.255.255.255",
        "denied-peer-ip=100.64.0.0-100.127.255.255",
        "denied-peer-ip=127.0.0.0-127.255.255.255",
        "denied-peer-ip=169.254.0.0-169.254.255.255",
        "denied-peer-ip=172.16.0.0-172.31.255.255",
        "denied-peer-ip=192.168.0.0-192.168.255.255",
        "denied-peer-ip=::1", "denied-peer-ip=fc00::-fdff:ffff:ffff:ffff:ffff:ffff:ffff:ffff",
        "denied-peer-ip=fe80::-febf:ffff:ffff:ffff:ffff:ffff:ffff:ffff",
    ]
    if config["public_ip"] != config["local_ip"]:
        lines.insert(3, f"external-ip={config['public_ip']}/{config['local_ip']}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=Path(__file__).with_name(".env"))
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    config = validate(read_env(args.env))
    verify_tls(config)
    if args.check_only:
        print("TURN_SETTINGS_OK: имя, IPv4, порты, сертификат и ключ проверены; сеть и relay не проверялись.")
        return 0
    if os.geteuid() != 0:
        raise ValueError("Для записи конфигурации нужен root; для проверки используйте --check-only")
    state = config["state"]
    assert isinstance(state, Path)
    if state.is_symlink():
        raise ValueError("TURN_STATE_DIR не должен быть символической ссылкой")
    state.mkdir(parents=True, exist_ok=True)
    os.chown(state, 0, 65534)
    os.chmod(state, 0o750)
    secret_file = state / "turn-secret"
    if secret_file.is_symlink():
        raise ValueError("Файл TURN-секрета не должен быть символической ссылкой")
    if secret_file.exists():
        secret = secret_file.read_text().strip()
        if not re.fullmatch(r"[a-f0-9]{64}", secret):
            raise ValueError("Существующий turn-secret имеет неверный формат; не заменяем его автоматически")
        os.chown(secret_file, 0, 0)
        os.chmod(secret_file, 0o600)
    else:
        secret = secrets.token_hex(32)
        atomic_write(secret_file, (secret + "\n").encode(), 0o600)
    tls_dir = state / "tls"
    if tls_dir.is_symlink():
        raise ValueError("Каталог TLS не должен быть символической ссылкой")
    tls_dir.mkdir(exist_ok=True)
    os.chown(tls_dir, 0, 65534)
    os.chmod(tls_dir, 0o750)
    atomic_write(tls_dir / "fullchain.pem", Path(config["cert"]).read_bytes(), 0o640, 65534)
    atomic_write(tls_dir / "privkey.pem", Path(config["key"]).read_bytes(), 0o640, 65534)
    atomic_write(state / "turnserver.conf", render(config, secret).encode(), 0o640, 65534)
    domain, port, tls_port = config["domain"], config["TURN_PORT"], config["TURN_TLS_PORT"]
    turn_urls = [f"turn:{domain}:{port}?transport=udp", f"turn:{domain}:{port}?transport=tcp",
                 f"turns:{domain}:{tls_port}?transport=tcp"]
    backend = (
        f"VECHER_STUN_URLS='{json.dumps([f'stun:{domain}:{port}'])}'\n"
        f"VECHER_TURN_URLS='{json.dumps(turn_urls)}'\n"
        f"VECHER_TURN_SECRET={secret}\n"
        "VECHER_TURN_CREDENTIAL_TTL_SECONDS=15000\n"
    )
    atomic_write(state / "backend-turn.env", backend.encode(), 0o600)
    print(f"TURN_CONFIG_READY: {state}/turnserver.conf")
    print(f"BACKEND_ENV_READY: {state}/backend-turn.env (содержит секрет; не отправлять в чат/git)")
    print("Сервисы не запускались. Проверьте DNS, firewall, свободные порты и relay с двух сетей.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"TURN_CONFIG_ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
