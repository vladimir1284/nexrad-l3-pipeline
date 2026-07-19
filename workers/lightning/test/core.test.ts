import assert from "node:assert/strict";
import { test } from "node:test";

import type { Flash } from "../src/core.ts";
import {
  BUCKET_S,
  FRAMES_PER_BUCKET,
  eligibleBucketStarts,
  encodeBucketJson,
  framesForBucket,
  glmHourPrefixes,
  glmKeyStartEpoch,
  haversineKm,
  isoNaive,
  lightningKey,
  parseS3ListKeys,
  parseUnitsBase,
  strikesForSite,
} from "../src/core.ts";

const utc = (iso: string): Date => new Date(iso + "Z");

test("eligibleBucketStarts: alineados, cerrados + margen, nuevo→viejo", () => {
  const now = utc("2026-07-19T12:07:45");
  const out = eligibleBucketStarts(now, 30 * 60, 90);
  // 12:00 cerró a las 12:05, +90 s = 12:06:30 ≤ now ✓; 12:05 cierra 12:10 ✗
  assert.equal(isoNaive(out[0]), "2026-07-19T12:00:00");
  assert.equal(isoNaive(out[out.length - 1]), "2026-07-19T11:40:00");
  for (const b of out) assert.equal((b.getTime() / 1000) % BUCKET_S, 0);
  for (let i = 1; i < out.length; i++) assert.ok(out[i] < out[i - 1]);
});

test("eligibleBucketStarts: margen no cumplido excluye el último cubo", () => {
  // 12:00 cerró a las 12:05; a las 12:06:00 aún no pasó el margen de 90 s
  const out = eligibleBucketStarts(utc("2026-07-19T12:06:00"), 30 * 60, 90);
  assert.equal(isoNaive(out[0]), "2026-07-19T11:55:00");
});

test("lightningKey: ejemplo del contrato", () => {
  assert.equal(
    lightningKey("BYX", utc("2026-07-18T12:05:00")),
    "BYX/LIGHTNING/2026/07/18/BYX_LTG_20260718_120500.json",
  );
});

test("glmKeyStartEpoch: campo s del nombre LCFA, con décimas", () => {
  const key = "GLM-L2-LCFA/2026/200/00/OR_GLM-L2-LCFA_G19_s20262000000000_e20262000000200_c20262000000222.nc";
  // día 200 de 2026 = 19 de julio
  assert.equal(glmKeyStartEpoch(key), utc("2026-07-19T00:00:00").getTime() / 1000);
  const conDecima = "OR_GLM-L2-LCFA_G19_s20262001523405_e20262001523605_c0.nc";
  assert.equal(glmKeyStartEpoch(conDecima), utc("2026-07-19T15:23:40").getTime() / 1000 + 0.5);
  assert.equal(glmKeyStartEpoch("otro_fichero.nc"), null);
});

test("glmHourPrefixes: una hora normal, dos si el frame extra la cruza", () => {
  assert.deepEqual(glmHourPrefixes(utc("2026-07-19T12:05:00")), ["GLM-L2-LCFA/2026/200/12/"]);
  // cubo :55: el frame s = bucket_end cae en la hora (y aquí, día) siguiente
  assert.deepEqual(glmHourPrefixes(utc("2026-07-19T23:55:00")), [
    "GLM-L2-LCFA/2026/200/23/",
    "GLM-L2-LCFA/2026/201/00/",
  ]);
});

test("framesForBucket: frames [start, end] con extremo superior inclusive", () => {
  const start = utc("2026-07-19T12:05:00");
  const mk = (hhmmss: string, tenth = "0") =>
    `GLM-L2-LCFA/2026/200/12/OR_GLM-L2-LCFA_G19_s2026200${hhmmss}${tenth}_e0_c0.nc`;
  const keys = [
    mk("120440"), // frame anterior — fuera
    mk("120500"),
    mk("120940"),
    mk("121000"), // frame extra s = bucket_end — dentro (regla de frontera)
    mk("121020"), // fuera
  ];
  const frames = framesForBucket(keys, start);
  assert.equal(frames.length, 3);
  assert.ok(frames.includes(mk("121000")));
  assert.ok(!frames.includes(mk("120440")));
  assert.equal(FRAMES_PER_BUCKET, 16);
});

test("parseS3ListKeys: claves y flag de truncado", () => {
  const xml = "<r><IsTruncated>false</IsTruncated><Contents><Key>a/b.nc</Key></Contents><Contents><Key>a/c.nc</Key></Contents></r>";
  assert.deepEqual(parseS3ListKeys(xml), { keys: ["a/b.nc", "a/c.nc"], truncated: false });
  assert.equal(parseS3ListKeys("<r><IsTruncated>true</IsTruncated></r>").truncated, true);
});

test("haversineKm: BYX→AMX ~172 km", () => {
  const d = haversineKm(24.5975, -81.7031, 25.6112, -80.4128);
  assert.ok(d > 168 && d < 176, `distancia ${d}`);
});

test("strikesForSite: recorte por radio, ventana temporal, orden y redondeo", () => {
  const start = utc("2026-07-19T12:05:00");
  const t0 = start.getTime() / 1000;
  const site = { lat: 24.5975, lon: -81.7031 }; // BYX
  const flashes: Flash[] = [
    { lon: -81.5, lat: 24.7, epochS: t0 + 120.34 }, // cerca, dentro
    { lon: -81.6, lat: 24.6, epochS: t0 + 3.06 }, // cerca, dentro, va primero
    { lon: -60.0, lat: 24.6, epochS: t0 + 10 }, // a ~2000 km — fuera
    { lon: -81.5, lat: 24.7, epochS: t0 - 0.5 }, // antes del cubo — fuera
    { lon: -81.5, lat: 24.7, epochS: t0 + 300.0 }, // == bucket_end — fuera
    { lon: -81.5, lat: 24.7, epochS: t0 + 299.96 }, // redondeo tocaría 300.0 → clavado a 299.9
  ];
  const strikes = strikesForSite(flashes, site.lat, site.lon, 460, start);
  assert.equal(strikes.length, 3);
  assert.deepEqual(strikes[0], [-81.6, 24.6, 3.1]);
  assert.deepEqual(strikes[1], [-81.5, 24.7, 120.3]);
  assert.deepEqual(strikes[2], [-81.5, 24.7, 299.9]);
  for (const [, , off] of strikes) assert.ok(off >= 0 && off < BUCKET_S);
});

test("encodeBucketJson: formato del contrato", () => {
  const body = encodeBucketJson("BYX", utc("2026-07-18T12:05:00"), [[-81.412, 24.607, 3.4]]);
  assert.deepEqual(JSON.parse(body), {
    site: "BYX",
    bucket_start: "2026-07-18T12:05:00",
    bucket_s: 300,
    strikes: [[-81.412, 24.607, 3.4]],
  });
});

test("parseUnitsBase: base de tiempo del atributo units GLM", () => {
  assert.equal(
    parseUnitsBase("seconds since 2026-07-19 00:00:00.000"),
    utc("2026-07-19T00:00:00").getTime() / 1000,
  );
  assert.equal(
    parseUnitsBase("seconds since 2000-01-01T12:00:00.500"),
    utc("2000-01-01T12:00:00").getTime() / 1000 + 0.5,
  );
  assert.throws(() => parseUnitsBase("days since 2000-01-01"));
});
