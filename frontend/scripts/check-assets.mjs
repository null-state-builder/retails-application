import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import sharp from "sharp";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const publicDir = join(root, "public");
const distDir = join(root, "dist");
const expected = new Map([
  ["icon-192.png", 192],
  ["icon-512.png", 512],
  ["icon-maskable-512.png", 512],
  ["apple-touch-icon.png", 180],
]);

const source = await readFile(join(publicDir, "kdps-mark.svg"), "utf8");
if (!source.includes("<svg ") || !source.includes('viewBox="0 0 512 512"')) {
  throw new Error("KDPS SVG source is missing or has the wrong viewBox");
}

const html = await readFile(join(root, "index.html"), "utf8");
const pwa = await readFile(join(root, "src/pwa/config.ts"), "utf8");
for (const name of expected.keys()) {
  if (!html.includes(`/${name}`) && !pwa.includes(`/${name}`) && !pwa.includes(`"${name}"`)) {
    throw new Error(`${name} is not referenced by the app or manifest`);
  }
}

for (const [name, size] of expected) {
  for (const directory of [publicDir, distDir]) {
    const bytes = await readFile(join(directory, name));
    const meta = await sharp(bytes).metadata();
    if (meta.format !== "png" || meta.width !== size || meta.height !== size) {
      throw new Error(`${name} in ${directory} must be a ${size}×${size} PNG`);
    }
  }
}
for (const directory of [publicDir, distDir]) await readFile(join(directory, "kdps-mark.svg"));

const { data, info } = await sharp(join(publicDir, "icon-maskable-512.png"))
  .raw()
  .toBuffer({ resolveWithObject: true });
const navy = [32, 29, 24];
for (let y = 0; y < 512; y += 1) {
  for (let x = 0; x < 512; x += 1) {
    if (Math.hypot(x - 255.5, y - 255.5) <= 204.8) continue;
    const index = (y * info.width + x) * info.channels;
    if (navy.some((channel, offset) => Math.abs(data[index + offset] - channel) > 2)) {
      throw new Error(`Maskable icon content leaves its safe circle at ${x},${y}`);
    }
  }
}
globalThis.console.log(
  "KDPS PWA assets exist in public and dist with valid sizes and a maskable safe area.",
);
