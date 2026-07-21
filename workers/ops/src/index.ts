/**
 * Worker de operación del pipeline NEXRAD L3: monitor de frescura E2E
 * (cron cada 5 min) + sweep de retención y reconciliación R2↔D1 (cron
 * horario). Port de ingest/monitor.py e ingest/retention/sweep.py con
 * bindings nativos de D1 y R2 en lugar del HTTP API + boto3.
 *
 * Diferencias deliberadas con la versión Python:
 *  - Primer chequeo de un sitio (sin fila de estado) manda resumen por
 *    Telegram — el monitor prueba que está vivo; el original solo
 *    hablaba en transiciones y un arranque en verde era mudo para
 *    siempre, indistinguible de un monitor muerto.
 *  - La reconciliación ignora objetos R2 subidos hace < 1 h: publish
 *    sube a R2 antes de insertar en D1, y un sweep en esa ventana veía
 *    un huérfano falso.
 *
 * El monitor cubre tres capas por sitio: rasters (clave = sitio pelado
 * en ops_monitor_state), viento (`SITE:wind`) y rayos (`SITE:ltg`).
 * Wind/lightning se activan solos cuando su tabla tiene filas — la
 * migración D1 llega antes que el Worker de ingesta y la tabla vacía
 * no debe alertar.
 *
 * Los vol_time de D1 son ISO 8601 UTC *naive* (sin sufijo Z). JS
 * interpreta esos strings como hora local, así que SIEMPRE se parsea
 * con "Z" explícita — ver parseUtc().
 */

export interface Env {
  DB: D1Database;
  BUCKET: R2Bucket;
  NEXRAD_SITES: string;
  MAX_AGE_MIN: string;
  WIND_MIN_LEAD_H: string;
  LTG_MAX_AGE_MIN: string;
  WINDOW_HOURS: string;
  TELEGRAM_BOT_TOKEN?: string;
  TELEGRAM_CHAT_ID?: string;
}

const MONITOR_CRON = "*/5 * * * *";
const D1_IN_CHUNK = 50; // filas por DELETE ... IN (...) — mismo margen que el sweep Python
const R2_DELETE_CHUNK = 1000; // máximo del binding R2 por llamada delete()
const RECONCILE_GRACE_MS = 3_600_000; // 1 h: no tocar objetos R2 recién subidos

function parseUtc(volTime: string): number {
  return Date.parse(volTime + "Z");
}

