"""Normalización de nombres compartida entre enrich_albums.py y server.py.

Distinta (a propósito) de la de lastfm_stats.db (solo minúsculas + espacios,
ver extraer_scrobbles.py:_normalize) y más parecida a la de
dieta_sonora/estadisticas (minúsculas + sin acentos), porque aquí se usa
para cruzar dos bases con convenciones de acentuación distintas.
"""
import re
import unicodedata


def normalize(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.strip().lower()
    return re.sub(r"\s+", " ", s)
