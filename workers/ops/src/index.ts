/**
 * Worker de operación del pipeline NEXRAD L3: monitor de frescura E2E
 * (cron cada 5 min) + sweep de retención y reconciliación R2↔Postgres
 * (cron horario).
 *
 * Ya no tiene binding D1: la metadata vive en un Postgres self-hosted
 * en el Swarm (migración de D1 por cuota agotada del plan gratuito de
 * Cloudflare), inalcanzable directo desde un Worker sin meter
 * Hyperdrive+Tunnel+Access de por medio. En su lugar, este Worker hace
 * fetch() contra una API HTTP mínima (`api/`, en el mismo Swarm) que
 * expone justo lo que este archivo necesita — ver api/README.md.
 *
 * Señal de frescura independiente (Layer A): este Worker existe para
 * alertar cuando el VPS/Swarm entero muere, así que "la API no
 * responde" no puede tratarse como "sin datos" — sería indistinguible
 * de un pipeline sano cuya única falla es la API. Cuando una llamada a
 * la API falla, cada chequeo cae a un chequeo R2-only (env.BUCKET, que
 * no depende del Swarm para nada) y reporta un motivo explícitamente
 * "degradado" en vez de silenciarse o mentir "sin datos frescos".
 *
 * Diferencias deliberadas con la versión Python original (preservadas
 * de la migración D1→Worker):
 *  - Primer chequeo de un sitio (sin fila de estado) manda resumen por
 *    Telegram — el monitor prueba que está vivo; el original solo
 *    hablaba en transiciones y un arranque en verde era mudo para
 *    siempre, indistinguible de un monitor muerto.
 *  - La reconciliación ignora objetos R2 subidos hace < 1 h: publish
 *    sube a R2 antes de insertar en Postgres, y un sweep en esa
 *    ventana veía un huérfano falso.
 *
 * El monitor cubre tres capas por sitio: rasters (clave = sitio pelado
 * en ops_monitor_state), viento (`SITE:wind`) y rayos (`SITE:ltg`).
 * Wind/lightning se activan solos cuando su tabla tiene filas.
 *
 * Los vol_time de Postgres son ISO 8601 UTC *naive* (sin sufijo Z). JS
 * interpreta esos strings como hora local, así que SIEMPRE se parsea
 * con "Z" explícita — ver parseUtc().
 */

export interface Env {
  BUCKET: R2Bucket;
  API_BASE_URL: string;
  API_TOKEN: string;
  NEXRAD_SITES: string;
  MAX_AGE_MIN: string;
  WIND_MIN_LEAD_H: string;
  LTG_MAX_AGE_MIN: string;
  WINDOW_HOURS: string;
  TELEGRAM_BOT_TOKEN?: string;
  TELEGRAM_CHAT_ID?: string;
}

const MONITOR_CRON = "*/5 * * * *";
const R2_DELETE_CHUNK = 1000; // máximo del binding R2 por llamada delete()
const RECONCILE_GRACE_MS = 3_600_000; // 1 h: no tocar objetos R2 recién subidos

function parseUtc(volTime: string): number {
  return Date.parse(volTime + "Z");
}

// ------------------------------------------------------------------- API

class ApiError extends Error {}

async function apiRequest<T>(env: Env, path: string, init?: RequestInit): Promise<T> {
  let resp: Response;
  try {
    resp = await fetch(`${env.API_BASE_URL}${path}`, {
      ...init,
      headers: { ...init?.headers, Authorization: `Bearer ${env.API_TOKEN}` },
    });
  } catch (exc) {
    throw new ApiError(`fetch a ${path} falló: ${exc}`);
  }
  if (!resp.ok) {
    throw new ApiError(`${path} respondió HTTP ${resp.status}`);
  }
  return (await resp.json()) as T;
}

function apiGet<T>(env: Env, path: string): Promise<T> {
  return apiRequest<T>(env, path);
}

