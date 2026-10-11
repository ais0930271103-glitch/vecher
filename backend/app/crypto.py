"""Шифрование личных данных в базе (AES-256-GCM).

Защищает от утечки самой базы или её бэкапа: в таблицах лежит не «да» и «пионы», а шифротекст.
Ключ — только в .env сервера (VECHER_DATA_KEYS), отдельно от базы и бэкапов.
Это НЕ сквозное шифрование: сервер ключ знает (иначе не работали бы совпадения и подборки).

Формат значения: enc1:<id ключа>:<base64(nonce12 + шифротекст)>.
Несколько ключей — для смены ключа: шифруем первым, расшифровываем любым.
Старые незашифрованные значения читаются как есть и шифруются при запуске (encrypt_existing).
"""
import base64
import hashlib
import os
from functools import lru_cache

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import Text, select, type_coerce
from sqlalchemy.types import TypeDecorator

from .config import get_settings

PREFIX = "enc1:"


@lru_cache
def _keys() -> list[tuple[str, bytes]]:
    s = get_settings()
    raw = (s.data_keys or "").strip()
    if not raw:
        if s.env != "dev":
            raise RuntimeError("VECHER_DATA_KEYS не задан — без ключа шифрования сервер не запускается")
        # Разработка и тесты: ключ из jwt_secret, чтобы ничего не настраивать.
        return [("dev", hashlib.sha256(("data-key:" + s.jwt_secret).encode()).digest())]
    keys = []
    for part in raw.split(","):
        kid, _, b64 = part.strip().partition(":")
        key = base64.b64decode(b64)
        if not kid or len(key) != 32:
            raise RuntimeError("VECHER_DATA_KEYS: ожидается «id:base64-ключ-32-байта[,id2:…]»")
        keys.append((kid, key))
    return keys


def new_key_line(kid: str = "k1") -> str:
    """Сгенерировать строку для .env: python -c "from app.crypto import new_key_line; print(new_key_line())" """
    return f"VECHER_DATA_KEYS={kid}:{base64.b64encode(os.urandom(32)).decode()}"


def encrypt(text: str) -> str:
    kid, key = _keys()[0]
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, text.encode(), kid.encode())
    return f"{PREFIX}{kid}:{base64.b64encode(nonce + ct).decode()}"


def decrypt(value: str) -> str:
    if not value.startswith(PREFIX):
        return value  # старое незашифрованное значение
    kid, _, b64 = value[len(PREFIX):].partition(":")
    key = dict(_keys()).get(kid)
    if key is None:
        raise RuntimeError(f"Нет ключа шифрования «{kid}» — проверь VECHER_DATA_KEYS")
    blob = base64.b64decode(b64)
    return AESGCM(key).decrypt(blob[:12], blob[12:], kid.encode()).decode()


class Encrypted(TypeDecorator):
    """Строковая колонка, которая хранится зашифрованной. В коде — обычная строка."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else encrypt(str(value))

    def process_result_value(self, value, dialect):
        return None if value is None else decrypt(value)


def encrypt_existing(conn) -> int:
    """Зашифровать то, что лежало открытым до появления шифрования. Повторный запуск ничего не меняет."""
    from .db import Base

    done = 0
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if not isinstance(col.type, Encrypted):
                continue
            raw = type_coerce(col, Text)
            pk = list(table.primary_key.columns)
            rows = conn.execute(
                select(*pk, raw.label("raw")).where(raw.is_not(None), raw.not_like(PREFIX + "%"))
            ).all()
            for row in rows:
                cond = [c == row._mapping[c.name] for c in pk]
                conn.execute(table.update().where(*cond).values({col.name: row._mapping["raw"]}))
                done += 1
    return done
