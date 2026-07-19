/**
 * Parse de un fichero GLM-L2-LCFA (netCDF-4/HDF5) con h5wasm vendorizado
 * (ver scripts/vendor-h5wasm.mjs y el README: el wasm va como módulo
 * importado + hook instantiateWasm porque workerd prohíbe compilar wasm
 * desde bytes en runtime).
 *
 * Extrae flashes con calidad buena: lon/lat (float32 ya desempaquetado)
 * y epoch UTC del primer evento. El tiempo viene packed uint16
 * (atributo _Unsigned sobre int16 crudo) con scale/offset y base en el
 * atributo `units` ("seconds since YYYY-MM-DD HH:MM:SS.mmm") — NO se
 * usa el dataset product_time: los escalares disparan "name not
 * defined" en h5wasm bajo workerd (visto en el spike 2026-07-19) y la
 * base de units es equivalente.
 */

import * as h5 from "./vendor/hdf5_hl.js";
import { Flash, parseUnitsBase } from "./core.ts";

export interface GlmParse {
  flashes: Flash[]; // solo flash_quality_flag == 0
  total: number; // todos los flashes del fichero
}

/** Atributos HDF5 llegan como escalar o como array de un elemento. */
function attrNumber(v: unknown): number {
  if (typeof v === "object" && v !== null && 0 in (v as Record<number, unknown>)) {
    return Number((v as Record<number, unknown>)[0]);
  }
  return Number(v);
}

function attrString(v: unknown): string {
  if (Array.isArray(v)) return String(v[0]);
  return String(v);
}

let seq = 0;

export async function parseGlm(bytes: Uint8Array): Promise<GlmParse> {
  const Module = await h5.ready;
  const path = `glm_${seq++}.nc`;
  Module.FS.writeFile(path, bytes);
  try {
    const f = new h5.File(path, "r");
    try {
      const lat = f.get("flash_lat");
      const lon = f.get("flash_lon");
      const toff = f.get("flash_time_offset_of_first_event");
      const qf = f.get("flash_quality_flag");
      if (!lat || !lon || !toff || !qf) throw new Error("dataset de flashes ausente");

      const scale = attrNumber(toff.attrs["scale_factor"].value);
      const offset = attrNumber(toff.attrs["add_offset"].value);
      const base = parseUnitsBase(attrString(toff.attrs["units"].value));

      const latv = lat.value as Float32Array;
      const lonv = lon.value as Float32Array;
      const toffv = toff.value as Int16Array;
      const qfv = qf.value as Int16Array;

      const flashes: Flash[] = [];
      for (let i = 0; i < latv.length; i++) {
        if (qfv[i] !== 0) continue; // solo calidad buena (provisional, ver contrato)
        // packed uint16: el atributo _Unsigned manda reinterpretar el int16
        flashes.push({ lon: lonv[i], lat: latv[i], epochS: base + (toffv[i] & 0xffff) * scale + offset });
      }
      return { flashes, total: latv.length };
    } finally {
      f.close();
    }
  } finally {
    Module.FS.unlink(path);
  }
}
