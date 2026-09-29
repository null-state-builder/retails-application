import { useEffect, useState } from "react";

import { TillDisplayLink, openChannel } from "./customerDisplay";

/**
 * The till's end of the customer display (ticket 09), open while this counter
 * is on screen and the store has the display switched on.
 *
 * Null when the display is off, or the browser has no channel to offer: the
 * counter then simply shows no display, and bills exactly as before. Leaving
 * the counter (or the page) tells the display to go back to its welcome.
 */
export function useTillDisplay(storeCode: string | null): TillDisplayLink | null {
  const [link, setLink] = useState<TillDisplayLink | null>(null);
  useEffect(() => {
    if (!storeCode) return;
    const channel = openChannel(storeCode);
    if (!channel) return;
    const opened = new TillDisplayLink(channel);
    setLink(opened);
    const leave = () => opened.close();
    window.addEventListener("pagehide", leave);
    return () => {
      window.removeEventListener("pagehide", leave);
      opened.close();
      setLink(null);
    };
  }, [storeCode]);
  return link;
}
