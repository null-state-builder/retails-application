import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { PackageCheck } from "lucide-react";

import { useAuth } from "../auth/AuthContext";
import { canAccess } from "../auth/routeAccess";
import { useCachedGet } from "../lib/cachedQuery";
import { Money } from "../lib/format";
import { ListSearchBar } from "../components/SearchBox";
import "./Booking.css";
import "./Shared.css";
import { KpiStrip, type Kpi } from "../components/KpiStrip";
import { PageHeader } from "../components/PageHeader";
import { EmptyState } from "../components/States";
import { UpdatedAgo } from "../components/UpdatedAgo";
import { Note } from "../components/Note";

interface EntryT {
  id: number;
  created_at: string;
  doc_number: string;
  kind: string;
  kind_label: string;
  store_code: string;
  sku_code: string;
  design: string;
  color: string;
  size: string;
  brand: string;
  season: string;
  item: string;
  qty: number;
  amount: number;
  booking_number: string;
}

interface SummaryT {
  entries: number;
  net_qty: number;
  net_value_paise: number;
  distinct_skus: number;
  distinct_documents: number;
}

const NO_ENTRIES: EntryT[] = [];

export default function StockLedger() {
  const { user, session, featuresOn } = useAuth();
  // The empty ledger points at Movements only for someone who can open it.
  const movementsOpen =
    !!user && canAccess("/goods/movements", user, session?.display_actions ?? [], featuresOn);
  const [params] = useSearchParams();
  const docFilter = params.get("doc") || "";
  const [page, setPage] = useState(1);
  const [q, setQ] = useState("");
  const PAGE_SIZE = 50;

  useEffect(() => setPage(1), [docFilter, q]);

  // Through the shared cache: a page of the ledger seen before shows at once.
  // A failed read leaves the screen as it was (it always has).
  const entriesRead = useCachedGet<{ results: EntryT[]; count: number; next: string | null }>(
    "/stockledger/entries",
    { params: { doc_number: docFilter, q, page, page_size: PAGE_SIZE } },
  );
  const summaryRead = useCachedGet<SummaryT>("/stockledger/summary");
  const entries = entriesRead.data?.results ?? NO_ENTRIES;
  const count = entriesRead.data?.count ?? 0;
  const hasNext = Boolean(entriesRead.data?.next);
  const summary = summaryRead.data ?? null;
  const loading = entriesRead.loading || summaryRead.loading;

  // Unknown is a dash, never a nought: before the summary arrives, and when
  // this ledger holds no postings for these stores (their stock is kept in
  // the goods journal, so a net of 0 here would misstate what is on hand).
  const ledgerEmpty = summary !== null && summary.entries === 0;
  const known = summary !== null && !ledgerEmpty;
  const cards = useMemo<Kpi[]>(
    () => [
      { label: "Ledger entries", value: summary ? summary.entries : "—" },
      { label: "Net units on hand", value: summary && known ? summary.net_qty : "—" },
      {
        label: "Net stock value (₹)",
        value: summary && known ? <Money paise={summary.net_value_paise} /> : "—",
      },
      {
        label: "Distinct SKUs",
        value: summary && known ? summary.distinct_skus : "—",
      },
    ],
    [summary, known],
  );

  return (
    <div className="page-pad">
      <PageHeader
        lead="Every movement, in the order it happened. The stock ledger is append-only — a correction is a reversing entry, never an edit."
        actions={
          <>
            <UpdatedAgo
              updatedAt={entriesRead.updatedAt}
              fetching={entriesRead.fetching || summaryRead.fetching}
            />
            <Link className="btn" to="/stock" data-testid="stock-on-hand-link">
              <PackageCheck size={16} /> Stock on Hand
            </Link>
          </>
        }
      />

      <div data-testid="stock-summary">
        <KpiStrip items={cards} label="Ledger summary" />
      </div>

      {docFilter && (
        <Note testId="stock-doc-filter">
          Showing entries for voucher <b className="mono">{docFilter}</b>.
        </Note>
      )}

      <ListSearchBar
        value={q}
        onChange={setQ}
        placeholder="Search movements — doc number, style, barcode"
        label="Search movement history"
        testId="stock-search"
        noun="movement"
        count={count}
        loading={loading}
      />

      {loading ? (
        <p className="lead">Loading…</p>
      ) : entries.length === 0 ? (
        <EmptyState testId="stock-empty">
          {q ? (
            `No movement matches “${q}”.`
          ) : (
            <>
              This ledger has no postings for your stores. Goods received through Receive Goods are
              recorded in the goods journal instead
              {movementsOpen ? (
                <>
                  {" "}
                  — <Link to="/goods/movements">see Movements</Link>.
                </>
              ) : (
                "."
              )}
            </>
          )}
        </EmptyState>
      ) : (
        <div className="table-wrap kdps-scroll" style={{ marginTop: 16 }}>
          <table className="data kdps-table" data-testid="stock-entries-table">
            <thead>
              <tr>
                <th>Voucher</th>
                <th>Type</th>
                <th>Store</th>
                <th>Barcode (SKU)</th>
                <th>Brand</th>
                <th>Design</th>
                <th>Colour</th>
                <th>Size</th>
                <th>Item</th>
                <th>Season</th>
                <th className="num">Qty</th>
                <th className="num">Value ₹</th>
                <th>Booking</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((e) => (
                <tr key={e.id} data-testid={`stock-entry-${e.id}`}>
                  <td className="mono">{e.doc_number}</td>
                  <td>{e.kind_label}</td>
                  <td>{e.store_code}</td>
                  <td className="mono">{e.sku_code}</td>
                  <td>{e.brand}</td>
                  <td>{e.design}</td>
                  <td>{e.color}</td>
                  <td>{e.size}</td>
                  <td>{e.item}</td>
                  <td>{e.season}</td>
                  <td
                    className="num"
                    style={{ color: e.qty < 0 ? "var(--red)" : "inherit", fontWeight: 600 }}
                  >
                    {e.qty}
                  </td>
                  <td className="num mono">
                    <Money paise={e.amount} />
                  </td>
                  <td>{e.booking_number || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="pager" data-testid="stock-pager">
            <span className="pager-info">
              Showing {(page - 1) * PAGE_SIZE + 1}–{(page - 1) * PAGE_SIZE + entries.length} of{" "}
              {count}
            </span>
            <div className="spacer" />
            <button
              className="btn btn-sm"
              disabled={page <= 1}
              onClick={() => setPage((p) => p - 1)}
              data-testid="stock-prev"
            >
              Prev
            </button>
            <span className="pager-page">Page {page}</span>
            <button
              className="btn btn-sm"
              disabled={!hasNext}
              onClick={() => setPage((p) => p + 1)}
              data-testid="stock-next"
            >
              Next
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
