#!/usr/bin/env python3
import json
import os
from datetime import datetime, timezone

from flask import Flask, jsonify, request, render_template_string

from db import get_search_conn, get_lastfm_conn, get_explorer_conn

app = Flask(__name__)

PER_PAGE = 50


def _int_or(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _date_to_ts(d: str, end_of_day: bool = False) -> int:
    dt = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    ts = int(dt.timestamp())
    return ts + 86399 if end_of_day else ts


def build_search(args) -> tuple:
    where = []
    params = []

    if args.get("listened_from"):
        where.append("s.ts >= ?")
        params.append(_date_to_ts(args["listened_from"]))
    if args.get("listened_to"):
        where.append("s.ts <= ?")
        params.append(_date_to_ts(args["listened_to"], end_of_day=True))
    if args.get("release_from"):
        where.append("substr(e.release_date,1,4) >= ?")
        params.append(args["release_from"][:4])
    if args.get("release_to"):
        where.append("substr(e.release_date,1,4) <= ?")
        params.append(args["release_to"][:4])
    if args.get("artist"):
        where.append("a.name LIKE ?")
        params.append(f"%{args['artist']}%")
    if args.get("album"):
        where.append("al.name LIKE ?")
        params.append(f"%{args['album']}%")
    if args.get("track"):
        where.append("s.track LIKE ?")
        params.append(f"%{args['track']}%")
    if args.get("label"):
        where.append("e.label LIKE ?")
        params.append(f"%{args['label']}%")
    if args.get("genre"):
        where.append(
            "(e.genres_json LIKE ? OR EXISTS ("
            "SELECT 1 FROM artist_genres ag JOIN genres g ON g.genre_id = ag.genre_id "
            "WHERE ag.artist_id = a.artist_id AND g.name LIKE ?))"
        )
        params.append(f"%{args['genre']}%")
        params.append(f"%{args['genre']}%")

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    return where_sql, params


BASE_QUERY = """
FROM scrobbles s
JOIN artists a ON a.artist_id = s.artist_id
LEFT JOIN albums al ON al.album_id = s.album_id
LEFT JOIN enrich.album_enrichment e ON e.lastfm_album_id = al.album_id
"""


@app.route("/api/search")
def api_search():
    page = max(1, _int_or(request.args.get("page"), 1))
    per_page = min(200, max(1, _int_or(request.args.get("per_page"), PER_PAGE)))
    where_sql, params = build_search(request.args)

    conn = get_search_conn()
    total = conn.execute(f"SELECT COUNT(*) {BASE_QUERY} {where_sql}", params).fetchone()[0]
    rows = conn.execute(
        f"""
        SELECT s.scrobble_id, s.track, s.ts, s.ts_iso, s.source,
               a.name AS artist, al.name AS album,
               e.release_date, e.label, e.genres_json,
               (SELECT GROUP_CONCAT(g.name, '||') FROM artist_genres ag
                JOIN genres g ON g.genre_id = ag.genre_id
                WHERE ag.artist_id = a.artist_id) AS artist_genres
        {BASE_QUERY}
        {where_sql}
        ORDER BY s.ts DESC
        LIMIT ? OFFSET ?
        """,
        [*params, per_page, (page - 1) * per_page],
    ).fetchall()
    conn.close()

    items = []
    for r in rows:
        genres = []
        if r["genres_json"]:
            try:
                genres = json.loads(r["genres_json"])
            except (json.JSONDecodeError, TypeError):
                pass
        if r["artist_genres"]:
            for g in r["artist_genres"].split("||"):
                if g not in genres:
                    genres.append(g)
        items.append({
            "artist": r["artist"], "album": r["album"], "track": r["track"],
            "listened_at": r["ts_iso"], "source": r["source"],
            "release_date": r["release_date"], "label": r["label"], "genres": genres,
        })

    return jsonify({"total": total, "page": page, "per_page": per_page, "items": items})


@app.route("/api/filters/meta")
def api_filters_meta():
    conn = get_search_conn()
    genres = {r["name"] for r in conn.execute("SELECT DISTINCT name FROM genres ORDER BY name")}
    for r in conn.execute("SELECT genres_json FROM enrich.album_enrichment WHERE genres_json IS NOT NULL"):
        try:
            genres.update(json.loads(r["genres_json"]))
        except (json.JSONDecodeError, TypeError):
            pass
    labels = {
        r["label"] for r in conn.execute(
            "SELECT DISTINCT label FROM enrich.album_enrichment WHERE label IS NOT NULL AND label != ''"
        )
    }
    conn.close()
    return jsonify({"genres": sorted(g for g in genres if g), "labels": sorted(labels)})


@app.route("/api/stats")
def api_stats():
    conn = get_search_conn()
    total_scrobbles = conn.execute("SELECT COUNT(*) FROM scrobbles").fetchone()[0]
    total_albums = conn.execute("SELECT COUNT(*) FROM albums").fetchone()[0]
    by_source = dict(conn.execute(
        "SELECT match_source, COUNT(*) FROM enrich.album_enrichment GROUP BY match_source"
    ).fetchall())
    conn.close()
    return jsonify({
        "total_scrobbles": total_scrobbles,
        "total_albums": total_albums,
        "enrichment_by_source": by_source,
    })


@app.route("/health")
def health():
    return jsonify({"ok": True})


HTML_PAGE = """
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Explorador de Escuchas</title>
<style>
:root {
    --bg: #0a0e27; --surface: #16213e; --surface-2: #1f2d4a; --border: #2d3e5f;
    --text: #b0b8c9; --text-muted: #7a8694; --accent: #35bf88; --accent-2: #8e44ad;
    --warning: #f39c12; --danger: #e74c3c;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
    background-color: var(--bg); color: var(--text); padding: 20px;
}
h1 { color: #fff; margin-bottom: 16px; font-size: 20px; }
.filters {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: 10px; background: var(--surface); padding: 16px; border-radius: 10px;
    border: 1px solid var(--border); margin-bottom: 16px;
}
.filters label { font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 4px; }
.filters input {
    width: 100%; padding: 6px 8px; background: var(--surface-2); border: 1px solid var(--border);
    border-radius: 6px; color: var(--text); font-size: 13px;
}
.filters input:focus { outline: none; border-color: var(--accent); }
.actions { grid-column: 1 / -1; display: flex; gap: 8px; }
button {
    background: var(--accent); color: var(--bg); border: none; padding: 8px 16px;
    border-radius: 6px; font-weight: bold; cursor: pointer; font-size: 13px;
}
button.secondary { background: var(--surface-2); color: var(--text); }
#summary { color: var(--text-muted); font-size: 13px; margin-bottom: 10px; }
table { width: 100%; border-collapse: collapse; background: var(--surface); border-radius: 10px; overflow: hidden; }
th, td { padding: 8px 10px; text-align: left; border-bottom: 1px solid var(--border); font-size: 13px; }
th { color: var(--text-muted); font-weight: 600; text-transform: uppercase; font-size: 11px; }
tr:hover td { background: var(--surface-2); }
.badge { display: inline-block; background: var(--surface-2); color: var(--text-muted); padding: 1px 6px; border-radius: 4px; font-size: 11px; margin-right: 3px; }
.pagination { display: flex; gap: 6px; justify-content: center; margin-top: 14px; }
.pagination button { background: var(--surface-2); color: var(--text); padding: 6px 10px; }
.pagination button.active { background: var(--accent); color: var(--bg); }
.pagination button:disabled { opacity: 0.4; cursor: default; }
</style>
</head>
<body>
<h1>Explorador de Escuchas — REPEAT-ONE</h1>
<div class="filters">
    <div><label>Escuchado desde</label><input type="date" id="f_listened_from"></div>
    <div><label>Escuchado hasta</label><input type="date" id="f_listened_to"></div>
    <div><label>Lanzamiento desde (año)</label><input type="number" id="f_release_from" placeholder="1990"></div>
    <div><label>Lanzamiento hasta (año)</label><input type="number" id="f_release_to" placeholder="2026"></div>
    <div><label>Artista</label><input type="text" id="f_artist"></div>
    <div><label>Disco</label><input type="text" id="f_album"></div>
    <div><label>Canción</label><input type="text" id="f_track"></div>
    <div><label>Género</label><input type="text" id="f_genre" list="genres_list"></div>
    <div><label>Sello</label><input type="text" id="f_label" list="labels_list"></div>
    <datalist id="genres_list"></datalist>
    <datalist id="labels_list"></datalist>
    <div class="actions">
        <button onclick="search(1)">Buscar</button>
        <button class="secondary" onclick="clearFilters()">Limpiar</button>
    </div>
</div>
<div id="summary"></div>
<table>
    <thead><tr><th>Escuchado</th><th>Artista</th><th>Disco</th><th>Canción</th><th>Sello</th><th>Lanzamiento</th><th>Género</th><th>Fuente</th></tr></thead>
    <tbody id="results"></tbody>
</table>
<div class="pagination" id="pagination"></div>

<script>
let currentPage = 1, totalPages = 1;

function escapeHtml(s) {
    const d = document.createElement('div');
    d.textContent = s || '';
    return d.innerHTML;
}

async function loadMeta() {
    const r = await fetch('/api/filters/meta');
    const data = await r.json();
    document.getElementById('genres_list').innerHTML = data.genres.map(g => `<option value="${escapeHtml(g)}">`).join('');
    document.getElementById('labels_list').innerHTML = data.labels.map(l => `<option value="${escapeHtml(l)}">`).join('');
}

function currentFilters() {
    const ids = ['listened_from', 'listened_to', 'release_from', 'release_to', 'artist', 'album', 'track', 'genre', 'label'];
    const params = new URLSearchParams();
    for (const id of ids) {
        const v = document.getElementById('f_' + id).value.trim();
        if (v) params.set(id, v);
    }
    return params;
}

function clearFilters() {
    document.querySelectorAll('.filters input').forEach(i => i.value = '');
    search(1);
}

async function search(page) {
    currentPage = page;
    const params = currentFilters();
    params.set('page', page);
    const r = await fetch('/api/search?' + params.toString());
    const data = await r.json();
    totalPages = Math.max(1, Math.ceil(data.total / data.per_page));

    document.getElementById('summary').textContent = `${data.total} escuchas encontradas`;
    document.getElementById('results').innerHTML = data.items.map(it => `
        <tr>
            <td>${new Date(it.listened_at).toLocaleString('es-ES')}</td>
            <td>${escapeHtml(it.artist)}</td>
            <td>${escapeHtml(it.album || '')}</td>
            <td>${escapeHtml(it.track)}</td>
            <td>${escapeHtml(it.label || '')}</td>
            <td>${escapeHtml(it.release_date || '')}</td>
            <td>${it.genres.map(g => `<span class="badge">${escapeHtml(g)}</span>`).join('')}</td>
            <td>${escapeHtml(it.source)}</td>
        </tr>
    `).join('');

    renderPagination();
}

function renderPagination() {
    const el = document.getElementById('pagination');
    let html = `<button ${currentPage <= 1 ? 'disabled' : ''} onclick="search(${currentPage - 1})">&laquo;</button>`;
    const start = Math.max(1, currentPage - 3), end = Math.min(totalPages, currentPage + 3);
    for (let p = start; p <= end; p++) {
        html += `<button class="${p === currentPage ? 'active' : ''}" onclick="search(${p})">${p}</button>`;
    }
    html += `<button ${currentPage >= totalPages ? 'disabled' : ''} onclick="search(${currentPage + 1})">&raquo;</button>`;
    el.innerHTML = html;
}

loadMeta();
search(1);
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(HTML_PAGE)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