function apiPost<T>(env: Env, path: string, body: unknown): Promise<T> {
  return apiRequest<T>(env, path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

// ---------------------------------------------------------------- telegram

async function sendTelegram(env: Env, text: string): Promise<void> {
  console.warn("notify:", text);
  if (!env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID) return; // solo-log
  const resp = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ chat_id: env.TELEGRAM_CHAT_ID, text }),
  });
  if (!resp.ok) {
    console.error(`telegram: HTTP ${resp.status} — ${(await resp.text()).slice(0, 200)}`);
  }
}

// ----------------------------------------------------------------- monitor

/** Una comprobación por (sitio, capa). `key` es la clave en
 * ops_monitor_state: el sitio pelado para rasters (formato histórico,
 * no se migra) y `SITE:wind` / `SITE:ltg` para las capas añadidas
 * después. */
interface CheckStatus {
  key: string;
  fresh: boolean;
  reason: string;
}

/** Objeto R2 más reciente bajo un prefijo — señal de frescura
 * independiente de Postgres/la API, usada solo en modo degradado. */
async function mostRecentUpload(env: Env, prefix: string): Promise<Date | null> {
  let latest: Date | null = null;
  let cursor: string | undefined;
  do {
    const page = await env.BUCKET.list({ prefix, cursor, limit: 1000 });
    for (const obj of page.objects) {
      if (!latest || obj.uploaded > latest) latest = obj.uploaded;
    }
    cursor = page.truncated ? page.cursor : undefined;
  } while (cursor);
  return latest;
}

async function fallbackByRecentUpload(
  env: Env,
  key: string,
  prefix: string,
  maxAgeMin: number,
): Promise<CheckStatus> {
  const latest = await mostRecentUpload(env, prefix);
  if (!latest) {
    return { key, fresh: false, reason: "🟠 degradado (API inalcanzable) — sin objetos R2" };
  }
  const ageMin = (Date.now() - latest.getTime()) / 60_000;
  const fresh = ageMin <= maxAgeMin;
  return {
    key,
    fresh,
    reason: `🟠 degradado (API inalcanzable) — R2 ${fresh ? "ok" : "viejo"} (${Math.round(ageMin)} min)`,
  };
}

async function checkRaster(env: Env, site: string, maxAgeMin: number): Promise<CheckStatus> {
  try {
    const row = await apiGet<{ vol_time: string; r2_key: string } | null>(
      env,
      `/v1/checks/raster?site=${encodeURIComponent(site)}`,
    );
    if (!row) return { key: site, fresh: false, reason: "sin datos" };
    const ageMin = (Date.now() - parseUtc(row.vol_time)) / 60_000;
    if (ageMin > maxAgeMin) {
      return { key: site, fresh: false, reason: `viejo (${Math.round(ageMin)} min)` };
    }
    if ((await env.BUCKET.head(row.r2_key)) === null) {
      return { key: site, fresh: false, reason: "falta objeto R2" };
    }
    return { key: site, fresh: true, reason: `ok (${Math.round(ageMin)} min)` };
  } catch (exc) {
    console.error(`monitor: API inalcanzable para checkRaster(${site}), cayendo a R2:`, exc);
    return fallbackByRecentUpload(env, site, `${site}/N0B/`, maxAgeMin);
  }
}

/** Viento: lo que rompe al viewer es quedarse sin cobertura futura, no
 * la edad del último insert — fresco = MAX(valid_time) llega al menos
 * WIND_MIN_LEAD_H horas por delante de ahora. En modo degradado se cae
 * a edad-del-último-objeto (no hay forma de derivar cobertura futura
 * solo de R2), que sigue siendo una señal útil de "el pipeline vive". */
