// Design System v2 Phase 2: rewrite literal font sizes, weights and corner radii
// in the app's stylesheets onto the v2 scale (docs/planning/design-system-v2-rollout.md).
//
//   node scripts/codemods/type-v2.mjs                  dry run: what would change, by area
//   node scripts/codemods/type-v2.mjs --list           every change with its file and line
//   node scripts/codemods/type-v2.mjs --judged         only the changes a person should check
//   node scripts/codemods/type-v2.mjs --write          apply
//   --area=shell|components|sell|pages                 limit to one area
//
// Sizes 15–19px and radii 7–10px depend on what the rule styles; the codemod
// decides from the selector (a heading, an amount, a container) and marks
// those changes "judged" so a reviewer can look at each one.
import { readdir, readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, join, relative } from "node:path";
import process from "node:process";

const root = dirname(dirname(dirname(fileURLToPath(import.meta.url))));
const srcDir = join(root, "src");
const args = process.argv.slice(2);
const flag = (name) => args.includes(`--${name}`);
const areaArg = args.find((a) => a.startsWith("--area="))?.slice(7) ?? "all";
const print = (line = "") => process.stdout.write(`${line}\n`);
// Piping into `head` closes stdout early; that is not an error.
process.stdout.on("error", (e) => (e.code === "EPIPE" ? process.exit(0) : Promise.reject(e)));

const AREAS = {
  shell: (f) => f.startsWith("src/shell/") || f.startsWith("src/theme/") || f === "src/index.css",
  sell: (f) => f.startsWith("src/pages/sell/"),
  pages: (f) => f.startsWith("src/pages/") && !f.startsWith("src/pages/sell/"),
  components: (f) =>
    !f.startsWith("src/pages/") &&
    !f.startsWith("src/shell/") &&
    !f.startsWith("src/theme/") &&
    f !== "src/index.css",
};
const areaOf = (f) => Object.keys(AREAS).find((a) => AREAS[a](f));

const HEADING = /(title|head|heading|\bh[1-4]\b|name|brand|hero|lead)/i;
const MONEY =
  /(total|amount|money|pay|due|net|value|num|figure|price|sum|balance|metric|kpi|stat|cash|tender)/i;
const OVERLAY = /(modal|dialog|drawer|sheet|overlay)/i;
// A control is named by the last part of the selector, whatever box it sits in.
const CONTROL =
  /(input|button|select|textarea|btn|chip|pill|badge|stepper|seg|toggle|tab|check|count|ic|icon|kbd|tag|dot)\b/i;
const BOX =
  /(card|panel|box|table|section|pane|aside|rail|tile|menu|popover|dropdown|list|band|note|banner|scan|bill|receipt|frame|zone|drop|tray|toast|wrap|block|preview|summary)/i;

/** The v2 size for a literal px size, and whether a person should check it. */
function mapSize(px, selector, ruleIsBold, ruleIsFigure) {
  const heading = HEADING.test(selector) || ruleIsBold;
  const money = MONEY.test(selector) || ruleIsFigure;
  if (px < 12.75) return [12, false]; // nothing smaller than 12
  if (px === 13.5 || px === 14.5) return [14, false];
  if (px === 15) return heading || money ? [16, true] : [14, true];
  if (px === 15.5) return [16, false];
  if (px >= 16.5 && px <= 19.5) {
    if (money && !HEADING.test(selector)) return [20, true];
    return px >= 19 && !heading ? [20, true] : [16, true];
  }
  if (px >= 21 && px <= 28) return [24, false];
  if (px >= 29 && px <= 38) return [32, false];
  if (px >= 39 && px <= 50) return [44, true];
  if (px > 50) return [56, true];
  return [px, false];
}

/** The v2 radius token for one px value. */
function mapRadius(px, selector) {
  if (px === 0) return ["0", false];
  if (px >= 99) return ["var(--radius-round)", false];
  if (px <= 6) return ["var(--radius-control)", false];
  if (OVERLAY.test(selector)) return ["var(--radius-overlay)", px < 12];
  const last = selector
    .split(",")
    .pop()
    .trim()
    .split(/[\s>+~]+/)
    .pop();
  if (CONTROL.test(last)) return ["var(--radius-control)", px > 6];
  if (px >= 14) return ["var(--radius-overlay)", false];
  if (px >= 11) return ["var(--radius-box)", false];
  // 7–10px: a container or a control, by what the selector names.
  return BOX.test(last) ? ["var(--radius-box)", true] : ["var(--radius-control)", true];
}

async function cssFiles(dir) {
  const out = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) out.push(...(await cssFiles(path)));
    else if (entry.name.endsWith(".css")) out.push(path);
  }
  return out;
}

