"""Хранилище бота в Postgres (DATABASE_URL). Переписку не храним: только связки и журнал сессий (ID, время, счётчики)."""
import os
import ssl
from urllib.parse import unquote, urlparse

import pg8000.native

SCHEMA = """
CREATE TABLE IF NOT EXISTS doctor_invites (
    code_hash TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS doctors (
    tg_id BIGINT PRIMARY KEY,
    name TEXT NOT NULL,
    link_hash TEXT UNIQUE NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS patients (
    tg_id BIGINT PRIMARY KEY,
    doctor_id BIGINT NOT NULL REFERENCES doctors(tg_id) ON DELETE CASCADE,
    state TEXT NOT NULL,
    full_name TEXT,
    phone TEXT,
    last_msg_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS consent_log (
    id BIGSERIAL PRIMARY KEY,
    tg_id BIGINT NOT NULL,
    action TEXT NOT NULL,
    at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS msg_map (
    doctor_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    patient_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (doctor_id, message_id)
);
ALTER TABLE patients ADD COLUMN IF NOT EXISTS preapproved BOOLEAN NOT NULL DEFAULT false;
CREATE TABLE IF NOT EXISTS patient_invites (
    code_hash TEXT PRIMARY KEY,
    doctor_id BIGINT NOT NULL REFERENCES doctors(tg_id) ON DELETE CASCADE,
    full_name TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS chat_sessions (
    day DATE NOT NULL,
    doctor_id BIGINT NOT NULL,
    patient_id BIGINT NOT NULL,
    first_at TIMESTAMPTZ NOT NULL,
    last_at TIMESTAMPTZ NOT NULL,
    from_patient INT NOT NULL DEFAULT 0,
    from_doctor INT NOT NULL DEFAULT 0,
    PRIMARY KEY (day, doctor_id, patient_id)
);
CREATE TABLE IF NOT EXISTS admin_logins (
    code_hash TEXT PRIMARY KEY,
    admin_id BIGINT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS admin_sessions (
    token_hash TEXT PRIMARY KEY,
    admin_id BIGINT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);
"""

_conn = None


def _connect():
    url = urlparse(os.environ["DATABASE_URL"])
    # Внутренний адрес Render работает без TLS, внешний требует его.
    use_ssl = "." in (url.hostname or "") and url.hostname != "localhost"
    return pg8000.native.Connection(
        user=unquote(url.username or ""),
        password=unquote(url.password or ""),
        host=url.hostname,
        port=url.port or 5432,
        database=url.path.lstrip("/"),
        ssl_context=ssl.create_default_context() if use_ssl else None,
    )


def run(sql, **params):
    """Выполняет запрос и возвращает строки как словари. Переподключается при обрыве."""
    global _conn
    for attempt in range(2):
        try:
            if _conn is None:
                _conn = _connect()
            rows = _conn.run(sql, **params)
            cols = [c["name"] for c in (_conn.columns or [])]
            return [dict(zip(cols, r)) for r in rows or []]
        except (pg8000.exceptions.InterfaceError, OSError):
            _conn = None
            if attempt:
                raise


def one(sql, **params):
    rows = run(sql, **params)
    return rows[0] if rows else None


def init():
    for statement in SCHEMA.split(";"):
        if statement.strip():
            run(statement)
