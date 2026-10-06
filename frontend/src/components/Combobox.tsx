import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, X } from "lucide-react";

import "./Combobox.css";

const MAX_VISIBLE = 60;
/** Scrolls this soon after opening come from focusing the input, not from the person. */
const GRACE_MS = 400;

/**
 * Select-only searchable combobox: the Excel data-validation dropdown, not a text
 * input. Typing filters the allowed list; only a listed value (or blank, via the
 * clear button) can ever be committed — free text never enters the field.
 */
export function Combobox({
  value,
  options,
  onChange,
  placeholder = "Select…",
  allowClear = true,
  size = "field",
  testId,
  suggested,
}: {
  value: string;
  options: string[];
  onChange: (v: string) => void;
  placeholder?: string;
  allowClear?: boolean;
  size?: "field" | "cell";
  testId?: string;
  // Master values to float to the top of the list (deterministic pre-fill hints);
  // marked with a ★. Never restricts the list — every option stays selectable.
  suggested?: string[];
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const openedAt = useRef(0);
  const [pos, setPos] = useState<{ top: number; left: number; width: number } | null>(null);

  const suggestedSet = useMemo(() => new Set(suggested ?? []), [suggested]);
  const filtered = useMemo(() => {
    const q = query.trim().toUpperCase();
    const hits = q ? options.filter((o) => o.toUpperCase().includes(q)) : options;
    if (suggestedSet.size) {
      // stable partition: suggested matches first (order preserved), then the rest
      const top = hits.filter((o) => suggestedSet.has(o));
      const rest = hits.filter((o) => !suggestedSet.has(o));
      return [...top, ...rest].slice(0, MAX_VISIBLE);
    }
    return hits.slice(0, MAX_VISIBLE);
  }, [options, query, suggestedSet]);
  const overflow = useMemo(() => {
    const q = query.trim().toUpperCase();
    const n = q ? options.filter((o) => o.toUpperCase().includes(q)).length : options.length;
    return Math.max(0, n - MAX_VISIBLE);
  }, [options, query]);

  function anchor() {
    const rect = rootRef.current?.getBoundingClientRect();
    if (rect) setPos({ top: rect.bottom + 2, left: rect.left, width: Math.max(rect.width, 190) });
  }

  function openList() {
    anchor();
    openedAt.current = Date.now();
    setQuery("");
    setActive(0);
    setOpen(true);
  }

  function close() {
    setOpen(false);
    setQuery("");
  }

  function commit(v: string) {
    onChange(v);
    close();
  }

  // The list is position:fixed (so a scrolling table never clips it) — any outside
  // scroll/resize invalidates the anchor, so just close.
  useEffect(() => {
    if (!open) return;
    const onScroll = (e: Event) => {
      if (listRef.current && e.target instanceof Node && listRef.current.contains(e.target)) return;
      // Focusing the input can make the browser scroll a grid to reveal it. That is
      // not the person moving away: keep the list open and anchor it again.
      if (Date.now() - openedAt.current < GRACE_MS) {
        anchor();
        return;
      }
      close();
    };
    const onDown = (e: MouseEvent) => {
      if (rootRef.current?.contains(e.target as Node)) return;
      if (listRef.current?.contains(e.target as Node)) return;
      close();
    };
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", close);
    window.addEventListener("mousedown", onDown);
    return () => {
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", close);
      window.removeEventListener("mousedown", onDown);
    };
  }, [open]);

  // Keep the active option in view by moving the list's own scroll only.
  // `scrollIntoView` would also scroll the page or grid behind it, and that outside
  // scroll closes the list while the person is still typing.
  useEffect(() => {
    if (!open) return;
    const list = listRef.current;
    const option = list?.querySelector<HTMLElement>(`[data-idx="${active}"]`);
    if (!list || !option) return;
    if (option.offsetTop < list.scrollTop) list.scrollTop = option.offsetTop;
    else if (option.offsetTop + option.offsetHeight > list.scrollTop + list.clientHeight)
      list.scrollTop = option.offsetTop + option.offsetHeight - list.clientHeight;
  }, [active, open]);

  function onKeyDown(e: React.KeyboardEvent) {
    if (!open && (e.key === "ArrowDown" || e.key === "Enter")) {
      e.preventDefault();
      openList();
      return;
    }
    if (!open) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((a) => Math.min(a + 1, filtered.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((a) => Math.max(a - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (filtered[active] !== undefined) commit(filtered[active]);
    } else if (e.key === "Escape") {
      e.preventDefault();
      close();
    } else if (e.key === "Tab") {
      close();
    }
  }

  return (
    <div className={`cbx cbx-${size}`} ref={rootRef}>
      <div
        className={`cbx-control ${open ? "open" : ""} ${value ? "" : "empty"}`}
        onClick={() => (open ? close() : (openList(), inputRef.current?.focus()))}
        data-testid={testId}
      >
        {open ? (
          <input
            ref={inputRef}
            className="cbx-input"
            value={query}
            placeholder={value || placeholder}
            onChange={(e) => {
              setQuery(e.target.value);
              setActive(0);
            }}
            onKeyDown={onKeyDown}
            onClick={(e) => e.stopPropagation()}
            autoFocus
            data-testid={testId ? `${testId}-input` : undefined}
          />
        ) : (
          <button
            type="button"
            className="cbx-value"
            onKeyDown={onKeyDown}
            data-testid={testId ? `${testId}-value` : undefined}
          >
            {value || <span className="cbx-placeholder">{placeholder}</span>}
          </button>
        )}
        {allowClear && value && !open && (
          <span
            className="cbx-clear"
            title="Clear"
            onClick={(e) => {
              e.stopPropagation();
              onChange("");
            }}
            data-testid={testId ? `${testId}-clear` : undefined}
          >
            <X size={12} />
          </span>
        )}
        <ChevronDown size={13} className="cbx-caret" />
      </div>
      {open && pos && (
        <div
          className="cbx-list"
          ref={listRef}
          style={{ top: pos.top, left: pos.left, minWidth: pos.width }}
          data-testid={testId ? `${testId}-list` : undefined}
        >
          {filtered.length === 0 && <div className="cbx-empty">No match in the Master Sheet</div>}
          {filtered.map((o, i) => (
            <div
              key={o}
              data-idx={i}
              className={`cbx-option ${i === active ? "active" : ""} ${o === value ? "selected" : ""} ${suggestedSet.has(o) ? "suggested" : ""}`}
              onMouseEnter={() => setActive(i)}
              onMouseDown={(e) => {
                e.preventDefault();
                commit(o);
              }}
            >
              {suggestedSet.has(o) ? `★ ${o}` : o}
            </div>
          ))}
          {overflow > 0 && <div className="cbx-more">+{overflow} more — type to narrow</div>}
        </div>
      )}
    </div>
  );
}
