// Size curve on the booking form (store operations ticket 40, ST-BUY-2).
//
// A booking line entered as a style total (a style and a quantity, no size) can
// have its sizes filled from the store's size curve: how the brand's category
// sold by size there in the same season last year. The server works the split
// out and records it (`POST /goods-v1/bookings/size-curve/fills`); this module
// only decides which lines can be filled and puts the answer into the form.
import type { ApiRead, ApiSchemas } from "./api";

export type SizeCurves = ApiRead<ApiSchemas["SizeCurves"]>;
export type SizeCurve = ApiRead<ApiSchemas["SizeCurve"]>;
export type SizeCurveFill = ApiRead<ApiSchemas["SizeCurveFill"]>;
export type FilledSize = ApiRead<ApiSchemas["SizeCurveFillSize"]>;

export const CURVE_PATH = "/goods-v1/bookings/size-curve";
export const FILL_PATH = "/goods-v1/bookings/size-curve/fills";

interface TypedLine {
  style_code: string;
  size: string;
  qty: string;
}

/** A style total: a style and a whole number of pieces, with no size yet. */
export function canFill(line: TypedLine): boolean {
  return (
    Boolean(line.style_code.trim()) &&
    !line.size.trim() &&
    /^[1-9]\d*$/.test(line.qty.trim())
  );
}

/** The form's lines with line `index` replaced by one line per size the split
 *  gives pieces to, each a copy of it with that size and quantity (passed
 *  through `renew`, to give each its own id). An index with no line changes
 *  nothing. */
export function expandLine<T extends TypedLine>(
  lines: T[],
  index: number,
  sizes: FilledSize[],
  renew: (line: T) => T = (line) => line,
): T[] {
  const typed = lines[index];
  if (index < 0 || typed === undefined) return lines;
  const filled = sizes
    .filter((s) => s.qty > 0)
    .map((s) => renew({ ...typed, size: s.size, qty: String(s.qty) }));
  return [...lines.slice(0, index), ...filled, ...lines.slice(index + 1)];
}

/** "S 20% · M 50% · L 30%": what a category's curve says. */
export function curveWords(curve: SizeCurve): string {
  return curve.sizes
    .map(
      (s) =>
        `${s.size} ${s.share_permille % 10 ? (s.share_permille / 10).toFixed(1) : s.share_permille / 10}%`,
    )
    .join(" · ");
}

/** "S 4, M 10, L 6": what a fill gave. */
export function splitWords(sizes: FilledSize[]): string {
  return sizes
    .filter((s) => s.qty > 0)
    .map((s) => `${s.size} ${s.qty}`)
    .join(", ");
}
