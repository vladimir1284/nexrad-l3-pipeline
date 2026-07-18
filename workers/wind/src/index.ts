/**
 * Worker de ingesta de viento GFS 0.25° 10 m (u/v) para la capa de
 * partículas del viewer. Cron horario: para cada valid_time de la
 * ventana [now − 72 h, now + 2 h] publica el JSON por sitio a R2 y la
 * fila a `wind_grids` en D1 (upsert que solo gana con `cycle_time` más
 * nuevo). Reemplaza al servicio `wind` del stack Swarm; la referencia
 * Python (`ingest/wind.py`, `l3proc wind`) queda para validación
 * cruzada — `scripts/validate_wind_worker.py`.
 *
 * Fuente: filtro GRIB de NOMADS (OPeNDAP fue retirado — SCN 25-81,
 * verificado 2026-07-18). Un fichero por (ciclo, fh) con el bbox unión
 * de todos los sitios de `radars`; recorte local por sitio (grilla
 * regular alineada a 0.25° → subset puro). Decode en src/grib.ts.
 *
 * Presupuesto por corrida: MAX_FETCHES descargas de NOMADS (cortesía —
 * bloquean IPs > ~120 hits/min — y margen para el límite de subrequests
 * del plan). Los valid_times se recorren del más nuevo al más viejo:
 * si el presupuesto se agota, lo fresco ya quedó publicado y el backfill
 * continúa en la corrida siguiente (idempotente, estado en D1).
 */

import {
  BBox,
  MODEL,
  candidateCycles,
  ceilHour,
  encodeJson,
  floorHour,
  isoNaive,
  nomadsUrl,
  siteBBox,
  subsetField,
  unionBBox,
  windKey,
} from "./core.ts";
import { WindField, decodeWind } from "./grib.ts";

export interface Env {
  DB: D1Database;
  BUCKET: R2Bucket;
  WINDOW_HOURS?: string;
  LOOKAHEAD_HOURS?: string;
  NOMADS_PAUSE_MS?: string;
  MAX_FETCHES?: string;
}

const UPSERT_SQL = `
INSERT INTO wind_grids
    (site_id, valid_time, cycle_time, forecast_hour, model, r2_key, size_bytes)
VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)
ON CONFLICT (site_id, valid_time) DO UPDATE SET
    cycle_time = excluded.cycle_time,
    forecast_hour = excluded.forecast_hour,
    model = excluded.model,
    r2_key = excluded.r2_key,
    size_bytes = excluded.size_bytes
WHERE excluded.cycle_time > wind_grids.cycle_time`;

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

class FetchBudgetExhausted extends Error {}

interface Existing {
  cycle: string;
  key: string;
}

class WindRun {
  private cache = new Map<string, WindField | null>();
  private fetches = 0;
  published = 0;
  fresh = 0;
  failed = 0;

  constructor(
    private env: Env,
    private pauseMs: number,
    private maxFetches: number,
  ) {}

  /** GRIB del filtro para (ciclo, fh); null = aún no publicado en NOMADS. */
  private async field(cycle: Date, fh: number, box: BBox): Promise<WindField | null> {
    const key = `${isoNaive(cycle)}|${fh}`;
    const hit = this.cache.get(key);
    if (hit !== undefined) return hit;
    if (this.fetches >= this.maxFetches) throw new FetchBudgetExhausted();
    this.fetches++;
    await sleep(this.pauseMs); // cortesía con NOMADS
    const resp = await fetch(nomadsUrl(cycle, fh, box), {
      headers: { "user-agent": "nexrad-l3-pipeline/wind-worker" },
    });
    let out: WindField | null = null;
    if (resp.ok) {
      const buf = await resp.arrayBuffer();
      // 200 con HTML de "data file is not present" cuenta como no disponible
      if (new DataView(buf).byteLength >= 4 && new DataView(buf).getUint32(0) === 0x47524942) {
        out = decodeWind(buf);
      }
    } else if (resp.status !== 404) {
      await resp.body?.cancel();
      throw new Error(`NOMADS HTTP ${resp.status}`); // 5xx/429: reintento próxima corrida
    }
    this.cache.set(key, out);
    return out;
  }

