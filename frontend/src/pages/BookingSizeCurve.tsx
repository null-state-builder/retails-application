// Size curve on the new-booking form (store operations ticket 40, ST-BUY-2).
//
// For each store the form's lines go to, the form reads that store's size curves
// for the booking's brand and season. A line typed as a style total (a style and
// a quantity, no size) then offers "Sizes": pick the category, and the server
// splits the total by how that category sold by size at the store in the same
// season last year. The line becomes one line per size, which the buyer can
// change like any other. Where there is no history the form says so; offline,
// nothing is filled and the line stays as typed.
//
// The switch, the rights and the split are all the server's: a store switched
// off, or one this person cannot book for, simply offers no curve.
import { useCallback, useEffect, useRef, useState } from "react";

import "./Shared.css";

import { api, apiErrorCode, apiErrorMessage, goodsMeta } from "../lib/api";
import { isConnectionLost } from "../lib/auditLog";
import {
  CURVE_PATH,
  FILL_PATH,
  curveWords,
  type SizeCurveFill,
  type SizeCurves,
} from "../lib/sizeCurve";

export type CurveState =
  | { kind: "loading" }
  | { kind: "ready"; data: SizeCurves }
  | { kind: "none"; data: SizeCurves }
  | { kind: "hidden" }
  | { kind: "lost" }
  | { kind: "error"; message: string };

/** Refusals that mean "no curve is offered here", never shown as an error. */
const NOT_OFFERED = new Set(["ACTION_DENIED", "NOT_FOUND"]);

const LOST =
  "No connection, so the size curve cannot be read. Type the sizes yourself, or try again.";

/** Each store's size curves for one brand and season. A new brand or season
 *  reads every store again; a store added to the lines is read on its own. */
export function useSizeCurves(
  storeIds: string[],
  brandId: string,
  seasonId: string,
) {
  const [states, setStates] = useState<Record<string, CurveState>>({});
  const generation = useRef(0);
  /** The brand and season read for, and the stores read for them. */
  const known = useRef({ scope: "", stores: new Set<string>() });
  /** Each fill's command id, kept until the server answers it (by what was
   *  asked), so a retry after a dropped connection - even from a panel closed
   *  and opened again - is the same fill and never recorded twice. */
  const commands = useRef(new Map<string, string>());
  const wanted = [...new Set(storeIds.filter(Boolean))].sort().join(",");
  const scope = `${brandId}|${seasonId}`;

  const load = useCallback(
    async (stores: string[]) => {
      const mine = generation.current;
      await Promise.all(
        stores.map(async (store) => {
          const put = (state: CurveState) => {
            if (mine === generation.current)
              setStates((s) => ({ ...s, [store]: state }));
          };
          if (!navigator.onLine) {
            put({ kind: "lost" });
            return;
          }
          put({ kind: "loading" });
          try {
            const { data } = await api.get<SizeCurves>(CURVE_PATH, {
              params: {
                site_id: store,
                brand_id: brandId,
                season_id: seasonId,
              },
            });
            // Switched off at this store: nothing is offered, and nothing said.
            if (data.reason === "switched_off") put({ kind: "hidden" });
            else
              put(
                data.reason ? { kind: "none", data } : { kind: "ready", data },
              );
          } catch (reason) {
            if (isConnectionLost(reason)) put({ kind: "lost" });
            else if (NOT_OFFERED.has(apiErrorCode(reason) ?? ""))
              put({ kind: "hidden" });
            else put({ kind: "error", message: apiErrorMessage(reason) });
          }
        }),
      );
    },
    [brandId, seasonId],
  );

  useEffect(() => {
    if (known.current.scope !== scope) {
      generation.current += 1;
      known.current = { scope, stores: new Set() };
      setStates({});
    }
    if (!brandId || !seasonId) return;
    const want = new Set(wanted ? wanted.split(",") : []);
    const had = known.current.stores;
    const gone = [...had].filter((store) => !want.has(store));
    const added = [...want].filter((store) => !had.has(store));
    gone.forEach((store) => had.delete(store));
    added.forEach((store) => had.add(store));
    if (gone.length) {
      setStates((s) =>
        Object.fromEntries(
          Object.entries(s).filter(([store]) => want.has(store)),
        ),
      );
    }
    if (added.length) void load(added);
  }, [wanted, scope, brandId, seasonId, load]);

  const retry = useCallback(() => {
    const again = Object.entries(states)
      .filter(([, state]) => state.kind === "lost" || state.kind === "error")
      .map(([store]) => store);
    if (again.length) void load(again);
  }, [states, load]);

  // Back online: read again whatever could not be read.
  useEffect(() => {
    window.addEventListener("online", retry);
    return () => window.removeEventListener("online", retry);
  }, [retry]);

  return { states, retry, commands: commands.current };
}