function utcNowIso(): string {
  return new Date().toISOString().slice(0, 19);
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

async function checkRaster(env: Env, site: string, maxAgeMin: number): Promise<CheckStatus> {
  // product_code=153 (N0B) fija: sin filtro, la query cruza las 7
  // particiones del índice (site_id, product_code, vol_time DESC) para
  // hallar el máximo global — ~9.7k filas leídas por llamada en vez de 1
  // (confirmado en Query Insights, 92.55M filas/9514 llamadas). N0B es
  // el proxy de salud del sitio: base reflectivity, siempre presente si
  // el VCP corre.
  const row = await env.DB.prepare(
    "SELECT vol_time, r2_key FROM rasters WHERE site_id = ?1 AND product_code = 153 ORDER BY vol_time DESC LIMIT 1",
  )
    .bind(site)
    .first<{ vol_time: string; r2_key: string }>();
  if (!row) return { key: site, fresh: false, reason: "sin datos" };

  const ageMin = (Date.now() - parseUtc(row.vol_time)) / 60_000;
  if (ageMin > maxAgeMin) {
    return { key: site, fresh: false, reason: `viejo (${Math.round(ageMin)} min)` };
  }
  if ((await env.BUCKET.head(row.r2_key)) === null) {
    return { key: site, fresh: false, reason: "falta objeto R2" };
  }
  return { key: site, fresh: true, reason: `ok (${Math.round(ageMin)} min)` };
}

/** Viento: lo que rompe al viewer es quedarse sin cobertura futura, no
 * la edad del último insert — fresco = MAX(valid_time) llega al menos
 * WIND_MIN_LEAD_H horas por delante de ahora. */
async function checkWind(env: Env, site: string, minLeadH: number): Promise<CheckStatus> {
  const key = `${site}:wind`;
  const row = await env.DB.prepare(
    "SELECT valid_time, r2_key FROM wind_grids WHERE site_id = ?1 ORDER BY valid_time DESC LIMIT 1",
  )
    .bind(site)
    .first<{ valid_time: string; r2_key: string }>();
  if (!row) return { key, fresh: false, reason: "sin datos" };

  const leadH = (parseUtc(row.valid_time) - Date.now()) / 3_600_000;
  if (leadH < minLeadH) {
    return { key, fresh: false, reason: `cobertura hasta ${row.valid_time} (${leadH.toFixed(1)} h)` };
  }
  if ((await env.BUCKET.head(row.r2_key)) === null) {
    return { key, fresh: false, reason: "falta objeto R2" };
  }
  return { key, fresh: true, reason: `ok (cobertura +${leadH.toFixed(1)} h)` };
}

async function checkLightning(env: Env, site: string, maxAgeMin: number): Promise<CheckStatus> {
  const key = `${site}:ltg`;
  const row = await env.DB.prepare(
    "SELECT bucket_start, r2_key FROM lightning_buckets WHERE site_id = ?1 ORDER BY bucket_start DESC LIMIT 1",
  )
    .bind(site)
    .first<{ bucket_start: string; r2_key: string | null }>();
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
}

/** Una capa entra al monitor cuando su tabla tiene filas o ya hay
 * estado previo suyo: antes de desplegar su Worker de ingesta la tabla
 * vacía no es un fallo (la migración llega antes que el Worker); una
 * vez activa, la tabla vaciada mantiene el rojo. */
async function layerActive(
  env: Env,
  prev: Map<string, number>,
  table: string,
  suffix: string,
): Promise<boolean> {
  if ([...prev.keys()].some((k) => k.endsWith(suffix))) return true;
  try {
    return (await env.DB.prepare(`SELECT 1 FROM ${table} LIMIT 1`).first()) !== null;
  } catch (exc) {
    // Tabla aún sin migrar: la capa no existe, pero el resto del
    // monitor tiene que seguir vigilando.
    console.error(`monitor: ${table} inaccesible (¿migración sin aplicar?):`, exc);
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

  const prevRows = await env.DB.prepare("SELECT site_id, fresh FROM ops_monitor_state").all<{
    site_id: string;
    fresh: number;
  }>();
  const prev = new Map(prevRows.results.map((r) => [r.site_id, r.fresh]));

  const windActive = await layerActive(env, prev, "wind_grids", ":wind");
  const ltgActive = await layerActive(env, prev, "lightning_buckets", ":ltg");
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
    const now = utcNowIso();
    await env.DB.batch(
      statuses.map((st) =>
        env.DB.prepare(
          `INSERT INTO ops_monitor_state (site_id, fresh, reason, updated_at)
           VALUES (?1, ?2, ?3, ?4)
           ON CONFLICT (site_id) DO UPDATE SET
             fresh = excluded.fresh, reason = excluded.reason, updated_at = excluded.updated_at`,
        ).bind(st.key, st.fresh ? 1 : 0, st.reason, now),
      ),
    );
  }
}

// ------------------------------------------------------------------- sweep

// Tablas D1 cuyas filas apuntan a objetos R2 (columna r2_key) y su
// columna temporal para el sweep. TODO objeto del bucket debe estar
// referenciado por una de estas tablas o la reconciliación lo borra.
const KEYED_TABLES = [
  { table: "rasters", timeCol: "vol_time" },
  { table: "wind_grids", timeCol: "valid_time" },
] as const;

// Tablas que la reconciliación consulta por r2_key. lightning_buckets
// no entra en KEYED_TABLES: su r2_key puede ser NULL (cubo sin rayos),
// así que el sweep la borra por bucket_start y no por r2_key.
const R2_KEY_TABLES = [...KEYED_TABLES.map((t) => t.table), "lightning_buckets"];

async function deleteR2Keys(env: Env, keys: string[]): Promise<void> {
  for (let i = 0; i < keys.length; i += R2_DELETE_CHUNK) {
    await env.BUCKET.delete(keys.slice(i, i + R2_DELETE_CHUNK));
  }
}

async function deleteRowsByKey(env: Env, table: string, keys: string[]): Promise<void> {
  for (let i = 0; i < keys.length; i += D1_IN_CHUNK) {
    const chunk = keys.slice(i, i + D1_IN_CHUNK);
    const marks = chunk.map((_, j) => `?${j + 1}`).join(",");
    await env.DB.prepare(`DELETE FROM ${table} WHERE r2_key IN (${marks})`)
      .bind(...chunk)
      .run();
  }
}

/** Borra todo lo anterior a la ventana: objetos R2 primero, filas D1
 * después — si el borrado R2 falla a mitad, la fila sobrevive y el
 * siguiente sweep reintenta. */
async function sweepWindow(env: Env, cutoff: string): Promise<Record<string, number>> {
  const deleted: Record<string, number> = {
    rasters: 0,
    wind_grids: 0,
    lightning_buckets: 0,
    phenomena: 0,
    vwp: 0,
  };
  for (const { table, timeCol } of KEYED_TABLES) {
    const old = await env.DB.prepare(`SELECT r2_key FROM ${table} WHERE ${timeCol} < ?1`)
      .bind(cutoff)
      .all<{ r2_key: string }>();
    const keys = old.results.map((r) => r.r2_key);
    if (keys.length) {
      await deleteR2Keys(env, keys);
      await deleteRowsByKey(env, table, keys);
      deleted[table] = keys.length;
    }
  }
  // lightning_buckets aparte: los objetos se borran por r2_key pero las
  // filas por bucket_start (r2_key NULL en cubos sin rayos). R2 primero
  // por la misma razón de arriba.
  const oldLtg = await env.DB.prepare(
    "SELECT r2_key FROM lightning_buckets WHERE bucket_start < ?1 AND r2_key IS NOT NULL",
  )
    .bind(cutoff)
    .all<{ r2_key: string }>();
  await deleteR2Keys(env, oldLtg.results.map((r) => r.r2_key));
  const resLtg = await env.DB.prepare("DELETE FROM lightning_buckets WHERE bucket_start < ?1")
    .bind(cutoff)
    .run();
  deleted.lightning_buckets = resLtg.meta.changes ?? 0;
  for (const table of ["phenomena", "vwp"] as const) {
    const res = await env.DB.prepare(`DELETE FROM ${table} WHERE vol_time < ?1`).bind(cutoff).run();
    deleted[table] = res.meta.changes ?? 0;
  }
  return deleted;
}

/** Compara bucket con las tablas con r2_key; borra huérfanos R2 y filas
 * colgantes. Objetos con menos de 1 h en el bucket no cuentan como
 * huérfanos (ventana upload-R2 → insert-D1 del publish). */
async function reconcile(env: Env): Promise<{ orphans: number; dangling: number }> {
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

  const inD1 = new Map<string, string>(); // r2_key → tabla dueña
  for (const table of R2_KEY_TABLES) {
    const rows = await env.DB.prepare(`SELECT r2_key FROM ${table} WHERE r2_key IS NOT NULL`).all<{
      r2_key: string;
    }>();
    for (const r of rows.results) inD1.set(r.r2_key, table);
  }

  const orphans = [...inR2].filter((k) => !inD1.has(k)).sort();
  const dangling = [...inD1.keys()].filter((k) => !inR2.has(k)).sort();
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
    for (const table of R2_KEY_TABLES) {
      const keys = confirmed.filter((k) => inD1.get(k) === table);
      if (keys.length) await deleteRowsByKey(env, table, keys);
    }
  } else {
    console.log(`reconcile: consistente (${inR2.size} objetos)`);
  }
  return { orphans: orphans.length, dangling: confirmed.length };
}

async function runSweep(env: Env): Promise<void> {
  const windowHours = parseFloat(env.WINDOW_HOURS || "72");
  const cutoff = new Date(Date.now() - windowHours * 3_600_000).toISOString().slice(0, 19);
  const deleted = await sweepWindow(env, cutoff);
  console.log(
    `sweep: cutoff=${cutoff} rasters=${deleted.rasters} wind_grids=${deleted.wind_grids} lightning_buckets=${deleted.lightning_buckets} phenomena=${deleted.phenomena} vwp=${deleted.vwp}`,
  );
  await reconcile(env);
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
