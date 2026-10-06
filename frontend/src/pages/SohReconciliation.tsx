import { useEffect, useState } from "react";

import { api, apiErrorMessage, goodsMeta } from "../lib/api";
import { formatPaiseString } from "../lib/format";
import { Feedback, Field, useStepUp } from "../lib/goodsScreen";

interface CountSource {
  id: string;
  revision: number;
  allowed_actions: string[];
  data: {
    requires_inventory_reconciliation: boolean;
    stock_reconciliation_id: string | null;
  };
}
interface Difference {
  sku_id: string;
  item_name: string;
  book_qty: number;
  observed_qty: number;
  delta: number;
  value_removed_paise: string;
}
interface Count {
  id: string;
  revision: number;
  state: string;
  content_hash: string;
  allowed_actions: string[];
  data: {
    cutoff_at: string;
    frozen_at: string;
    number: string | null;
    freeze_active: boolean;
    approval_request_id: string | null;
    journal_batch_id: string | null;
    pending_owned_differences: boolean;
    field_access: { readable_fields: string[]; writable_fields: string[] };
    review: {
      differences: Difference[];
      problems: { code: string; owner: string; barcode?: string; sku_id?: string }[];
      pause: { paused_at: string; fy: string; next_seq: number };
    } | null;
    history: { items: { id: string; kind: string; recorded_at: string }[] };
  };
}

