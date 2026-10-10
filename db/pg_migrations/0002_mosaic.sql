-- Migration number: 0002    2026-10-10
-- Mosaico multi-radar. Tablas nuevas, nada de lo existente se toca.
--
-- Parte del contrato con LAMULA-WebViewer: el viewer SELECTea estas tres
-- tablas igual que SELECTea radars/rasters. Convenciones sin cambios:
-- timestamps TEXT ISO-8601 UTC naive, comparables lexicográficamente.
--
-- Por qué tablas propias y no filas en `rasters`:
--   - `rasters.site_id` tiene FK a `radars` y un mosaico no tiene sitio
--     único. Meter un pseudo-radar "GULF" en `radars` lo colaría en el
--     selector de radares del viewer con lat/lon/height inventados y con
--     phenomena/vwp vacíos — mentira en el catálogo para ahorrar una tabla.
--   - El mosaico tiene su propia rejilla temporal (slots fijos), su propia
--     procedencia (qué sitios aportaron) y su propia regla de composición.

-- Un dominio es una malla AEQD común a N radares. Convención idéntica a la
-- de los COG por radar: malla centrada, fila 0 al norte, origen implícito en
-- (-width*cell_m/2, +height*cell_m/2). El viewer registra `proj4` tal cual,
-- igual que hace con radars.proj4 — sin EPSG, sin nada hardcodeado.
CREATE TABLE mosaic_domains (
    domain_id  TEXT PRIMARY KEY,   -- 'GULF'
    name       TEXT NOT NULL,      -- display
    proj4      TEXT NOT NULL,
    width      INTEGER NOT NULL,
    height     INTEGER NOT NULL,
    cell_m     REAL NOT NULL,
    radius_m   REAL NOT NULL,      -- alcance nativo del producto que definió la malla
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Qué radares componen el dominio. Normalizado y con FK real en vez de un
-- JSON: el viewer necesita restar `contributing` a esta lista para pintar
-- los radares AUSENTES de cada slot, que es la mitad de la información del
-- overlay de cobertura.
CREATE TABLE mosaic_domain_sites (
    domain_id TEXT NOT NULL REFERENCES mosaic_domains (domain_id) ON DELETE CASCADE,
    site_id   TEXT NOT NULL REFERENCES radars (site_id),
    PRIMARY KEY (domain_id, site_id)
);

CREATE INDEX idx_mosaic_domain_sites_site ON mosaic_domain_sites (site_id);

-- Un COG compuesto por (dominio, producto, slot).
--
-- `product_code` reusa el código NEXRAD: el mosaico se identifica por
-- dominio, así que el catálogo de productos del viewer y su paleta valen
-- sin cambios y no hace falta un código inventado.
--
-- Se compone con los sitios que haya dentro de tolerancia, no con todos: un
-- radar caído no apaga el mosaico. `contributing` dice quién aportó y con
-- qué desfase, y el viewer pinta la cobertura real encima.
CREATE TABLE mosaic_rasters (
    id           BIGSERIAL PRIMARY KEY,
    domain_id    TEXT NOT NULL REFERENCES mosaic_domains (domain_id),
    product_code INTEGER NOT NULL REFERENCES products (code),
    slot_time    TEXT NOT NULL,    -- inicio del slot UTC, alineado a slot_s
    slot_s       INTEGER NOT NULL DEFAULT 300,
    r2_key       TEXT NOT NULL UNIQUE,
    size_bytes   INTEGER NOT NULL,
    -- Calibración canónica del producto mosaico: fija y conocida por el
    -- viewer, NO heredada de ninguna fila de rasters (esas varían por fila).
    value_scale  REAL NOT NULL,
    value_offset REAL NOT NULL,
    max_level    INTEGER,
    -- Geometría de ESTE COG, no la del dominio. Añadir un radar a un dominio
    -- recalcula su malla, y un COG viejo seguiría georreferenciado con la
    -- malla vieja: sin esto el viewer lo pintaría desplazado. Misma razón por
    -- la que `rasters` guarda width/height/cell_m por fila.
    proj4        TEXT NOT NULL,
    width        INTEGER NOT NULL,
    height       INTEGER NOT NULL,
    cell_m       REAL NOT NULL,
    method       TEXT NOT NULL CHECK (method IN ('weighted', 'lowest_beam')),
    -- JSON [{"site":"AMX","vol_time":"...","lag_s":42}, ...]. Procedencia:
    -- qué radares entraron en ESTE slot y cuánto desfase traía cada uno.
    contributing TEXT NOT NULL DEFAULT '[]',
    created_at   TEXT NOT NULL,
    UNIQUE (domain_id, product_code, slot_time)
);

CREATE INDEX idx_mosaic_rasters_lookup
    ON mosaic_rasters (domain_id, product_code, slot_time DESC);
CREATE INDEX idx_mosaic_rasters_created ON mosaic_rasters (created_at); -- sweep 72 h
