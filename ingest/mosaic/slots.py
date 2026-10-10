"""Rejilla temporal del mosaico y selección de volúmenes por slot.

Los radares no escanean sincronizados: cada sitio tiene sus propios
`vol_time` y su propia cadencia según el VCP. El mosaico necesita un reloj
maestro propio, así que usa **slots UTC fijos** (300 s por defecto), el mismo
patrón que `lightning_buckets`. Para cada slot, cada sitio aporta el volumen
más cercano al centro del slot dentro de una tolerancia.

Se compone con los sitios que haya, no con todos: un radar caído no apaga el
mosaico. Quién aportó y con cuánto desfase queda en `contributing`, y el
viewer pinta encima la cobertura real — incluidos los ausentes.

Módulo puro: no toca Postgres ni R2.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class Contribution:
    """Un sitio aportando a un slot."""

    site_id: str
    vol_time: datetime
    lag_s: int  # vol_time - centro del slot; negativo = el volumen va por delante

    def as_json(self) -> dict:
        return {
            "site": self.site_id,
            "vol_time": self.vol_time.isoformat(),
            "lag_s": self.lag_s,
        }


def slot_start(moment: datetime, slot_s: int) -> datetime:
    """Inicio del slot que contiene `moment` (alineado a época UTC)."""
    if slot_s <= 0:
        raise ValueError(f"slot_s debe ser positivo ({slot_s})")
    epoch = datetime(1970, 1, 1)
    elapsed = int((moment - epoch).total_seconds())
    return epoch + timedelta(seconds=elapsed - elapsed % slot_s)


def slot_center(start: datetime, slot_s: int) -> datetime:
    return start + timedelta(seconds=slot_s / 2.0)


def closed_slots(
    now: datetime,
    slot_s: int,
    *,
    lookback_s: int,
    since: datetime | None = None,
) -> list[datetime]:
    """Slots ya cerrados dentro de la ventana, del más viejo al más nuevo.

    Un slot está cerrado cuando `now` ya salió de él: componer un slot
    abierto daría un mosaico que cambia bajo los pies del cliente, y el
    `r2_key` es inmutable.
    """
    actual = slot_start(now, slot_s)
    primero = slot_start(now - timedelta(seconds=lookback_s), slot_s)
    if since is not None:
        primero = max(primero, slot_start(since, slot_s) + timedelta(seconds=slot_s))
    salida = []
    cursor = primero
    while cursor < actual:
        salida.append(cursor)
        cursor += timedelta(seconds=slot_s)
    return salida


def pick_contributions(
    start: datetime,
    slot_s: int,
    volumes_by_site: dict[str, list[datetime]],
    *,
    tolerance_s: int,
) -> list[Contribution]:
    """Volumen más cercano al centro del slot por sitio, dentro de tolerancia.

    Empate exacto (dos volúmenes equidistantes): gana el más reciente. Es una
    convención, pero tiene que ser determinista — el `r2_key` de un slot no
    puede depender del orden en que la consulta devolvió las filas.
    """
    centro = slot_center(start, slot_s)
    salida: list[Contribution] = []
    for site_id in sorted(volumes_by_site):
        mejor: datetime | None = None
        mejor_dist: float | None = None
        for vol_time in volumes_by_site[site_id]:
            dist = abs((vol_time - centro).total_seconds())
            if dist > tolerance_s:
                continue
            if mejor_dist is None or dist < mejor_dist or (dist == mejor_dist and vol_time > mejor):
                mejor, mejor_dist = vol_time, dist
        if mejor is not None:
            salida.append(
                Contribution(
                    site_id=site_id,
                    vol_time=mejor,
                    lag_s=int(round((mejor - centro).total_seconds())),
                )
            )
    return salida
