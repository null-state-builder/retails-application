import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { PageHeader } from "../components/PageHeader";
import { api, apiErrorMessage } from "../lib/api";

type Setup = {
  first_store_id: number;
  setup_complete: boolean;
  summary: {
    company: { name: string };
    store: { code: string; name: string; setup_kind: "new" | "existing"; source_system: string };
    owner: { name: string; email: string; staff_code: string };
    admin: { name: string; email: string; staff_code: string };
    proposed_team: { name: string; email: string; staff_code: string; role_code: string }[];
  };
};
type Readiness = {
  data: {
    sell_ready: boolean;
    goods_ready: boolean;
    opening_setup_ready: boolean;
    checks: { key: string; passed: boolean; reason?: string }[];
    selling_checks: { key: string; passed: boolean; reason?: string }[];
  };
};

export function FirstStoreSetupPage() {
  const [setup, setSetup] = useState<Setup | null>(null);
  const [readiness, setReadiness] = useState<Readiness | null>(null);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let live = true;
    api
      .get<Setup>("/auth/admin/registration")
      .then(async ({ data }) => {
        if (live) {
          setSetup(data);
          setError("");
        }
        const response = await api.get<Readiness>(
          `/goods-v1/masters/stores/${data.first_store_id}/readiness`,
        );
        if (live) setReadiness(response.data);
      })
      .catch((e: unknown) => {
        if (live) setError(apiErrorMessage(e));
      });
    return () => {
      live = false;
    };
  }, [retry]);
  return (
    <div className="page-pad">
      <PageHeader
        title="Set up your first store"
        lead="Follow the required setup, people, stock and counter checks before trading."
      />
      {error && (
        <p className="warn-note" role="alert">
          {error}
        </p>
      )}
      <button className="btn" onClick={() => setRetry(retry + 1)}>
        Refresh setup checks
      </button>
      {!setup ? (
        !error && <p>Loading registered company setup…</p>
      ) : (
        <>
          <section className="card section-card">
            <h2>
              {setup.summary.store.name} ({setup.summary.store.code})
            </h2>
            <p>
              {setup.summary.company.name} ·{" "}
              {setup.summary.store.setup_kind === "existing"
                ? `Switching from ${setup.summary.store.source_system}`
                : "New store"}
            </p>
            <p>
              {setup.setup_complete
                ? "Selling has been approved. Continue to the assigned counter."
                : "Registered; trading is inactive until the checks and approvals below are complete."}
            </p>
            <Link to="/setup/organisation?panel=sites" className="btn">
              Review store identity and readiness
            </Link>
          </section>
          <section className="card section-card">
            <h2>1 · Approve company configuration</h2>
            <p>
              Admin drafts the real calendar, identity/profile, numbering and tax settings. Owner
              independently approves required versions. Fixture defaults are never real business
              approvals.
            </p>
            <div className="toolbar">
              <Link to="/setup/configuration?kind=working_calendar&draft=new" className="btn">
                Working calendar
              </Link>
              <Link to="/setup/configuration?kind=business_profile&draft=new" className="btn">
                Business profile
              </Link>
              <Link to="/setup/configuration?view=wizard" className="btn">
                Identity and PT configuration
              </Link>
              <Link to="/setup/document-numbering" className="btn">
                Document numbering
              </Link>
              <Link to="/setup/tax-settings" className="btn">
                Tax settings
              </Link>
              <Link to="/setup/configuration?kind=sell_policy&draft=new" className="btn">
                Discount and selling policy
              </Link>
              <Link to="/setup/configuration?view=approvals" className="btn">
                Review configuration changes
              </Link>
            </div>
          </section>
          <section className="card section-card">
            <h2>2 · Activate the proposed people through normal administration</h2>
            <p>
              Owner: {setup.summary.owner.name} ({setup.summary.owner.email}). Admin:{" "}
              {setup.summary.admin.name} ({setup.summary.admin.email}). Both must have replaced
              their temporary password.
            </p>
            <p>
              Owner separately assigns Warehouse preparation to the named Admin, scoped to this
              store and its brands. Admin’s administration role grants no protected business data.
            </p>
            {setup.summary.proposed_team.length ? (
              <table className="data">
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Email</th>
                    <th>Staff code</th>
                    <th>Proposed role</th>
                  </tr>
                </thead>
                <tbody>
                  {setup.summary.proposed_team.map((p) => (
                    <tr key={p.email}>
                      <td>{p.name}</td>
                      <td>{p.email}</td>
                      <td>{p.staff_code}</td>
                      <td>{p.role_code}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p>No other people were proposed. Add the manager through People and access.</p>
            )}
            <p>
              For each person: confirm their identity/email, create or explicitly link their staff
              record, create their login, place them at the store, then assign explicit site/brand
              access. Store Person is the pilot manager role. Each manager sets their own personal
              counter PIN. Admin reviews Owner’s privileged changes within the approved working
              calendar.
            </p>
            <div className="toolbar">
              <Link to="/setup/people-access?panel=people" className="btn">
                People, login and assignments
              </Link>
              <Link to="/setup/people-access?panel=workflow" className="btn">
                Workflow responsibility and limits
              </Link>
              <Link to="/setup/people-access?panel=privileged" className="btn">
                Independent access review
              </Link>
            </div>
          </section>
          <section className="card section-card">
            <h2>3 · Establish real, accepted stock</h2>
            {setup.summary.store.setup_kind === "existing" ? (
              <>
                <p>
                  Upload a fresh export, retain its original evidence, review stable identities and
                  explain quantities/values. Manager supplies physical verification; scoped
                  Warehouse prepares valuation; a different Owner approves the exact opening
                  revision. Accept verified stock before sale.
                </p>
                <Link to="/goods/opening" className="btn">
                  Opening stock and reconciliation
                </Link>
              </>
            ) : (
              <>
                <p>
                  Create configured products and receive evidenced stock through the normal
                  receiving workflow. No inventory is created by company signup.
                </p>
                <Link to="/setup/products-parties" className="btn">
                  Products and parties
                </Link>{" "}
                <Link to="/goods/receive" className="btn">
                  Receive new store stock
                </Link>
              </>
            )}
          </section>
          <section className="card section-card">
            <h2>4 · Approve readiness and pair the counter</h2>
            {readiness && (
              <>
                <p>
                  Opening setup: {readiness.data.opening_setup_ready ? "approved" : "not approved"}{" "}
                  · Goods: {readiness.data.goods_ready ? "approved" : "not approved"} · Selling:{" "}
                  {readiness.data.sell_ready ? "approved" : "inactive"}
                </p>
                <ul>
                  {[...readiness.data.checks, ...readiness.data.selling_checks]
                    .filter((c) => !c.passed)
                    .map((c) => (
                      <li key={c.key}>
                        <strong>{c.key.replaceAll("_", " ")}</strong>:{" "}
                        {c.reason ?? "required configuration is missing"}
                      </li>
                    ))}
                </ul>
              </>
            )}
            <p>
              Complete scanner/printer checks, configured discounts and registered-counter pairing.
              Online sales show success and permit printing only after the server accepts the bill.
            </p>
            <div className="toolbar">
              <Link to="/setup/organisation?panel=sites" className="btn">
                Approve store readiness
              </Link>
              <Link to="/sell/till" className="btn">
                Counter pairing and checks
              </Link>
              {setup.setup_complete && (
                <Link to="/sell" className="btn btn-cta">
                  Open the assigned counter
                </Link>
              )}
            </div>
          </section>
        </>
      )}
    </div>
  );
}
