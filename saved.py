"""
saved.py

Per-account list of saved cards.

Storage backend:
  * DATABASE_URL (or POSTGRES_URI) set  -> PostgreSQL (e.g. a Northflank managed addon)
  * otherwise                           -> a local SQLite file (SAVED_DB_PATH, default
                                           "saved_cards.db" next to this file), fine for
                                           running on your own machine.
Only this module knows which one is in use; app.py just calls the functions below.
"""
import os
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone

DATABASE_URL = (os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URI") or "").strip()
USE_POSTGRES = bool(DATABASE_URL)
SQLITE_PATH = os.environ.get("SAVED_DB_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "saved_cards.db")

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras

_PH = "%s" if USE_POSTGRES else "?"   # query placeholder style

_init_lock = threading.Lock()
_initialized = False

_SCHEMA = """
CREATE TABLE IF NOT EXISTS saved_cards (
    username     TEXT    NOT NULL,
    product_id   INTEGER NOT NULL,
    name         TEXT    NOT NULL DEFAULT '',
    set_name     TEXT    NOT NULL DEFAULT '',
    number       TEXT    NOT NULL DEFAULT '',
    rarity       TEXT    NOT NULL DEFAULT '',
    set_id       INTEGER,
    market_price DOUBLE PRECISION,
    printing     TEXT    NOT NULL DEFAULT '',
    language     TEXT    NOT NULL DEFAULT 'en',
    saved_at     TEXT    NOT NULL,
    PRIMARY KEY (username, product_id)
)
"""


def _connect():
    global _initialized
    if USE_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
    else:
        conn = sqlite3.connect(SQLITE_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
    if not _initialized:
        with _init_lock:
            if not _initialized:
                _execute(conn, _SCHEMA)
                conn.commit()
                _initialized = True
    return conn


def _execute(conn, sql, params=()):
    """Run one statement; returns a cursor whose rows can be read as dicts."""
    if USE_POSTGRES:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        return cur
    return conn.execute(sql, params)


def is_saved(username, product_id):
    with closing(_connect()) as conn:
        row = _execute(
            conn,
            f"SELECT 1 FROM saved_cards WHERE username = {_PH} AND product_id = {_PH}",
            (username, int(product_id))).fetchone()
    return row is not None


def add(username, card):
    """card: dict with product_id and optional name, set_name, number, rarity,
    set_id, market_price, printing, language. Saving twice keeps the original date."""
    with closing(_connect()) as conn:
        _execute(
            conn,
            f"""INSERT INTO saved_cards
                (username, product_id, name, set_name, number, rarity, set_id,
                 market_price, printing, language, saved_at)
                VALUES ({', '.join([_PH] * 11)})
                ON CONFLICT (username, product_id) DO NOTHING""",
            (username, int(card["product_id"]), card.get("name") or "",
             card.get("set_name") or "", card.get("number") or "",
             card.get("rarity") or "", card.get("set_id"), card.get("market_price"),
             card.get("printing") or "", card.get("language") or "en",
             datetime.now(timezone.utc).isoformat(timespec="seconds")))
        conn.commit()


def remove(username, product_id):
    with closing(_connect()) as conn:
        _execute(conn,
                 f"DELETE FROM saved_cards WHERE username = {_PH} AND product_id = {_PH}",
                 (username, int(product_id)))
        conn.commit()


def list_cards(username):
    """Saved cards, newest first, as plain dicts."""
    with closing(_connect()) as conn:
        rows = _execute(
            conn,
            f"SELECT * FROM saved_cards WHERE username = {_PH} "
            "ORDER BY saved_at DESC, product_id DESC",
            (username,)).fetchall()
    return [dict(r) for r in rows]
