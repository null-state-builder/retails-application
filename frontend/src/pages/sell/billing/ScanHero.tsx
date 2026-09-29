import { AlertTriangle, Barcode, Search } from "lucide-react";

import type { CounterMode } from "./BillBar";

/** The scanner says one short, glanceable thing. A failed scan wins over the
 * normal mode prompt until the cashier explicitly clears it. */
export type ReturnScanStage = "bill" | "return" | "exchange";

export function scanHeroState(
  mode: CounterMode,
  hasError: boolean,
  returnStage: ReturnScanStage = "bill",
): string {
  if (hasError) return "NOT FOUND";
  if (mode !== "return") return "LISTENING";
  if (returnStage === "return") return "MARKING RETURN";
  if (returnStage === "exchange") return "EXCHANGE OUT";
  return "AWAITING BILL";
}

export function ScanHero({
  mode,
  boxRef,
  value,
  disabled,
  placeholder,
  hasError,
  errorBarcode,
  errorMessage,
  returnStage = "bill",
  demoCodes,
  onChange,
  onSubmit,
  onLookup,
  onDismissError,
  onBillOffTag,
  onDemoScan,
}: {
  mode: CounterMode;
  boxRef: (node: HTMLInputElement | null) => void;
  value: string;
  disabled: boolean;
  placeholder: string;
  hasError: boolean;
  errorBarcode: string;
  errorMessage?: string;
  returnStage?: ReturnScanStage;
  demoCodes: string[];
  onChange: (value: string) => void;
  onSubmit: (value: string) => void;
  onLookup: () => void;
  onDismissError: () => void;
  onBillOffTag: () => void;
  onDemoScan: (code: string) => void;
}) {
  const state = scanHeroState(mode, hasError, returnStage);

  return (
    <section className={hasError ? "scan-hero is-error" : "scan-hero"} aria-label="Scanner">
      <div className="scan-hero-row">
        <div className="scan-hero-input-wrap">
          <Barcode size={22} aria-hidden="true" />
          <input
            ref={boxRef}
            className="scan-hero-input"
            data-testid="bill-scan"
            autoComplete="off"
            spellCheck={false}
            disabled={disabled}
            placeholder={placeholder}
            aria-label={placeholder}
            value={value}
            onChange={(event) => onChange(event.target.value)}
            onKeyDown={(event) => {
              if (event.key !== "Enter") return;
              event.preventDefault();
              // The live DOM value, not the closed-over `value` prop (#257): a
              // wedge firing barcode after barcode faster than React repaints
              // can have this handler still bound to a render from before the
              // last keystroke committed, and the prop would submit whatever
              // was typed as of that stale render - a truncated code that
              // genuinely fails to resolve, on a tag that is really on the
              // shelf.
              onSubmit(event.currentTarget.value);
            }}
          />
          <span className="scan-hero-state" data-testid="scan-hero-state">
            <span className="scan-hero-state-ring" aria-hidden="true" />
            {state}
          </span>
        </div>
        <button
          type="button"
          className="scan-hero-lookup"
          aria-label="Look up"
          title="Look up (F3)"
          onClick={onLookup}
          disabled={disabled}
        >
          <Search size={16} />
          <span>Look up</span>
          <small>F3</small>
        </button>
      </div>

      {hasError && (
        <div className="scan-hero-error" data-testid="bill-scan-error" role="alert">
          <span className="scan-hero-error-icon" aria-hidden="true">
            <AlertTriangle size={14} />
          </span>
          <span>
            <strong>
              {mode === "return" && returnStage !== "exchange"
                ? "That scan cannot be used here"
                : "We could not find that tag"}
            </strong>
            <small>
              {errorMessage ?? (
                <>
                  {errorBarcode ? <><span className="mono">{errorBarcode}</span> is not on this counter. </> : null}
                  Check the tag or look it up; if it arrived before its paperwork, bill it off the tag.
                </>
              )}
            </small>
          </span>
          {(mode === "sale" || returnStage === "exchange") && (
            <button type="button" className="btn" onClick={onBillOffTag} disabled={disabled}>
              Bill off tag
            </button>
          )}
          <button type="button" className="btn" onClick={onDismissError}>
            Dismiss · Esc
          </button>
        </div>
      )}

      {import.meta.env.DEV && demoCodes.length > 0 && (
        <div className="scan-hero-demo" data-testid="scan-hero-demo">
          <span>Demo scanner</span>
          {demoCodes.map((code) => (
            <button key={code} type="button" onClick={() => onDemoScan(code)} disabled={disabled}>
              {code}
            </button>
          ))}
        </div>
      )}
    </section>
  );
}
