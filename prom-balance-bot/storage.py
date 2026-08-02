"""Хранение состояния (SQLite): последний баланс, уровень алерта, обработанные заказы."""
import sqlite3
import time
from contextlib import contextmanager

import config


@contextmanager
def _conn():
    con = sqlite3.connect(config.DB_FILE)
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init():
    with _conn() as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS seen_orders (order_id TEXT PRIMARY KEY, ts REAL)"
        )


def get(key: str, default=None):
    with _conn() as con:
        row = con.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set(key: str, value):
    with _conn() as con:
        con.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def get_float(key: str, default: float = 0.0) -> float:
    v = get(key)
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# --- заказы для режима оценки по API ---

def is_order_seen(order_id: str) -> bool:
    with _conn() as con:
        row = con.execute(
            "SELECT 1 FROM seen_orders WHERE order_id=?", (str(order_id),)
        ).fetchone()
    return row is not None


def mark_order_seen(order_id: str):
    with _conn() as con:
        con.execute(
            "INSERT OR IGNORE INTO seen_orders(order_id, ts) VALUES(?, ?)",
            (str(order_id), time.time()),
        )


def seen_orders_count() -> int:
    with _conn() as con:
        return con.execute("SELECT COUNT(*) FROM seen_orders").fetchone()[0]