async function checkWind(env: Env, site: string, minLeadH: number): Promise<CheckStatus> {
  const key = `${site}:wind`;
  try {
    const row = await apiGet<{ valid_time: string; r2_key: string } | null>(
      env,
      `/v1/checks/wind?site=${encodeURIComponent(site)}`,
    );
    if (!row) return { key, fresh: false, reason: "sin datos" };
    const leadH = (parseUtc(row.valid_time) - Date.now()) / 3_600_000;
    if (leadH < minLeadH) {
      return {
        key,
        fresh: false,
        reason: `cobertura hasta ${row.valid_time} (${leadH.toFixed(1)} h)`,
      };
    }
    if ((await env.BUCKET.head(row.r2_key)) === null) {
      return { key, fresh: false, reason: "falta objeto R2" };
    }
    return { key, fresh: true, reason: `ok (cobertura +${leadH.toFixed(1)} h)` };
  } catch (exc) {
    console.error(`monitor: API inalcanzable para checkWind(${site}), cayendo a R2:`, exc);
    return fallbackByRecentUpload(env, key, `${site}/WIND/`, minLeadH * 60);
  }
}

async function checkLightning(env: Env, site: string, maxAgeMin: number): Promise<CheckStatus> {
  const key = `${site}:ltg`;
  try {
    const row = await apiGet<{ bucket_start: string; r2_key: string | null } | null>(
      env,
      `/v1/checks/lightning?site=${encodeURIComponent(site)}`,
    );
    if (!row) return { key, fresh: false, reason: "sin datos" };
    const ageMin = (Date.now() - parseUtc(row.bucket_start)) / 60_000;
    if (ageMin > maxAgeMin) {
      return { key, fresh: false, reason: `viejo (${Math.round(ageMin)} min)` };
    }
    // r2_key NULL = cubo sin rayos, no hay objeto que verificar.
    if (row.r2_key !== null && (await env.BUCKET.head(row.r2_key)) === null) {
      return { key, fresh: false, reason: "falta objeto R2" };
    }
    return { key, fresh: true, reason: `ok (${Math.round(ageMin)} min)` };
  } catch (exc) {
    console.error(`monitor: API inalcanzable para checkLightning(${site}), cayendo a R2:`, exc);
    return fallbackByRecentUpload(env, key, `${site}/LIGHTNING/`, maxAgeMin);
  }
}

/** Una capa entra al monitor cuando su tabla tiene filas o ya hay
 * estado previo suyo. Si la API falla y no hay estado previo, se trata
 * como "todavía no desplegada" (mismo comportamiento que una tabla sin
 * migrar en la versión D1) — no es el modo degradado de los checks
 * individuales porque activar una capa nueva requiere sí o sí poder
 * confirmar contra la base. */
async function layerActive(
  env: Env,
  prev: Map<string, number>,
  layer: "wind" | "lightning",
  suffix: string,
): Promise<boolean> {
  if ([...prev.keys()].some((k) => k.endsWith(suffix))) return true;
  try {
    return await apiGet<boolean>(env, `/v1/layers/${layer}/active`);
  } catch (exc) {
    console.error(`monitor: ${layer} — no se pudo confirmar contra la API:`, exc);
    return false;
  }
}

function fmtStatus(st: CheckStatus): string {
  return `${st.fresh ? "🟢" : "🔴"} ${st.key}: ${st.reason}`;
}

