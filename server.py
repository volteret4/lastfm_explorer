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


from collections import Counter

TOP_LIMIT = 12


@app.route("/api/stats/breakdown")
def api_stats_breakdown():
    dimension = request.args.get("dimension", "timeline")
    where_sql, params = build_search(request.args)
    conn = get_search_conn()

    if dimension == "timeline":
        rows = conn.execute(
            f"""
            SELECT strftime('%Y-%m', datetime(s.ts, 'unixepoch')) AS period, COUNT(*) AS c
            {BASE_QUERY}
            {where_sql}
            GROUP BY period
            ORDER BY period ASC
            """,
            params,
        ).fetchall()
        data = [{"label": r["period"], "count": r["c"]} for r in rows]

    elif dimension == "artist":
        rows = conn.execute(
            f"""
            SELECT a.name AS label, COUNT(*) AS c
            {BASE_QUERY}
            {where_sql}
            GROUP BY a.artist_id
            ORDER BY c DESC
            LIMIT {TOP_LIMIT}
            """,
            params,
        ).fetchall()
        data = [{"label": r["label"], "count": r["c"]} for r in rows]

    elif dimension == "label":
        rows = conn.execute(
            f"""
            SELECT e.label AS label, COUNT(*) AS c
            {BASE_QUERY}
            {where_sql}{' AND' if where_sql else 'WHERE'} e.label IS NOT NULL AND e.label != ''
            GROUP BY e.label
            ORDER BY c DESC
            LIMIT {TOP_LIMIT}
            """,
            params,
        ).fetchall()
        data = [{"label": r["label"], "count": r["c"]} for r in rows]

    elif dimension == "genre":
        rows = conn.execute(
            f"""
            SELECT e.genres_json,
                   (SELECT GROUP_CONCAT(g.name, '||') FROM artist_genres ag
                    JOIN genres g ON g.genre_id = ag.genre_id
                    WHERE ag.artist_id = a.artist_id) AS artist_genres
            {BASE_QUERY}
            {where_sql}
            """,
            params,
        ).fetchall()
        counter = Counter()
        for r in rows:
            genres = set()
            if r["genres_json"]:
                try:
                    genres.update(json.loads(r["genres_json"]))
                except (json.JSONDecodeError, TypeError):
                    pass
            if r["artist_genres"]:
                genres.update(r["artist_genres"].split("||"))
            counter.update(genres)
        data = [{"label": g, "count": c} for g, c in counter.most_common(TOP_LIMIT)]

    else:
        conn.close()
        return jsonify({"error": "dimensión desconocida"}), 400

    conn.close()
    return jsonify({"dimension": dimension, "data": data})


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

.charts { display: flex; flex-direction: column; gap: 14px; margin-bottom: 16px; }
.charts-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 14px; }
.chart-card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 14px; }
.chart-card h3 { font-size: 12px; text-transform: uppercase; letter-spacing: 0.4px; color: var(--text-muted); font-weight: 600; margin-bottom: 10px; }
.chart-card svg { display: block; width: 100%; overflow: visible; }
.chart-empty { color: var(--text-muted); font-size: 12px; padding: 20px 0; text-align: center; }
.chart-bar { fill: var(--accent); cursor: pointer; transition: opacity 0.15s; }
.chart-bar:hover, .chart-bar.hovered { opacity: 0.75; }
.chart-axis-line { stroke: var(--border); stroke-width: 1; }
.chart-tick { fill: var(--text-muted); font-size: 10px; }
.chart-label { fill: var(--text-muted); font-size: 11px; }
.chart-value { fill: var(--text); font-size: 11px; font-weight: 600; }
#chart-tooltip {
    position: fixed; pointer-events: none; background: var(--surface-2); border: 1px solid var(--border);
    border-radius: 6px; padding: 6px 10px; font-size: 12px; color: var(--text); box-shadow: 0 4px 10px rgba(0,0,0,0.4);
    display: none; z-index: 100; white-space: nowrap;
}
#chart-tooltip .tt-value { color: #fff; font-weight: 700; }
#chart-tooltip .tt-label { color: var(--text-muted); }
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
<div class="charts">
    <div class="chart-card">
        <h3>Escuchas en el tiempo</h3>
        <div id="chart-timeline"></div>
    </div>
    <div class="charts-row">
        <div class="chart-card">
            <h3>Top artistas</h3>
            <div id="chart-artist"></div>
        </div>
        <div class="chart-card">
            <h3>Top géneros</h3>
            <div id="chart-genre"></div>
        </div>
        <div class="chart-card">
            <h3>Top sellos</h3>
            <div id="chart-label"></div>
        </div>
    </div>
