/**
 * ScanScreen — the phone-first scan surface (#68, #71).
 *
 * Full-screen scan target, big running count, distinct beeps for right and
 * wrong pieces, one confirm button. Built for keyboard-wedge scanners (the
 * scanner types the code and sends Enter): an invisible input keeps focus and
 * swallows the keystrokes, with a manual type-in fallback for tags that won't
 * scan. Every later scan flow (receive, counting, returns) reuses this screen.
 *
 * Scan-is-truth: the scanned counts are the only quantities handed to
 * onConfirm. A piece outside the target list beeps wrong and is rejected
 * (unless the flow allows scan-to-build via `lookup`). A quantity gap versus
 * the expected numbers is flagged on screen, never blocked (Rule 5) — except
 * `strictExpected` flows (receive), where scanning MORE than was sent is a
 * wrong-piece beep.
 *
 * With `exceptions` on (receive), one scan can land in three places: good,
 * damaged, or — for a piece the transfer never sent — extra. Damaged is a mode
 * the operator switches into, because only they can see the piece is broken;
 * extra is detected, not chosen, so a wrong delivery cannot be missed by
 * forgetting to press something first.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Check, CornerUpLeft, PackageX, ShieldAlert, X } from "lucide-react";

import "./ScanScreen.css";

export interface ScanTarget {
  barcode: string;
  label: string; // "Shirt · Blue · M"
  expected: number | null; // planned (dispatch) / sent (receive); null = no plan
  available?: number | null; // stock cap for scan-to-build lines
}

/** What the operator ended up with. Flows without `exceptions` only fill `scans`. */
export interface ScanResult {
  scans: Record<string, number>;
  damaged: Record<string, number>;
  extras: Record<string, number>;
  notes: string;
}

type Bucket = "scans" | "damaged" | "extras";

interface ScanScreenProps {
  mode: string; // "DISPATCH" | "RECEIVE" | "COUNT" — shown in the header chip
  docLabel: string; // doc number or "Draft #12"
  routeLabel: string; // "RAN-WH → DEO"
  targets: ScanTarget[];
  /** Scan-to-build: resolve an unknown barcode (null → wrong piece). */
  lookup?: (barcode: string) => Promise<ScanTarget | null>;
  /** Why a rejected piece was rejected, in this flow's own words. A count is
   *  not a transfer, and telling a counter their shirt is "not on this
   *  transfer" tells them nothing about what to do with it. */
  rejectReason?: string;
  /** What `available` counts, in this flow's words. A return is capped by what
   *  is *returnable*, which on a defect claim is the quarantine bucket and not
   *  the shelf — telling the operator there are only 2 "in stock" while 10 sit
   *  in front of them reads as a bug. */
  availableNoun?: string;
  /** Receive: never allow scanning past `expected` (server rejects too). */
  strictExpected?: boolean;
  /** Receive: offer the damaged mode, accept off-document pieces as extras,
   *  and ask for the shortfall note. */
  exceptions?: boolean;
  confirmLabel: string;
  busy?: boolean;
  error?: string;
  onConfirm: (result: ScanResult) => void;
  onClose: () => void;
}

// --- audio: two unmistakable beeps (ok = short high ping, wrong = low buzz) --

let audioCtx: AudioContext | null = null;
function ctx(): AudioContext {
  audioCtx ??= new AudioContext();
  return audioCtx;
}

function tone(freq: number, ms: number, type: OscillatorType, when = 0) {
  try {
    const ac = ctx();
    const osc = ac.createOscillator();
    const gain = ac.createGain();
    osc.type = type;
    osc.frequency.value = freq;
    gain.gain.value = 0.25;
    osc.connect(gain).connect(ac.destination);
    const t0 = ac.currentTime + when;
    osc.start(t0);
    osc.stop(t0 + ms / 1000);
  } catch {
    /* no audio available — the visual flash still fires */
  }
}

function beepOk() {
  tone(1320, 90, "sine");
  navigator.vibrate?.(30);
}

function beepWrong() {
  tone(196, 160, "square");
  tone(196, 160, "square", 0.2);
  navigator.vibrate?.([120, 60, 120]);
}

/** Accepted, but not what was expected — a third sound, because an extra or a
 *  damaged piece must not feel like a clean scan. */
