# Mosaico multi-radar

Compone los COG por radar en un único COG sobre una malla común, para que el
viewer pinte varios radares como un solo campo continuo en vez de apilar
capas que se tapan entre sí.

## Por qué no basta con apilar capas

Apilar N capas en el cliente resuelve la vista, no el dato: en el solape gana
la capa que esté encima, no la medida mejor. Dos radares que ven la misma
tormenta dan dos valores distintos en el mismo punto, y hay que elegir uno
con criterio físico. Eso es lo que hace este servicio.

## La regla de composición

El cálculo, en orden, y cada paso importa:

1. **Remuestreo nearest** de los niveles crudos de cada sitio a la malla del
   dominio. Nearest y no bilinear por la misma razón que en el grillado
   polar: son datos umbralizados y la interpolación suave inventa valores.
2. El **rango al radar viaja con el dato** (se remuestrea igual), así la
   calidad de cada píxel es la del sample que realmente aterrizó ahí.
3. Niveles 0 (below threshold) y 1 (range folded) **pesan cero**. El mosaico
   nunca emite nivel 1: el folding es propiedad del radar individual.
4. Nivel → físico con el `value_scale`/`value_offset` **de la fila de
   origen**. La calibración es por fila de `rasters` y dos sitios no tienen
   por qué coincidir; componer en niveles sería un error silencioso.
5. En reflectividad, el promedio pesado va en **Z lineal**
   (`Z = 10^(dBZ/10)`), nunca en dBZ.
6. Re-cuantización con la **calibración canónica del producto**, fija y
   conocida por el viewer, para que la paleta no cambie entre slots.

### Por qué Z lineal y no dBZ

El dBZ es logarítmico. Promediar dos dBZ es promediar logaritmos, que no es
el promedio de las magnitudes. Con 40 y 50 dBZ al mismo peso:

| | |
|---|---|
| promedio en dBZ (mal) | 45.0 dBZ |
| promedio en Z lineal (bien) | **47.4 dBZ** |

El error siempre va en la misma dirección —hunde los máximos— que es justo
lo que un mosaico tiene que preservar. Hay un test que falla si alguien
quita el paso a Z (`tests/test_mosaic_wradlib_xcheck.py`).

### El campo de calidad

Un radar que mira una tormenta a 50 km la ve cerca del suelo con un haz
estrecho; otro que la ve a 300 km la mira desde ~5 km de altura con un haz
ensanchado, probablemente por encima de la parte del eco que importa. Cada
píxel se pesa por los dos efectos:

```
h = sqrt(r² + (k·a)² + 2·r·k·a·sin φ) − k·a + h_antena      k = 4/3
q = exp(−(h/h_ref)²) · exp(−(r/r_ref)²)
```

`k = 4/3` es el radio terrestre efectivo que absorbe la refracción estándar.
`h` sale sobre el nivel del mar y se usa como proxy de altura sobre el
terreno: válido en dominio costero o llano, no en terreno complejo (ahí
habría que restar un DEM). Los derivados de volumen (EET, DVL) no tienen
elevación: el término de altura desaparece y queda la calidad por rango.

`h_ref` y `r_ref` son **los dos números que calibra el experto**. Van por
variable de entorno precisamente para que ajustarlos no exija reconstruir la
imagen.

### Dos reglas

| `method` | Qué hace | Cuándo |
|---|---|---|
| `weighted` (default) | `Z̄ = Σ qᵢZᵢ / Σ qᵢ` | Sin costura en el solape; suaviza algo los picos |
| `lowest_beam` | Gana el radar que mira más bajo | Preserva gradientes; frontera visible, pero sigue iso-altura |

No está la regla del **máximo** (la clásica de GEMPAK): sesga alto y premia
al radar que sobrepasa la tormenta. Se documenta como descartada, no como
pendiente.

### Qué no se compone

