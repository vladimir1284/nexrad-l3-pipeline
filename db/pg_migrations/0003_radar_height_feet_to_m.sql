-- Migration number: 0003    2026-11-05
-- `radars.height_m` guardaba PIES etiquetados como metros.
--
-- El PDB del Level III da la altura de la antena en pies sobre el nivel
-- del mar (ICD 2620001, halfword 6) y `Level3File.height` de MetPy
-- devuelve ese entero sin convertir. El pipeline lo asignaba directo a
-- `height_m`, así que todas las filas escritas antes del arreglo de
-- `ingest/decoder/level3.py::antenna_height_m` están en pies.
--
-- ORDEN DE DESPLIEGUE: aplicar esta migración ANTES de desplegar la
-- imagen con el arreglo. Al revés, un radar que ya hubiera publicado un
-- producto con el código nuevo tendría su fila correcta (metros) y esta
-- sentencia la dejaría 0,3048 veces más pequeña. Para un radar vivo el
-- daño es transitorio — el siguiente producto reescribe la fila por el
-- upsert de `publish.py` —, pero para un radar que ya no emite es
-- permanente, que es justo el caso para el que existe esta migración.
--
-- Se aplica una sola vez: `db/apply_pg_migrations.py` lleva registro en
-- `schema_migrations`. No hay rollback automático (multiplicar por
-- 1/0,3048 lo revierte si hiciera falta).

-- Solo datos: la columna ya es REAL y el contrato con el viewer no
-- cambia de forma, solo deja de mentir en la unidad (ver db/README.md).

UPDATE radars SET height_m = height_m * 0.3048;