async function runMonitor(env: Env): Promise<void> {
  const sites = env.NEXRAD_SITES.split(",").map((s) => s.trim()).filter(Boolean);
  const maxAgeMin = parseFloat(env.MAX_AGE_MIN || "30");
  const windMinLeadH = parseFloat(env.WIND_MIN_LEAD_H || "2");
  const ltgMaxAgeMin = parseFloat(env.LTG_MAX_AGE_MIN || "30");

  let prevRows: { site_id: string; fresh: number }[];
  let degraded = false;
  try {
    prevRows = await apiGet<{ site_id: string; fresh: number }[]>(env, "/v1/monitor-state");
  } catch (exc) {
    console.error("monitor: API inalcanzable, no se puede leer ops_monitor_state:", exc);
    await sendTelegram(
      env,
      "🟠 API/Postgres inalcanzable — monitor en modo degradado (solo señal R2, sin persistencia de estado ni detección de transiciones)",
    );
    prevRows = [];
    degraded = true;
  }
  const prev = new Map(prevRows.map((r) => [r.site_id, r.fresh]));

  const windActive = degraded || (await layerActive(env, prev, "wind", ":wind"));
  const ltgActive = degraded || (await layerActive(env, prev, "lightning", ":ltg"));
  if (!windActive) console.log("monitor: wind_grids vacía y sin estado previo — capa no desplegada, se omite");
  if (!ltgActive) console.log("monitor: lightning_buckets vacía y sin estado previo — capa no desplegada, se omite");

  const checks: Array<{ label: string; run: () => Promise<CheckStatus> }> = [];
  for (const site of sites) {
    checks.push({ label: site, run: () => checkRaster(env, site, maxAgeMin) });
    if (windActive) checks.push({ label: `${site}:wind`, run: () => checkWind(env, site, windMinLeadH) });
    if (ltgActive) checks.push({ label: `${site}:ltg`, run: () => checkLightning(env, site, ltgMaxAgeMin) });
  }

  const statuses: CheckStatus[] = [];
  for (const { label, run } of checks) {
    try {
      statuses.push(await run());
    } catch (exc) {
      console.error(`monitor: fallo comprobando ${label} (se reintenta):`, exc);
    }
  }

  if (degraded) {
    // Sin `prev` no hay transiciones que detectar — solo reportar el
    // estado crudo de la señal R2, una vez por ciclo.
    if (statuses.length) await sendTelegram(env, statuses.map(fmtStatus).join("\n"));
    return;
  }

  const firstEval = statuses.filter((st) => !prev.has(st.key));
  const messages: string[] = [];
  if (firstEval.length) {
    messages.push("🩺 monitor activo — primer chequeo:\n" + firstEval.map(fmtStatus).join("\n"));
  }
  for (const st of statuses) {
    const p = prev.get(st.key);
    if (p === undefined) continue; // ya cubierto por el resumen
    if (!st.fresh && p === 1) {
      messages.push(`🔴 ${st.key}: sin datos frescos — ${st.reason}`);
    } else if (st.fresh && p === 0) {
      messages.push(`🟢 ${st.key}: recuperado — ${st.reason}`);
    } else if (st.fresh) {
      console.log(`monitor: ${st.key} ${st.reason}`);
    }
  }
  for (const text of messages) await sendTelegram(env, text);

  if (statuses.length) {
    try {
      await apiPost(
        env,
        "/v1/monitor-state",
        statuses.map((st) => ({
          site_id: st.key,
          fresh: st.fresh ? 1 : 0,
          reason: st.reason,
          updated_at: new Date().toISOString().slice(0, 19),
        })),
      );
    } catch (exc) {
      console.error("monitor: no se pudo persistir ops_monitor_state (se reintenta el próximo ciclo):", exc);
    }
  }
}

// ------------------------------------------------------------------- sweep

async function deleteR2Keys(env: Env, keys: string[]): Promise<void> {
  for (let i = 0; i < keys.length; i += R2_DELETE_CHUNK) {
    await env.BUCKET.delete(keys.slice(i, i + R2_DELETE_CHUNK));
  }
}

interface ExpiredR2Keys {
  rasters: string[];
  wind_grids: string[];
  lightning_buckets: string[];
}

interface PurgeResult {
  rasters: number;
  wind_grids: number;
  lightning_buckets: number;
  phenomena: number;
  vwp: number;
}

/** Borra todo lo anterior a la ventana: objetos R2 primero (vía la API,
 * de solo lectura), filas Postgres después (POST purge) — si el
 * borrado R2 falla a mitad, las filas sobreviven y el siguiente sweep
 * reintenta (el cutoff solo avanza, las mismas filas vuelven a
 * calificar). Si la propia API no responde, se omite el ciclo entero
 * — no es time-critical, corre cada hora. */
