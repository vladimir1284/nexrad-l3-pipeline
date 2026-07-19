/**
 * Worker de ingesta de rayos GLM (GOES-19, GLM-L2-LCFA) → JSON por
 * (sitio, cubo de 300 s) a R2 + fila SIEMPRE a `lightning_buckets`
 * (contrato en db/README.md). Dos crons:
 *
 *  - cada minuto: cubos cerrados hace ≥ MARGIN_S dentro del lookback
 *    corto (LOOKBACK_MIN) que falten para algún sitio — el camino
 *    caliente y la auto-recuperación de caídas breves.
 *  - horario (:39): mismo algoritmo con lookback BACKFILL_HOURS — los
 *    ficheros GLM siguen en S3, rellena huecos largos.
 *
 * Idempotente: cubo inmutable, INSERT OR IGNORE, re-put R2 con la misma
 * clave produce el mismo contenido. Orden por cubo: R2 → D1; si D1
 * falla, la reconciliación de nexrad-l3-ops recoge los huérfanos y la
 * fila ausente hace que la próxima corrida reintente. Retención: en el
 * Worker de ops, no aquí.
 *
 * Cubos con ficheros incompletos (< 16 frames listados) se difieren
 * hasta 1 h para no congelar un cubo a medias en un blip del listado;
 * pasada la hora se ingiere lo que haya (huecos reales del downlink
 * GOES existen).
 */

import {
  BUCKET_S,
  FRAMES_PER_BUCKET,
  Flash,
  SOURCE,
  eligibleBucketStarts,
  encodeBucketJson,
  framesForBucket,
  glmHourPrefixes,
  isoNaive,
  lightningKey,
  parseS3ListKeys,
  strikesForSite,
} from "./core.ts";
import { parseGlm } from "./glm.ts";

export interface Env {
  DB: D1Database;
  BUCKET: R2Bucket;
  GLM_BASE?: string;
  LOOKBACK_MIN?: string;
  BACKFILL_HOURS?: string;
  MARGIN_S?: string;
  RADIUS_KM?: string;
  MAX_BUCKETS?: string;
  MAX_BUCKETS_BACKFILL?: string;
}

const BACKFILL_CRON = "39 * * * *";
const DEFER_INCOMPLETE_S = 3600;

const INSERT_SQL = `
INSERT OR IGNORE INTO lightning_buckets
    (site_id, bucket_start, bucket_s, strike_count, r2_key, size_bytes, source)
VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)`;

interface Site {
  site_id: string;
  lat: number;
  lon: number;
}

class LightningRun {
  private listCache = new Map<string, string[]>();
  private fileCache = new Map<string, Flash[]>(); // frame frontera compartido entre cubos vecinos
  buckets = 0;
  rows = 0;
  objects = 0;
  deferred = 0;
  failed = 0;

  constructor(
    private env: Env,
    private base: string,
    private radiusKm: number,
    private maxBuckets: number,
  ) {}

  private async listPrefix(prefix: string): Promise<string[]> {
    const hit = this.listCache.get(prefix);
    if (hit !== undefined) return hit;
    // ~180 ficheros/hora — una página; si S3 truncara, tratamos el cubo
    // como incompleto y se difiere (no paginamos por presupuesto).
    const resp = await fetch(`${this.base}/?list-type=2&prefix=${encodeURIComponent(prefix)}&max-keys=1000`);
    if (!resp.ok) throw new Error(`S3 list HTTP ${resp.status}`);
    const { keys, truncated } = parseS3ListKeys(await resp.text());
    if (truncated) console.warn(`lightning: listado truncado en ${prefix} (inesperado)`);
    this.listCache.set(prefix, keys);
    return keys;
  }

  private async fileFlashes(key: string): Promise<Flash[]> {
    const hit = this.fileCache.get(key);
    if (hit !== undefined) return hit;
    const resp = await fetch(`${this.base}/${key}`);
    if (!resp.ok) throw new Error(`S3 GET ${key} HTTP ${resp.status}`);
    const { flashes } = await parseGlm(new Uint8Array(await resp.arrayBuffer()));
    this.fileCache.set(key, flashes);
    return flashes;
  }

