# lastfm_explorer

Web de búsqueda sobre el historial de escuchas de Last.fm/ListenBrainz.

## Arquitectura

Servicio de solo lectura (más un job de enriquecimiento). No genera ni
modifica `lastfm_stats.db`; solo lo lee.

- `db.py` — conexiones a las 3 bases: `lastfm_stats.db` (viva, compartida,
  rellenada por el servicio `lastfm-scrobbles` de repeat-one),
  `musica_local.sqlite` (biblioteca local de music-fuzzy, misma que usa
  `sellos-discograficos`) y `explorer.db` (caché propia de este servicio).
- `normalize.py` — normalización (minúsculas + sin acentos) compartida
  entre `enrich_albums.py` y `server.py`. Es distinta de la normalización
  de `lastfm_stats.db` (que no quita acentos) precisamente porque se usa
  para cruzar esa base con `musica_local.sqlite`, que sí los normaliza de
  otra forma — sin esto, nombres como "Café Tacvba" no cruzan.
- `enrich_albums.py` — job de enriquecimiento (biblioteca local primero,
  MusicBrainz de fallback, cacheado en `explorer.db`). Corre como job
  diario de Ofelia en producción; hay que lanzar `--full` a mano una vez
  tras el primer despliegue para el escaneo inicial completo.
- `server.py` — Flask, una sola plantilla HTML con JS vanilla (mismo
  patrón que el resto de servicios del monorepo), sin build step ni
  frontend aparte.

## Por qué no hay cliente de Discogs

Ninguna base ni servicio del repo tenía ya un cliente de Discogs
reutilizable. El release de MusicBrainz (`get_release_by_id(includes=
["labels"])`) ya trae el sello en la mayoría de los casos, así que el
fallback se quedó solo en MusicBrainz. Si en el futuro hace falta más
cobertura de sellos, `services/mixxx_info/src/enricher.py::_discogs_enrich`
es la plantilla a seguir (usa el mismo `DISCOGS_TOKEN` que ya existe en
los secrets de `mixxx_info`/`charts_orpheus`).
