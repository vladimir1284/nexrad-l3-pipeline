-- Estado interno del monitor de frescura (Worker nexrad-l3-ops).
-- NO es parte del contrato con el viewer — solo persiste el último
-- estado por sitio para alertar únicamente en transiciones verde↔rojo.
CREATE TABLE ops_monitor_state (
    site_id    TEXT PRIMARY KEY,
    fresh      INTEGER NOT NULL, -- 0/1
    reason     TEXT NOT NULL,    -- "ok" | "sin datos" | "viejo (Xm)" | "falta objeto R2"
    updated_at TEXT NOT NULL     -- ISO 8601 UTC naive, igual que el resto del schema
);
