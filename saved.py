"""
saved.py

Per-account list of saved cards. A card is saved per printing: the same
product can be saved once for each printing (e.g. Normal and Reverse Holofoil).

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

_COLUMNS = """
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
    PRIMARY KEY (username, product_id, printing)
"""
_SCHEMA = f"CREATE TABLE IF NOT EXISTS saved_cards ({_COLUMNS})"

_COPY_COLUMNS = ("username, product_id, name, set_name, number, rarity, set_id, "
                 "market_price, printing, language, saved_at")


def _norm_printing(printing):
    return (printing or "").strip()


def _migrate_primary_key(conn):
    """
    Older databases have PRIMARY KEY (username, product_id), which allows only
    one saved row per card. Widen it to include printing. Existing rows keep
    their printing value, so nothing is lost. Safe to run repeatedly.
    """
    if USE_POSTGRES:
        cur = _execute(conn, """
            SELECT a.attname AS col
            FROM pg_index i
            JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
            WHERE i.indrelid = 'saved_cards'::regclass AND i.indisprimary""")
        pk_cols = {r["col"] for r in cur.fetchall()}
        if "printing" not in pk_cols:
            _execute(conn, "ALTER TABLE saved_cards DROP CONSTRAINT IF EXISTS saved_cards_pkey")
            _execute(conn, "ALTER TABLE saved_cards ADD PRIMARY KEY (username, product_id, printing)")
            conn.commit()
    else:
        info = conn.execute("PRAGMA table_info(saved_cards)").fetchall()
        pk_cols = {r["name"] for r in info if r["pk"]}
        if "printing" not in pk_cols:
            conn.execute("ALTER TABLE saved_cards RENAME TO saved_cards_old")
            conn.execute(f"CREATE TABLE saved_cards ({_COLUMNS})")
            conn.execute(f"INSERT INTO saved_cards ({_COPY_COLUMNS}) "
                         f"SELECT {_COPY_COLUMNS} FROM saved_cards_old")
            conn.execute("DROP TABLE saved_cards_old")
            conn.commit()


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
                _migrate_primary_key(conn)
                _initialized = True
    return conn


def _execute(conn, sql, params=()):
    """Run one statement; returns a cursor whose rows can be read as dicts."""
    if USE_POSTGRES:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        return cur
    return conn.execute(sql, params)


def is_saved(username, product_id, printing=""):
    with closing(_connect()) as conn:
        row = _execute(
            conn,
            f"SELECT 1 FROM saved_cards WHERE username = {_PH} AND product_id = {_PH} "
            f"AND printing = {_PH}",
            (username, int(product_id), _norm_printing(printing))).fetchone()
    return row is not None


def add(username, card):
    """card: dict with product_id and optional name, set_name, number, rarity,
    set_id, market_price, printing, language. Saving the same card + printing
    twice keeps the original date."""
    with closing(_connect()) as conn:
        _execute(
            conn,
            f"""INSERT INTO saved_cards
                (username, product_id, name, set_name, number, rarity, set_id,
                 market_price, printing, language, saved_at)
                VALUES ({', '.join([_PH] * 11)})
                ON CONFLICT (username, product_id, printing) DO NOTHING""",
            (username, int(card["product_id"]), card.get("name") or "",
             card.get("set_name") or "", card.get("number") or "",
             card.get("rarity") or "", card.get("set_id"), card.get("market_price"),
             _norm_printing(card.get("printing")), card.get("language") or "en",
             datetime.now(timezone.utc).isoformat(timespec="seconds")))
        conn.commit()


def remove(username, product_id, printing=""):
    with closing(_connect()) as conn:
        _execute(conn,
                 f"DELETE FROM saved_cards WHERE username = {_PH} AND product_id = {_PH} "
                 f"AND printing = {_PH}",
                 (username, int(product_id), _norm_printing(printing)))
        conn.commit()


def list_cards(username):
    """Saved cards, newest first, as plain dicts."""
    with closing(_connect()) as conn:
        rows = _execute(
            conn,
            f"SELECT * FROM saved_cards WHERE username = {_PH} "
            "ORDER BY saved_at DESC, product_id DESC, printing",
            (username,)).fetchall()
    return [dict(r) for r in rows]
