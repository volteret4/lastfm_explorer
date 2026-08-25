#!/usr/bin/env python3
"""
Escaneo/enriquecimiento de álbumes: rellena explorer.db.album_enrichment con
fecha de lanzamiento, sello y género para cada álbum de lastfm_stats.db.

Orden de fuentes:
  1. musica_local.sqlite (biblioteca local de music-fuzzy, ya usada por
     sellos-discograficos) — match por nombre normalizado, sin red.
  2. MusicBrainz (musicbrainzngs) — fallback cuando no hay match local.
  3. Si tampoco hay nada: fila con match_source='none' (no se reintenta
     hasta pasados RETRY_NONE_DAYS días).

Uso:
    python3 enrich_albums.py            # incremental: solo álbumes sin fila
                                         # (o 'none' caducados)
    python3 enrich_albums.py --full     # fuerza reprocesar todo
"""
import argparse
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import musicbrainzngs

from db import get_lastfm_conn, get_musica_local_conn, get_explorer_conn, init_explorer_db
from normalize import normalize

MB_RATE_LIMIT = 1.1
MB_MAX_RETRIES = 5
RETRY_NONE_DAYS = 30

musicbrainzngs.set_useragent("lastfm_explorer", "0.1", "contacto@example.org")

_mb_last_call = 0.0


def _mb_throttle():
    global _mb_last_call
    elapsed = time.monotonic() - _mb_last_call
    if elapsed < MB_RATE_LIMIT:
        time.sleep(MB_RATE_LIMIT - elapsed)
    _mb_last_call = time.monotonic()


def _mb_call(fn, *args, **kwargs):
    for attempt in range(MB_MAX_RETRIES):
        try:
            _mb_throttle()
            return fn(*args, **kwargs)
        except musicbrainzngs.NetworkError:
            time.sleep(2 ** attempt)
        except musicbrainzngs.ResponseError:
            return None
    return None


def _best_release(releases: list) -> dict:
    status_rank = {"Official": 0, "Promotion": 1, "Bootleg": 2}
    return min(
        releases,
        key=lambda r: (status_rank.get(r.get("status", ""), 9), r.get("date", "9999")),
        default={},
    )


def _mb_enrich(artist: str, album: str) -> dict:
    result = _mb_call(musicbrainzngs.search_release_groups, artist=artist, releasegroup=album, limit=1)
    if not result:
        return {}
    rgs = result.get("release-group-list", [])
    if not rgs:
        return {}
    rg = rgs[0]
    data = {"mb_release_group_id": rg.get("id")}

    tags = sorted(rg.get("tag-list", []), key=lambda t: int(t.get("count", 0)), reverse=True)
    if tags:
        data["genres"] = [t["name"] for t in tags[:10]]

    details = _mb_call(musicbrainzngs.get_release_group_by_id, rg["id"], includes=["releases"])
    releases = (details or {}).get("release-group", {}).get("release-list", [])
    best = _best_release(releases)
    release_mbid = best.get("id")
    if not release_mbid:
        return data

    data["mb_release_id"] = release_mbid
    rel_details = _mb_call(musicbrainzngs.get_release_by_id, release_mbid, includes=["labels"])
    rel = (rel_details or {}).get("release", {})
    data["release_date"] = rel.get("date") or None

    label_info = rel.get("label-info-list", [])
    if label_info:
        data["label"] = label_info[0].get("label", {}).get("name")

    return data


def _load_local_library(conn) -> dict:
    """artist_norm|album_norm -> {label, release_date, genre}"""
    index = {}
    rows = conn.execute(
        """
        SELECT ar.name AS artist, al.name AS album, al.label, al.year, al.genre
        FROM albums al
        JOIN artists ar ON ar.id = al.artist_id
        """
    ).fetchall()
    for r in rows:
        key = f"{normalize(r['artist'])}|{normalize(r['album'])}"
        index[key] = {"label": r["label"], "release_date": r["year"], "genre": r["genre"]}
    return index


def enrich(full: bool = False):
    init_explorer_db()
    lastfm = get_lastfm_conn()
    explorer = get_explorer_conn()
    local = get_musica_local_conn()

    local_index = _load_local_library(local)
    local.close()

    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETRY_NONE_DAYS)).isoformat()

    if full:
        pending_ids = {r["album_id"] for r in lastfm.execute("SELECT album_id FROM albums")}
    else:
        done = {
            r["lastfm_album_id"]
            for r in explorer.execute(
                "SELECT lastfm_album_id FROM album_enrichment "
                "WHERE match_source != 'none' OR fetched_at > ?",
                (cutoff,),
            )
        }
        all_ids = {r["album_id"] for r in lastfm.execute("SELECT album_id FROM albums")}
        pending_ids = all_ids - done

    print(f"Álbumes a procesar: {len(pending_ids)}")
    n_local = n_mb = n_none = 0

    for album_id in sorted(pending_ids):
        row = lastfm.execute(
            """
            SELECT al.name AS album, ar.name AS artist
            FROM albums al JOIN artists ar ON ar.artist_id = al.artist_id
            WHERE al.album_id = ?
            """,
            (album_id,),
        ).fetchone()
        if not row:
            continue

        key = f"{normalize(row['artist'])}|{normalize(row['album'])}"
        local_hit = local_index.get(key)

        if local_hit and (local_hit["label"] or local_hit["release_date"] or local_hit["genre"]):
            genres = [local_hit["genre"]] if local_hit["genre"] else []
            explorer.execute(
                """
                INSERT INTO album_enrichment
                    (lastfm_album_id, release_date, label, genres_json, mb_release_id, match_source, fetched_at)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(lastfm_album_id) DO UPDATE SET
                    release_date=excluded.release_date, label=excluded.label,
                    genres_json=excluded.genres_json, match_source=excluded.match_source,
                    fetched_at=excluded.fetched_at
                """,
                (album_id, local_hit["release_date"], local_hit["label"],
                 json.dumps(genres, ensure_ascii=False), None, "musica_local",
                 datetime.now(timezone.utc).isoformat()),
            )
            n_local += 1
        else:
            mb = _mb_enrich(row["artist"], row["album"])
            if mb.get("label") or mb.get("release_date") or mb.get("genres"):
                explorer.execute(
                    """
                    INSERT INTO album_enrichment
                        (lastfm_album_id, release_date, label, genres_json, mb_release_id, match_source, fetched_at)
                    VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(lastfm_album_id) DO UPDATE SET
                        release_date=excluded.release_date, label=excluded.label,
                        genres_json=excluded.genres_json, mb_release_id=excluded.mb_release_id,
                        match_source=excluded.match_source, fetched_at=excluded.fetched_at
                    """,
                    (album_id, mb.get("release_date"), mb.get("label"),
                     json.dumps(mb.get("genres", []), ensure_ascii=False),
                     mb.get("mb_release_id"), "musicbrainz",
                     datetime.now(timezone.utc).isoformat()),
                )
                n_mb += 1
            else:
                explorer.execute(
                    """
                    INSERT INTO album_enrichment
                        (lastfm_album_id, match_source, fetched_at)
                    VALUES (?, 'none', ?)
                    ON CONFLICT(lastfm_album_id) DO UPDATE SET
                        match_source='none', fetched_at=excluded.fetched_at
                    """,
                    (album_id, datetime.now(timezone.utc).isoformat()),
                )
                n_none += 1
        explorer.commit()

    print(f"Local: {n_local}  MusicBrainz: {n_mb}  Sin match: {n_none}")
    lastfm.close()
    explorer.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="Reprocesar todos los álbumes, no solo los pendientes")
    args = parser.parse_args()
    enrich(full=args.full)