async function runSweep(env: Env): Promise<void> {
  const windowHours = parseFloat(env.WINDOW_HOURS || "72");
  const cutoff = new Date(Date.now() - windowHours * 3_600_000).toISOString().slice(0, 19);

  let expired: ExpiredR2Keys;
  try {
    expired = await apiGet<ExpiredR2Keys>(env, `/v1/sweep/expired-r2-keys?cutoff=${cutoff}`);
  } catch (exc) {
    console.error("sweep: API inalcanzable, se omite este ciclo (reintenta la próxima hora):", exc);
    return;
  }
  await deleteR2Keys(env, expired.rasters);
  await deleteR2Keys(env, expired.wind_grids);
  await deleteR2Keys(env, expired.lightning_buckets);

  try {
    const deleted = await apiPost<PurgeResult>(env, "/v1/sweep/purge", { cutoff });
    console.log(
      `sweep: cutoff=${cutoff} rasters=${deleted.rasters} wind_grids=${deleted.wind_grids} lightning_buckets=${deleted.lightning_buckets} phenomena=${deleted.phenomena} vwp=${deleted.vwp}`,
    );
  } catch (exc) {
    console.error("sweep: objetos R2 borrados pero la purga de filas falló (reintenta la próxima hora):", exc);
    return;
  }
  await reconcile(env);
}

interface ReconcileKeys {
  rasters: string[];
  wind_grids: string[];
  lightning_buckets: string[];
}

/** Compara el bucket con lo que Postgres dice que existe; borra
 * huérfanos R2 y filas colgantes. Objetos con menos de 1 h en el
 * bucket no cuentan como huérfanos (ventana upload-R2 → insert-DB del
 * publish). */
async function reconcile(env: Env): Promise<void> {
  const graceCutoff = Date.now() - RECONCILE_GRACE_MS;
  const inR2 = new Set<string>();
  let cursor: string | undefined;
  do {
    const page = await env.BUCKET.list({ cursor, limit: 1000 });
    for (const obj of page.objects) {
      if (obj.uploaded.getTime() < graceCutoff) inR2.add(obj.key);
    }
    cursor = page.truncated ? page.cursor : undefined;
  } while (cursor);

  let keys: ReconcileKeys;
  try {
    keys = await apiGet<ReconcileKeys>(env, "/v1/reconcile/keys");
  } catch (exc) {
    console.error("reconcile: API inalcanzable, se omite este ciclo (reintenta la próxima hora):", exc);
    return;
  }
  const inDb = new Map<string, "rasters" | "wind_grids" | "lightning_buckets">();
  for (const table of ["rasters", "wind_grids", "lightning_buckets"] as const) {
    for (const key of keys[table]) inDb.set(key, table);
  }

  const orphans = [...inR2].filter((k) => !inDb.has(k)).sort();
  const dangling = [...inDb.keys()].filter((k) => !inR2.has(k)).sort();
  // Filas más recientes que la gracia pueden apuntar a objetos aún no
  // listados arriba — verificar contra el bucket antes de declararlas
  // colgantes.
  const confirmed: string[] = [];
  for (const key of dangling) {
    if ((await env.BUCKET.head(key)) === null) confirmed.push(key);
  }

  if (orphans.length || confirmed.length) {
    console.warn(`reconcile: ${orphans.length} huérfanos R2, ${confirmed.length} filas colgantes (corrigiendo)`);
    await deleteR2Keys(env, orphans);
    for (const table of ["rasters", "wind_grids", "lightning_buckets"] as const) {
      const tableKeys = confirmed.filter((k) => inDb.get(k) === table);
      if (tableKeys.length) {
        try {
          await apiPost(env, "/v1/reconcile/delete-dangling", { table, keys: tableKeys });
        } catch (exc) {
          console.error(`reconcile: no se pudieron borrar filas colgantes de ${table}:`, exc);
        }
      }
    }
  } else {
    console.log(`reconcile: consistente (${inR2.size} objetos)`);
  }
}

// ---------------------------------------------------------------- handler

export default {
  async scheduled(controller: ScheduledController, env: Env, _ctx: ExecutionContext): Promise<void> {
    if (controller.cron === MONITOR_CRON) {
      await runMonitor(env);
    } else {
      await runSweep(env);
    }
  },
} satisfies ExportedHandler<Env>;
