/**
 * Decodificador GRIB2 mínimo para los subsets del filtro de NOMADS.
 *
 * Soporta exactamente lo que el filtro produce al subsetear GFS 0.25°
 * (verificado 2026-07-18): grilla regular lat/lon (template 3.0), datos
 * grid_simple (template 5.0, `valor = (R + X·2^E)·10^−D`), sin bitmap.
 * Los GFS crudos escanean norte→sur pero el filtro re-empaqueta los
 * subsets sur→norte (jScansPositively=1) — aquí se normaliza todo a
 * filas norte→sur desde la esquina NO, con lo1 en [-180, 180), que es
 * la convención del contrato con el viewer.
 *
 * Cualquier template/flag fuera de eso lanza GribError: mejor reventar
 * visible que decodificar mal. La validación cruzada contra eccodes
 * vive en scripts/validate_wind_worker.py del repo raíz.
 */

export class GribError extends Error {}

export interface WindField {
  la1: number; // latitud norte
  lo1: number; // longitud oeste, en [-180, 180)
  dx: number;
  dy: number;
  ny: number;
  nx: number;
  u: Float64Array; // (ny*nx) row-major desde la esquina NO, m/s
  v: Float64Array;
}

/** Entero sign-magnitude de GRIB (bit alto = signo, NO complemento a dos). */
function signMagnitude(raw: number, signBit: number): number {
  return raw & signBit ? -(raw & (signBit - 1)) : raw;
}

function readBits(bytes: Uint8Array, startBit: number, nbits: number): number {
  let value = 0;
  let bit = startBit;
  let remaining = nbits;
  while (remaining > 0) {
    const byte = bytes[bit >> 3];
    const offsetInByte = bit & 7;
    const take = Math.min(8 - offsetInByte, remaining);
    const chunk = (byte >> (8 - offsetInByte - take)) & ((1 << take) - 1);
    value = value * 2 ** take + chunk; // hasta 25 bits: seguro en float64
    bit += take;
    remaining -= take;
  }
  return value;
}

interface Message {
  paramNumber: number; // 2 = UGRD, 3 = VGRD (categoría 2, disciplina 0)
  ni: number;
  nj: number;
  la1: number;
  lo1: number;
  dx: number;
  dy: number;
  values: Float64Array; // normalizados: filas norte→sur
}

