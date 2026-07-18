-- Migration number: 0003    2026-07-18
-- Viento GFS 0.25° 10 m para la capa de partículas del viewer.
-- Contrato acordado con LAMULA-WebViewer (spec jul-2026): una fila por
-- (sitio, valid_time); el JSON u/v vive en R2 bajo {SITE}/WIND/… con el
-- ciclo en el nombre (inmutable — un ciclo más nuevo sube objeto nuevo
-- y borra el anterior tras el upsert).

CREATE TABLE wind_grids (
  site_id       TEXT    NOT NULL REFERENCES radars(site_id),
  valid_time    TEXT    NOT NULL,  -- ISO naive UTC 'YYYY-MM-DDTHH:MM:SS', misma convención que vol_time
  cycle_time    TEXT    NOT NULL,  -- ciclo del modelo, ISO naive UTC
  forecast_hour INTEGER NOT NULL,  -- valid_time - cycle_time, en horas
  model         TEXT    NOT NULL DEFAULT 'gfs0p25',
  r2_key        TEXT    NOT NULL,
  size_bytes    INTEGER NOT NULL,
  created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
  PRIMARY KEY (site_id, valid_time)
);

-- La PK cubre el único lookup del viewer:
--   WHERE site_id = ? AND valid_time >= ? AND valid_time < ?
-- No hace falta índice extra.
