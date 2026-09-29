import { readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import sharp from "sharp";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const publicDir = join(root, "public");
const svg = await readFile(join(publicDir, "kdps-mark.svg"));

for (const [name, size] of [
  ["icon-192.png", 192],
  ["icon-512.png", 512],
  ["apple-touch-icon.png", 180],
]) {
  await writeFile(join(publicDir, name), await sharp(svg).resize(size, size).png().toBuffer());
}

// A maskable icon can lose its outer fifth. Keep the whole visible mark and
// lettering in the central safe circle, with the same navy filling the edges.
const inset = 340;
const offset = (512 - inset) / 2;
const central = await sharp(svg).resize(inset, inset).png().toBuffer();
await sharp({ create: { width: 512, height: 512, channels: 4, background: "#201d18" } })
  .composite([{ input: central, left: offset, top: offset }])
  .png()
  .toFile(join(publicDir, "icon-maskable-512.png"));
