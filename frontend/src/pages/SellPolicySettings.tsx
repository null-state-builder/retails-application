import { useEffect, useState } from "react";

import { useAuth } from "../auth/AuthContext";
import { Note } from "../components/Note";
import { PageHeader } from "../components/PageHeader";
import { Section } from "../components/Section";
import { api, apiErrorMessage } from "../lib/api";
import { userCan } from "../shell/navConfig";

interface SellPolicy {
  manual_discount_cap_percent: string;
  manual_discount_on_offer_lines: boolean;
}

const EMPTY: SellPolicy = {
  manual_discount_cap_percent: "10.00",
  manual_discount_on_offer_lines: false,
};

/** The two chain-wide counter dials (#271), deliberately small and whole-row. */
export function SellPolicySettingsPage() {
  const { user } = useAuth();
  const canManage = userCan(user, "sell", "manage");
  const [policy, setPolicy] = useState<SellPolicy>(EMPTY);
  const [loading, setLoading] = useState(canManage);
  const [loaded, setLoaded] = useState(false);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState("");

  useEffect(() => {
    if (!canManage) return;
    let live = true;
    setLoading(true);
    setError("");
    api
      .get<SellPolicy>("/sell/policy")
      .then((response) => {
        if (!live) return;
        setPolicy(response.data);
        setLoaded(true);
      })
      .catch((reason) => {
        if (!live) return;
        setLoaded(false);
        setError(apiErrorMessage(reason));
      })
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [canManage, loadAttempt]);

  async function save() {
    setError("");
    setSaved("");
    setSaving(true);
    try {
      const response = await api.put<SellPolicy>("/sell/policy", policy);
      setPolicy(response.data);
      setSaved("Counter discount policy saved.");
    } catch (reason) {
      setError(apiErrorMessage(reason));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="page-pad">
      <PageHeader title="Settings" lead="Counter controls" />
      {canManage ? (
        <Section heading="Manual discounts" testId="sell-policy-card">
          <p className="muted-cell">
            These are head-office limits. The counter applies them offline and cannot use a manager
            PIN to go past them.
          </p>
          {loading ? (
            <p>Loading policy…</p>
          ) : !loaded ? (
            <div>
              <Note tone="error">{error || "Counter policy could not be loaded."}</Note>
              <button
                type="button"
                className="btn"
                onClick={() => setLoadAttempt((attempt) => attempt + 1)}
              >
                Retry
              </button>
            </div>
          ) : (
            <div className="form-grid wide-form">
              <label className="field">
                <span>Maximum manual discount (%)</span>
                <input
                  className="input"
                  data-testid="sell-policy-cap"
                  inputMode="decimal"
                  value={policy.manual_discount_cap_percent}
                  onChange={(event) =>
                    setPolicy({ ...policy, manual_discount_cap_percent: event.target.value })
                  }
                />
              </label>
              <label className="check-row">
                <input
                  type="checkbox"
                  data-testid="sell-policy-offer-stacking"
                  checked={policy.manual_discount_on_offer_lines}
                  onChange={(event) =>
                    setPolicy({ ...policy, manual_discount_on_offer_lines: event.target.checked })
                  }
                />
                Allow manual discount on offer-discounted lines
              </label>
              <button
                type="button"
                className="btn btn-primary"
                data-testid="sell-policy-save"
                disabled={saving}
                onClick={() => void save()}
              >
                {saving ? "Saving…" : "Save policy"}
              </button>
            </div>
          )}
          {loaded && error && <Note tone="error">{error}</Note>}
          {saved && (
            <Note tone="success" role="status">
              {saved}
            </Note>
          )}
        </Section>
      ) : (
        <p className="muted-cell">You do not have access to counter discount settings.</p>
      )}
    </div>
  );
}
