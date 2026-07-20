-- Migration number: 0005    2026-07-20
-- Fase 2 de viento: niveles de altura (850/700/500 hPa, terna "steering
-- flow") además de la superficie 10 m ya en producción. Contrato acordado
-- con LAMULA-WebViewer: selector de altura que muestra un nivel a la vez
-- (no simultáneo), así que el lookup sigue siendo de una fila.
--
-- PK pasa de (site_id, valid_time) a (site_id, valid_time, level).
-- SQLite/D1 no soportan ALTER de PK — se reconstruye la tabla. Filas
-- existentes (todas superficie) se backfillan con level='10m'.

CREATE TABLE wind_grids_new (
  site_id       TEXT    NOT NULL REFERENCES radars(site_id),
  valid_time    TEXT    NOT NULL,
  level         TEXT    NOT NULL DEFAULT '10m', -- '10m' | '850hPa' | '700hPa' | '500hPa'
  cycle_time    TEXT    NOT NULL,
  forecast_hour INTEGER NOT NULL,
  model         TEXT    NOT NULL DEFAULT 'gfs0p25',
  r2_key        TEXT    NOT NULL,
  size_bytes    INTEGER NOT NULL,
  created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
  PRIMARY KEY (site_id, valid_time, level)
);

INSERT INTO wind_grids_new
  (site_id, valid_time, level, cycle_time, forecast_hour, model, r2_key, size_bytes, created_at)
SELECT site_id, valid_time, '10m', cycle_time, forecast_hour, model, r2_key, size_bytes, created_at
FROM wind_grids;

DROP TABLE wind_grids;
ALTER TABLE wind_grids_new RENAME TO wind_grids;

-- La PK cubre el lookup del viewer (un nivel a la vez):
--   WHERE site_id = ? AND level = ? AND valid_time >= ? AND valid_time < ?
-- No hace falta índice extra.
