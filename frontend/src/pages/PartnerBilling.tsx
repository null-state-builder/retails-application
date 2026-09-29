// Partner store billing (Rule 12) — its own page rather than a card bolted
// onto Counter Settings, whose route sits at `sell: manage` (an IT-admin-only
// rung Owner does not hold). This dial answers to `money`, the section Owner
// and Accounts already hold, so it needs a page whose *own* gate says that —
// mirrors "Store Targets"'s reasoning in navConfig exactly: filed under Money,
// not Setup, because that is the gate deciding the filing.
import { useEffect, useState } from "react";

import { useAuth } from "../auth/AuthContext";
import { PageHeader } from "../components/PageHeader";
import { api, apiErrorMessage } from "../lib/api";
import { userCan } from "../shell/navConfig";

interface BillingPolicy {
  mode: "informational" | "gl_posting";
  mode_label: string;
  set_by_name: string;
  updated_at: string;
}

const EMPTY_BILLING: BillingPolicy = {
  mode: "informational",
  mode_label: "Informational only — no ledger entry",
  set_by_name: "",
  updated_at: "",
};

export function PartnerBillingPage() {
  const { user } = useAuth();
  const canView = userCan(user, "money", "view");
  const canManage = userCan(user, "money", "manage");
  const [policy, setPolicy] = useState<BillingPolicy>(EMPTY_BILLING);
  const [loading, setLoading] = useState(canView);
  const [loaded, setLoaded] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState("");

  useEffect(() => {
    if (!canView) return;
    let live = true;
    api
      .get<BillingPolicy>("/outbound/partner-billing-policy")
      .then((r) => { if (live) { setPolicy(r.data); setLoaded(true); } })
      .catch((e) => { if (live) setError(apiErrorMessage(e)); })
      .finally(() => live && setLoading(false));
    return () => { live = false; };
  }, [canView]);

  async function save(mode: BillingPolicy["mode"]) {
    setError(""); setSaved(""); setSaving(true);
    try {
      const r = await api.put<BillingPolicy>("/outbound/partner-billing-policy", { mode });
      setPolicy(r.data);
      setSaved("Partner billing policy saved.");
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="page-pad">
      <PageHeader lead="Every transfer to a partner store is billed to it at Purchase Price and shown on the document. This dial decides whether that also raises a receivable in the ledger — no margin, since a partner is billed at cost." />
      {!canView ? (
        <p className="muted-cell">You do not have access to partner billing settings.</p>
      ) : (
        <section className="card section-card" data-testid="billing-policy-card">
          {loading ? (
            <p>Loading policy…</p>
          ) : !loaded ? (
            <p className="warn-note">{error || "Billing policy could not be loaded."}</p>
          ) : (
            <div className="form-grid wide-form">
              <label className="check-row">
                <input
                  type="radio"
                  name="billing-mode"
                  data-testid="billing-policy-informational"
                  checked={policy.mode === "informational"}
                  disabled={!canManage}
                  onChange={() => canManage && void save("informational")}
                />
                Informational only — no ledger entry
              </label>
              <label className="check-row">
                <input
                  type="radio"
                  name="billing-mode"
                  data-testid="billing-policy-gl-posting"
                  checked={policy.mode === "gl_posting"}
                  disabled={!canManage}
                  onChange={() => canManage && void save("gl_posting")}
                />
                Post a receivable at Purchase Price
              </label>
              {policy.set_by_name && <p className="muted-cell">Last set by {policy.set_by_name}.</p>}
              {saving && <p className="muted-cell">Saving…</p>}
            </div>
          )}
          {error && <p className="warn-note">{error}</p>}
          {saved && <p className="ok-note" data-testid="billing-policy-saved">{saved}</p>}
        </section>
      )}
    </div>
  );
}

export default PartnerBillingPage;