  private async publish(
    site: string,
    box: BBox,
    vt: Date,
    cycle: Date,
    fh: number,
    field: WindField,
    existing: Map<string, Existing>,
  ): Promise<void> {
    const body = encodeJson(subsetField(field, box), cycle, fh);
    const key = windKey(site, vt, cycle, fh);
    const mapKey = `${site}|${isoNaive(vt)}`;
    const old = existing.get(mapKey);
    // orden: R2 → D1 → borrar el reemplazado. Si D1 falla, el objeto nuevo
    // queda huérfano y lo recoge la reconciliación del Worker de ops.
    await this.env.BUCKET.put(key, body, {
      httpMetadata: {
        contentType: "application/json",
        cacheControl: "public, max-age=31536000, immutable",
      },
    });
    await this.env.DB.prepare(UPSERT_SQL)
      .bind(site, isoNaive(vt), isoNaive(cycle), fh, MODEL, key, body.length)
      .run();
    if (old && old.key !== key) await this.env.BUCKET.delete(old.key);
    existing.set(mapKey, { cycle: isoNaive(cycle), key });
    this.published++;
  }

  private async ingestValidTime(
    vt: Date,
    boxes: Map<string, BBox>,
    union: BBox,
    existing: Map<string, Existing>,
  ): Promise<void> {
    let served = false;
    for (const { cycle, fh } of candidateCycles(vt)) {
      const cycleIso = isoNaive(cycle);
      const wanting = [...boxes.keys()].filter((site) => {
        const row = existing.get(`${site}|${isoNaive(vt)}`);
        return row === undefined || row.cycle < cycleIso;
      });
      if (!wanting.length) break; // todos con ciclo >= cualquier candidato restante
      const field = await this.field(cycle, fh, union);
      if (field === null) continue; // ciclo aún no publicado → probar uno más viejo
      for (const site of wanting) {
        await this.publish(site, boxes.get(site)!, vt, cycle, fh, field, existing);
      }
      served = true;
      break;
    }
    if (!served) this.fresh++;
  }

  async run(now: Date): Promise<void> {
    const windowMs = parseFloat(this.env.WINDOW_HOURS || "72") * 3_600_000;
    const lookaheadMs = parseFloat(this.env.LOOKAHEAD_HOURS || "2") * 3_600_000;

    const sites = (
      await this.env.DB.prepare("SELECT site_id, lat, lon FROM radars ORDER BY site_id").all<{
        site_id: string;
        lat: number;
        lon: number;
      }>()
    ).results;
    if (!sites.length) {
      console.log("wind: sin radares en D1 todavía — nada que hacer");
      return;
    }
    const boxes = new Map(sites.map((s) => [s.site_id, siteBBox(s.lat, s.lon)]));
    const union = unionBBox([...boxes.values()]);

    const rows = (
      await this.env.DB.prepare(
        "SELECT site_id, valid_time, cycle_time, r2_key FROM wind_grids",
      ).all<{ site_id: string; valid_time: string; cycle_time: string; r2_key: string }>()
    ).results;
    const existing = new Map<string, Existing>(
      rows.map((r) => [`${r.site_id}|${r.valid_time}`, { cycle: r.cycle_time, key: r.r2_key }]),
    );

    // del más nuevo al más viejo: lo fresco primero si el presupuesto se agota
    const first = ceilHour(new Date(now.getTime() - windowMs));
    for (
      let vt = floorHour(new Date(now.getTime() + lookaheadMs));
      vt >= first;
      vt = new Date(vt.getTime() - 3_600_000)
    ) {
      try {
        await this.ingestValidTime(vt, boxes, union, existing);
      } catch (exc) {
        if (exc instanceof FetchBudgetExhausted) {
          console.log(`wind: presupuesto de descargas agotado en ${isoNaive(vt)} — continúa en la próxima corrida`);
          break;
        }
        this.failed++;
        console.error(`wind: fallo en valid_time ${isoNaive(vt)} (reintento próxima corrida):`, exc);
      }
    }
    console.log(
      `wind: publicados=${this.published} al_día=${this.fresh} fallidos=${this.failed} descargas=${this.fetches}`,
    );
  }
}

export default {
  async scheduled(_controller: ScheduledController, env: Env, _ctx: ExecutionContext) {
    const run = new WindRun(
      env,
      parseFloat(env.NOMADS_PAUSE_MS || "2000"),
      parseInt(env.MAX_FETCHES || "20", 10),
    );
    await run.run(new Date());
  },
} satisfies ExportedHandler<Env>;
