/**
 * Lógica pura de la ingesta de rayos GLM: cubos de 300 s, claves R2,
 * timestamps de los ficheros LCFA, recorte por sitio y formato JSON del
 * contrato (db/README.md). Sin I/O — testeable bajo node --test.
 */

export const BUCKET_S = 300; // contrato: cubos fijos alineados a UTC
export const FRAME_S = 20; // cadencia de ficheros GLM-L2-LCFA
export const SOURCE = "glm-goes19";

/** Frames que cubren un cubo, incluido el frame s = bucket_end: un flash
 * que cruza frames aparece en el fichero posterior con el primer evento
 * hacia atrás (verificado con datos reales: offsets negativos en el
 * primer frame del día). */
export const FRAMES_PER_BUCKET = BUCKET_S / FRAME_S + 1;

export interface Flash {
  lon: number;
  lat: number;
  epochS: number; // UTC, con décimas
}

export type Strike = [lon: number, lat: number, offsetS: number];

const pad = (n: number, w: number): string => String(n).padStart(w, "0");

/** ISO 8601 UTC naive 'YYYY-MM-DDTHH:MM:SS' — convención del schema D1. */
export function isoNaive(d: Date): string {
  return d.toISOString().slice(0, 19);
}

/** Inicios de cubo elegibles en [now − windowS, now], del más nuevo al
 * más viejo. Elegible = cerrado hace ≥ marginS (latencia GLM). */
export function eligibleBucketStarts(now: Date, windowS: number, marginS: number): Date[] {
  const nowS = Math.floor(now.getTime() / 1000);
  const newest = Math.floor((nowS - marginS - BUCKET_S) / BUCKET_S) * BUCKET_S;
  const oldest = Math.ceil((nowS - windowS) / BUCKET_S) * BUCKET_S;
  const out: Date[] = [];
  for (let t = newest; t >= oldest; t -= BUCKET_S) out.push(new Date(t * 1000));
  return out;
}

/** Clave R2 del contrato: {SITE}/LIGHTNING/{YYYY}/{MM}/{DD}/{SITE}_LTG_{YYYYMMDD}_{HHMMSS}.json */
export function lightningKey(site: string, bucketStart: Date): string {
  const y = bucketStart.getUTCFullYear();
  const mo = pad(bucketStart.getUTCMonth() + 1, 2);
  const dd = pad(bucketStart.getUTCDate(), 2);
  const hh = pad(bucketStart.getUTCHours(), 2);
  const mi = pad(bucketStart.getUTCMinutes(), 2);
  const ss = pad(bucketStart.getUTCSeconds(), 2);
  return `${site}/LIGHTNING/${y}/${mo}/${dd}/${site}_LTG_${y}${mo}${dd}_${hh}${mi}${ss}.json`;
}

function dayOfYear(d: Date): number {
  return Math.floor((d.getTime() - Date.UTC(d.getUTCFullYear(), 0, 1)) / 86_400_000) + 1;
}

/** Prefijos horarios S3 (GLM-L2-LCFA/YYYY/DDD/HH/) que cubren los frames
 * [start, start + BUCKET_S] — dos cuando el frame extra cae en la hora
 * siguiente (cubos :55). */
export function glmHourPrefixes(bucketStart: Date, product = "GLM-L2-LCFA"): string[] {
  const out: string[] = [];
  for (const t of [bucketStart, new Date(bucketStart.getTime() + BUCKET_S * 1000)]) {
    const p = `${product}/${t.getUTCFullYear()}/${pad(dayOfYear(t), 3)}/${pad(t.getUTCHours(), 2)}/`;
    if (!out.includes(p)) out.push(p);
  }
  return out;
}

/** Epoch (s UTC) del campo s del nombre LCFA
 * (OR_GLM-L2-LCFA_G19_sYYYYDDDHHMMSSt_…); null si la clave no matchea. */
export function glmKeyStartEpoch(key: string): number | null {
  const m = /_s(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})(\d)_/.exec(key);
  if (!m) return null;
  const [, y, doy, hh, mi, ss, tenth] = m;
  return (
    Date.UTC(Number(y), 0, 1) / 1000 +
    (Number(doy) - 1) * 86_400 +
    Number(hh) * 3600 +
    Number(mi) * 60 +
    Number(ss) +
    Number(tenth) / 10
  );
}

/** Claves <Key>…</Key> de un listado REST de S3 (list-type=2). */
export function parseS3ListKeys(xml: string): { keys: string[]; truncated: boolean } {
  const keys = [...xml.matchAll(/<Key>([^<]+)<\/Key>/g)].map((m) => m[1]);
  const truncated = /<IsTruncated>true<\/IsTruncated>/.test(xml);
  return { keys, truncated };
}

/** Frames del cubo: s ∈ [start, start + BUCKET_S], extremo superior
 * inclusive (frame extra de la regla de frontera). */
export function framesForBucket(keys: string[], bucketStart: Date): string[] {
  const startS = bucketStart.getTime() / 1000;
  return keys.filter((k) => {
    const s = glmKeyStartEpoch(k);
    return s !== null && s >= startS && s <= startS + BUCKET_S;
  });
}

const EARTH_RADIUS_KM = 6371.0;

export function haversineKm(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const rad = Math.PI / 180;
  const dLat = (lat2 - lat1) * rad;
  const dLon = (lon2 - lon1) * rad;
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(lat1 * rad) * Math.cos(lat2 * rad) * Math.sin(dLon / 2) ** 2;
  return 2 * EARTH_RADIUS_KM * Math.asin(Math.sqrt(a));
}

const round = (v: number, decimals: number): number => {
  const f = 10 ** decimals;
  return Math.round(v * f) / f;
};

/** Strikes de un sitio: flashes del cubo a ≤ radiusKm del radar, como
 * [lon, lat, offset_s] (3 decimales, 1 decimal) en offset ascendente.
 * El redondeo a 1 decimal puede tocar el techo (299.96 → 300.0): se
 * clava a BUCKET_S − 0.1 para mantener offset ∈ [0, bucket_s). */
export function strikesForSite(
  flashes: Flash[],
  siteLat: number,
  siteLon: number,
  radiusKm: number,
  bucketStart: Date,
): Strike[] {
  const startS = bucketStart.getTime() / 1000;
  const out: Strike[] = [];
  for (const f of flashes) {
    if (f.epochS < startS || f.epochS >= startS + BUCKET_S) continue;
    if (haversineKm(siteLat, siteLon, f.lat, f.lon) > radiusKm) continue;
    const offset = Math.min(round(f.epochS - startS, 1), BUCKET_S - 0.1);
    out.push([round(f.lon, 3), round(f.lat, 3), offset]);
  }
  out.sort((a, b) => a[2] - b[2]);
  return out;
}

/** Epoch (s) de un atributo units GLM "seconds since YYYY-MM-DD HH:MM:SS(.mmm)". */
export function parseUnitsBase(units: string): number {
  const m = /seconds since (\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(\.\d+)?/.exec(units);
  if (!m) throw new Error(`units de tiempo GLM no reconocidas: "${units}"`);
  const [, y, mo, d, hh, mi, ss, frac] = m;
  return (
    Date.UTC(Number(y), Number(mo) - 1, Number(d), Number(hh), Number(mi), Number(ss)) / 1000 +
    (frac ? Number(frac) : 0)
  );
}

/** JSON del contrato (db/README.md). */
export function encodeBucketJson(site: string, bucketStart: Date, strikes: Strike[]): string {
  return JSON.stringify({
    site,
    bucket_start: isoNaive(bucketStart),
    bucket_s: BUCKET_S,
    strikes,
  });
}
