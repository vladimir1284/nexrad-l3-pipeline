# Imagen única del pipeline: el mismo binario `l3proc` sirve de poller
# (l3proc poll) y de procesador (l3proc watch) — el stack elige el comando.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependencias primero (capa cacheable), proyecto después.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project --no-dev

COPY README.md ./
COPY ingest/ ingest/
RUN uv sync --locked --no-dev

ENV PATH="/app/.venv/bin:$PATH"

RUN useradd -r -u 10001 -m l3proc \
    && mkdir -p /data/incoming \
    && chown -R l3proc /data
USER l3proc
VOLUME /data/incoming

ENTRYPOINT ["l3proc"]
CMD ["--help"]
