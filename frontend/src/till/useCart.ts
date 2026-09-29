// The bill's own cart state, and the one safe way to add a scanned piece to
// it (#257).

import { useCallback, useState } from "react";
import type { Dispatch, SetStateAction } from "react";

import { emptyCart, scanPiece } from "./cart";
import type { Cart } from "./cart";
import type { TillItem } from "./types";

export interface CartApi {
  cart: Cart;
  setCart: Dispatch<SetStateAction<Cart>>;
  /**
   * A scanned piece, onto the bill.
   *
   * Chains through `setCart`'s own functional updater rather than a
   * closed-over `cart` - a wedge firing scans faster than React repaints can
   * call `take` again before the previous scan's `setCart` has committed,
   * and reading a closure would then compute both from the same pre-burst
   * cart: whichever `setCart` call React applies last would win outright,
   * silently dropping the line computed before it. The updater always
   * receives whatever React actually has pending, so the same race chains
   * the scans onto one another instead of racing them - the #168 fix's own
   * pattern (PR #314's `addLineIfAbsent`).
   *
   * `onApplied` runs synchronously inside the updater, with the cart this
   * exact scan produced - the one place a caller can read that result
   * without waiting for a render, for a synchronous mirror (Billing's
   * `onScreenRef`) that cannot wait for one.
   */
  take: (
    piece: TillItem,
    found: { stock: number; alternatives: TillItem[] },
    salesperson: string | null,
    onApplied?: (next: Cart) => void,
  ) => void;
}

export function useCart(initial: Cart = emptyCart()): CartApi {
  const [cart, setCart] = useState<Cart>(initial);

  const take = useCallback(
    (
      piece: TillItem,
      found: { stock: number; alternatives: TillItem[] },
      salesperson: string | null,
      onApplied?: (next: Cart) => void,
    ) => {
      setCart((current) => {
        const next = scanPiece(current, piece, found, salesperson);
        onApplied?.(next);
        return next;
      });
    },
    [],
  );

  return { cart, setCart, take };
}
