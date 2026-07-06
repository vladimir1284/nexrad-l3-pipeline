-- Migration number: 0001    2026-07-06
-- Schema inicial. Este schema es el contrato con LAMULA-WebViewer:
-- cambios incompatibles requieren coordinación con el viewer.
-- Convenciones: timestamps TEXT ISO-8601 UTC ("YYYY-MM-DDTHH:MM:SS"),
-- sin timezone explícita (todo es UTC). Diseñado migrable a PostgreSQL.

-- Catálogo de radares, poblado dinámicamente desde la metadata entrante.
-- Nunca se insertan radares a mano.
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

-- Descriptores de producto (dimensión pequeña, upsert al publicar).
CREATE TABLE products (
    code INTEGER PRIMARY KEY, -- código NEXRAD (153)
    mnemonic TEXT NOT NULL UNIQUE, -- N0B
    unit TEXT, -- dBZ, kt, mm…
    kind TEXT NOT NULL CHECK (kind IN ('raster', 'phenomena', 'vwp'))
);

-- Metadata de cada COG subido a R2.
CREATE TABLE rasters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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

-- Fenómenos puntuales extraídos de NMD/NST/NHI/NTV (se puebla en F6).
CREATE TABLE phenomena (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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

-- Perfiles de viento VAD (producto NVW; se puebla en F6).
-- Una fila por (volumen, altura).
CREATE TABLE vwp (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