// Octetos del spec GRIB2 son 1-based; aquí `at(n)` = octeto n de la sección.
function decodeMessage(view: DataView, base: number, msgLen: number): Message | null {
  const discipline = view.getUint8(base + 6);
  let offset = base + 16; // fin de la sección 0
  const end = base + msgLen - 4; // antes del '7777' final

  let grid: Omit<Message, "paramNumber" | "values"> | null = null;
  let southToNorth = false;
  let param = -1;
  let category = -1;
  let numberOfValues = 0;
  let refValue = 0;
  let binaryScale = 0;
  let decimalScale = 0;
  let bitsPerValue = 0;
  let values: Float64Array | null = null;

  while (offset < end) {
    const secLen = view.getUint32(offset);
    const secNum = view.getUint8(offset + 4);
    const at = (octet: number) => offset + octet - 1; // octeto 1-based → índice

    if (secNum === 3) {
      const template = view.getUint16(at(13));
      if (template !== 0) throw new GribError(`grid template ${template} no soportado (solo 3.0)`);
      const basicAngle = view.getUint32(at(39));
      if (basicAngle !== 0 && basicAngle !== 1) {
        throw new GribError(`basicAngle ${basicAngle} no soportado`);
      }
      const scan = view.getUint8(at(72));
      if ((scan & ~0x40) !== 0) {
        throw new GribError(`scanning mode 0x${scan.toString(16)} no soportado`);
      }
      southToNorth = (scan & 0x40) !== 0;
      const la1 = signMagnitude(view.getUint32(at(47)), 0x80000000) / 1e6;
      const la2 = signMagnitude(view.getUint32(at(56)), 0x80000000) / 1e6;
      let lo1 = signMagnitude(view.getUint32(at(51)), 0x80000000) / 1e6;
      if (lo1 >= 180) lo1 -= 360; // GFS usa 0–360; el contrato pide [-180, 180)
      grid = {
        ni: view.getUint32(at(31)),
        nj: view.getUint32(at(35)),
        la1: southToNorth ? la2 : la1, // siempre el borde norte
        lo1,
        dx: view.getUint32(at(64)) / 1e6,
        dy: view.getUint32(at(68)) / 1e6,
      };
    } else if (secNum === 4) {
      const template = view.getUint16(at(8));
      if (template !== 0) throw new GribError(`product template ${template} no soportado (4.0)`);
      category = view.getUint8(at(10));
      param = view.getUint8(at(11));
    } else if (secNum === 5) {
      const template = view.getUint16(at(10));
      if (template !== 0) {
        throw new GribError(`packing template 5.${template} no soportado (solo 5.0 grid_simple)`);
      }
      numberOfValues = view.getUint32(at(6));
      refValue = view.getFloat32(at(12));
      binaryScale = signMagnitude(view.getUint16(at(16)), 0x8000);
      decimalScale = signMagnitude(view.getUint16(at(18)), 0x8000);
      bitsPerValue = view.getUint8(at(20));
    } else if (secNum === 6) {
      const indicator = view.getUint8(at(6));
      if (indicator !== 255) throw new GribError(`bitmap ${indicator} no soportado (solo 255)`);
    } else if (secNum === 7) {
      const scale = 2 ** binaryScale * 10 ** -decimalScale;
      const ref = refValue * 10 ** -decimalScale;
      values = new Float64Array(numberOfValues);
      if (bitsPerValue === 0) {
        values.fill(ref); // campo constante
      } else {
        const bytes = new Uint8Array(view.buffer, view.byteOffset + at(6), secLen - 5);
        for (let i = 0; i < numberOfValues; i++) {
          values[i] = ref + readBits(bytes, i * bitsPerValue, bitsPerValue) * scale;
        }
      }
    }
    offset += secLen;
  }

  if (grid === null || values === null) throw new GribError("mensaje sin secciones 3/7");
  if (values.length !== grid.ni * grid.nj) {
    throw new GribError(`${values.length} valores para grilla ${grid.ni}×${grid.nj}`);
  }
  if (discipline !== 0 || category !== 2 || (param !== 2 && param !== 3)) {
    return null; // no es UGRD/VGRD 10 m — el filtro no debería mandar otra cosa
  }
  if (southToNorth) {
    const flipped = new Float64Array(values.length);
    for (let j = 0; j < grid.nj; j++) {
      flipped.set(values.subarray(j * grid.ni, (j + 1) * grid.ni), (grid.nj - 1 - j) * grid.ni);
    }
    values = flipped;
  }
  return { paramNumber: param, values, ...grid };
}

/** GRIB2 (posiblemente multi-mensaje) del filtro NOMADS → campo u/v. */
export function decodeWind(buf: ArrayBuffer): WindField {
  const view = new DataView(buf);
  const fields = new Map<number, Message>();
  let offset = 0;
  while (offset < buf.byteLength) {
    if (view.getUint32(offset) !== 0x47524942) {
      // "GRIB"
      throw new GribError(`basura en offset ${offset}: no empieza con GRIB`);
    }
    if (view.getUint8(offset + 7) !== 2) throw new GribError("solo GRIB edición 2");
    const msgLen = Number(view.getBigUint64(offset + 8));
    const msg = decodeMessage(view, offset, msgLen);
    if (msg !== null) fields.set(msg.paramNumber, msg);
    offset += msgLen;
  }

  const u = fields.get(2);
  const v = fields.get(3);
  if (!u || !v) throw new GribError(`faltan mensajes u/v (presentes: ${[...fields.keys()]})`);
  for (const k of ["ni", "nj", "la1", "lo1", "dx", "dy"] as const) {
    if (u[k] !== v[k]) throw new GribError(`grillas u/v distintas en ${k}: ${u[k]} vs ${v[k]}`);
  }
  return {
    la1: u.la1,
    lo1: u.lo1,
    dx: u.dx,
    dy: u.dy,
    ny: u.nj,
    nx: u.ni,
    u: u.values,
    v: v.values,
  };
}
