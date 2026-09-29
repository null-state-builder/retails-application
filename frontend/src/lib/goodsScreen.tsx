// Shared plumbing for a goods-v1 masters/admin screen: the `ResourceDTO<T>` /
// `Page<T>` wire shapes (design §6.1), the read-a-resource hooks, the
// step-up password dialog, and the small "not found" / feedback bits every
// such screen needs. First written for Organisation (ticket 02); pulled out
// here once a second screen (People and access, ticket 03) needed the same
// fetch/error/step-up shape rather than a second copy of it.
import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { X } from "lucide-react";

import { api, apiErrorCode, apiErrorMessage, authApi, type ApiRead } from "./api";
import type { paths } from "./api-schema";
import { SearchBox } from "../components/SearchBox";

export interface ResourceDTO<T> {
  id: string;
  revision: number;
  /** The hash of exactly the reviewable content — what a submission quotes back
   *  as `reviewed_hash` so an approver can never freeze something else. */
  content_hash: string;
  state: string;
  /** The official number, once the document has one. A draft has none, and a
   *  record that is never numbered (an arrival, a count session) has none ever. */
  number?: string | null;
  /** The official version this read answered from, where the record has versions. */
  version?: number | null;
  data: T;
  allowed_actions: string[];
  context?: Record<string, unknown>;
}

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}

// The `org-*` test ids below predate this file's extraction (ticket 02,
// Organisation-only at the time) and now render on every screen that shares
// this module, People and access included. Left as-is rather than renamed:
// organisation.spec.ts already asserts on them, and a shared "goods admin
// screen" prefix is a defensible reading of "org", not only "Organisation".
export function Feedback({ error, ok }: { error: string; ok: string }) {
  return (
    <>
      {error && (
        <div className="warn-note" data-testid="org-error">
          {error}
        </div>
      )}
      {ok && (
        <div className="ok-note" data-testid="org-ok">
          {ok}
        </div>
      )}
    </>
  );
}

export function Denied({ what }: { what: string }) {
  return (
    <div className="card section-card" data-testid="org-denied">
      <p className="eyebrow">Not found</p>
      <h3 className="h3">There is no {what} here for you</h3>
      <p className="lead">
        Either this does not exist, or it is outside what you may see (ADR-0003).
      </p>
    </div>
  );
}

/** A goods-v1 command needs a same-session password confirmation no older
 *  than five minutes (design §4.2). On `STEP_UP_REQUIRED`, ask once and
 *  retry the exact call that was refused. */
