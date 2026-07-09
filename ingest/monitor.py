"""Monitor de frescura E2E + alertas Telegram.

Por cada sitio configurado comprueba la cadena completa: D1 tiene un
raster reciente (< umbral) **y** el objeto R2 correspondiente responde
a HEAD. El feed es continuo (volumen cada 4–10 min según VCP), así que
el umbral de 30 min separa "roto" de "cielo despejado".

Alerta por Telegram solo en transiciones (verde→rojo y rojo→verde),
no en cada ciclo — sin spam. Sin credenciales de Telegram el monitor
funciona igual y deja las transiciones en el log.
"""

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

import httpx

from ingest.storage.d1 import D1Client
from ingest.storage.r2 import R2Client

log = logging.getLogger("l3proc")


@dataclass(frozen=True)
class SiteStatus:
    site: str
    fresh: bool
    reason: str  # "ok" | "sin datos" | "viejo (Xm)" | "falta objeto R2"
    age_min: float | None = None


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id

    def send(self, text: str) -> None:
        try:
            resp = httpx.post(self._url, json={"chat_id": self._chat_id, "text": text}, timeout=15)
            if resp.status_code != 200:
                log.error("telegram: HTTP %d — %s", resp.status_code, resp.text[:200])
        except httpx.HTTPError as exc:
            log.error("telegram: %s", exc)


def check_site(
    d1: D1Client,
    r2: R2Client,
    site: str,
    *,
    max_age_min: float = 30.0,
    now: datetime | None = None,
) -> SiteStatus:
    rows = d1.execute(
        "SELECT vol_time, r2_key FROM rasters WHERE site_id = ? ORDER BY vol_time DESC LIMIT 1",
        [site],
    )
    if not rows:
        return SiteStatus(site, False, "sin datos")

    now = (now or datetime.now(UTC)).replace(tzinfo=None)
    latest = datetime.fromisoformat(rows[0]["vol_time"])
    age_min = (now - latest).total_seconds() / 60.0
    if age_min > max_age_min:
        return SiteStatus(site, False, f"viejo ({age_min:.0f} min)", age_min)
    if r2.head(rows[0]["r2_key"]) is None:
        return SiteStatus(site, False, "falta objeto R2", age_min)
    return SiteStatus(site, True, "ok", age_min)


def run_monitor(
    d1: D1Client,
    r2: R2Client,
    sites: list[str],
    *,
    notifier: TelegramNotifier | None = None,
    max_age_min: float = 30.0,
    interval_s: float = 300.0,
    heartbeat: Path | None = None,
    stop: Event | None = None,
) -> None:
    stop = stop or Event()
    was_fresh: dict[str, bool | None] = dict.fromkeys(sites)  # None = aún sin evaluar

    def notify(text: str) -> None:
        log.warning("monitor: %s", text)
        if notifier is not None:
            notifier.send(text)

    log.info("monitor: %s cada %.0f s (umbral %.0f min)", ",".join(sites), interval_s, max_age_min)
    while not stop.is_set():
        t0 = time.monotonic()
        for site in sites:
            try:
                status = check_site(d1, r2, site, max_age_min=max_age_min)
            except Exception:
                log.exception("monitor: fallo comprobando %s (se reintenta)", site)
                continue
            prev = was_fresh[site]
            if not status.fresh and prev is not False:
                notify(f"🔴 {site}: sin datos frescos — {status.reason}")
            elif status.fresh and prev is False:
                notify(f"🟢 {site}: recuperado (último raster hace {status.age_min:.0f} min)")
            elif status.fresh:
                log.info("monitor: %s ok (%.0f min)", site, status.age_min)
            was_fresh[site] = status.fresh
        if heartbeat is not None:
            heartbeat.touch()
        elapsed = time.monotonic() - t0
        stop.wait(max(1.0, interval_s - elapsed))
