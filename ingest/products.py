"""Registro de productos raster soportados: geometría nativa y calibración.

La geometría (ancho de gate) no viene en el paquete radial digital — es
convención del producto según el ICD (el `gate_scale` del paquete es un
artefacto inconsistente entre productos y se ignora). La extensión de la
malla sale de n_gates × gate_width (celda = gate nativo).

`calibration` nombra la estrategia nivel→físico del decoder:

- ``linear10``: thresholds ×10 (thr1 = mínimo, thr2 = incremento) —
  reflectividad/velocidad super-res (153/154).
- ``eet``: bits 0-6 = topes en kft + 2; bit 7 (topped) se enmascara.
- ``dvl``: float16 en thresholds, lineal hasta un nivel de corte y
  logarítmico por encima — se re-encodea a lineal.
- ``dpr``: scale/offset float32 partidos en halfwords (familia de
  precipitación digital), físico en centésimas de pulgada → mm.

`has_elevation`: solo los productos radiales por elevación llevan
el_angle; en derivados de volumen el metadata trae basura.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ProductSpec:
    code: int
    mnemonic: str
    gate_width_m: float
    unit: str
    calibration: str
    has_elevation: bool = False


RASTER_PRODUCTS: dict[int, ProductSpec] = {
    153: ProductSpec(153, "N0B", 250.0, "dBZ", "linear10", has_elevation=True),
    154: ProductSpec(154, "N0G", 250.0, "kt", "linear10", has_elevation=True),
    135: ProductSpec(135, "EET", 1000.0, "kft", "eet"),
    134: ProductSpec(134, "DVL", 1000.0, "kg/m2", "dvl"),
    170: ProductSpec(170, "DAA", 250.0, "mm", "dpr"),
    173: ProductSpec(173, "DU3", 250.0, "mm", "dpr"),
    172: ProductSpec(172, "DTA", 250.0, "mm", "dpr"),
}

# Fenómenos no-raster: código → (mnemónico, kind en la tabla phenomena).
# NHI/NTV no fluyen en el bucket (verificado jun-jul 2026); la señal de
# TVS viaja en la columna TVS del NMD.
PHENOMENA_PRODUCTS: dict[int, tuple[str, str]] = {
    58: ("NST", "storm_cell"),
    141: ("NMD", "meso"),
}


VWP_MNEMONIC = "NVW"  # 48 — perfil de viento VAD, camino propio a la tabla vwp


def all_mnemonics() -> list[str]:
    """Todos los productos que el poller baja por defecto."""
    return (
        [spec.mnemonic for spec in RASTER_PRODUCTS.values()]
        + [mnemo for mnemo, _ in PHENOMENA_PRODUCTS.values()]
        + [VWP_MNEMONIC]
    )
