import os
import sqlite3

LASTFM_DB = os.environ.get("LASTFM_DB", "/data/lastfm_stats.db")
MUSICA_LOCAL_DB = os.environ.get("MUSICA_LOCAL_DB", "/musica_local.sqlite")
EXPLORER_DB = os.environ.get("EXPLORER_DB", "/app/explorer.db")

EXPLORER_SCHEMA = """
CREATE TABLE IF NOT EXISTS album_enrichment (
    lastfm_album_id INTEGER PRIMARY KEY,
    release_date    TEXT,
    label           TEXT,
    genres_json     TEXT,
    mb_release_id   TEXT,
    match_source    TEXT NOT NULL,
    fetched_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_enrichment_source ON album_enrichment(match_source);
"""


def init_explorer_db(path: str = EXPLORER_DB) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(EXPLORER_SCHEMA)
    conn.commit()
    conn.close()


def get_lastfm_conn(readonly: bool = True) -> sqlite3.Connection:
    uri = f"file:{LASTFM_DB}?mode=ro" if readonly else LASTFM_DB
    conn = sqlite3.connect(uri, uri=readonly)
    conn.row_factory = sqlite3.Row
    return conn


def get_musica_local_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{MUSICA_LOCAL_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def get_explorer_conn(readonly: bool = False) -> sqlite3.Connection:
    init_explorer_db()
    uri = f"file:{EXPLORER_DB}?mode=ro" if readonly else EXPLORER_DB
    conn = sqlite3.connect(uri, uri=readonly)
    conn.row_factory = sqlite3.Row
    return conn


def get_search_conn() -> sqlite3.Connection:
    """Conexión de solo lectura sobre lastfm_stats.db con explorer.db adjunta
    como esquema 'enrich' para poder hacer JOIN entre ambas bases."""
    init_explorer_db()
    conn = sqlite3.connect(f"file:{LASTFM_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("ATTACH DATABASE ? AS enrich", (f"file:{EXPLORER_DB}?mode=ro",))
    return conn
