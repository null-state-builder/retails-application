// Getting the paper out of the machine (#181, requirement G2).
//
// One interface with one implementation, because the implementation is the open
// question: the printer route (WebUSB / Web Serial against the thermal head, or
// a QZ Tray agent on the counter machine) is decided by the hardware spike
// (#190), which is a lead-time item like the barcode spike was. `PrintAdapter`
// is the seam that lets that answer land without touching a caller.
//
// The rule the whole thing exists to serve is G2, and it is a rule about
// *money*, not about paper: **the bill saves whether or not it prints.** Save &
// Print commits the sale into the local database first and prints second, so a
// printer that is off is a banner and a Reprint button, never a lost sale and
// never a bill number spent on nothing.
//
// `probe` is honest about what a browser can see. Nothing in the web platform
// says whether a thermal printer is switched on; what it can say is whether this
// device can print at all. So the browser adapter's probe answers that question
// and says so, rather than returning a confident `true` that means nothing. The
// screen's warning comes from the print *failing*, which is a fact.

/** Why printing did not happen, or that it did. */
export interface PrintOutcome {
  ok: boolean;
  /** A sentence for the counter. Empty when it worked. */
  reason: string;
}

export interface PrintAdapter {
  /** Can this device print at all? */
  probe(): Promise<PrintOutcome>;
  /** Put a whole HTML document on paper. */
  print(html: string): Promise<PrintOutcome>;
}

const OK: PrintOutcome = { ok: true, reason: "" };

/** How long to leave the frame in the document after asking for the print.
 *
 *  `window.print()` is synchronous in every browser that matters, but the frame
 *  cannot be torn down in the same tick: Chrome renders the preview from the
 *  live document, and removing it immediately prints a blank page. */
const TEARDOWN_MS = 1_000;

/** How long to wait for the frame to paint before giving up on it.
 *
 *  Without a bound, a `load` that never fires is not a failed print - it is a
 *  promise that never settles, so the caller's `finally` never runs and every
 *  control on the counter stays disabled until somebody reloads the page. The
 *  bill is already committed by then, so the honest outcome is "it did not
 *  print", not a frozen till. */
const PAINT_TIMEOUT_MS = 5_000;

/**
 * Print through the browser: a hidden frame, and the browser's own dialogue.
 *
 * A frame rather than a new window, because a pop-up blocker is a printer that
 * silently does nothing on a shop floor, and because the receipt's own styles
 * must not inherit the app's - it is 80mm of paper, not a page of the PWA.
 */
export const browserPrintAdapter: PrintAdapter = {
  async probe(): Promise<PrintOutcome> {
    if (typeof window === "undefined" || typeof window.print !== "function") {
      return { ok: false, reason: "This device cannot print. The bill will still save." };
    }
    return OK;
  },

  async print(html: string): Promise<PrintOutcome> {
    const ready = await this.probe();
    if (!ready.ok) return ready;

    let frame: HTMLIFrameElement | null = null;
    try {
      frame = document.createElement("iframe");
      frame.setAttribute("aria-hidden", "true");
      frame.title = "Receipt";
      // Off-screen rather than `display: none`: a frame that is not rendered has
      // nothing to print.
      frame.style.cssText = "position:fixed;right:0;bottom:0;width:1px;height:1px;border:0;";
      frame.srcdoc = html;
      const painted = new Promise<boolean>((resolve) => {
        frame!.addEventListener("load", () => resolve(true), { once: true });
        window.setTimeout(() => resolve(false), PAINT_TIMEOUT_MS);
      });
      document.body.appendChild(frame);
      if (!(await painted)) {
        teardown(frame, 0);
        return { ok: false, reason: "The receipt took too long to prepare." };
      }
      const inner = frame.contentWindow;
      if (!inner) throw new Error("The receipt could not be prepared.");
      inner.focus();
      inner.print();
      teardown(frame);
      return OK;
    } catch (error) {
      if (frame) teardown(frame, 0);
      return {
        ok: false,
        reason: error instanceof Error ? error.message : "The receipt did not print.",
      };
    }
  },
};

function teardown(frame: HTMLIFrameElement, after = TEARDOWN_MS): void {
  window.setTimeout(() => frame.remove(), after);
}
