/**
 * Lógica pura de la ingesta de viento: dominios, ciclos, claves y
 * formato JSON del contrato. Sin I/O — testeable bajo node --test.
 * Espejo de ingest/wind.py (la referencia para validación cruzada).
 */

import type { WindField } from "./grib.ts";

export const GRID_STEP = 0.25; // grados; grilla GFS 0p25
export const HALF_SPAN_DEG = 6.0; // dominio por sitio: radar ± 6°
export const FH_MAX = 12; // ciclos cada 6 h → ~2 h de colchón
export const CYCLE_STEP_H = 6;
export const MODEL = "gfs0p25";
export const NOMADS_FILTER = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl";

export interface BBox {
  north: number;
  south: number;
  west: number;
  east: number;
}

export const bboxNx = (b: BBox): number => Math.round((b.east - b.west) / GRID_STEP) + 1;
export const bboxNy = (b: BBox): number => Math.round((b.north - b.south) / GRID_STEP) + 1;

/** radar ± 6°, expandido hacia fuera a múltiplos de 0.25° (nodos = grilla
 * GFS, subset puro). No cubre radares que crucen el antimeridiano. */
export function siteBBox(lat: number, lon: number): BBox {
  return {
    north: Math.ceil((lat + HALF_SPAN_DEG) / GRID_STEP) * GRID_STEP,
    south: Math.floor((lat - HALF_SPAN_DEG) / GRID_STEP) * GRID_STEP,
    west: Math.floor((lon - HALF_SPAN_DEG) / GRID_STEP) * GRID_STEP,
    east: Math.ceil((lon + HALF_SPAN_DEG) / GRID_STEP) * GRID_STEP,
  };
}

export function unionBBox(boxes: BBox[]): BBox {
  return {
    north: Math.max(...boxes.map((b) => b.north)),
    south: Math.min(...boxes.map((b) => b.south)),
    west: Math.min(...boxes.map((b) => b.west)),
    east: Math.max(...boxes.map((b) => b.east)),
  };
}

/** ISO naive UTC 'YYYY-MM-DDTHH:MM:SS', la convención de D1. */
export const isoNaive = (d: Date): string => d.toISOString().slice(0, 19);

export const floorHour = (d: Date): Date => {
  const out = new Date(d);
  out.setUTCMinutes(0, 0, 0);
  return out;
};

export const ceilHour = (d: Date): Date => {
  const floor = floorHour(d);
  return floor.getTime() === d.getTime() ? floor : new Date(floor.getTime() + 3_600_000);
};

/** (ciclo, fh) con fh en 0..FH_MAX, del ciclo más nuevo al más viejo. */
export function candidateCycles(validTime: Date): Array<{ cycle: Date; fh: number }> {
  const first = new Date(validTime);
  first.setUTCHours(Math.floor(validTime.getUTCHours() / CYCLE_STEP_H) * CYCLE_STEP_H, 0, 0, 0);
  const out: Array<{ cycle: Date; fh: number }> = [];
  for (let cycle = first; ; cycle = new Date(cycle.getTime() - CYCLE_STEP_H * 3_600_000)) {
    const fh = Math.round((validTime.getTime() - cycle.getTime()) / 3_600_000);
    if (fh > FH_MAX) break;
    out.push({ cycle, fh });
  }
  return out;
}

const pad = (n: number, width: number): string => String(n).padStart(width, "0");

const stampParts = (d: Date) => ({
  y: d.getUTCFullYear(),
  m: pad(d.getUTCMonth() + 1, 2),
  dd: pad(d.getUTCDate(), 2),
  hh: pad(d.getUTCHours(), 2),
});

/** {SITE}/WIND/{Y}/{M}/{D}/{SITE}_WIND_{ts}_c{ciclo}f{FFF}.json (inmutable). */
export function windKey(siteId: string, validTime: Date, cycleTime: Date, fh: number): string {
  const v = stampParts(validTime);
  const c = stampParts(cycleTime);
  const stamp = `${v.y}${v.m}${v.dd}_${v.hh}0000`;
  return (
    `${siteId}/WIND/${v.y}/${v.m}/${v.dd}/` +
    `${siteId}_WIND_${stamp}_c${c.y}${c.m}${c.dd}${c.hh}f${pad(fh, 3)}.json`
  );
}

export function nomadsUrl(cycle: Date, fh: number, box: BBox): string {
  const c = stampParts(cycle);
  const params = new URLSearchParams({
    dir: `/gfs.${c.y}${c.m}${c.dd}/${c.hh}/atmos`,
    file: `gfs.t${c.hh}z.pgrb2.0p25.f${pad(fh, 3)}`,
    var_UGRD: "on",
    var_VGRD: "on",
    lev_10_m_above_ground: "on",
    subregion: "",
    toplat: String(box.north),
    bottomlat: String(box.south),
    // el filtro trabaja en 0–360 (múltiplos de 0.25 son exactos en binario)
    leftlon: String(((box.west % 360) + 360) % 360),
    rightlon: String(((box.east % 360) + 360) % 360),
  });
  return `${NOMADS_FILTER}?${params}`;
}

/** Recorte por índice a un bbox alineado; el bbox debe caber en el campo. */
export function subsetField(field: WindField, box: BBox): WindField {
  const rowIdx = (field.la1 - box.north) / field.dy;
  const colIdx = (box.west - field.lo1) / field.dx;
  if (Math.abs(rowIdx - Math.round(rowIdx)) > 1e-6 || Math.abs(colIdx - Math.round(colIdx)) > 1e-6) {
    throw new Error(`bbox no alineado a la grilla del campo (${rowIdx}, ${colIdx})`);
  }
  const row0 = Math.round(rowIdx);
  const col0 = Math.round(colIdx);
  const ny = bboxNy(box);
  const nx = bboxNx(box);
  if (row0 < 0 || col0 < 0 || row0 + ny > field.ny || col0 + nx > field.nx) {
    throw new Error(`bbox fuera del campo descargado (${field.ny}×${field.nx})`);
  }
  const u = new Float64Array(ny * nx);
  const v = new Float64Array(ny * nx);
  for (let j = 0; j < ny; j++) {
    const src = (row0 + j) * field.nx + col0;
    u.set(field.u.subarray(src, src + nx), j * nx);
    v.set(field.v.subarray(src, src + nx), j * nx);
  }
  return { la1: box.north, lo1: box.west, dx: field.dx, dy: field.dy, ny, nx, u, v };
}

const round2 = (x: number): number => Math.round(x * 100) / 100 + 0; // +0 mata el -0

/** Formato del contrato: header + u/v planos en m/s a 2 decimales. */
export function encodeJson(field: WindField, cycleTime: Date, fh: number): string {
  return JSON.stringify({
    header: {
      nx: field.nx,
      ny: field.ny,
      lo1: field.lo1,
      la1: field.la1,
      dx: field.dx,
      dy: field.dy,
      refTime: isoNaive(cycleTime) + "Z",
      forecastHour: fh,
    },
    u: Array.from(field.u, round2),
    v: Array.from(field.v, round2),
  });
}
