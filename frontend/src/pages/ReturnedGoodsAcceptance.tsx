import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, apiErrorMessage, goodsMeta } from "../lib/api";
import { Field, useAllPages, useStepUp, type ResourceDTO } from "../lib/goodsScreen";
import type { InboxItem } from "../lib/goodsReceiving";

type ReturnedLine = {
  sale_line_id: string;
  barcode: string;
  description: string;
  qty: number;
};
type Location = ResourceDTO<{
  name: string;
  kind: string;
  system: boolean;
  retired_at?: string | null;
}>;
const ORDINARY = new Set(["floor", "backstore", "bin", "zone", "fixture"]);

/** Physical acceptance uses the bill's immutable portions and the sole P09 writer. */
export function ReturnedGoodsAcceptance({ item }: { item: InboxItem }) {
  const waiting = useAllPages<ReturnedLine>(
    `/goods-v1/stockledger/returned-pieces?site_id=${item.site_id}&limit=100`,
  );
  const locations = useAllPages<Location>(
    `/goods-v1/masters/stores/${item.site_id}/locations?limit=100`,
  );
  const line = waiting.items.find((row) => row.sale_line_id === item.sale_line_id);
  const destinations = locations.items.filter(
    (row) => !row.data.system && !row.data.retired_at && ORDINARY.has(row.data.kind),
  );
  const [location, setLocation] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState("");
  const command = useRef<ReturnType<typeof goodsMeta> | null>(null);
  const stepUp = useStepUp();
  if (done)
    return (
      <div role="status" data-testid="return-accepted">
        <p>
          The returned pieces are accepted and put away. Stock search and the till now use their
          original identity and cost.
        </p>
        <Link className="btn btn-sm" to="/goods/stock">
          Check stock
        </Link>
      </div>
    );
  if (waiting.loading || locations.loading) return <p role="status">Loading returned goods…</p>;
  if (waiting.failure || locations.failure)
    return (
      <p className="warn-note" role="status">
        {waiting.failure || locations.failure}
      </p>
    );
  if (waiting.denied || locations.denied || !line)
    return <p role="status">These pieces are no longer waiting here, or you cannot accept them.</p>;
  async function accept() {
    if (!line || !location || !confirmed) return;
    setBusy(true);
    setError("");
    command.current ??= goodsMeta();
    try {
      await stepUp.guarded(() =>
        api.post("/goods-v1/stockledger/returned-pieces/accept", {
          ...command.current,
          site_id: Number(item.site_id),
          sale_line_ids: [Number(line.sale_line_id)],
          location_id: location,
        }),
      );
      setDone(true);
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="form-grid" data-testid="return-acceptance">
      <h3 className="h3">Accept customer return</h3>
      <p>
        {line.description} · {line.barcode} · {line.qty} piece(s)
      </p>
      <p className="muted">
        Check the pieces physically. They stay unavailable to Sell until accepted into this store’s
        ordinary stock. Damaged returns require Damage &amp; Quarantine review.
      </p>
      <Field id="return-putaway" label="Put away in">
        <select
          id="return-putaway"
          className="select"
          value={location}
          disabled={busy}
          onChange={(e) => {
            setLocation(e.target.value);
            command.current = null;
          }}
          data-testid="return-putaway"
        >
          <option value="">Choose a location</option>
          {destinations.map((row) => (
            <option key={row.id} value={row.id}>
              {row.data.name}
            </option>
          ))}
        </select>
      </Field>
      <label>
        <input
          type="checkbox"
          checked={confirmed}
          disabled={busy}
          onChange={(e) => setConfirmed(e.target.checked)}
          data-testid="return-physical-check"
        />{" "}
        I checked these returned pieces and put them in the selected location.
      </label>
      {error && (
        <p className="warn-note" role="alert">
          {error}
        </p>
      )}
      <button
        className="btn btn-cta"
        disabled={!location || !confirmed || busy}
        onClick={() => void accept()}
        data-testid="return-accept"
      >
        {busy ? "Recording…" : "Accept and put away"}
      </button>
      {stepUp.dialog}
    </div>
  );
}
