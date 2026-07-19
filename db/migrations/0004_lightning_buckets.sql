-- Migration number: 0004    2026-07-19
-- Descargas eléctricas GLM (GOES-19, producto L2 LCFA) para la capa de
-- rayos animados del viewer. Contrato acordado con LAMULA-WebViewer
-- (spec jul-2026): cubos fijos de 300 s alineados a UTC — desacoplados
-- del VCP a propósito, el cliente junta los cubos que solapan la
-- ventana de la observación. La fila se escribe SIEMPRE al cerrar el
-- cubo, incluso con 0 rayos (strike_count = 0, r2_key NULL, sin objeto
-- R2): fila presente = cubo cubierto sin descargas; fila ausente =
-- hueco de ingesta. El JSON de strikes vive en R2 bajo
-- {SITE}/LIGHTNING/… (inmutable — el cubo se procesa una única vez,
-- cerrado + ≥ 90 s de margen de latencia GLM).

CREATE TABLE lightning_buckets (
  site_id       TEXT    NOT NULL REFERENCES radars(site_id),
  bucket_start  TEXT    NOT NULL,  -- ISO naive UTC 'YYYY-MM-DDTHH:MM:SS', alineado a 300 s
  bucket_s      INTEGER NOT NULL DEFAULT 300,
  strike_count  INTEGER NOT NULL,
  r2_key        TEXT,              -- NULL cuando strike_count = 0 (no se sube objeto)
  size_bytes    INTEGER,
  source        TEXT    NOT NULL DEFAULT 'glm-goes19',
  created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
  PRIMARY KEY (site_id, bucket_start)
);

-- La PK cubre el único lookup del viewer:
--   WHERE site_id = ? AND bucket_start >= ? AND bucket_start < ?
-- No hace falta índice extra.
