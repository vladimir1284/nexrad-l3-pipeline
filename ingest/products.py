"""Registro de productos raster soportados y su geometría nativa.

La geometría (ancho de gate) no viene en el paquete radial digital — es
convención del producto según el ICD. La extensión de la malla sale de
n_gates × gate_width (celda = gate nativo, extensión = rango nativo).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ProductSpec:
    code: int
    mnemonic: str
    gate_width_m: float
    unit: str


RASTER_PRODUCTS: dict[int, ProductSpec] = {
    153: ProductSpec(code=153, mnemonic="N0B", gate_width_m=250.0, unit="dBZ"),
}
