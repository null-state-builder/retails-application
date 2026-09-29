import { useEffect } from "react";

export type CounterKeyAction =
  | "hold"
  | "lookup"
  | "new-bill"
  | "save"
  | "back-to-scan"
  | "next-bill";

/** The counter owns these keys. Keeping the map pure makes the browser handler
 * small and gives the operational key contract one direct test seam. */
export function counterKeyAction(key: string): CounterKeyAction | null {
  switch (key) {
    case "F2":
      return "hold";
    case "F3":
      return "lookup";
    case "F4":
      return "new-bill";
    case "F9":
      return "save";
    case "Escape":
      return "back-to-scan";
    default:
      return null;
  }
}

/** A modal or decision surface owns the keyboard until it is answered. */
export function activeCounterKeyAction(
  key: string,
  disabled: boolean,
  finishOpen: boolean,
): CounterKeyAction | null {
  if (disabled) return null;
  if (finishOpen) return key === "Enter" ? "next-bill" : null;
  return counterKeyAction(key);
}

/** Claim the counter's accelerators unless a modal is asking the cashier a
 * question. A modal owns its own Escape handling, so the bill must stay still. */
export function useCounterKeys({
  disabled,
  finishOpen,
  onHold,
  onLookup,
  onNewBill,
  onSave,
  onBackToScan,
  onNextBill,
}: {
  disabled: boolean;
  finishOpen: boolean;
  onHold: () => void;
  onLookup: () => void;
  onNewBill: () => void;
  onSave: () => void;
  onBackToScan: () => void;
  onNextBill: () => void;
}): void {
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      const action = activeCounterKeyAction(event.key, disabled, finishOpen);
      if (!action) return;

      event.preventDefault();
      if (event.repeat) return;

      switch (action) {
        case "hold":
          onHold();
          break;
        case "lookup":
          onLookup();
          break;
        case "new-bill":
          onNewBill();
          break;
        case "save":
          onSave();
          break;
        case "back-to-scan":
          onBackToScan();
          break;
        case "next-bill":
          onNextBill();
          break;
      }
    }

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [disabled, finishOpen, onBackToScan, onHold, onLookup, onNewBill, onNextBill, onSave]);
}
