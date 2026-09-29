import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Search } from "lucide-react";

import { api, apiErrorMessage } from "../lib/api";
import { formatINR } from "../lib/format";
import { useMobileNavExclusion } from "./MobileNavContext";
import "./GlobalSearch.css";

/**
 * The top bar's real search (#86) — type or scan, one grouped answer.
 *
 * Two input habits share one box. A person *types* a few letters and reads the
 * panel, so the query is debounced and the panel opens under the box. A wedge
 * scanner *types a whole barcode at machine speed and presses Enter*, so Enter
 * runs the search immediately and, when the answer is unambiguous (one exact
 * match, or several cohorts of the one scanned barcode — all the same screen),
 * goes straight there without the person touching the mouse.
 *
 * All scoping and gating live on the server: this component renders exactly what
 * it is sent and infers nothing about what the user may see.
 */

const MIN_QUERY = 2;
const DEBOUNCE_MS = 220;

type ResultKind = "item" | "brand" | "document";

export interface SearchResult {
  kind: ResultKind;
  title: string;
  subtitle: string;
  meta: string;
  to: string;
  exact: boolean;
  mrp_paise?: number | null;
}

interface SearchGroup {
  key: string;
  label: string;
  results: SearchResult[];
  truncated: boolean;
}

export interface SearchResponse {
  query: string;
  groups: SearchGroup[];
  total: number;
  notes: string[];
}

/**
 * The wedge-scan rule: where should Enter take this answer, if anywhere?
 *
 * A scan is unambiguous when every exact match opens the *same* screen — one
 * voucher number, or the several cohorts of one scanned barcode, which all
 * point at that barcode's stock. Anything else (two different documents, an
 * exact match beside partial ones, nothing exact at all) is a choice only the
 * person can make, so the panel stays open and Enter goes nowhere.
 */
export function scanDestination(body: SearchResponse | null): SearchResult | null {
  const exact = (body?.groups ?? []).flatMap((g) => g.results).filter((r) => r.exact);
  if (exact.length === 0) return null;
  return new Set(exact.map((r) => r.to)).size === 1 ? exact[0] : null;
}

export function GlobalSearch() {
  const navigate = useNavigate();
  const inputRef = useRef<HTMLInputElement>(null);
  const [q, setQ] = useState("");
  const [data, setData] = useState<SearchResponse | null>(null);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [highlight, setHighlight] = useState(-1);
  // Answers can land out of order; only the newest query may paint.
  const seq = useRef(0);
  useMobileNavExclusion(open, setOpen);

  const flat = useMemo(() => data?.groups.flatMap((g) => g.results) ?? [], [data]);

  const run = useCallback(async (query: string): Promise<SearchResponse | null> => {
    const mine = ++seq.current;
    setLoading(true);
    try {
      const { data: body } = await api.get<SearchResponse>(
        `/search?q=${encodeURIComponent(query)}`,
      );
      if (mine !== seq.current) return null;
      setError("");
      setData(body);
      setOpen(true);
      setHighlight(-1);
      return body;
    } catch (e) {
      if (mine === seq.current) {
        setError(apiErrorMessage(e));
        setData(null);
        setOpen(true);
      }
      return null;
    } finally {
      if (mine === seq.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    const query = q.trim();
    if (query.length < MIN_QUERY) {
      seq.current++; // a shrinking query must not be overwritten by its own tail
      setData(null);
      setError("");
      setLoading(false);
      return;
    }
    const t = window.setTimeout(() => void run(query), DEBOUNCE_MS);
    return () => window.clearTimeout(t);
  }, [q, run]);

  // Ctrl/Cmd-K puts the caret in the box, so a scan needs no mouse first.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        inputRef.current?.focus();
        inputRef.current?.select();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  function close() {
    setOpen(false);
    setHighlight(-1);
  }

  function openResult(r: SearchResult) {
    // Clear on open: the next scan starts from an empty box, not a stale one.
    setQ("");
    setData(null);
    close();
    navigate(r.to);
  }

  async function onEnter() {
    if (highlight >= 0 && flat[highlight]) {
      openResult(flat[highlight]);
      return;
    }
    const query = q.trim();
    if (query.length < MIN_QUERY) return;
    const target = scanDestination(await run(query));
    if (target) openResult(target);
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter") {
      e.preventDefault();
      void onEnter();
    } else if (e.key === "Escape") {
      if (open) close();
      else setQ("");
    } else if (e.key === "ArrowDown" && flat.length) {
      e.preventDefault();
      setOpen(true);
      setHighlight((h) => (h + 1) % flat.length);
    } else if (e.key === "ArrowUp" && flat.length) {
      e.preventDefault();
      setHighlight((h) => (h <= 0 ? flat.length - 1 : h - 1));
    }
  }

  const notes = data?.notes ?? [];

  return (
    <div className="search-wrap">
      <div className="topbar-search">
        <Search size={15} />
        <input
          ref={inputRef}
          value={q}
          placeholder="Search or scan — item, barcode, brand, voucher no."
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={onKeyDown}
          onFocus={() => data && setOpen(true)}
          autoComplete="off"
          aria-label="Global search"
          data-testid="global-search"
        />
      </div>
      {open && (
        <>
          <div className="dropdown-backdrop" onClick={close} />
          <div className="dropdown search-panel" data-testid="global-search-panel">
            {error && <div className="search-note search-note-bad">{error}</div>}
            {loading && !data && <div className="search-note">Searching…</div>}
            {data?.groups.map((g) => (
              <div key={g.key} className="search-group">
                <div className="dropdown-head">{g.label}</div>
                {g.results.map((r) => {
                  const i = flat.indexOf(r);
                  return (
                    <button
                      key={`${r.kind}-${r.to}-${r.subtitle}`}
                      className={`dropdown-item search-hit ${i === highlight ? "active" : ""}`}
                      onClick={() => openResult(r)}
                      onMouseEnter={() => setHighlight(i)}
                      data-testid={`search-hit-${r.kind}`}
                    >
                      <span className="search-hit-main">
                        <span className="search-hit-title">{r.title}</span>
                        <span className="search-hit-sub">{r.subtitle}</span>
                      </span>
                      <span className="search-hit-side">
                        {typeof r.mrp_paise === "number" && (
                          <span className="search-hit-mrp tabular">{formatINR(r.mrp_paise)}</span>
                        )}
                        <span className="search-hit-meta">{r.meta}</span>
                      </span>
                    </button>
                  );
                })}
                {g.truncated && (
                  <div className="search-note">More matches — type a bit more to narrow it.</div>
                )}
              </div>
            ))}
            {notes.map((n) => (
              <div className="search-note" key={n}>
                {n}
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
