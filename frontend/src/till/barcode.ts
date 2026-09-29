/** Code 128 subset B for bill numbers on an offline receipt.
 *
 * The encoder is intentionally self-contained: printing must keep working with
 * no network and no library loaded beyond the app bundle. Codewords 0-106 use
 * the symbol patterns defined by ISO/IEC 15417; each digit is the width of the
 * next black/white run in modules. */
const PATTERNS = [
  "212222", "222122", "222221", "121223", "121322", "131222", "122213", "122312",
  "132212", "221213", "221312", "231212", "112232", "122132", "122231", "113222",
  "123122", "123221", "223211", "221132", "221231", "213212", "223112", "312131",
  "311222", "321122", "321221", "312212", "322112", "322211", "212123", "212321",
  "232121", "111323", "131123", "131321", "112313", "132113", "132311", "211313",
  "231113", "231311", "112133", "112331", "132131", "113123", "113321", "133121",
  "313121", "211331", "231131", "213113", "213311", "213131", "311123", "311321",
  "331121", "312113", "312311", "332111", "314111", "221411", "431111", "111224",
  "111422", "121124", "121421", "141122", "141221", "112214", "112412", "122114",
  "122411", "142112", "142211", "241211", "221114", "413111", "241112", "134111",
  "111242", "121142", "121241", "114212", "124112", "124211", "411212", "421112",
  "421211", "212141", "214121", "412121", "111143", "111341", "131141", "114113",
  "114311", "411113", "411311", "113141", "114131", "311141", "411131", "211412",
  "211214", "211232", "2331112",
] as const;

const START_B = 104;
const STOP = 106;

/** Return the complete Code 128 B stream: start, data, checksum and stop. */
export function code128B(value: string): number[] {
  if (!value.length) throw new Error("Code 128 B needs at least one character.");

  const data = [...value].map((character) => {
    const point = character.codePointAt(0) ?? 0;
    if (point < 32 || point > 126) {
      throw new Error(`Code 128 B cannot encode ${JSON.stringify(character)}.`);
    }
    return point - 32;
  });
  const checksum = (START_B + data.reduce((sum, code, index) => sum + code * (index + 1), 0)) % 103;
  return [START_B, ...data, checksum, STOP];
}

function attr(value: string): string {
  return value.replace(
    /[&<>"]/g,
    (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[character] as string,
  );
}

/** Draw a scalable, one-dimensional barcode. A ten-module quiet zone is kept
 * on both sides and the SVG scales as one unit, so it stays inside 80mm paper. */
export function code128Svg(
  value: string,
  options: { className?: string } = {},
): string {
  const quiet = 10;
  const height = 64;
  let x = quiet;
  const bars: string[] = [];

  for (const code of code128B(value)) {
    const pattern = PATTERNS[code];
    for (let run = 0; run < pattern.length; run += 1) {
      const width = Number(pattern[run]);
      if (run % 2 === 0) bars.push(`<rect x="${x}" y="0" width="${width}" height="${height}"/>`);
      x += width;
    }
  }

  const className = options.className ? ` class="${attr(options.className)}"` : "";
  return `<svg xmlns="http://www.w3.org/2000/svg"${className} role="img" aria-label="Bill ${attr(value)}" viewBox="0 0 ${x + quiet} ${height}" width="100%" height="${height}" preserveAspectRatio="none" shape-rendering="crispEdges">${bars.join("")}</svg>`;
}
