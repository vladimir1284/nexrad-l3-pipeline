-- Migration number: 0001    2026-09-01
-- Schema Postgres, squasheado desde db/migrations/0001..0005 (D1/SQLite,
-- congeladas como referencia histórica/rollback, no editar). Este schema
-- es el contrato con LAMULA-WebViewer: cambios incompatibles requieren
-- coordinación con el viewer. Convenciones sin cambios respecto a D1:
-- timestamps TEXT ISO-8601 UTC ("YYYY-MM-DDTHH:MM:SS"), sin timezone
-- explícita (todo es UTC), comparables lexicográficamente.
--
-- Cambios mecánicos respecto al schema D1 (ver db/README.md):
--   - INTEGER PRIMARY KEY AUTOINCREMENT -> BIGSERIAL PRIMARY KEY
--     (rasters.id, phenomena.id, vwp.id).
--   - wind_grids/lightning_buckets.created_at: se quita el DEFAULT
--     strftime(...) (SQLite-only) — el valor siempre lo pasa la app
--     (ver ingest/wind.py, ingest/lightning.py), columna sigue TEXT NOT NULL.
--   - FKs (REFERENCES radars(site_id) / products(code)): en D1 no se
--     enforceaban (sin PRAGMA foreign_keys=ON); en Postgres SÍ se
--     enforcean por defecto — cambio de comportamiento real, no solo
--     sintáctico (ver plan de migración).

CREATE TABLE radars (
    site_id TEXT PRIMARY KEY, -- id de 3 chars del feed (AMX, JUA)
    icao TEXT, -- ICAO completo (KAMX, TJUA) cuando la config lo mapea
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    height_m REAL NOT NULL, -- altitud de la antena (msl)
    proj4 TEXT NOT NULL, -- definición AEQD que el viewer registra tal cual
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE products (
    code INTEGER PRIMARY KEY, -- código NEXRAD (153)
    mnemonic TEXT NOT NULL UNIQUE, -- N0B
    unit TEXT, -- dBZ, kt, mm…
    kind TEXT NOT NULL CHECK (kind IN ('raster', 'phenomena', 'vwp'))
);

CREATE TABLE rasters (
    id BIGSERIAL PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES radars (site_id),
    product_code INTEGER NOT NULL REFERENCES products (code),
    vol_time TEXT NOT NULL, -- inicio del volumen (UTC)
    r2_key TEXT NOT NULL UNIQUE,
    size_bytes INTEGER NOT NULL,
    el_angle REAL, -- NULL en derivados de volumen
    vcp INTEGER,
    -- calibración: físico = nivel · value_scale + value_offset (niveles >= 2;
    -- 0 = below threshold / nodata, 1 = range folded)
    value_scale REAL NOT NULL,
    value_offset REAL NOT NULL,
    max_level INTEGER, -- nivel máximo presente (para leyendas)
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    cell_m REAL NOT NULL, -- tamaño de celda de la malla AEQD
    created_at TEXT NOT NULL,
    UNIQUE (site_id, product_code, vol_time)
);

CREATE INDEX idx_rasters_lookup ON rasters (site_id, product_code, vol_time DESC);
CREATE INDEX idx_rasters_created ON rasters (created_at); -- sweep de retención

CREATE TABLE phenomena (
    id BIGSERIAL PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES radars (site_id),
    product_code INTEGER NOT NULL REFERENCES products (code),
    vol_time TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('hail', 'meso', 'tvs', 'storm_cell')),
    cell_id TEXT, -- storm ID del RPG (p.ej. "A0"), estable entre volúmenes
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    azimuth_deg REAL, -- posición original radar-céntrica
    range_km REAL,
    attrs TEXT NOT NULL DEFAULT '{}', -- JSON: atributos específicos del tipo
    created_at TEXT NOT NULL
);

CREATE INDEX idx_phenomena_lookup ON phenomena (site_id, vol_time DESC);
CREATE INDEX idx_phenomena_created ON phenomena (created_at);

CREATE TABLE vwp (
    id BIGSERIAL PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES radars (site_id),
    vol_time TEXT NOT NULL,
    height_ft INTEGER NOT NULL, -- altura del nivel (ft msl, unidad nativa del producto)
    wind_dir_deg REAL NOT NULL,
    wind_speed_kt REAL NOT NULL,
    rms_kt REAL, -- error RMS del ajuste VAD
    created_at TEXT NOT NULL,
    UNIQUE (site_id, vol_time, height_ft)
);

CREATE INDEX idx_vwp_lookup ON vwp (site_id, vol_time DESC);
CREATE INDEX idx_vwp_created ON vwp (created_at);

-- Estado interno del monitor de frescura (Worker nexrad-l3-ops).
-- NO es parte del contrato con el viewer — solo persiste el último
-- estado por sitio para alertar únicamente en transiciones verde↔rojo.
CREATE TABLE ops_monitor_state (
    site_id    TEXT PRIMARY KEY,
    fresh      INTEGER NOT NULL, -- 0/1
    reason     TEXT NOT NULL,    -- "ok" | "sin datos" | "viejo (Xm)" | "falta objeto R2"
    updated_at TEXT NOT NULL
);

-- Viento GFS 0.25° 10 m + niveles de altura ("steering flow" 850/700/500 hPa)
-- para la capa de partículas del viewer. Selector de altura en el viewer
-- muestra un nivel a la vez, así que el lookup sigue siendo de una fila:
--   WHERE site_id = ? AND level = ? AND valid_time >= ? AND valid_time < ?
-- La PK cubre ese lookup — no hace falta índice extra.
CREATE TABLE wind_grids (
    site_id       TEXT    NOT NULL REFERENCES radars(site_id),
    valid_time    TEXT    NOT NULL, -- ISO naive UTC, misma convención que vol_time
    level         TEXT    NOT NULL DEFAULT '10m', -- '10m' | '850hPa' | '700hPa' | '500hPa'
    cycle_time    TEXT    NOT NULL, -- ciclo del modelo, ISO naive UTC
    forecast_hour INTEGER NOT NULL, -- valid_time - cycle_time, en horas
    model         TEXT    NOT NULL DEFAULT 'gfs0p25',
    r2_key        TEXT    NOT NULL,
    size_bytes    INTEGER NOT NULL,
    created_at    TEXT    NOT NULL,
    PRIMARY KEY (site_id, valid_time, level)
);

-- Descargas eléctricas GLM (GOES-19 GLM-L2-LCFA) para la capa de rayos
-- animados del viewer. Cubos fijos de 300 s alineados a UTC, desacoplados
-- del VCP a propósito. Fila SIEMPRE al cerrar el cubo, incluso con 0 rayos
-- (strike_count = 0, r2_key NULL, sin objeto R2): fila presente = cubo
-- cubierto sin descargas; fila ausente = hueco de ingesta. Lookup del
-- viewer: WHERE site_id = ? AND bucket_start >= ? AND bucket_start < ? —
-- cubierto por la PK, no hace falta índice extra.
CREATE TABLE lightning_buckets (
    site_id       TEXT    NOT NULL REFERENCES radars(site_id),
    bucket_start  TEXT    NOT NULL, -- ISO naive UTC, alineado a 300 s
    bucket_s      INTEGER NOT NULL DEFAULT 300,
    strike_count  INTEGER NOT NULL,
    r2_key        TEXT,             -- NULL cuando strike_count = 0 (no se sube objeto)
    size_bytes    INTEGER,
    source        TEXT    NOT NULL DEFAULT 'glm-goes19',
    created_at    TEXT    NOT NULL,
    PRIMARY KEY (site_id, bucket_start)
);