function beepFlagged() {
  tone(660, 110, "triangle");
  tone(440, 130, "triangle", 0.12);
  navigator.vibrate?.([60, 40, 60]);
}

// ---------------------------------------------------------------------------

const EMPTY: Record<string, number> = {};

function total(counts: Record<string, number>): number {
  return Object.values(counts).reduce((a, b) => a + b, 0);
}

/** Appends `found` unless its barcode is already a line. Two scans of the same
 *  new barcode race their lookups, but both resolve into this functional
 *  update, and React applies them one after another — so the second call
 *  always sees the first one's result and skips the append (#168). */
export function addLineIfAbsent(lines: ScanTarget[], found: ScanTarget): ScanTarget[] {
  return lines.some((l) => l.barcode === found.barcode) ? lines : [...lines, found];
}

export function ScanScreen({
  mode,
  docLabel,
  routeLabel,
  targets,
  lookup,
  rejectReason = "not on this transfer",
  availableNoun = "in stock here",
  strictExpected = false,
  exceptions = false,
  confirmLabel,
  busy = false,
  error = "",
  onConfirm,
  onClose,
}: ScanScreenProps) {
  const [lines, setLines] = useState<ScanTarget[]>(targets);
  const [scanned, setScanned] = useState<Record<string, number>>(EMPTY);
  const [damaged, setDamaged] = useState<Record<string, number>>(EMPTY);
  const [extras, setExtras] = useState<Record<string, number>>(EMPTY);
  const [notes, setNotes] = useState("");
  const [asDamaged, setAsDamaged] = useState(false);
  const [history, setHistory] = useState<{ code: string; bucket: Bucket }[]>([]);
  const [flash, setFlash] = useState<"" | "ok" | "bad" | "flag">("");
  const [last, setLast] = useState<{ text: string; tone: "ok" | "bad" | "flag" } | null>(null);
  const [manual, setManual] = useState("");
  const sinkRef = useRef<HTMLInputElement>(null);
  const flashTimer = useRef<number>(0);

  // The wedge scanner types into whatever is focused — keep the sink focused.
  useEffect(() => {
    const t = window.setInterval(() => {
      const active = document.activeElement;
      const typing =
        active?.className === "scan-manual-input" || active?.className === "scan-notes-input";
      if (!typing && active !== sinkRef.current) {
        sinkRef.current?.focus();
      }
    }, 400);
    sinkRef.current?.focus();
    return () => window.clearInterval(t);
  }, []);

  const showResult = useCallback((tone: "ok" | "bad" | "flag", text: string) => {
    setFlash(tone);
    setLast({ text, tone });
    window.clearTimeout(flashTimer.current);
    flashTimer.current = window.setTimeout(() => setFlash(""), 250);
    if (tone === "ok") beepOk();
    else if (tone === "flag") beepFlagged();
    else beepWrong();
  }, []);

  const bump = useCallback((bucket: Bucket, code: string) => {
    const setter = { scans: setScanned, damaged: setDamaged, extras: setExtras }[bucket];
    setter((s) => ({ ...s, [code]: (s[code] ?? 0) + 1 }));
    setHistory((h) => [...h, { code, bucket }]);
  }, []);

  const applyScan = useCallback(
    async (raw: string) => {
      const code = raw.trim();
      if (!code || busy) return;

      let line = lines.find((l) => l.barcode === code);

      if (!line && lookup) {
        // scan-to-build: ask the server whether this piece exists here
        try {
          const found = await lookup(code);
          if (found) {
            line = found;
            setLines((ls) => addLineIfAbsent(ls, found));
          }
        } catch {
          line = undefined;
        }
      }

      if (!line) {
        if (!exceptions) {
          showResult("bad", `${code} — ${rejectReason}`);
          return;
        }
        // A piece that arrived without being sent. Taken in and flagged rather
        // than turned away: refusing it is how stock goes physically missing.
        bump("extras", code);
        showResult("flag", `${code} — extra, not on this transfer`);
        return;
      }

      const already = (scanned[code] ?? 0) + (damaged[code] ?? 0);
      if (strictExpected && line.expected != null && already >= line.expected) {
        showResult("bad", `${code} — more than was sent (${line.expected})`);
        return;
      }
      if (line.available != null && already >= line.available) {
        showResult("bad", `${code} — only ${line.available} ${availableNoun}`);
        return;
      }

      if (exceptions && asDamaged) {
        bump("damaged", code);
        showResult("flag", `${code} — damaged, to quarantine`);
        return;
      }
      bump("scans", code);
      showResult("ok", `${code} ✓`);
    },
    [
      lines,
      scanned,
      damaged,
      busy,
      lookup,
      rejectReason,
      availableNoun,
      strictExpected,
      exceptions,
      asDamaged,
      showResult,
      bump,
    ],
  );

  function undoLast() {
    const entry = history[history.length - 1];
    if (!entry) return;
    setHistory((h) => h.slice(0, -1));
    const setter = { scans: setScanned, damaged: setDamaged, extras: setExtras }[entry.bucket];
    setter((s) => {
      const next = { ...s, [entry.code]: Math.max(0, (s[entry.code] ?? 0) - 1) };
      if (next[entry.code] === 0) delete next[entry.code];
      return next;
    });
    setLast({ text: `undid ${entry.code}`, tone: "ok" });
  }

  const totalGood = useMemo(() => total(scanned), [scanned]);
  const totalDamaged = useMemo(() => total(damaged), [damaged]);
  const totalExtras = useMemo(() => total(extras), [extras]);
  const totalArrived = totalGood + totalDamaged;
  const totalExpected = useMemo(() => {
    // No lines yet means nothing is expected *of*, not that nothing is expected:
    // a scan-to-build flow starts empty, and a blind count (#76) stays that way.
    // Summing to 0 would put "0 / 0" over the counter's head, which reads as a
    // number the books gave them — the one thing a blind count must never show.
    if (!lines.length || lines.some((l) => l.expected == null)) return null;
    return lines.reduce((a, l) => a + (l.expected ?? 0), 0);
  }, [lines]);

  const hasMismatch =
    totalExpected != null && totalArrived !== totalExpected && totalArrived > 0 && !strictExpected;
  const short =
    strictExpected && totalExpected != null && totalArrived > 0
      ? totalExpected - totalArrived
      : 0;

  return (
    <div className="scan-screen" data-testid="scan-screen">
      <div className="scan-head">
        <span className="scan-head-mode">{mode}</span>
        <div className="scan-head-doc">
          <div className="doc">{docLabel}</div>
          <div className="route">{routeLabel}</div>
        </div>
        <button type="button" className="scan-close" onClick={onClose} aria-label="Close" data-testid="scan-close">
          <X size={22} />
        </button>
      </div>

      {/* wedge-capture sink: the scanner types here and sends Enter */}
      <input
        ref={sinkRef}
        className="scan-sink"
        aria-hidden="true"
        tabIndex={-1}
        autoComplete="off"
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            const v = (e.target as HTMLInputElement).value;
            (e.target as HTMLInputElement).value = "";
            void applyScan(v);
          }
        }}
        data-testid="scan-sink"
      />

      {exceptions && (
        // Two big targets, not a checkbox: the operator is holding a piece in
        // one hand and a phone in the other, and the mode has to be readable
        // at a glance while the next 200 pieces go through.
        <div className="scan-modes" data-testid="scan-mode-toggle">
          <button
            type="button"
            className={`scan-mode ${asDamaged ? "" : "active"}`}
            onClick={() => setAsDamaged(false)}
            data-testid="scan-mode-good"
          >
            <Check size={16} /> Good
          </button>
          <button
            type="button"
            className={`scan-mode danger ${asDamaged ? "active" : ""}`}
            onClick={() => setAsDamaged(true)}
            data-testid="scan-mode-damaged"
          >
            <ShieldAlert size={16} /> Damaged
          </button>
        </div>
      )}

      <div className={`scan-target ${flash ? `flash-${flash}` : ""}`} onClick={() => sinkRef.current?.focus()}>
        <div className="scan-count" data-testid="scan-count">
          {totalArrived}
          {totalExpected != null && <span className="of"> / {totalExpected}</span>}
        </div>
        <div className="hint">
          {exceptions && asDamaged
            ? "Damaged mode — every scan goes to quarantine"
            : "Scan each piece — count climbs, wrong pieces buzz"}
        </div>
        <div className={`scan-last ${last ? last.tone : ""}`} data-testid="scan-last">
          {last?.text ?? ""}
        </div>
      </div>

      {exceptions && (totalDamaged > 0 || totalExtras > 0) && (
        <div className="scan-tallies" data-testid="scan-tallies">
          {totalDamaged > 0 && (
            <span className="chip chip-amber" data-testid="scan-damaged-tally">
              <ShieldAlert size={13} /> {totalDamaged} damaged → quarantine
            </span>
          )}
          {totalExtras > 0 && (
            <span className="chip chip-amber" data-testid="scan-extras-tally">
              <PackageX size={13} /> {totalExtras} extra, not on this transfer
            </span>
          )}
        </div>
      )}

      <div className="scan-manual">
        <input
          className="scan-manual-input"
          value={manual}
          placeholder="Tag won't scan? Type the barcode…"
          onChange={(e) => setManual(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              void applyScan(manual);
              setManual("");
            }
          }}
          data-testid="scan-manual-input"
        />
        <button
          type="button"
          onClick={() => {
            void applyScan(manual);
            setManual("");
          }}
          data-testid="scan-manual-add"
        >
          Add
        </button>
      </div>

      <div className="scan-lines" data-testid="scan-lines">
        {lines.map((l) => {
          const good = scanned[l.barcode] ?? 0;
          const broken = damaged[l.barcode] ?? 0;
          const count = good + broken;
          const done = l.expected != null && count === l.expected;
          const over = l.expected != null && count > l.expected;
          return (
            <div key={l.barcode} className={`scan-line ${done ? "done" : ""} ${over ? "over" : ""}`} data-testid={`scan-line-${l.barcode}`}>
              <div>
                <div className="code">{l.barcode}</div>
                <div className="meta">
                  {l.label}
                  {broken > 0 && <b className="scan-line-flag"> · {broken} damaged</b>}
                </div>
              </div>
              <div className="counts">
                {count}
                {l.expected != null && <span className="exp"> / {l.expected}</span>}
              </div>
              {done && (
                <span className="tick">
                  <Check size={18} />
                </span>
              )}
            </div>
          );
        })}
        {Object.entries(extras).map(([code, qty]) => (
          <div key={`extra-${code}`} className="scan-line extra" data-testid={`scan-extra-${code}`}>
            <div>
              <div className="code">{code}</div>
              <div className="meta">Extra — not on this transfer</div>
            </div>
            <div className="counts">{qty}</div>
          </div>
        ))}
        {lines.length === 0 && Object.keys(extras).length === 0 && (
          <div className="scan-line">
            <div className="meta">Nothing scanned yet — scan the first piece to start the list.</div>
          </div>
        )}
      </div>

      {/* The note only appears once there is a shortfall to explain — asking for
          it on every clean receive would train people to leave it blank. */}
      {exceptions && short > 0 && (
        <div className="scan-notes">
          <input
            className="scan-notes-input"
            value={notes}
            placeholder={`${short} piece(s) missing — say what you saw (optional)`}
            onChange={(e) => setNotes(e.target.value)}
            data-testid="scan-notes-input"
          />
        </div>
      )}

      {error ? (
        <div className="scan-error" data-testid="scan-error">{error}</div>
      ) : hasMismatch ? (
        <div className="scan-mismatch-note" data-testid="scan-mismatch-note">
          Scanned ≠ planned — the transfer will carry a mismatch flag.
        </div>
      ) : short > 0 ? (
        <div className="scan-mismatch-note" data-testid="scan-short-note">
          {short} piece(s) not scanned — they stay in transit until a senior closes the gap.
        </div>
      ) : null}

      <div className="scan-foot">
        <button type="button" className="scan-undo" onClick={undoLast} disabled={history.length === 0 || busy} data-testid="scan-undo">
          <CornerUpLeft size={16} /> Undo
        </button>
        <button
          type="button"
          className="scan-confirm"
          disabled={history.length === 0 || busy}
          onClick={() => onConfirm({ scans: scanned, damaged, extras, notes })}
          data-testid="scan-confirm"
        >
          <Check size={20} /> {busy ? "Posting…" : confirmLabel}
        </button>
      </div>
    </div>
  );
}