export function useStepUp() {
  const [pending, setPending] = useState<{ retry: () => void } | null>(null);
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function guarded<T>(fn: () => Promise<T>): Promise<T> {
    try {
      return await fn();
    } catch (e) {
      if (apiErrorCode(e) === "STEP_UP_REQUIRED") {
        return new Promise<T>((resolve, reject) => {
          setError("");
          setPassword("");
          setPending({
            retry: () => {
              fn().then(resolve, reject);
            },
          });
        });
      }
      throw e;
    }
  }

  async function confirm() {
    if (!pending) return;
    setBusy(true);
    setError("");
    try {
      await authApi.stepUp(password);
      const retry = pending.retry;
      setPending(null);
      setPassword("");
      retry();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  const dialog = pending ? (
    <div className="card section-card" data-testid="org-stepup">
      <h3 className="h3">Confirm it's you</h3>
      <p className="lead">
        This change needs your password again — it has been more than a moment since you last
        confirmed it.
      </p>
      <Feedback error={error} ok="" />
      <div className="form-grid">
        <input
          className="input"
          type="password"
          placeholder="Password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          data-testid="org-stepup-password"
        />
        <button
          className="btn btn-cta"
          onClick={confirm}
          disabled={busy || !password}
          data-testid="org-stepup-confirm"
        >
          Confirm
        </button>
        <button
          className="btn btn-sm"
          onClick={() => setPending(null)}
          data-testid="org-stepup-cancel"
        >
          <X size={14} /> Cancel
        </button>
      </div>
    </div>
  ) : null;

  return { guarded, dialog };
}

// A read endpoint answers 403/404 the same way for "you may not see this" —
// distinguished here only so the screen can say something honest without
// pretending it knows which. Anything else is a real failure, not an answer.
//
// `FIELD_DENIED` (a value basis refused for want of the cost grant — design
// §6.1's basis rule) is a narrower thing than "you may not see this at all":
// the same read at `basis=quantity` is still open. Kept apart from `denied` so
// a caller that cares (a stock read) can say so precisely instead of folding it
// into the generic not-found card.
export function readFailure(e: unknown): {
  denied: boolean;
  deniedField: boolean;
  failure: string;
} {
  if (apiErrorCode(e) === "FIELD_DENIED") {
    return { denied: false, deniedField: true, failure: "" };
  }
  const status = (e as { response?: { status?: number } })?.response?.status;
  if (status === 403 || status === 404) {
    return { denied: true, deniedField: false, failure: "" };
  }
  return { denied: false, deniedField: false, failure: apiErrorMessage(e) };
}

// `useResourceList` and `useResourceDoc` are this one fetch/error shape
// twice (list vs single document); this is the one place that spells it.
export function useGoodsFetch<R, T>(url: string | null, extract: (r: R) => T, empty: T) {
  const [value, setValue] = useState<T>(empty);
  const [loading, setLoading] = useState(true);
  const [denied, setDenied] = useState(false);
  const [deniedField, setDeniedField] = useState(false);
  const [failure, setFailure] = useState("");
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (!url) {
      setValue(empty);
      setLoading(false);
      return;
    }
    let live = true;
    setLoading(true);
    setDenied(false);
    setDeniedField(false);
    setFailure("");
    api
      .get<R>(url)
      .then((r) => live && setValue(extract(r.data)))
      .catch((e) => {
        if (!live) return;
        const outcome = readFailure(e);
        setDenied(outcome.denied);
        setDeniedField(outcome.deniedField);
        setFailure(outcome.failure);
      })
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [url, tick]);

  return { value, loading, denied, deniedField, failure, reload: () => setTick((t) => t + 1) };
}

export function useResourceList<T>(url: string | null) {
  const { value, ...rest } = useGoodsFetch<Page<ResourceDTO<T>>, ResourceDTO<T>[]>(
    url,
    (r) => r.items ?? [],
    [],
  );
  return { items: value, ...rest };
}

/** How many pages `useAllPages` will follow before it stops and says so. At the
 *  endpoints' 100-row maximum that is 5,000 rows — far past any list a screen
 *  puts in front of a person, and a hard stop if a server ever keeps handing
 *  back a cursor. */
const MAX_PAGES = 50;

/** Every page of a cursor-paged list, not only the first.
 *
 *  `useGoodsFetch` reads one page. That is right for a picker with a "Show
 *  more" button and wrong for a screen that has to act on the whole list: an
 *  approval sitting on page two is a decision the operator never sees and so
 *  never makes. This follows `next_cursor` to the end and answers the list
 *  whole, or says it could not — never a silent prefix of it. A caller must
 *  therefore show `failure` and `denied`: a screen that renders only `items`
 *  turns "the list could not be read" into "there is nothing to decide",
 *  which is the very thing this hook exists to prevent. */
export function useAllPages<T>(url: string | null) {
  const [items, setItems] = useState<T[]>([]);
  const [loading, setLoading] = useState(true);
  const [denied, setDenied] = useState(false);
  const [failure, setFailure] = useState("");
  const [tick, setTick] = useState(0);
  const generation = useRef(0);

  useEffect(() => {
    const mine = ++generation.current;
    if (!url) {
      setItems([]);
      setLoading(false);
      return;
    }
    setLoading(true);
    setDenied(false);
    setFailure("");
    void (async () => {
      const all: T[] = [];
      let cursor: string | null = null;
      for (let read = 0; read < MAX_PAGES; read += 1) {
        try {
          // The caller's url may already carry a query (`?limit=100`); axios
          // joins these params onto whatever is there.
          const params: Record<string, string> = cursor ? { cursor } : {};
          const { data } = await api.get<Page<T>>(url, { params });
          if (mine !== generation.current) return;
          all.push(...(data.items ?? []));
          cursor = data.next_cursor;
        } catch (e) {
          if (mine !== generation.current) return;
          const outcome = readFailure(e);
          setDenied(outcome.denied);
          // A refused field basis is not "you may not see this at all", but it
          // is not nothing either: without a sentence it reads as an empty list.
          setFailure(
            outcome.deniedField
              ? "Part of this list needs a permission you do not have."
              : outcome.failure,
          );
          setItems([]);
          setLoading(false);
          return;
        }
        if (!cursor) break;
      }
      if (mine !== generation.current) return;
      if (cursor) {
        // Still a cursor in hand at the cap. Showing the prefix would be the
        // silent truncation this hook promises not to do.
        setItems([]);
        setFailure(`This list is longer than ${MAX_PAGES} pages and was not read.`);
      } else {
        setItems(all);
      }
      setLoading(false);
    })();
    return () => {
      generation.current += 1;
    };
  }, [url, tick]);

  return { items, loading, denied, failure, reload: () => setTick((t) => t + 1) };
}

/** One row a searchable, paginated reference picker can offer. `data` is
 *  absent for the placeholder row a selection gets before any fetch has ever
 *  actually returned it — see `usePagedPicker`. */
export interface PickerOption<T> {
  id: string;
  label: string;
  data?: T;
}

type ListPath = {
  [P in keyof paths]: P extends `/api${infer Rest}`
    ? paths[P] extends { get: { parameters: { query?: unknown } } }
      ? Rest
      : never
    : never;
}[keyof paths];
type ListGet<P extends ListPath> = paths[`/api${P}` & keyof paths] extends { get: infer Get }
  ? Get
  : never;
/** The documented query of a list read, from the generated API client. */
export type PickerQuery<P extends ListPath> =
  ListGet<P> extends { parameters: { query?: infer Query } } ? NonNullable<Query> : never;
/** A goods list path whose GET documents both a search term and a cursor — the
 *  only kind a paged picker may be pointed at (ticket 02D). */
export type PickerPath = {
  [P in ListPath]: "q" extends keyof PickerQuery<P>
    ? "cursor" extends keyof PickerQuery<P>
      ? P
      : never
    : never;
}[ListPath];
/** The static filters a picker keeps while its person searches and pages. */
export type PickerFilters<P extends PickerPath> = Omit<PickerQuery<P>, "q" | "cursor" | "limit">;
/** One row of a picker's list, as the generated API client describes it. */
export type PickerRow<P extends PickerPath> =
  ListGet<P> extends {
    responses: { 200: { content: { "application/json": { items?: (infer Row)[] } } } };
  }
    ? ApiRead<Row> & { id: string }
    : never;

/** A master's picker label, marked when retired so a stale choice reads as one.
 *  The command refuses a retired master whatever the option said (ticket 02D). */
export function masterLabel(
  row: { state: string; data: { code?: string; name?: string } },
  withCode = false,
): string {
  const name = row.data.name ?? row.data.code ?? "";
  const label = withCode && row.data.code ? `${name} (${row.data.code})` : name;
  return row.state === "retired" ? `${label} — retired` : label;
}

/** Shown for a selection whose own row this person has not been shown and may
 *  not read — never an id fragment, and never anything they could not see. */
export const CURRENT_SELECTION = "Current selection";

/** A growing reference list (entity, registration, vendor, brand, season,
 *  booking — ticket 02D) a person searches and pages through, rather than one
 *  fixed first page hoped to be big enough. `path` is a list endpoint whose
 *  generated contract documents `q` and `cursor`; `filters` are its static
 *  filters (an entity's registrations, say), typed from the same contract.
 *  Pass `null` filters while there is nothing to scope to yet, and the picker
 *  holds empty rather than fetching. When the filters change, the search term
 *  and any selection kept from the old scope are dropped: another entity's
 *  registrations are not searched with the last entity's term.
 *
 *  Whatever `selectedId` already names stays visible as an option even once a
 *  later search or page no longer includes it — the last authorised row seen
 *  for that id is kept, not re-fetched with any wider grant. An edit screen
 *  that opens with a selection already made has usually never fetched that
 *  row itself: pass `knownLabel` when the caller already has one, or
 *  `detailPath` to read that one row through its own authorised detail read.
 *  If neither names it, or the read is refused, the option reads "Current
 *  selection". This is a display convenience only: the command that actually
 *  uses `selectedId` re-checks its scope and eligibility itself, never
 *  trusting that a stale option was still good (GSA-T05's
 *  scope-changes-are-the-command's-job rule). */
export function usePagedPicker<P extends PickerPath, T extends { id: string } = PickerRow<P>>(
  path: P,
  filters: PickerFilters<P> | null,
  selectedId: string,
  labelOf: (row: T) => string,
  options: { knownLabel?: string | undefined; detailPath?: (id: string) => string } = {},
) {
  const { knownLabel, detailPath } = options;
  const scope = filters === null ? null : `${path}?${JSON.stringify(filters)}`;
  const [query, setQuery] = useState("");
  const [items, setItems] = useState<T[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [failure, setFailure] = useState("");
  const [, setResolved] = useState(0);
  const requestGeneration = useRef(0);
  const pinned = useRef<Map<string, T>>(new Map());
  const unreadable = useRef<Set<string>>(new Set());
  const lastScope = useRef(scope);

  async function load(term: string, cursor: string | null, append: boolean) {
    if (filters === null) return;
    const generation = ++requestGeneration.current;
    if (append) setLoadingMore(true);
    else setLoading(true);
    setFailure("");
    try {
      // `PickerPath` already holds this endpoint to a documented `q` and `cursor`.
      const params: Record<string, unknown> = { ...filters };
      if (term.trim()) params.q = term.trim();
      if (cursor) params.cursor = cursor;
      const { data } = await api.get<Page<T>>(path, { params });
      if (generation !== requestGeneration.current) return;
      setItems((current) => (append ? [...current, ...data.items] : data.items));
      setNext(data.next_cursor);
    } catch (e) {
      if (generation !== requestGeneration.current) return;
      setFailure(apiErrorMessage(e));
      // A failed search must not leave the last term's rows choosable under the
      // new one; a failed "Show more" keeps what is already shown.
      if (!append) {
        setItems([]);
        setNext(null);
      }
    } finally {
      if (generation === requestGeneration.current) {
        setLoading(false);
        setLoadingMore(false);
      }
    }
  }

  useEffect(() => {
    if (lastScope.current !== scope) {
      lastScope.current = scope;
      pinned.current.clear();
      unreadable.current.clear();
      if (query) {
        // The term belonged to the old scope; this effect runs again with none.
        setQuery("");
        return;
      }
    }
    if (filters === null) {
      setItems([]);
      setNext(null);
      setLoading(false);
      return;
    }
    void load(query, null, false);
    return () => {
      requestGeneration.current += 1;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope, query]);

  useEffect(() => {
    if (!selectedId) return;
    const found = items.find((row) => row.id === selectedId);
    if (found) pinned.current.set(selectedId, found);
  }, [selectedId, items]);

  const selectedInItems = selectedId ? items.some((row) => row.id === selectedId) : false;
  const needsDetail =
    Boolean(selectedId && detailPath && !knownLabel && !selectedInItems && !loading) &&
    !pinned.current.has(selectedId) &&
    !unreadable.current.has(selectedId);
  useEffect(() => {
    if (!needsDetail || !detailPath) return;
    let live = true;
    const id = selectedId;
    api
      .get<T>(detailPath(id))
      .then(({ data }) => {
        if (live) pinned.current.set(id, data);
      })
      .catch(() => {
        if (live) unreadable.current.add(id);
      })
      .finally(() => {
        if (live) setResolved((n) => n + 1);
      });
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [needsDetail, selectedId]);

  const choices: PickerOption<T>[] = [];
  const seen = new Set<string>();
  if (selectedId && !selectedInItems) {
    const pinnedRow = pinned.current.get(selectedId);
    choices.push(
      pinnedRow
        ? { id: pinnedRow.id, label: labelOf(pinnedRow), data: pinnedRow }
        : { id: selectedId, label: knownLabel ?? CURRENT_SELECTION },
    );
    seen.add(selectedId);
  }
  for (const row of items) {
    if (seen.has(row.id)) continue;
    seen.add(row.id);
    choices.push({ id: row.id, label: labelOf(row), data: row });
  }

  return {
    query,
    setQuery,
    options: choices,
    loading,
    loadingMore,
    failure,
    hasMore: next !== null,
    loadMore: () => {
      // Never fire a page-more request while a search is still in flight —
      // it would win the same generation guard and strand the search's own
      // response as discarded, leaving the box showing a new term over old
      // results with nothing to retry it.
      if (next && !loading) void load(query, next, true);
    },
  };
}

interface PickerRender {
  query: string;
  setQuery: (q: string) => void;
  options: { id: string; label: string }[];
  loading: boolean;
  loadingMore: boolean;
  failure: string;
  hasMore: boolean;
  loadMore: () => void;
}

/** A `Field` built from `usePagedPicker`'s state: a search box narrowing the
 *  `<select>` beneath it, a "Show more" button while another page remains, and
 *  the honest empty message a searched-but-empty list needs (ticket 02D). */
export function PickerField({
  id,
  label,
  hint,
  noun,
  placeholder,
  value,
  onChange,
  picker,
  disabled,
}: {
  id: string;
  label: string;
  hint?: ReactNode;
  /** Singular noun this picker offers — "vendor", "legal entity" — for its
   *  search placeholder and empty-result message. */
  noun: string;
  placeholder: string;
  value: string;
  onChange: (id: string) => void;
  picker: PickerRender;
  /** True while this picker has nothing to scope to yet (no entity picked for
   *  its registrations, say) — the select alone shows, disabled, rather than
   *  a search box and an honest-but-misleading "none available" message. */
  disabled?: boolean;
}) {
  return (
    <Field id={id} label={label} hint={hint}>
      {!disabled && (
        <SearchBox
          value={picker.query}
          onChange={picker.setQuery}
          placeholder={`Search ${noun}s`}
          label={`Search ${noun}s`}
          testId={`${id}-q`}
        />
      )}
      <select
        id={id}
        className="select"
        aria-describedby={hint ? `${id}-hint` : undefined}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        data-testid={id}
      >
        <option value="">{placeholder}</option>
        {picker.options.map((opt) => (
          <option key={opt.id} value={opt.id}>
            {opt.label}
          </option>
        ))}
      </select>
      {picker.failure && <p className="warn-note">{picker.failure}</p>}
      {/* A failed read is not an empty one: "none available" would misreport it. */}
      {!disabled && !picker.loading && !picker.failure && picker.options.length === 0 && (
        <p className="goods-hint" data-testid={`${id}-none`}>
          {picker.query.trim() ? `No ${noun} matches that.` : `No ${noun} is available to you.`}
        </p>
      )}
      {picker.hasMore && (
        <button
          type="button"
          className="btn btn-sm"
          onClick={picker.loadMore}
          disabled={picker.loadingMore || picker.loading}
          data-testid={`${id}-more`}
        >
          {picker.loadingMore ? "Loading…" : "Show more"}
        </button>
      )}
    </Field>
  );
}

export function useResourceDoc<T>(url: string | null) {
  const { value, ...rest } = useGoodsFetch<ResourceDTO<T>, ResourceDTO<T> | null>(
    url,
    (r) => r,
    null,
  );
  return { doc: value, ...rest };
}

export function hold(session: { actions: string[] } | null, action: string): boolean {
  return Boolean(session?.actions?.includes(action));
}

/** A labelled control. Every control on a goods screen carries a visible label
 *  tied to it by `id`/`htmlFor` — the UX brief's rule, and what makes the
 *  screen usable with a keyboard and a screen reader. The caller puts the same
 *  `id` on the input it wraps; nothing here can do that for it.
 *
 *  A `hint` is announced with the control, not merely printed beside it: these
 *  hints carry the refusal that explains why a date was rejected, and a reader
 *  who cannot hear it is told the field is invalid and not why. The caller adds
 *  `aria-describedby={`${id}-hint`}` to the control it wraps. */
export function Field({
  id,
  label,
  hint,
  children,
}: {
  id: string;
  label: string;
  hint?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {children}
      {hint ? (
        <span className="goods-hint" id={`${id}-hint`}>
          {hint}
        </span>
      ) : null}
    </div>
  );
}

/** The four states every list on these screens has to be able to say out loud.
 *  Returns the row to render instead of the data, or `null` when there is data. */
export function listState(
  { loading, failure, empty }: { loading: boolean; failure: string; empty: boolean },
  emptyText: string,
): ReactNode | null {
  if (loading) return <span className="muted">Loading…</span>;
  if (failure) return <span className="warn-note">{failure}</span>;
  if (empty) return <span className="muted">{emptyText}</span>;
  return null;
}
