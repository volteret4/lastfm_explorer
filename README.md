# lastfm_explorer

Web para explorar el historial de escuchas de Last.fm/ListenBrainz de
[repeat-one](https://github.com/volteret4/repeat-one), con filtros por fecha
de escucha, fecha de lanzamiento, género, artista, disco, canción y sello.

## Fuentes de datos

- **Escuchas** (fecha, artista, disco, canción, fuente): lee en modo
  solo-lectura `lastfm_stats.db`, la base que rellena a diario el servicio
  `lastfm-scrobbles` de repeat-one en el volumen compartido `lastfm_data`.
  No la genera ni la modifica esta app.
- **Metadatos** (sello, fecha de lanzamiento, género): lee en modo
  solo-lectura `musica_local.sqlite` (la base de
  [music-fuzzy](https://github.com/volteret4/mfuz), la misma que usa
  `sellos-discograficos`) y, para lo que no encuentre ahí, consulta
  MusicBrainz una vez y guarda el resultado en `explorer.db` (propia de
  este servicio) para no volver a consultarlo.

## Enriquecimiento

`enrich_albums.py` recorre los álbumes de `lastfm_stats.db` sin metadatos
todavía (o con intentos fallidos de hace más de 30 días) y los rellena:

```bash
python3 enrich_albums.py          # incremental
python3 enrich_albums.py --full   # reprocesa todo desde cero
```

En producción corre como job diario de Ofelia justo después de
`lastfm-scrobbles-daily`, así que se mantiene al día solo. La primera vez
hay que lanzar `--full` a mano para hacer el escaneo completo inicial.

## Variables de entorno

```bash
LASTFM_DB=/data/lastfm_stats.db
MUSICA_LOCAL_DB=/musica_local.sqlite
EXPLORER_DB=/app/explorer.db
PORT=5000
```

## Desarrollo local

```bash
pip install -r requirements.txt
LASTFM_DB=./lastfm_stats.db MUSICA_LOCAL_DB=./musica_local.sqlite python3 server.py
```

## Instalación con Docker Compose

```bash
cp .env.example .env
```

Rellena `.env` (credenciales y carpetas de tu equipo) y arranca:

```bash
docker compose up -d --build
```

Los datos que genera (bases, cachés) se quedan en esta misma carpeta, fuera de git.

## Actualizaciones automáticas

El `docker-compose.yml` trae su propia [Ofelia](https://github.com/mcuadros/ofelia), que lanza dentro del contenedor los mismos trabajos y a las mismas horas que en REPEAT-ONE (formato con segundos: `seg min hora día mes día-semana`, hora de `TZ`, por defecto Europe/Madrid):

| Trabajo | Cuándo | Qué lanza |
|---|---|---|
| `lastfm-explorer-enrich` | `0 10 0 * * *` | `python3 enrich_albums.py` |

Para cambiar una hora, edita su etiqueta `ofelia.job-exec.<trabajo>.schedule` en `docker-compose.yml` y vuelve a hacer `docker compose up -d`. Para lanzarlo a mano: `docker compose exec <servicio> <orden>`. Para no tener actualizaciones automáticas: `docker compose stop ofelia`.

Esta Ofelia solo ve los contenedores de este repo (etiqueta `repeat-one.ofelia`), así que puedes tener varios módulos instalados por separado sin que se pisen. Necesita el socket de Docker en solo lectura.