  private async ingestBucket(start: Date, sites: Site[], have: Set<string>, now: Date): Promise<void> {
    const startIso = isoNaive(start);
    const pending = sites.filter((s) => !have.has(`${s.site_id}|${startIso}`));
    if (!pending.length) return;

    let keys: string[] = [];
    for (const prefix of glmHourPrefixes(start)) keys = keys.concat(await this.listPrefix(prefix));
    const frames = framesForBucket(keys, start);

    const ageS = (now.getTime() - start.getTime()) / 1000 - BUCKET_S;
    if (frames.length < FRAMES_PER_BUCKET && ageS < DEFER_INCOMPLETE_S) {
      this.deferred++;
      console.log(`lightning: ${startIso} con ${frames.length}/${FRAMES_PER_BUCKET} frames — se difiere`);
      return;
    }
    if (frames.length < FRAMES_PER_BUCKET) {
      console.warn(`lightning: ${startIso} incompleto (${frames.length}/${FRAMES_PER_BUCKET} frames), se ingiere igual`);
    }

    const flashes: Flash[] = [];
    for (const key of frames) flashes.push(...(await this.fileFlashes(key)));

    const stmts = [];
    for (const site of pending) {
      const strikes = strikesForSite(flashes, site.lat, site.lon, this.radiusKm, start);
      let r2Key: string | null = null;
      let size: number | null = null;
      if (strikes.length) {
        r2Key = lightningKey(site.site_id, start);
        const body = encodeBucketJson(site.site_id, start, strikes);
        size = body.length;
        await this.env.BUCKET.put(r2Key, body, {
          httpMetadata: {
            contentType: "application/json",
            cacheControl: "public, max-age=31536000, immutable",
          },
        });
        this.objects++;
      }
      stmts.push(
        this.env.DB.prepare(INSERT_SQL).bind(
          site.site_id,
          startIso,
          BUCKET_S,
          strikes.length,
          r2Key,
          size,
          SOURCE,
        ),
      );
    }
    await this.env.DB.batch(stmts);
    for (const site of pending) have.add(`${site.site_id}|${startIso}`);
    this.buckets++;
    this.rows += stmts.length;
  }

  async run(now: Date, windowS: number, marginS: number): Promise<void> {
    const sites = (
      await this.env.DB.prepare("SELECT site_id, lat, lon FROM radars ORDER BY site_id").all<Site>()
    ).results;
    if (!sites.length) {
      console.log("lightning: sin radares en D1 todavía — nada que hacer");
      return;
    }

    const candidates = eligibleBucketStarts(now, windowS, marginS);
    if (!candidates.length) return;

    const oldest = isoNaive(candidates[candidates.length - 1]);
    const existing = (
      await this.env.DB.prepare(
        "SELECT site_id, bucket_start FROM lightning_buckets WHERE bucket_start >= ?1",
      )
        .bind(oldest)
        .all<{ site_id: string; bucket_start: string }>()
    ).results;
    const have = new Set(existing.map((r) => `${r.site_id}|${r.bucket_start}`));

    const targets = candidates
      .filter((b) => sites.some((s) => !have.has(`${s.site_id}|${isoNaive(b)}`)))
      .slice(0, this.maxBuckets);

    for (const bucket of targets) {
      try {
        await this.ingestBucket(bucket, sites, have, now);
      } catch (exc) {
        this.failed++;
        console.error(`lightning: fallo en cubo ${isoNaive(bucket)} (reintento próxima corrida):`, exc);
      }
    }
    if (targets.length || this.failed) {
      console.log(
        `lightning: cubos=${this.buckets} filas=${this.rows} objetos=${this.objects} diferidos=${this.deferred} fallidos=${this.failed}`,
      );
    }
  }
}

export default {
  async scheduled(controller: ScheduledController, env: Env, _ctx: ExecutionContext) {
    const backfill = controller.cron === BACKFILL_CRON;
    const windowS = backfill
      ? parseFloat(env.BACKFILL_HOURS || "72") * 3600
      : parseFloat(env.LOOKBACK_MIN || "30") * 60;
    const cap = backfill
      ? parseInt(env.MAX_BUCKETS_BACKFILL || "30", 10)
      : parseInt(env.MAX_BUCKETS || "4", 10);
    const run = new LightningRun(
      env,
      (env.GLM_BASE || "https://noaa-goes19.s3.amazonaws.com").replace(/\/$/, ""),
      parseFloat(env.RADIUS_KM || "460"),
      cap,
    );
    await run.run(new Date(), windowS, parseFloat(env.MARGIN_S || "90"));
  },
} satisfies ExportedHandler<Env>;
