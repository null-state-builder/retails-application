import { useEffect, useRef, useState } from "react";
import { Search, X } from "lucide-react";

import "./SearchBox.css";

/**
 * The in-page search box (#102) — one control, every list screen.
 *
 * The top bar searches the whole system; this narrows the list you are already
 * standing on. Screens do not build their own: a store person learns the
 * behaviour once, on the stock screen, and every other screen honours it.
 *
 * Two input habits share one box, exactly as the top bar's does. A person
 * *types* a few letters, so the term is debounced — the list settles instead of
 * flickering through a query per keystroke. A wedge scanner *types a whole
 * barcode at machine speed and presses Enter*, so Enter commits immediately and
 * the pending debounce is dropped: the scan must not be re-run a moment later.
 *
 * The box owns the letters on screen; the screen owns the term it queries with.
 * That split is what lets a screen clear the search (or restore one) without the
 * box arguing, and it is why `value` is the committed term rather than the
 * keystrokes.
 */

/** Long enough that a typist stops between words, short enough to feel live. */
export const DEBOUNCE_MS = 220;

interface SearchBoxProps {
  /** The committed term the screen is querying with. */
  value: string;
  /** Called with a new committed term — debounced typing, or an immediate scan. */
  onChange: (term: string) => void;
  placeholder: string;
  /** What this box searches, for a screen reader: "Search stock". */
  label: string;
  testId: string;
}

export function SearchBox({ value, onChange, placeholder, label, testId }: SearchBoxProps) {
  const [draft, setDraft] = useState(value);
  // The last term we handed up. A `value` that differs from it came from the
  // screen (a reset, a deep link), so the box follows; a `value` that matches is
  // our own echo and must not fight what is being typed.
  const committed = useRef(value);

  useEffect(() => {
    if (value !== committed.current) {
      committed.current = value;
      setDraft(value);
    }
  }, [value]);

  useEffect(() => {
    if (draft === committed.current) return;
    const t = window.setTimeout(() => {
      committed.current = draft;
      onChange(draft);
    }, DEBOUNCE_MS);
    return () => window.clearTimeout(t);
  }, [draft, onChange]);

  function commitNow(term: string) {
    committed.current = term;
    setDraft(term);
    onChange(term);
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter") {
      // A scanner's Enter. Commit what is in the box now rather than a beat
      // later, and never twice — the effect above sees the term already
      // committed and stands down.
      e.preventDefault();
      commitNow(draft);
    } else if (e.key === "Escape") {
      e.preventDefault();
      commitNow("");
    }
  }

  return (
    <div className="searchbox">
      <Search size={15} />
      <input
        value={draft}
        placeholder={placeholder}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={onKeyDown}
        autoComplete="off"
        aria-label={label}
        data-testid={testId}
      />
      {draft && (
        <button
          type="button"
          className="searchbox-clear"
          onClick={() => commitNow("")}
          aria-label="Clear search"
          data-testid={`${testId}-clear`}
        >
          <X size={14} />
        </button>
      )}
    </div>
  );
}

/** How many rows a list is showing, and whether that is a searched set. */
export function searchCountLabel(count: number, noun: string, term: string): string {
  const rows = `${count} ${count === 1 ? noun : `${noun}s`}`;
  return term ? `${rows} matching “${term}”` : rows;
}

interface ListSearchBarProps extends SearchBoxProps {
  /** Singular noun for the row count — "booking", "transfer". */
  noun: string;
  /** Rows currently on screen. Hidden while the list is still loading, because
   *  a count of a list that has not arrived is a lie, not a zero. */
  count: number;
  loading: boolean;
}

/**
 * A list screen's search bar: the box, plus the count of what it found.
 *
 * The count is part of the control rather than each page's own line, because
 * "the number on screen reflects the searched set" is a property of searching —
 * a screen that forgot to update it would be the bug, and there is no way to
 * forget if the count travels with the box.
 */
export function ListSearchBar({ noun, count, loading, ...box }: ListSearchBarProps) {
  return (
    <div className="filter-bar" style={{ marginBottom: 18 }}>
      <SearchBox {...box} />
      {!loading && (
        <span className="stat-label" data-testid={`${box.testId}-count`}>
          {searchCountLabel(count, noun, box.value)}
        </span>
      )}
    </div>
  );
}
