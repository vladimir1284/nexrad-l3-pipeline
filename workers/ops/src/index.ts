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
 * Los vol_time de D1 son ISO 8601 UTC *naive* (sin sufijo Z). JS
 * interpreta esos strings como hora local, así que SIEMPRE se parsea
 * con "Z" explícita — ver parseUtc().
 */

export interface Env {
  DB: D1Database;
  BUCKET: R2Bucket;
  NEXRAD_SITES: string;
  MAX_AGE_MIN: string;
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

interface SiteStatus {
  site: string;
  fresh: boolean;
  reason: string;
  ageMin: number | null;
}

async function checkSite(env: Env, site: string, maxAgeMin: number): Promise<SiteStatus> {
  const row = await env.DB.prepare(
    "SELECT vol_time, r2_key FROM rasters WHERE site_id = ?1 ORDER BY vol_time DESC LIMIT 1",
  )
    .bind(site)
    .first<{ vol_time: string; r2_key: string }>();
  if (!row) return { site, fresh: false, reason: "sin datos", ageMin: null };

  const ageMin = (Date.now() - parseUtc(row.vol_time)) / 60_000;
  if (ageMin > maxAgeMin) {
    return { site, fresh: false, reason: `viejo (${Math.round(ageMin)} min)`, ageMin };
  }
  if ((await env.BUCKET.head(row.r2_key)) === null) {
    return { site, fresh: false, reason: "falta objeto R2", ageMin };
  }
  return { site, fresh: true, reason: "ok", ageMin };
}

function fmtStatus(st: SiteStatus): string {
  const icon = st.fresh ? "🟢" : "🔴";
  const age = st.ageMin === null ? "" : ` (${Math.round(st.ageMin)} min)`;
  return `${icon} ${st.site}: ${st.reason}${age}`;
}

async function runMonitor(env: Env): Promise<void> {
  const sites = env.NEXRAD_SITES.split(",").map((s) => s.trim()).filter(Boolean);
  const maxAgeMin = parseFloat(env.MAX_AGE_MIN || "30");

  const prevRows = await env.DB.prepare("SELECT site_id, fresh FROM ops_monitor_state").all<{
    site_id: string;
    fresh: number;
  }>();
  const prev = new Map(prevRows.results.map((r) => [r.site_id, r.fresh]));

  const statuses: SiteStatus[] = [];
  for (const site of sites) {
    try {
      statuses.push(await checkSite(env, site, maxAgeMin));
    } catch (exc) {
      console.error(`monitor: fallo comprobando ${site} (se reintenta):`, exc);
    }
  }

  const firstEval = statuses.filter((st) => !prev.has(st.site));
  const messages: string[] = [];
  if (firstEval.length) {
    messages.push("🩺 monitor activo — primer chequeo:\n" + firstEval.map(fmtStatus).join("\n"));
  }
  for (const st of statuses) {
    const p = prev.get(st.site);
    if (p === undefined) continue; // ya cubierto por el resumen
    if (!st.fresh && p === 1) {
      messages.push(`🔴 ${st.site}: sin datos frescos — ${st.reason}`);
    } else if (st.fresh && p === 0) {
      messages.push(`🟢 ${st.site}: recuperado (último raster hace ${Math.round(st.ageMin ?? 0)} min)`);
    } else if (st.fresh) {
      console.log(`monitor: ${st.site} ok (${Math.round(st.ageMin ?? 0)} min)`);
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
        ).bind(st.site, st.fresh ? 1 : 0, st.reason, now),
      ),
    );
  }
}

// ------------------------------------------------------------------- sweep

async function deleteR2Keys(env: Env, keys: string[]): Promise<void> {
  for (let i = 0; i < keys.length; i += R2_DELETE_CHUNK) {
    await env.BUCKET.delete(keys.slice(i, i + R2_DELETE_CHUNK));
  }
}

async function deleteRastersByKey(env: Env, keys: string[]): Promise<void> {
  for (let i = 0; i < keys.length; i += D1_IN_CHUNK) {
    const chunk = keys.slice(i, i + D1_IN_CHUNK);
    const marks = chunk.map((_, j) => `?${j + 1}`).join(",");
    await env.DB.prepare(`DELETE FROM rasters WHERE r2_key IN (${marks})`)
      .bind(...chunk)
      .run();
  }
}

/** Borra todo lo anterior a la ventana: objetos R2 primero, filas D1
 * después — si el borrado R2 falla a mitad, la fila sobrevive y el
 * siguiente sweep reintenta. */
async function sweepWindow(env: Env, cutoff: string): Promise<Record<string, number>> {
  const old = await env.DB.prepare("SELECT r2_key FROM rasters WHERE vol_time < ?1")
    .bind(cutoff)
    .all<{ r2_key: string }>();
  const keys = old.results.map((r) => r.r2_key);
  const deleted: Record<string, number> = { rasters: 0, phenomena: 0, vwp: 0 };
  if (keys.length) {
    await deleteR2Keys(env, keys);
    await deleteRastersByKey(env, keys);
    deleted.rasters = keys.length;
  }
  for (const table of ["phenomena", "vwp"] as const) {
    const res = await env.DB.prepare(`DELETE FROM ${table} WHERE vol_time < ?1`).bind(cutoff).run();
    deleted[table] = res.meta.changes ?? 0;
  }
  return deleted;
}

/** Compara bucket con tabla rasters; borra huérfanos R2 y filas
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

  const rows = await env.DB.prepare("SELECT r2_key FROM rasters").all<{ r2_key: string }>();
  const inD1 = new Set(rows.results.map((r) => r.r2_key));

  const orphans = [...inR2].filter((k) => !inD1.has(k)).sort();
  const dangling = [...inD1].filter((k) => !inR2.has(k)).sort();
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
    await deleteRastersByKey(env, confirmed);
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
    `sweep: cutoff=${cutoff} rasters=${deleted.rasters} phenomena=${deleted.phenomena} vwp=${deleted.vwp}`,
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
