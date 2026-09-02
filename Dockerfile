# Imagen única del pipeline: el mismo binario `l3proc` sirve de poller
# (l3proc poll) y de procesador (l3proc watch) — el stack elige el comando.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# libexpat1: el wheel manylinux de rasterio la enlaza del sistema y la
# imagen slim no la trae (ImportError: libexpat.so.1 al importar rasterio).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependencias primero (capa cacheable), proyecto después.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project --no-dev

COPY README.md ./
COPY ingest/ ingest/
# db/: scripts de migración/backfill de Postgres (apply_pg_migrations.py,
# backfill_d1_to_pg.py) + el schema (pg_migrations/) — no son parte de
# l3proc, pero viajan en la misma imagen para poder correrlos con
# `docker run --entrypoint python` desde cualquier nodo del Swarm sin
# necesitar un checkout del repo ahí.
COPY db/*.py db/
COPY db/pg_migrations/ db/pg_migrations/
RUN uv sync --locked --no-dev

ENV PATH="/app/.venv/bin:$PATH"

RUN useradd -r -u 10001 -m l3proc \
    && mkdir -p /data/incoming \
    && chown -R l3proc /data
USER l3proc
VOLUME /data/incoming

ENTRYPOINT ["l3proc"]
CMD ["--help"]
