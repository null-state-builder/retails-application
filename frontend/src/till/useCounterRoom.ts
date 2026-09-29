import { useLayoutEffect } from "react";

/** Mark the scanner-first workspace without replacing the application's theme.
 *  Exported as the browser seam so StrictMode cleanup stays directly testable. */
export function enterCounterRoom(root: HTMLElement): () => void {
  const previousRoom = root.dataset.room;
  root.dataset.room = "counter";

  return () => {
    if (previousRoom === undefined) delete root.dataset.room;
    else root.dataset.room = previousRoom;
  };
}

/** Apply operational counter roles before the first frame, then restore them. */
export function useCounterRoom(): void {
  useLayoutEffect(() => enterCounterRoom(document.documentElement), []);
}
