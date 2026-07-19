/**
 * Vendoriza h5wasm para workerd (corre solo vía npm `prepare`/`predeploy`).
 *
 * El paquete npm trae un build SINGLE_FILE de Emscripten: el wasm va
 * embebido como string y se compila en runtime con
 * WebAssembly.instantiate(bytes) — prohibido en Workers (no code-gen).
 * Este script:
 *   1. extrae el binario wasm interceptando WebAssembly.instantiate,
 *   2. copia hdf5_util.js (el factory Emscripten) tal cual,
 *   3. parchea hdf5_hl.js para importar el .wasm como módulo (wrangler
 *      lo precompila) e inyectarlo con el hook instantiateWasm,
 *   4. escribe un .d.ts mínimo para lo que usa src/glm.ts.
 *
 * Salida en src/vendor/ (gitignored — regenerable, determinista para la
 * versión pinada de h5wasm). Si el replace falla tras subir h5wasm,
 * revisar el nuevo dist a mano.
 */

import { mkdirSync, writeFileSync, copyFileSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const dist = join(root, "node_modules", "h5wasm", "dist", "esm");
const out = join(root, "src", "vendor");
mkdirSync(out, { recursive: true });

// 1. extraer el wasm
let captured = null;
const orig = WebAssembly.instantiate.bind(WebAssembly);
WebAssembly.instantiate = (bytes, imports) => {
  if (bytes instanceof Uint8Array || bytes instanceof ArrayBuffer) {
    captured = bytes instanceof ArrayBuffer ? new Uint8Array(bytes) : bytes;
  }
  return orig(bytes, imports);
};
const { default: ModuleFactory } = await import(join(dist, "hdf5_util.js"));
await ModuleFactory({ noInitialRun: true });
WebAssembly.instantiate = orig;
if (!captured) throw new Error("no se capturó el binario wasm — ¿cambió el loader de h5wasm?");
writeFileSync(join(out, "hdf5_util.wasm"), captured);

// 2. el factory tal cual (el string embebido queda muerto: el hook
// instantiateWasm retorna antes de decodificarlo)
copyFileSync(join(dist, "hdf5_util.js"), join(out, "hdf5_util.js"));

// 3. parchear hdf5_hl.js
const hl = readFileSync(join(dist, "hdf5_hl.js"), "utf8");
const anchor =
  "const ready = ModuleFactory({ noInitialRun: true }).then(result => { Module = result; FS = Module.FS; return Module; });";
if (!hl.includes(anchor)) throw new Error("anchor de parcheo ausente en hdf5_hl.js — revisar versión de h5wasm");
const patched = hl.replace(
  anchor,
  `import wasmModule from './hdf5_util.wasm';
const ready = ModuleFactory({
    noInitialRun: true,
    instantiateWasm(info, receiveInstance) {
        const instance = new WebAssembly.Instance(wasmModule, info);
        receiveInstance(instance, wasmModule);
        return instance.exports;
    },
}).then(result => { Module = result; FS = Module.FS; return Module; });`,
);
writeFileSync(join(out, "hdf5_hl.js"), patched);

// 4. tipos mínimos (solo lo que consume src/glm.ts)
writeFileSync(
  join(out, "hdf5_hl.d.ts"),
  `// Generado por scripts/vendor-h5wasm.mjs — tipos mínimos para src/glm.ts.
export interface VendorAttr { value: unknown }
export interface VendorDataset {
  shape: number[];
  value: unknown;
  attrs: Record<string, VendorAttr>;
}
export class File {
  constructor(path: string, mode?: string);
  get(name: string): VendorDataset | null;
  close(): number;
}
export interface VendorFS {
  writeFile(path: string, data: Uint8Array): void;
  unlink(path: string): void;
}
export const ready: Promise<{ FS: VendorFS }>;
`,
);

console.log(`vendor h5wasm listo: ${(captured.length / 1048576).toFixed(2)} MB wasm → src/vendor/`);