</div>
<div id="chart-tooltip"></div>
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
    loadCharts();
}

// ── Gráficos ────────────────────────────────────────────────────────────────

const tooltipEl = document.getElementById('chart-tooltip');

function showTooltip(evt, label, value) {
    tooltipEl.innerHTML = '';
    const valSpan = document.createElement('span');
    valSpan.className = 'tt-value';
    valSpan.textContent = value;
    const labSpan = document.createElement('span');
    labSpan.className = 'tt-label';
    labSpan.textContent = '  ' + label;
    tooltipEl.appendChild(valSpan);
    tooltipEl.appendChild(labSpan);
    tooltipEl.style.display = 'block';
    tooltipEl.style.left = (evt.clientX + 14) + 'px';
    tooltipEl.style.top = (evt.clientY + 14) + 'px';
}

function hideTooltip() {
    tooltipEl.style.display = 'none';
}

function niceMax(n) {
    if (n <= 0) return 1;
    const mag = Math.pow(10, Math.floor(Math.log10(n)));
    const steps = [1, 2, 2.5, 5, 10];
    for (const s of steps) {
        if (n <= s * mag) return s * mag;
    }
    return 10 * mag;
}

// Rectángulo con el extremo del dato redondeado y la base cuadrada — nunca al revés.
function barShape(x, y, w, h, dir) {
    const r = 4;
    if (dir === 'right') {
        w = Math.max(w, 1);
        const patch = Math.min(r, w);
        return `<rect class="chart-bar" x="${x}" y="${y}" width="${w}" height="${h}" rx="${r}" ry="${r}"></rect>` +
               `<rect x="${x}" y="${y}" width="${patch}" height="${h}" fill="var(--accent)" pointer-events="none"></rect>`;
    }
    h = Math.max(h, 1);
    const patch = Math.min(r, h);
    return `<rect class="chart-bar" x="${x}" y="${y}" width="${w}" height="${h}" rx="${r}" ry="${r}"></rect>` +
           `<rect x="${x}" y="${y + h - patch}" width="${w}" height="${patch}" fill="var(--accent)" pointer-events="none"></rect>`;
}

function truncate(s, n) {
    return s.length > n ? s.slice(0, n - 1) + '…' : s;
}

function renderTopBars(containerId, dimension, data, filterId) {
    const el = document.getElementById(containerId);
    if (!data.length) { el.innerHTML = '<div class="chart-empty">Sin datos todavía</div>'; return; }

    const rowH = 26, barH = 14, labelW = 96, valueW = 46, width = 600;
    const plotW = width - labelW - valueW;
    const height = data.length * rowH;
    const max = data[0].count;

    let svg = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Gráfico de ${dimension}">`;
    svg += `<line class="chart-axis-line" x1="${labelW}" y1="0" x2="${labelW}" y2="${height}"></line>`;
    data.forEach((d, i) => {
        const y = i * rowH + (rowH - barH) / 2;
        const w = (d.count / max) * plotW;
        svg += `<text class="chart-label" x="${labelW - 8}" y="${i * rowH + rowH / 2 + 4}" text-anchor="end">${escapeHtml(truncate(d.label, 16))}<title>${escapeHtml(d.label)}</title></text>`;
        svg += `<g data-idx="${i}">`;
        svg += barShape(labelW, y, w, barH, 'right');
        svg += `<text class="chart-value" x="${labelW + w + 6}" y="${i * rowH + rowH / 2 + 4}">${d.count.toLocaleString('es-ES')}</text>`;
        svg += `</g>`;
    });
    svg += `</svg>`;
    el.innerHTML = svg;

    el.querySelectorAll('g[data-idx]').forEach(g => {
        const d = data[parseInt(g.dataset.idx)];
        g.addEventListener('pointermove', evt => showTooltip(evt, d.label, d.count.toLocaleString('es-ES')));
        g.addEventListener('pointerleave', hideTooltip);
        g.addEventListener('click', () => {
            document.getElementById('f_' + filterId).value = d.label;
            search(1);
        });
        g.style.cursor = 'pointer';
    });
}

