"""Geometría del dominio: malla AEQD común a N radares.

Misma convención que `ingest.gridding.aeqd.AeqdGrid`: malla centrada, fila 0
al norte, origen en (-half_x, +half_y), celda cuadrada. El centro es el punto
medio del bbox de los sitios; los semiejes se fuerzan simétricos
(max|x| + radio, max|y| + radio) para conservar esa convención. Cuesta un
pequeño porcentaje de malla vacía y evita meter un segundo sistema de
orígenes en el contrato con el viewer, que hoy deriva la extensión de
width/height/cell_m asumiendo malla centrada.

No hay EPSG: igual que con los radares, el viewer registra el proj4 tal cual.
"""

from dataclasses import dataclass

from pyproj import Transformer
from rasterio.crs import CRS
from rasterio.transform import from_origin

from ingest.gridding.aeqd import radar_proj4

# Cap de textura WebGL del viewer. Una malla por encima de esto no se puede
# renderizar en el cliente, así que es un error de configuración, no un aviso.
MAX_TEXTURE_PX = 4096


@dataclass(frozen=True)
class RadarSite:
    """Lo que el mosaico necesita de una fila de `radars`."""

    site_id: str
    lat: float
    lon: float
    height_m: float


@dataclass(frozen=True)
class DomainGrid:
    domain_id: str
    proj4: str
    width: int
    height: int
    cell_m: float

    @property
    def half_x_m(self) -> float:
        return self.width * self.cell_m / 2.0

    @property
    def half_y_m(self) -> float:
        return self.height * self.cell_m / 2.0

    @property
    def transform(self):
        return from_origin(-self.half_x_m, self.half_y_m, self.cell_m, self.cell_m)

    @property
    def crs(self) -> CRS:
        return CRS.from_proj4(self.proj4)


def _even_ceil(value: float) -> int:
    """Siguiente entero par >= value (malla par = centro en una arista de pixel)."""
    n = int(-(-value // 1))
    return n + (n % 2)


def build_domain(
    domain_id: str,
    sites: list[RadarSite],
    radius_m: float,
    cell_m: float,
) -> DomainGrid:
    """Malla del dominio que cubre todos los sitios más su radio nativo.

    `radius_m` es el alcance del producto (n_gates × gate_width), no una
    constante: el dominio de N0B y el de EET no tienen por qué coincidir.
    """
    if not sites:
        raise ValueError("un dominio necesita al menos un sitio")
    if cell_m <= 0:
        raise ValueError(f"cell_m debe ser positivo ({cell_m})")

    lats = [s.lat for s in sites]
    lons = [s.lon for s in sites]
    # Punto medio del bbox, no centroide de masa: no queremos que añadir un
    # radar en un extremo mueva la malla de forma desproporcionada. Asume que
    # el dominio no cruza el antimeridiano (cierto para el alcance del demo).
    lat0 = (min(lats) + max(lats)) / 2.0
    lon0 = (min(lons) + max(lons)) / 2.0
    proj4 = radar_proj4(lat0, lon0)

    tf = Transformer.from_crs("EPSG:4326", proj4, always_xy=True)
    xy = [tf.transform(s.lon, s.lat) for s in sites]
    half_x = max(abs(x) for x, _ in xy) + radius_m
    half_y = max(abs(y) for _, y in xy) + radius_m

    width = _even_ceil(2.0 * half_x / cell_m)
    height = _even_ceil(2.0 * half_y / cell_m)
    if max(width, height) > MAX_TEXTURE_PX:
        raise ValueError(
            f"malla {width}x{height} supera el cap de textura {MAX_TEXTURE_PX} "
            f"(sube cell_m: con {cell_m:.0f} m no entra)"
        )
    return DomainGrid(
        domain_id=domain_id,
        proj4=proj4,
        width=width,
        height=height,
        cell_m=cell_m,
    )