/** Split prettier-formatted CSS into rules: selector text plus its declaration lines. */
function rules(lines) {
  const out = [];
  const stack = [];
  let pending = "";
  let inComment = false;
  lines.forEach((line, i) => {
    let text = line;
    if (inComment) {
      if (!text.includes("*/")) return;
      text = text.slice(text.indexOf("*/") + 2);
      inComment = false;
    }
    text = text.replace(/\/\*.*?\*\//g, "");
    if (text.includes("/*")) {
      inComment = true;
      text = text.slice(0, text.indexOf("/*"));
    }
    const trimmed = text.trim();
    if (!trimmed) return;
    if (trimmed.endsWith("{")) {
      const sel = (pending + " " + trimmed.slice(0, -1)).trim();
      pending = "";
      const rule = { selector: sel, lines: [] };
      stack.push(rule);
      if (!sel.startsWith("@")) out.push(rule);
      return;
    }
    if (trimmed.startsWith("}")) {
      stack.pop();
      return;
    }
    if (trimmed.endsWith(",") && !trimmed.includes(":")) {
      pending += " " + trimmed;
      return;
    }
    const current = stack[stack.length - 1];
    if (current && !current.selector.startsWith("@")) current.lines.push(i);
    else if (trimmed.endsWith(",")) pending += " " + trimmed;
  });
  return out;
}

const fmt = (n) => `${Number.isInteger(n) ? n : n}px`;

function transform(rel, css) {
  const lines = css.split("\n");
  const changes = [];
  for (const rule of rules(lines)) {
    const ruleIsBold = rule.lines.some((i) =>
      /font-weight\s*:\s*(6\d\d|7\d\d|8\d\d|bold)|font\s*:\s*(6|7|8)\d\d\s/.test(lines[i]),
    );
    const ruleIsFigure = rule.lines.some((i) => /tabular-nums/.test(lines[i]));
    for (const i of rule.lines) {
      const before = lines[i];
      let after = before;
      const judged = [];
      // font-size: 13.5px
      after = after.replace(/(font-size\s*:\s*)([0-9.]+)px/, (m, pre, n) => {
        const [v, j] = mapSize(Number(n), rule.selector, ruleIsBold, ruleIsFigure);
        if (j) judged.push(`size ${n}→${v}`);
        return `${pre}${fmt(v)}`;
      });
      // font: 600 13.5px/1.4 …
      after = after.replace(
        /(font\s*:\s*(?:italic\s+)?(?:\d{3}\s+|bold\s+)?)([0-9.]+)px/,
        (m, pre, n) => {
          const [v, j] = mapSize(Number(n), rule.selector, ruleIsBold, ruleIsFigure);
          if (j) judged.push(`size ${n}→${v}`);
          return `${pre}${fmt(v)}`;
        },
      );
      // weights: 650, 700, 800, bold → 600
      after = after.replace(/(font-weight\s*:\s*)(?:[7-9]\d\d|6[5-9]\d|bold(?:er)?)\b/, "$1600");
      after = after.replace(
        /(font\s*:\s*(?:italic\s+)?)(?:[7-9]\d\d|6[5-9]\d|bold)(\s)/,
        "$1600$2",
      );
      // radius literals
      after = after.replace(/(border(?:-[a-z]+)*-radius\s*:\s*)([^;]+)/, (m, pre, value) => {
        if (!/\d+(?:\.\d+)?px/.test(value)) return m;
        let mapped = value.replace(/(\d+(?:\.\d+)?)px/g, (_, n) => {
          const [v, j] = mapRadius(Number(n), rule.selector);
          if (j) judged.push(`radius ${n}px→${v}`);
          return v;
        });
        // "4px 4px 4px 4px" after mapping reads better as one value.
        const parts = mapped.trim().split(/\s+/);
        if (parts.length > 1 && parts.every((p) => p === parts[0])) mapped = parts[0];
        return `${pre}${mapped}`;
      });
      if (after !== before) {
        lines[i] = after;
        changes.push({
          file: rel,
          line: i + 1,
          selector: rule.selector,
          before: before.trim(),
          after: after.trim(),
          judged,
        });
      }
    }
  }
  return { css: lines.join("\n"), changes };
}

const files = (await cssFiles(srcDir)).sort();
const all = [];
for (const file of files) {
  const rel = relative(root, file);
  if (areaArg !== "all" && areaOf(rel) !== areaArg) continue;
  const css = await readFile(file, "utf8");
  const { css: next, changes } = transform(rel, css);
  all.push(...changes);
  if (flag("write") && next !== css) await writeFile(file, next);
}

const byArea = new Map();
for (const c of all) byArea.set(areaOf(c.file), (byArea.get(areaOf(c.file)) ?? 0) + 1);
const judged = all.filter((c) => c.judged.length);
print(
  `${flag("write") ? "Changed" : "Would change"} ${all.length} declarations ` +
    `(${judged.length} judged) in ${new Set(all.map((c) => c.file)).size} files`,
);
for (const [area, n] of byArea) print(`  ${String(n).padStart(5)}  ${area}`);
if (flag("list") || flag("judged")) {
  print("");
  for (const c of flag("judged") ? judged : all) {
    const note = c.judged.length ? `   [${c.judged.join(", ")}]` : "";
    print(`${c.file}:${c.line}  ${c.selector.slice(0, 60)}`);
    print(`    ${c.before}  →  ${c.after}${note}`);
  }
}