function renderTimeline(containerId, data) {
    const el = document.getElementById(containerId);
    if (!data.length) { el.innerHTML = '<div class="chart-empty">Sin datos todavía</div>'; return; }

    const width = 900, height = 160, padL = 34, padB = 20, padT = 6;
    const plotW = width - padL - 10, plotH = height - padB - padT;
    const max = niceMax(Math.max(...data.map(d => d.count)));
    const barGap = 2;
    const barW = Math.max((plotW / data.length) - barGap, 1);

    let svg = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Escuchas por mes">`;
    [0, 0.5, 1].forEach(f => {
        const y = padT + plotH * (1 - f);
        svg += `<line class="chart-axis-line" x1="${padL}" y1="${y}" x2="${width - 10}" y2="${y}"></line>`;
        svg += `<text class="chart-tick" x="${padL - 6}" y="${y + 3}" text-anchor="end">${Math.round(max * f).toLocaleString('es-ES')}</text>`;
    });

    const labelStep = Math.max(1, Math.ceil(data.length / 10));
    data.forEach((d, i) => {
        const x = padL + i * (barW + barGap);
        const h = (d.count / max) * plotH;
        const y = padT + plotH - h;
        svg += `<g data-idx="${i}">`;
        svg += barShape(x, y, barW, h, 'up');
        svg += `</g>`;
        if (i % labelStep === 0) {
            svg += `<text class="chart-tick" x="${x + barW / 2}" y="${height - 4}" text-anchor="middle">${d.label}</text>`;
        }
    });
    svg += `</svg>`;
    el.innerHTML = svg;

    el.querySelectorAll('g[data-idx]').forEach(g => {
        const d = data[parseInt(g.dataset.idx)];
        g.addEventListener('pointermove', evt => showTooltip(evt, d.label, d.count.toLocaleString('es-ES')));
        g.addEventListener('pointerleave', hideTooltip);
        g.addEventListener('click', () => {
            const [y, m] = d.label.split('-');
            const lastDay = new Date(parseInt(y), parseInt(m), 0).getDate();
            document.getElementById('f_listened_from').value = `${d.label}-01`;
            document.getElementById('f_listened_to').value = `${d.label}-${String(lastDay).padStart(2, '0')}`;
            search(1);
        });
        g.style.cursor = 'pointer';
    });
}

async function loadCharts() {
    const params = currentFilters();
    const dims = [
        ['timeline', 'chart-timeline', null],
        ['artist', 'chart-artist', 'artist'],
        ['genre', 'chart-genre', 'genre'],
        ['label', 'chart-label', 'label'],
    ];
    const results = await Promise.all(dims.map(([dim]) => {
        const p = new URLSearchParams(params);
        p.set('dimension', dim);
        return fetch('/api/stats/breakdown?' + p.toString()).then(r => r.json());
    }));
    results.forEach((res, i) => {
        const [dim, containerId, filterId] = dims[i];
        if (dim === 'timeline') renderTimeline(containerId, res.data);
        else renderTopBars(containerId, dim, res.data, filterId);
    });
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