/** One store's curve, in words, above the lines. */
export function SizeCurveNote({
  state,
  storeLabel,
  onRetry,
}: {
  state: CurveState | undefined;
  storeLabel: string;
  onRetry: () => void;
}) {
  if (!state || state.kind === "loading" || state.kind === "hidden")
    return null;
  if (state.kind === "ready") {
    return (
      <p className="hint" data-testid="gb-curve-ready">
        Size curve for {storeLabel}: from its {state.data.reference_season}{" "}
        sales ({state.data.curves.map((c) => c.category).join(", ")}). For a
        style total, leave the size blank, type the total and press{" "}
        <strong>Sizes</strong>.
      </p>
    );
  }
  if (state.kind === "none") {
    return (
      <p className="warn-note" data-testid="gb-curve-none">
        {storeLabel}: {state.data.message}
      </p>
    );
  }
  return (
    <p className="warn-note" role="status" data-testid="gb-curve-offline">
      {storeLabel}: {state.kind === "lost" ? LOST : state.message}{" "}
      <button
        type="button"
        className="btn btn-sm"
        onClick={onRetry}
        data-testid="gb-curve-retry"
      >
        Try again
      </button>
    </p>
  );
}

/** The panel under one line: pick the category, see its curve, fill the sizes. */
export function SizeCurveLineFill({
  index,
  curves,
  styleCode,
  total,
  colSpan,
  commands,
  onFilled,
  onClose,
}: {
  index: number;
  curves: SizeCurves;
  styleCode: string;
  total: number;
  colSpan: number;
  /** Command ids kept by `useSizeCurves`, outliving this panel. */
  commands: Map<string, string>;
  onFilled: (fill: SizeCurveFill) => void;
  onClose: () => void;
}) {
  const [category, setCategory] = useState(
    curves.curves.length === 1 ? curves.curves[0].category : "",
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  /** Closed before the answer came: the answer is not put in the form. */
  const open = useRef(true);
  useEffect(() => {
    open.current = true;
    return () => {
      open.current = false;
    };
  }, []);
  const curve = curves.curves.find((c) => c.category === category);

  async function fill() {
    if (!curve) return;
    setError("");
    if (!navigator.onLine) {
      setError(
        "No connection, so the sizes cannot be filled. The line is as you typed it.",
      );
      return;
    }
    const body = {
      site_id: curves.store.id,
      brand_id: curves.brand.id,
      season_id: curves.season.id,
      category: curve.category,
      style_code: styleCode,
      total,
    };
    const key = JSON.stringify(body);
    const commandId = commands.get(key) ?? crypto.randomUUID();
    commands.set(key, commandId);
    setBusy(true);
    try {
      const { data } = await api.post<SizeCurveFill>(FILL_PATH, {
        ...goodsMeta(undefined, commandId),
        ...body,
      });
      commands.delete(key);
      if (open.current) onFilled(data);
    } catch (reason) {
      if (!open.current) {
        if (!isConnectionLost(reason)) commands.delete(key);
        return;
      }
      if (isConnectionLost(reason)) {
        setError(
          "The connection dropped before the sizes came back. The line is as you typed it. Try again.",
        );
      } else {
        commands.delete(key);
        setError(apiErrorMessage(reason));
      }
    } finally {
      if (open.current) setBusy(false);
    }
  }

  return (
    <tr className="size-curve-row" data-testid={`gb-curve-panel-${index}`}>
      <td colSpan={colSpan}>
        <div
          className="toolbar"
          style={{ gap: 8, flexWrap: "wrap", alignItems: "center" }}
        >
          <label htmlFor={`gb-curve-category-${index}`}>
            Sizes for {total} of {styleCode} from {curves.reference_season},
            category
          </label>
          <select
            id={`gb-curve-category-${index}`}
            className="select"
            value={category}
            onChange={(e) => setCategory(e.target.value)}
            data-testid={`gb-curve-category-${index}`}
          >
            <option value="">Choose a category</option>
            {curves.curves.map((c) => (
              <option key={c.category} value={c.category}>
                {c.category}
              </option>
            ))}
          </select>
          <button
            type="button"
            className="btn btn-primary btn-sm"
            disabled={!curve || busy}
            onClick={() => void fill()}
            data-testid={`gb-curve-fill-${index}`}
          >
            {busy ? "Filling…" : "Fill sizes"}
          </button>
          <button
            type="button"
            className="btn btn-sm"
            onClick={onClose}
            data-testid={`gb-curve-close-${index}`}
          >
            Cancel
          </button>
        </div>
        {curve && (
          <p
            className="hint"
            style={{ marginTop: 6 }}
            data-testid={`gb-curve-shares-${index}`}
          >
            {curve.category} sold {curve.pieces} pieces: {curveWords(curve)}
          </p>
        )}
        {error && (
          <p
            className="warn-note"
            role="alert"
            data-testid={`gb-curve-error-${index}`}
          >
            {error}
          </p>
        )}
      </td>
    </tr>
  );
}