export function SohReconciliationPanel({
  source,
  onChanged,
}: {
  source: CountSource;
  onChanged: () => Promise<unknown>;
}) {
  const [count, setCount] = useState<Count | null>(null);
  const [reasonCode, setReasonCode] = useState("");
  const [cancelReason, setCancelReason] = useState("");
  const [fullExport, setFullExport] = useState(false);
  const [physical, setPhysical] = useState(false);
  const [omissions, setOmissions] = useState(false);
  const [reviewed, setReviewed] = useState(false);
  const [pageIndex, setPageIndex] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const stepUp = useStepUp();
  const base = "/goods-v1/outbound/soh-reconciliations";

  useEffect(() => {
    let active = true;
    setCount(null);
    setReviewed(false);
    setPageIndex(0);
    if (source.data.stock_reconciliation_id) {
      api
        .get<Count>(`${base}/${source.data.stock_reconciliation_id}`)
        .then((r) => {
          if (active) setCount(r.data);
        })
        .catch((e: unknown) => {
          if (active) setError(apiErrorMessage(e));
        });
    }
    return () => {
      active = false;
    };
  }, [source.id, source.data.stock_reconciliation_id]);

  async function refresh(id: string) {
    const response = await api.get<Count>(`${base}/${id}`);
    setCount(response.data);
    setReviewed(false);
    await onChanged();
  }
  async function run(task: () => Promise<void>) {
    setBusy(true);
    setError("");
    setOk("");
    try {
      await task();
    } catch (e: unknown) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  if (!source.data.requires_inventory_reconciliation) return null;
  return (
    <section className="panel">
      {stepUp.dialog}
      <h3>Update this store from a later SOH export</h3>
      <p>
        Pause and reconcile every till online first. Export a fresh full-store SOH with its exact
        cutoff, record the physical count and obtain independent source approval. This count
        compares that evidence with frozen inventory. It preserves previous sales and opening
        history.
      </p>
      <Feedback error={error} ok={ok} />
      {!count && source.allowed_actions.includes("reconcile") && (
        <>
          <Field id="soh-count-reason" label="Approved count-correction reason code">
            <input
              id="soh-count-reason"
              className="input"
              value={reasonCode}
              onChange={(e) => setReasonCode(e.target.value)}
            />
          </Field>
          <label>
            <input
              type="checkbox"
              checked={fullExport}
              onChange={(e) => setFullExport(e.target.checked)}
            />{" "}
            This is the complete fresh export for the whole store after every till paused.
          </label>
          <br />
          <label>
            <input
              type="checkbox"
              checked={physical}
              onChange={(e) => setPhysical(e.target.checked)}
            />{" "}
            I physically counted the whole store, including empty locations, against this exact
            source.
          </label>
          <br />
          <label>
            <input
              type="checkbox"
              checked={omissions}
              onChange={(e) => setOmissions(e.target.checked)}
            />{" "}
            Goods absent from this complete source are physically absent and must be counted as
            zero.
          </label>
          <br />
          <button
            className="btn btn-cta"
            disabled={busy || !reasonCode.trim() || !fullExport || !physical || !omissions}
            onClick={() =>
              run(async () => {
                const response = await api.post<Count>(base, {
                  source_import_id: source.id,
                  reason_code: reasonCode,
                  full_store_export: fullExport,
                  whole_store_physically_counted: physical,
                  omissions_are_zero: omissions,
                  ...goodsMeta(source.revision),
                });
                setCount(response.data);
                await onChanged();
                setOk("This store is frozen for its exact reviewed inventory difference.");
              })
            }
          >
            Freeze this store and prepare the reviewed difference
          </button>
        </>
      )}
      {!count && !source.allowed_actions.includes("reconcile") && (
        <p>
          The source remains inactive until its mapping, physical evidence and independent review
          are complete. New source uploads leave the existing store inventory unchanged.
        </p>
      )}
      {count && (
        <>
          <p>
            {count.state} · {count.data.number ?? "Count awaiting approval"} · cutoff{" "}
            {count.data.cutoff_at}
          </p>
          {count.data.freeze_active && (
            <p>
              Selling and stock movements remain frozen until this count closes or is cancelled. The
              till remains paused afterward until it is explicitly resumed.
            </p>
          )}
          {count.data.pending_owned_differences && (
            <p>
              Owned differences remain unresolved. New stock, unknown identities, ambiguous origins
              and encumbered shortages cannot be applied automatically. Cancel this count to release
              the freeze while C08 resolves them.
            </p>
          )}
          {count.data.review ? (
            <>
              <p>
                Independent review: {count.data.review.differences.length} inventory lines. The till
                paused at {count.data.review.pause.paused_at}; next number{" "}
                {count.data.review.pause.fy}/{count.data.review.pause.next_seq}.
              </p>
              <div
                className="table-wrap"
                role="region"
                aria-label="SOH inventory differences"
                tabIndex={0}
              >
                <table className="table">
                  <thead>
                    <tr>
                      <th>Stable item</th>
                      <th>Frozen book</th>
                      <th>Counted</th>
                      <th>Difference</th>
                      <th>Recorded value removed</th>
                    </tr>
                  </thead>
                  <tbody>
                    {count.data.review.differences
                      .slice(pageIndex * 100, (pageIndex + 1) * 100)
                      .map((line) => (
                        <tr key={line.sku_id}>
                          <td>
                            {line.item_name}
                            <br />
                            <small>{line.sku_id}</small>
                          </td>
                          <td>{line.book_qty}</td>
                          <td>{line.observed_qty}</td>
                          <td>{line.delta}</td>
                          <td>{formatPaiseString(line.value_removed_paise)}</td>
                        </tr>
                      ))}
                  </tbody>
                </table>
              </div>
              <button
                className="btn btn-sm"
                disabled={pageIndex === 0}
                onClick={() => setPageIndex((n) => n - 1)}
              >
                Previous differences
              </button>{" "}
              <button
                className="btn btn-sm"
                disabled={(pageIndex + 1) * 100 >= count.data.review.differences.length}
                onClick={() => setPageIndex((n) => n + 1)}
              >
                Next differences
              </button>
              {count.data.review.problems.map((problem, index) => (
                <p key={index}>
                  {problem.code} · {problem.barcode ?? problem.sku_id ?? "Custody evidence"} · owner{" "}
                  {problem.owner}
                </p>
              ))}
            </>
          ) : (
            <p>
              The frozen book and recorded values are available to the independently authorised
              reviewer.
            </p>
          )}
          {count.allowed_actions.includes("submit") && (
            <button
              className="btn btn-cta"
              disabled={busy}
              onClick={() =>
                run(async () => {
                  await api.post(`${base}/${count.id}/submit`, {
                    reviewed_hash: count.content_hash,
                    ...goodsMeta(count.revision),
                  });
                  await refresh(count.id);
                })
              }
            >
              Submit the exact frozen count for independent Owner review
            </button>
          )}
          {count.allowed_actions.includes("approve") && count.data.approval_request_id && (
            <>
              <label>
                <input
                  type="checkbox"
                  checked={reviewed}
                  onChange={(e) => setReviewed(e.target.checked)}
                />{" "}
                I independently reviewed this full source, physical declarations, all differences
                and original valuation.
              </label>
              <br />
              <button
                className="btn btn-cta"
                disabled={busy || !reviewed}
                onClick={() =>
                  run(async () => {
                    await stepUp.guarded(() =>
                      api.post(`/goods-v1/approvals/${count.data.approval_request_id}/decide`, {
                        decision: "approve",
                        reviewed_hash: count.content_hash,
                        ...goodsMeta(count.revision),
                      }),
                    );
                    await refresh(count.id);
                    setOk(
                      "The reviewed difference closed. Previous business history is preserved.",
                    );
                  })
                }
              >
                Approve this exact inventory difference
              </button>
            </>
          )}
          {count.allowed_actions.includes("cancel") && (
            <>
              <Field id="soh-count-cancel" label="Why this count is cancelled">
                <input
                  id="soh-count-cancel"
                  className="input"
                  value={cancelReason}
                  onChange={(e) => setCancelReason(e.target.value)}
                />
              </Field>
              <button
                className="btn btn-sm"
                disabled={busy || !cancelReason.trim()}
                onClick={() =>
                  run(async () => {
                    await api.post(`${base}/${count.id}/cancel`, {
                      reason: cancelReason,
                      ...goodsMeta(count.revision),
                    });
                    await refresh(count.id);
                    setOk("Count evidence retained; its stock freeze is released.");
                  })
                }
              >
                Cancel count and retain its evidence
              </button>
            </>
          )}
          <details>
            <summary>Count history</summary>
            {count.data.history.items.map((event) => (
              <p key={event.id}>
                {event.kind} · {event.recorded_at}
              </p>
            ))}
          </details>
        </>
      )}
    </section>
  );
}
