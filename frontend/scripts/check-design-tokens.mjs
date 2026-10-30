// Design System v2 guard (docs/planning/design-system-v2-rollout.md, Phase 0).
// Reports CSS that bypasses the v2 tokens: half-pixel or off-scale font sizes,
// heavy weights, colour literals outside the token files, and radius or shadow
// literals. `yarn check:design-tokens` runs it with `--strict`, which fails on any
// finding; that is part of `npm run verify`, so CI fails on it too.
//
//   node scripts/check-design-tokens.mjs            summary by rule and file
//   node scripts/check-design-tokens.mjs --list     every finding with its line
//   node scripts/check-design-tokens.mjs --json     machine-readable
//   node scripts/check-design-tokens.mjs --strict   exit 1 when anything is found
import { readdir, readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, join, relative } from "node:path";
import process from "node:process";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const srcDir = join(root, "src");
const args = new Set(process.argv.slice(2));
const print = (line = "") => process.stdout.write(`${line}\n`);
// Piping into `head` closes stdout early; that is not an error.
process.stdout.on("error", (e) => (e.code === "EPIPE" ? process.exit(0) : Promise.reject(e)));

// v2 type scale (tokens.json, type.note). Anything else is off-scale.
const SCALE = new Set([12, 13, 14, 16, 20, 24, 32, 44, 56]);
// Token files may hold literal colours; everything else uses var(--…).
const COLOUR_HOMES = new Set(["src/index.css", "src/theme/fonts.css", "src/theme/counter.css"]);

const RULES = {
  "half-pixel-size": "font size with a fraction (12.5px, 13.5px …): use 12 or 14",
  "off-scale-size": "font size outside 12/13/14/16/20/24/32/44/56",
  "heavy-weight":
    "font weight 650/700/800/bold: v2 tops out at 600 (page titles use var(--weight-title))",
  "colour-literal": "hex/rgb/hsl colour outside the token files: use a colour token",
  "radius-literal": "border-radius in px: use --radius-control/box/overlay/round",
  "shadow-literal":
    "box-shadow literal: in-flow content has none; floating layers use a shadow token",
};

async function cssFiles(dir) {
  const out = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) out.push(...(await cssFiles(path)));
    else if (entry.name.endsWith(".css")) out.push(path);
  }
  return out;
}

// Blank out comments but keep newlines, so line numbers stay true.
const stripComments = (css) => css.replace(/\/\*[\s\S]*?\*\//g, (c) => c.replace(/[^\n]/g, " "));

function sizesIn(decl) {
  // font-size: 13.5px  |  font: 600 13.5px/1.4 var(--font-sans)
  const m =
    decl.match(/^font-size\s*:\s*([0-9.]+)px/) ||
    decl.match(/^font\s*:[^;]*?(?:^|\s)([0-9.]+)px(?:\/|\s|$)/);
  return m ? [Number(m[1])] : [];
}

function weightIn(decl) {
  const m =
    decl.match(/^font-weight\s*:\s*(\d{3}|bold|bolder)\b/) ||
    decl.match(/^font\s*:\s*(?:italic\s+)?(\d{3}|bold)\s/);
  return m ? m[1] : null;
}

function check(file, css) {
  const findings = [];
  const rel = relative(root, file);
  const lines = stripComments(css).split("\n");
  lines.forEach((line, i) => {
    for (const raw of line.split(";")) {
      const decl = raw.trim().replace(/^[^{]*\{\s*/, "");
      if (!decl.includes(":")) continue;
      const at = (rule, text) => findings.push({ file: rel, line: i + 1, rule, text: text.trim() });
      for (const px of sizesIn(decl)) {
        if (!Number.isInteger(px)) at("half-pixel-size", decl);
        else if (!SCALE.has(px)) at("off-scale-size", decl);
      }
      const w = weightIn(decl);
      if (w && (w === "bold" || w === "bolder" || Number(w) > 600)) at("heavy-weight", decl);
      if (
        !COLOUR_HOMES.has(rel) &&
        !decl.startsWith("--") &&
        /#[0-9a-fA-F]{3,8}\b|\b(?:rgb|rgba|hsl|hsla)\(\s*\d/.test(decl)
      )
        at("colour-literal", decl);
      if (/^border(?:-[a-z]+)*-radius\s*:/.test(decl) && /\d+(?:\.\d+)?px/.test(decl))
        at("radius-literal", decl);
      // An inset bar (the "where you are" mark) is a border, not a shadow. A bare
      // `box-shadow:` is the head of a list Prettier put on the lines below;
      // those lines hold no colon, so only inset lists are written that way here.
      if (
        /^box-shadow\s*:/.test(decl) &&
        !/^box-shadow\s*:\s*(?:none|var\(--[a-z-]+\)|inset\b.*)?\s*$/.test(decl)
      )
        at("shadow-literal", decl);
    }
  });
  return findings;
}

const files = (await cssFiles(srcDir)).sort();
const findings = [];
for (const file of files) findings.push(...check(file, await readFile(file, "utf8")));

if (args.has("--json")) {
  print(JSON.stringify({ files: files.length, findings }, null, 2));
} else {
  const byRule = new Map(Object.keys(RULES).map((r) => [r, 0]));
  const byFile = new Map();
  for (const f of findings) {
    byRule.set(f.rule, byRule.get(f.rule) + 1);
    byFile.set(f.file, (byFile.get(f.file) ?? 0) + 1);
  }
  print(`Design tokens v2: ${findings.length} findings in ${files.length} stylesheets\n`);
  for (const [rule, n] of byRule)
    print(`  ${String(n).padStart(5)}  ${rule.padEnd(17)} ${RULES[rule]}`);
  print("\nMost findings:");
  for (const [file, n] of [...byFile].sort((a, b) => b[1] - a[1]).slice(0, 15))
    print(`  ${String(n).padStart(5)}  ${file}`);
  if (args.has("--list")) {
    print("");
    for (const f of findings) print(`${f.file}:${f.line}  ${f.rule}  ${f.text}`);
  }
}

if (args.has("--strict") && findings.length) process.exit(1);
