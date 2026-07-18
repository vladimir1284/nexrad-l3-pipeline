/**
 * Tests del decodificador GRIB2 y de la lógica pura, sin red.
 *
 * El fixture es un subset REAL del filtro de NOMADS (GFS 2026-07-17 12Z
 * f003, bbox unión AMX+JUA, re-empaquetado grid_simple sur→norte por el
 * filtro). El golden lo generó eccodes vía ingest/wind.py (decode_grib),
 * ya normalizado a filas norte→sur — ver scripts/validate_wind_worker.py
 * para la validación cruzada online.
 *
 * Correr: npm test  (node >= 22.6, --experimental-strip-types)
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  bboxNx,
  bboxNy,
  candidateCycles,
  encodeJson,
  siteBBox,
  subsetField,
  unionBBox,
  windKey,
} from "../src/core.ts";
import { decodeWind } from "../src/grib.ts";

const DATA = new URL("./data/", import.meta.url);
const fixture = readFileSync(new URL("nomads_subset_f003.grib2", DATA));
const golden = JSON.parse(readFileSync(new URL("golden.json", DATA), "utf8"));

const asArrayBuffer = (b: Buffer): ArrayBuffer =>
  b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) as ArrayBuffer;

function assertClose(got: ArrayLike<number>, want: number[], what: string) {
  for (let i = 0; i < want.length; i++) {
    assert.ok(Math.abs(got[i] - want[i]) < 1e-6, `${what}[${i}]: ${got[i]} != ${want[i]}`);
  }
}

test("decodeWind reproduce el golden de eccodes", () => {
  const f = decodeWind(asArrayBuffer(fixture));
  assert.deepEqual(
    { la1: f.la1, lo1: f.lo1, dx: f.dx, dy: f.dy, ny: f.ny, nx: f.nx },
    { la1: golden.la1, lo1: golden.lo1, dx: golden.dx, dy: golden.dy, ny: golden.ny, nx: golden.nx },
  );
  assertClose(f.u.subarray(0, 5), golden.u_first5, "u_first5");
  assertClose(f.u.subarray(-5), golden.u_last5, "u_last5");
  assertClose(f.v.subarray(0, 5), golden.v_first5, "v_first5");
  assertClose(f.v.subarray(-5), golden.v_last5, "v_last5");
  const mean = (a: Float64Array) => a.reduce((s, x) => s + x, 0) / a.length;
  assert.ok(Math.abs(mean(f.u) - golden.u_mean) < 1e-6, "u_mean");
  assert.ok(Math.abs(mean(f.v) - golden.v_mean) < 1e-6, "v_mean");
});

test("subset + encode cumplen el contrato del viewer", () => {
  const field = decodeWind(asArrayBuffer(fixture));
  const amx = siteBBox(25.6111, -80.4128);
  const sub = subsetField(field, amx);
  const doc = JSON.parse(encodeJson(sub, new Date(Date.UTC(2026, 6, 17, 12)), 3));
  assert.deepEqual(doc.header, {
    nx: bboxNx(amx),
    ny: bboxNy(amx),
    lo1: amx.west,
    la1: amx.north,
    dx: 0.25,
    dy: 0.25,
    refTime: "2026-07-17T12:00:00Z",
    forecastHour: 3,
  });
  assert.equal(doc.u.length, doc.header.nx * doc.header.ny);
  assert.equal(doc.v.length, doc.u.length);
  for (const x of doc.u) assert.equal(Math.round(x * 100) / 100, x); // 2 decimales
  // el recorte conserva los valores del campo grande (esquina NO del bbox)
  const row0 = Math.round((field.la1 - amx.north) / 0.25);
  const col0 = Math.round((amx.west - field.lo1) / 0.25);
  assert.ok(Math.abs(sub.u[0] - field.u[row0 * field.nx + col0]) < 1e-12);
});

test("siteBBox alineado a 0.25 y cubre ±6°", () => {
  const box = siteBBox(25.6111, -80.4128);
  for (const edge of [box.north, box.south, box.west, box.east]) {
    assert.equal(edge, Math.round(edge / 0.25) * 0.25);
  }
  assert.ok(box.north >= 31.6111 && box.south <= 19.6111);
  assert.ok(box.east >= -74.4128 && box.west <= -86.4128);
});

test("unionBBox", () => {
  const a = siteBBox(25.6111, -80.4128);
  const b = siteBBox(18.1156, -66.0781);
  const u = unionBBox([a, b]);
  assert.deepEqual(u, { north: a.north, south: b.south, west: a.west, east: b.east });
});

test("windKey ejemplo de la spec", () => {
  const key = windKey(
    "AMX",
    new Date(Date.UTC(2026, 6, 18, 12)),
    new Date(Date.UTC(2026, 6, 18, 6)),
    6,
  );
  assert.equal(key, "AMX/WIND/2026/07/18/AMX_WIND_20260718_120000_c2026071806f006.json");
});

test("candidateCycles fh 0..12, del más nuevo al más viejo", () => {
  const cycles = candidateCycles(new Date(Date.UTC(2026, 6, 18, 13)));
  assert.deepEqual(
    cycles.map((c) => [c.cycle.toISOString().slice(0, 13), c.fh]),
    [
      ["2026-07-18T12", 1],
      ["2026-07-18T06", 7],
    ],
  );
  assert.equal(candidateCycles(new Date(Date.UTC(2026, 6, 18, 12))).length, 3); // f0/f6/f12
});
