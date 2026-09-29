import { useCallback, useEffect, useRef, useState } from "react";
import { Ruler } from "lucide-react";

import { api, apiErrorMessage } from "../../../lib/api";
import { isConnectionLost } from "../../../lib/auditLog";
import { formatDateTime } from "../../../lib/format";
import {
  SAVED_SIZE_DROPPED,
  SAVED_SIZES_API,
  SAVED_SIZES_OFFLINE,
  correctionBody,
  correctionProblem,
  savedSizeNote,
} from "../../../lib/savedSizes";
import type { CorrectionDraft, SavedSize, SavedSizes } from "../../../lib/savedSizes";
import { consentMobile } from "../../../till/consent";
import { newUuid } from "../../../till/uuid";

/**
 * The customer's saved sizes at the counter (store operations ticket 18, ST-CUS-2).
 *
 * Shown once a 10-digit mobile is on the bill, and only where the store has
 * saved sizes switched on. Head office learns the last size bought per brand
 * and category from the customer's bills; this card reads it and shows it.
 * Staff can correct a size with the customer's agreement: the change is
 * recorded at head office.
 *
 * Online only, and never part of Save & Print: offline, or when the connection
 * drops, the card says so and the bill goes on as before.
 *
 * Keyed by the number, so typing a different number starts afresh.
 */
export function SavedSizeCard({ mobile, online }: { mobile: string; online: boolean }) {
  const number = consentMobile(mobile);
  if (!number) return null;
  return <SizesFor key={number} mobile={number} online={online} />;
}

function when(iso: string): string {
  return `on ${formatDateTime(iso)}`;
}

function SizesFor({ mobile, online }: { mobile: string; online: boolean }) {
  const [found, setFound] = useState<SavedSizes | null>(null);
  const [problem, setProblem] = useState("");
  const [loading, setLoading] = useState(false);
  const [editing, setEditing] = useState<SavedSize | null>(null);
  const [note, setNote] = useState("");
  const live = useRef(true);
  // Which read is the newest: an older one that answers late never overwrites
  // what a newer one found (the line dropping in between, for one).
  const reads = useRef(0);

  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);

  const load = useCallback(async () => {
    const mine = ++reads.current;
    const current = () => live.current && reads.current === mine;
    if (!navigator.onLine) {
      setProblem(SAVED_SIZES_OFFLINE);
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const response = await api.get<SavedSizes>(SAVED_SIZES_API, { params: { mobile } });
      if (!current()) return;
      setFound(response.data);
      setProblem("");
    } catch (reason) {
      if (current()) {
        setProblem(isConnectionLost(reason) ? SAVED_SIZES_OFFLINE : apiErrorMessage(reason));
      }
    } finally {
      if (current()) setLoading(false);
    }
  }, [mobile]);

  // Read again when the connection comes back.
  useEffect(() => {
    void load();
  }, [load, online]);

  const sizes = found?.sizes ?? [];
  return (
    <section className="bill-consent" data-testid="saved-sizes">
      <div className="bill-customer-heading">
        <p className="eyebrow">Saved sizes</p>
        <span>Learned from this customer's bills</span>
      </div>
      {problem ? (
        <p className="muted-cell bill-consent-note" data-testid="saved-sizes-problem">
          {problem}
        </p>
      ) : loading && !found ? (
        <p className="muted-cell bill-consent-note">Checking…</p>
      ) : sizes.length === 0 ? (
        <p className="muted-cell bill-consent-note" data-testid="saved-sizes-none">
          No sizes saved yet. They are learned from this customer's bills.
        </p>
      ) : (
        <dl className="bill-consent-rows" data-testid="saved-sizes-list">
          {sizes.map((row) => (
            <div
              className="bill-consent-row"
              key={`${row.brand}|${row.category}`}
              data-testid="saved-size-row"
              data-brand={row.brand}
              data-category={row.category}
            >
              <dt>
                <span className="bill-size-name">
                  {row.brand} · {row.category}
                </span>
                <span className="bill-size-from">{savedSizeNote(row, when)}</span>
              </dt>
              <dd data-testid="saved-size-value">{row.size}</dd>
              {online && !problem && editing === null && (
                <button
                  type="button"
                  className="btn btn-sm"
                  data-testid="saved-size-correct"
                  onClick={() => {
                    setEditing(row);
                    setNote("");
                  }}
                >
                  Correct
                </button>
              )}
            </div>
          ))}
        </dl>
      )}
      {editing && (
        <CorrectionForm
          key={`${editing.brand}|${editing.category}|${editing.size}`}
          mobile={mobile}
          row={editing}
          online={online}
          onDone={(saved) => {
            setEditing(null);
            if (saved) {
              setFound(saved);
              setNote("Saved. The change is recorded.");
            }
          }}
        />
      )}
      {note && (
        <p className="muted-cell bill-consent-note" data-testid="saved-sizes-note">
          {note}
        </p>
      )}
    </section>
  );
}

function CorrectionForm({
  mobile,
  row,
  online,
  onDone,
}: {
  mobile: string;
  row: SavedSize;
  online: boolean;
  onDone: (saved: SavedSizes | null) => void;
}) {
  const [draft, setDraft] = useState<CorrectionDraft>({ size: "", agreed: false });
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  // One id per correction typed: a retry after a dropped answer reuses it, so
  // head office never records the change twice. A new size is a new change.
  const attempt = useRef<{ id: string; size: string } | null>(null);
  const problem = correctionProblem(row, draft);

  async function save() {
    if (problem || busy) return;
    if (!navigator.onLine) {
      setMessage(SAVED_SIZES_OFFLINE);
      return;
    }
    const body = correctionBody("", mobile, row, draft);
    if (!attempt.current || attempt.current.size !== body.size) {
      attempt.current = { id: newUuid(), size: body.size };
    }
    setBusy(true);
    setMessage("");
    try {
      const response = await api.post<SavedSizes>(SAVED_SIZES_API, {
        ...body,
        id: attempt.current.id,
      });
      onDone(response.data);
    } catch (reason) {
      setMessage(isConnectionLost(reason) ? SAVED_SIZE_DROPPED : apiErrorMessage(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="bill-consent-asking" data-testid="saved-size-form">
      <label className="field">
        <span>
          New {row.brand} {row.category} size (saved: {row.size})
        </span>
        <input
          className="input"
          data-testid="saved-size-new"
          value={draft.size}
          maxLength={24}
          onChange={(e) => setDraft({ ...draft, size: e.target.value })}
        />
      </label>
      <label className="check-row">
        <input
          type="checkbox"
          data-testid="saved-size-agreed"
          checked={draft.agreed}
          onChange={(e) => setDraft({ ...draft, agreed: e.target.checked })}
        />
        <span>The customer agreed to this change</span>
      </label>
      {draft.size.trim() && problem && (
        <p className="muted-cell bill-consent-note" data-testid="saved-size-hint">
          {problem}
        </p>
      )}
      {message && (
        <p className="bill-alert bill-consent-note" data-testid="saved-size-message">
          {message}
        </p>
      )}
      <div className="toolbar">
        <button
          type="button"
          className="btn btn-primary"
          data-testid="saved-size-save"
          disabled={Boolean(problem) || busy || !online}
          onClick={() => void save()}
        >
          <Ruler size={15} /> Save
        </button>
        <button
          type="button"
          className="btn"
          data-testid="saved-size-cancel"
          disabled={busy}
          onClick={() => onDone(null)}
        >
          Cancel
        </button>
      </div>
    </div>
  );
}
