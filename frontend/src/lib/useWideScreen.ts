import { useEffect, useState } from "react";

/** The width at which a list keeps its record beside it rather than over it
 *  (desktop redesign rulebook, layout L). Kept in step with `.split-view` in
 *  index.css, which collapses below the same width. */
export const SPLIT_MIN_WIDTH = 1100;

/** True while the window is wide enough for a list and its record side by
 *  side. Follows resizes, so a narrowed window falls back to the drawer. */
export function useWideScreen(minWidth = SPLIT_MIN_WIDTH): boolean {
  const query = `(min-width: ${minWidth}px)`;
  const [wide, setWide] = useState(() =>
    typeof window === "undefined" ? true : window.matchMedia(query).matches,
  );
  useEffect(() => {
    const mq = window.matchMedia(query);
    const onChange = () => setWide(mq.matches);
    onChange();
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [query]);
  return wide;
}