`N0G` (velocidad radial) **no es componible** y no lo será: la velocidad es
relativa al radar, y dos radares miden componentes distintas del mismo
viento. No es una limitación de implementación. Los acumulados de
precipitación (`DAA`, `DU3`, `DTA`) quedan fuera por ahora: los períodos de
acumulación de cada radar no están alineados.

## El dominio

Un dominio es una malla AEQD común a N radares, centrada en el punto medio
del bbox de los sitios, con los semiejes forzados a simétricos para
conservar la convención del COG por radar (malla centrada, fila 0 al norte).

La **pertenencia** se configura (`MOSAIC_DOMAINS`); la **geometría** se
calcula de las filas vivas de `radars`, nada hardcodeado. El radio es el del
mayor de los productos configurados, para que todos compartan una sola malla
y cambiar de producto en el viewer no cambie la extensión de la vista.

Cap duro de **4096 px** por lado: por encima, el viewer no puede subir la
textura a la GPU. Es un error de configuración, no un aviso. Para el dominio
`GULF` (AMX, BYX, HDC, EOX) con el radio de N0B:

| `MOSAIC_CELL_M` | malla | |
|---|---|---|
| 2000 | 962 × 842 | |
| **1000** (default) | 1924 × 1682 | |
| 500 | 3848 × 3362 | entra, pero ~4x de memoria en la composición |
| 250 | 7696 × 6724 | **rechazado** |

El default no lo fija el cap de textura sino la memoria del VPS: el pico es
el stack de N fuentes en float32 sobre la malla del dominio.

## La rejilla temporal

Los radares no escanean sincronizados: cada sitio tiene sus `vol_time` y su
cadencia según el VCP. El mosaico usa **slots UTC fijos** de 300 s (mismo
patrón que `lightning_buckets`), y cada sitio aporta el volumen más cercano
al centro del slot dentro de `MOSAIC_SLOT_TOLERANCE_S`.

Sólo se componen **slots cerrados**: componer uno abierto daría un COG que
cambia bajo los pies del cliente, y la clave R2 es inmutable.

**Se compone con los sitios que haya**, no con todos: un radar caído no
apaga el mosaico. Quién aportó y con cuánto desfase queda en la columna
`contributing`, y el viewer pinta encima la cobertura real — incluidos los
ausentes, que es la mitad de la información.

## Operación

```bash
uv run l3proc mosaic --once      # una corrida y salir
uv run l3proc mosaic             # servicio (cron interno de 60 s)
```

Servicio `mosaic` en `docker-compose.yml`. Estado real en Postgres (no en
disco): reanuda desde el último `slot_time` de `mosaic_rasters`. Idempotente
— la clave R2 es determinista en (dominio, producto, slot), así que
recomponer reescribe el mismo objeto.

| Variable | Default | |
|---|---|---|
| `MOSAIC_DOMAINS` | — | `GULF=AMX,BYX,HDC,EOX;OTRO=XXX,YYY` |
| `MOSAIC_PRODUCTS` | `153` | códigos NEXRAD |
| `MOSAIC_CELL_M` | `1000` | |
| `MOSAIC_METHOD` | `weighted` | o `lowest_beam` |
| `MOSAIC_H_REF_M` | `4000` | calibración del experto |
| `MOSAIC_R_REF_M` | `200000` | calibración del experto |
| `MOSAIC_SLOT_S` | `300` | |
| `MOSAIC_SLOT_TOLERANCE_S` | `150` | |
| `MOSAIC_MIN_SITES` | `1` | |
| `MOSAIC_LOOKBACK_S` | `3600` | ventana de recuperación |
| `MOSAIC_MAX_SLOTS` | `12` | tope por corrida |

El sweep de retención de 72 h y la reconciliación R2↔Postgres cubren
`mosaic_rasters` igual que las demás tablas. Las claves llevan prefijo
`mosaic/` propio: si `api/` se desplegara sin conocer la tabla, la
reconciliación excluye el prefijo entero en vez de tratar sus objetos como
huérfanos y borrarlos. **Desplegar `api/` antes que el Worker `ops`.**
