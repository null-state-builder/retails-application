/**
 * Building a list URL from its filters (#102).
 *
 * Every list screen now carries at least one filter — a search term — and some
 * carry two or three. Hand-rolling that per screen is how two screens end up
 * sending `?q=` and `?search=`, or how a blank box sends `?q=` and quietly
 * becomes a different request from no box at all.
 *
 * So: one function. A parameter that is empty, blank or absent is *not sent*,
 * which is what makes an empty search box byte-for-byte the request the screen
 * made before search existed.
 */

export type QueryParams = Record<string, string | number | undefined | null>;

export function withQuery(path: string, params: QueryParams = {}): string {
  const search = new URLSearchParams();
  for (const [key, raw] of Object.entries(params)) {
    const value = raw == null ? "" : String(raw).trim();
    if (value) search.set(key, value);
  }
  const qs = search.toString();
  if (!qs) return path;
  return `${path}${path.includes("?") ? "&" : "?"}${qs}`;
}
