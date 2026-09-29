// The sync light (#180, D10 grill Q1).
//
// The one piece of the till layer a cashier looks at all day, so it says a word
// and a number rather than a colour alone: "Synced", "3 waiting", "Bill refused".
// Colour-blindness aside, a red dot on its own does not tell anybody what to do.
//
// Both the word and the sentence behind it come from `deriveStatus`, so this
// renders the light and decides nothing about it.
//
// Billing lifts its till provider above the shell so this same light can live in
// the top bar while the counter room is open. Other Sell screens keep their
// route-local provider and render it where their own screen needs it.

import { useTill } from "./TillProvider";

/** The light itself. Renders nothing when this login has no counter. */
export function SyncLight({ counter = false }: { counter?: boolean } = {}) {
  const { till } = useTill();
  if (!till) return null;
  const { colour, label, reason } = till.status;
  const visibleLabel = counter && label === "Offline" ? "Offline · will sync" : label;
  return (
    <span className="sync-light" data-testid="till-sync-light" data-colour={colour}>
      <span className={`chip chip-${colour}`} title={reason}>
        {visibleLabel}
      </span>
    </span>
  );
}
