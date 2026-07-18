"""Validación cruzada del Worker de viento contra la referencia Python.

Para las filas más recientes de `wind_grids` (escritas por el Worker
`nexrad-l3-wind`): baja el JSON de R2, recalcula el mismo (sitio,
valid_time, ciclo, fh) con `ingest.wind` (eccodes sobre el GRIB del
filtro de NOMADS) y compara header (exacto) y u/v (tolerancia 0.011 m/s
— ambos lados redondean a 2 decimales y los empates de redondeo pueden
caer distinto entre JS y Python).

Requiere credenciales en el entorno (las mismas de `l3proc wind`) y red
hacia NOMADS. Los ciclos deben seguir publicados en NOMADS (~10 días).

    uv run python scripts/validate_wind_worker.py [--site AMX] [-n 3]
"""

import argparse
import json
import sys
from datetime import datetime

from ingest.config import StorageConfig
from ingest.storage.d1 import D1Client
from ingest.storage.r2 import R2Client
from ingest.wind import WindIngestor, decode_grib, encode_json, site_bbox, subset

TOLERANCE = 0.011


def validate_row(row: dict, box, r2: R2Client, ingestor: WindIngestor) -> list[str]:
    """Lista de discrepancias (vacía = OK)."""
    got = json.loads(r2.download_bytes(row["r2_key"]))
    cycle = datetime.fromisoformat(row["cycle_time"])
    fh = row["forecast_hour"]

    data = ingestor._fetch_nomads(cycle, fh, box)
    if data is None:
        return [f"NOMADS ya no publica el ciclo {row['cycle_time']} f{fh:03d} (¿muy viejo?)"]
    want = json.loads(encode_json(subset(decode_grib(data), box), cycle, fh))

    problems = []
    if got["header"] != want["header"]:
        problems.append(f"header difiere: {got['header']} != {want['header']}")
    for comp in ("u", "v"):
        if len(got[comp]) != len(want[comp]):
            problems.append(f"{comp}: longitud {len(got[comp])} != {len(want[comp])}")
            continue
        worst = max(abs(a - b) for a, b in zip(got[comp], want[comp], strict=True))
        if worst > TOLERANCE:
            problems.append(f"{comp}: desviación máxima {worst:.4f} m/s > {TOLERANCE}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--site", action="append", default=None, help="repetible; def. todos")
    parser.add_argument("-n", type=int, default=3, help="filas más recientes por sitio (def. 3)")
    args = parser.parse_args()

    cfg = StorageConfig.from_env()
    r2 = R2Client(cfg.r2_endpoint, cfg.r2_bucket, cfg.r2_access_key_id, cfg.r2_secret_access_key)
    failures = 0
    with D1Client(cfg.cf_account_id, cfg.d1_database_id, cfg.cf_api_token) as d1:
        ingestor = WindIngestor(d1, r2)  # solo se usa su fetcher NOMADS
        radars = {r["site_id"]: r for r in d1.execute("SELECT site_id, lat, lon FROM radars")}
        sites = args.site or sorted(radars)
        for site in sites:
            if site not in radars:
                print(f"{site}: no está en radars — saltado")
                continue
            box = site_bbox(radars[site]["lat"], radars[site]["lon"])
            rows = d1.execute(
                "SELECT valid_time, cycle_time, forecast_hour, r2_key FROM wind_grids"
                " WHERE site_id = ? ORDER BY valid_time DESC LIMIT ?",
                [site, args.n],
            )
            if not rows:
                print(f"{site}: sin filas en wind_grids (¿corrió ya el Worker?)")
                failures += 1
                continue
            for row in rows:
                problems = validate_row(row, box, r2, ingestor)
                status = "OK" if not problems else "FALLO"
                cycle = f"c{row['cycle_time']} f{row['forecast_hour']:03d}"
                print(f"{status}  {site} {row['valid_time']} ({cycle})")
                for p in problems:
                    print(f"       {p}")
                failures += bool(problems)
    print("todo consistente" if failures == 0 else f"{failures} validaciones fallidas")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
