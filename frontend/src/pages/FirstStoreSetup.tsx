import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { PageHeader } from "../components/PageHeader";
import { api, apiErrorMessage } from "../lib/api";
import "./FirstStoreSetup.css";

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
type Check = { key: string; passed: boolean; reason?: string };
type Readiness = {
  data: {
    sell_ready: boolean;
    goods_ready: boolean;
    opening_setup_ready: boolean;
    checks: Check[];
    selling_checks: Check[];
  };
};

type StepKey = "config" | "people" | "stock" | "counter";

/** Which step fixes each readiness check (masters/goods_services.py
 *  compute_readiness_checks and masters/first_store_readiness.py). Store
 *  identity checks (legal_entity, registration, business_units,
 *  system_locations) and any unknown key land on the final step, which links to
 *  store readiness, so nothing failing is ever hidden. */
const CHECK_STEP: Record<string, StepKey> = {
  current_setup: "config",
  business_profile: "config",
  working_calendar: "config",
  document_series: "config",
  receipt_configuration: "config",
  staff_assigned: "people",
  transfer_approval: "people",
  selling_policy: "config",
  tax_configuration: "config",
  invoice_prefix: "config",
  bill_number_series: "config",
  authorised_cashier: "people",
  opening_reconciled: "stock",
  sellable_item_inputs: "stock",
  source_costing: "stock",
  goods_active: "counter",
  registered_counter: "counter",
};

function failingByStep(readiness: Readiness | null) {
  const out: Record<StepKey, Check[]> = { config: [], people: [], stock: [], counter: [] };
  if (!readiness) return out;
  const seen = new Set<string>();
  for (const c of [...readiness.data.checks, ...readiness.data.selling_checks]) {
    if (c.passed || seen.has(c.key)) continue;
    seen.add(c.key);
    out[CHECK_STEP[c.key] ?? "counter"].push(c);
  }
  return out;
}

const label = (key: string) => {
  const s = key.replaceAll("_", " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
};

type Task = { to: string; title: string; hint: string; cta?: boolean };

function Tasks({ items }: { items: Task[] }) {
  return (
    <ul className="fss-tasks">
      {items.map((t) => (
        <li key={t.to + t.title}>
          <Link to={t.to} className={t.cta ? "fss-task is-cta" : "fss-task"}>
            <b>{t.title}</b>
            <span>{t.hint}</span>
          </Link>
        </li>
      ))}
    </ul>
  );
}

const Tick = () => (
  <svg viewBox="0 0 20 20" fill="none" aria-hidden="true">
    <path
      d="M4.5 10.5l3.5 3.5 7.5-8"
      stroke="currentColor"
      strokeWidth="2.4"
      strokeLinecap="round"
      strokeLinejoin="round"
    />
  </svg>
);

function Step({
  n,
  title,
  state,
  failing,
  who,
  summary,
  children,
  details,
}: {
  n: number;
  title: string;
  /** null while the readiness checks are still loading. */
  state: "clear" | "open" | null;
  failing: Check[];
  who: string[];
  summary: ReactNode;
  children: ReactNode;
  details: ReactNode;
}) {
  const cls =
    state === "clear" ? "fss-step is-clear" : state === "open" ? "fss-step is-open" : "fss-step";
  return (
    <li className={cls}>
      <div className="fss-node" aria-hidden="true">
        {state === "clear" ? <Tick /> : n}
      </div>
      <section className="card fss-body" aria-labelledby={`fss-step-${n}`}>
        <div className="fss-body-head">
          <h2 id={`fss-step-${n}`}>
            <span className="sr-only">Step {n}: </span>
            {title}
          </h2>
          {state === "clear" && <span className="chip chip-green">Checks passed</span>}
          {state === "open" && (
            <span className="chip chip-amber">
              {failing.length === 1 ? "1 check open" : `${failing.length} checks open`}
            </span>
          )}
          {state === null && <span className="chip">Checking…</span>}
        </div>
        <p className="fss-summary">{summary}</p>
        <div className="fss-who" aria-label="Who does this">
          {who.map((w) => (
            <span key={w} className="chip chip-navy">
              {w}
            </span>
          ))}
        </div>
        {failing.length > 0 && (
          <div className="fss-blockers">
            <p>Still blocking trading</p>
            <ul>
              {failing.map((c) => (
                <li key={c.key}>
                  <strong>{label(c.key)}:</strong> {c.reason ?? "required configuration is missing"}
                </li>
              ))}
            </ul>
          </div>
        )}
        {children}
        <details className="fss-more">
          <summary>How this step works</summary>
          {details}
        </details>
      </section>
    </li>
  );
}

export function FirstStoreSetupPage() {
  const [setup, setSetup] = useState<Setup | null>(null);
  const [readiness, setReadiness] = useState<Readiness | null>(null);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const [checking, setChecking] = useState(false);
  useEffect(() => {
    let live = true;
    setChecking(true);
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
      })
      .finally(() => {
        if (live) setChecking(false);
      });
    return () => {
      live = false;
    };
  }, [retry]);

  const failing = failingByStep(readiness);
  const stateOf = (k: StepKey) => (readiness ? (failing[k].length ? "open" : "clear") : null);
  const counterState =
    readiness && (failing.counter.length || !readiness.data.sell_ready)
      ? "open"
      : stateOf("counter");

  return (
    <div className="page-pad fss">
      <PageHeader
        title="Set up your first store"
        lead="Work through these four steps. Selling opens once the server has accepted every check."
        actions={
          <button className="btn" onClick={() => setRetry(retry + 1)} disabled={checking}>
            {checking ? "Checking…" : "Refresh checks"}
          </button>
        }
      />
      {error && (
        <p className="warn-note" role="alert">
          {error}
        </p>
      )}
      {!setup ? (
        !error && <p className="lead">Loading your registered company…</p>
      ) : (
        <>
          <section className="card fss-store" aria-label="Store">
            <div className="fss-store-head">
              <h2 className="fss-store-name">{setup.summary.store.name}</h2>
              <span className="fss-store-code">{setup.summary.store.code}</span>
              {setup.setup_complete ? (
                <span className="chip chip-green">Selling approved</span>
              ) : (
                <span className="chip chip-amber">Trading inactive</span>
              )}
            </div>
            <p className="fss-store-sub">
              {setup.summary.company.name},{" "}
              {setup.summary.store.setup_kind === "existing"
                ? `switching from ${setup.summary.store.source_system}`
                : "a new store"}
            </p>
            <div className="fss-store-foot">
              <ol className="fss-gates" aria-label="Approvals">
                {[
                  ["Opening setup", readiness?.data.opening_setup_ready],
                  ["Goods", readiness?.data.goods_ready],
                  ["Selling", readiness?.data.sell_ready],
                ].map(([name, on]) => (
                  <li key={String(name)} className={on ? "fss-gate is-on" : "fss-gate"}>
                    <span className="fss-gate-dot" aria-hidden="true" />
                    {name}
                    <span className="sr-only">{on ? " approved" : " not approved"}</span>
                  </li>
                ))}
              </ol>
              {setup.setup_complete ? (
                <Link to="/sell" className="btn btn-cta">
                  Open the assigned counter
                </Link>
              ) : (
                <Link to="/setup/organisation?panel=sites" className="btn">
                  Review store identity
                </Link>
              )}
            </div>
          </section>

          <ol className="fss-steps">
            <Step
              n={1}
              title="Approve company configuration"
              state={stateOf("config")}
              failing={failing.config}
              who={["Admin drafts", "Owner approves"]}
              summary="Set the real calendar, business profile, numbering, tax and selling rules."
              details={
                <p>
                  Admin drafts each setting; Owner independently approves the required versions.
                  Fixture defaults never count as real business approvals.
                </p>
              }
            >
              <Tasks
                items={[
                  {
                    to: "/setup/configuration?kind=working_calendar&draft=new",
                    title: "Working calendar",
                    hint: "Trading days and hours",
                  },
                  {
                    to: "/setup/configuration?kind=business_profile&draft=new",
                    title: "Business profile",
                    hint: "Legal name and address",
                  },
                  {
                    to: "/setup/configuration?view=wizard",
                    title: "Identity and PT configuration",
                    hint: "How items are identified",
                  },
                  {
                    to: "/setup/document-numbering",
                    title: "Document numbering",
                    hint: "Invoice prefix and bill series",
                  },
                  {
                    to: "/setup/tax-settings",
                    title: "Tax settings",
                    hint: "Rates applied at the counter",
                  },
                  {
                    to: "/setup/configuration?kind=sell_policy&draft=new",
                    title: "Discount and selling policy",
                    hint: "What the counter may offer",
                  },
                  {
                    to: "/setup/configuration?view=approvals",
                    title: "Review configuration changes",
                    hint: "Owner approves drafts here",
                  },
                ]}
              />
            </Step>

            <Step
              n={2}
              title="Activate your people"
              state={stateOf("people")}
              failing={failing.people}
              who={["Owner assigns", "Admin reviews"]}
              summary={
                <>
                  Owner {setup.summary.owner.name} and Admin {setup.summary.admin.name} must both
                  have replaced their temporary password. Then give each manager a login, a store
                  and their own counter PIN.
                </>
              }
              details={
                <>
                  <p>
                    Owner: {setup.summary.owner.name} ({setup.summary.owner.email}). Admin:{" "}
                    {setup.summary.admin.name} ({setup.summary.admin.email}).
                  </p>
                  <p>
                    Owner separately assigns Warehouse preparation to the named Admin, scoped to
                    this store and its brands. Admin’s administration role grants no protected
                    business data.
                  </p>
                  <p>
                    For each person: confirm their identity and email, create or explicitly link
                    their staff record, create their login, place them at the store, then assign
                    explicit site and brand access. Store Person is the pilot manager role. Each
                    manager sets their own personal counter PIN. Admin reviews Owner’s privileged
                    changes within the approved working calendar.
                  </p>
                </>
              }
            >
              {setup.summary.proposed_team.length ? (
                <div className="fss-people">
                  <table className="data">
                    <thead>
                      <tr>
                        <th>Proposed person</th>
                        <th>Email</th>
                        <th>Staff code</th>
                        <th>Role</th>
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
                </div>
              ) : (
                <p className="fss-summary">
                  No one else was proposed at signup. Add the store manager under People, login and
                  assignments.
                </p>
              )}
              <Tasks
                items={[
                  {
                    to: "/setup/people-access?panel=people",
                    title: "People, login and assignments",
                    hint: "Staff record, login and store",
                  },
                  {
                    to: "/setup/people-access?panel=workflow",
                    title: "Workflow responsibility and limits",
                    hint: "Who prepares and approves",
                  },
                  {
                    to: "/setup/people-access?panel=privileged",
                    title: "Independent access review",
                    hint: "Admin checks Owner’s changes",
                  },
                ]}
              />
            </Step>

            <Step
              n={3}
              title="Establish accepted stock"
              state={stateOf("stock")}
              failing={failing.stock}
              who={
                setup.summary.store.setup_kind === "existing"
                  ? ["Manager verifies", "Warehouse values", "Owner approves"]
                  : ["Warehouse receives"]
              }
              summary={
                setup.summary.store.setup_kind === "existing"
                  ? `Bring over a fresh ${setup.summary.store.source_system} export and accept the verified opening stock before selling.`
                  : "Create your products and receive evidenced stock. Signup creates no inventory."
              }
              details={
                setup.summary.store.setup_kind === "existing" ? (
                  <p>
                    Upload a fresh export, retain its original evidence, review stable identities
                    and explain quantities and values. Manager supplies physical verification;
                    scoped Warehouse prepares valuation; a different Owner approves the exact
                    opening revision.
                  </p>
                ) : (
                  <p>
                    Create configured products and receive evidenced stock through the normal
                    receiving workflow. No inventory is created by company signup.
                  </p>
                )
              }
            >
              <Tasks
                items={
                  setup.summary.store.setup_kind === "existing"
                    ? [
                        {
                          to: "/goods/opening",
                          title: "Opening stock and reconciliation",
                          hint: "Upload, verify and approve",
                        },
                      ]
                    : [
                        {
                          to: "/setup/products-parties",
                          title: "Products and parties",
                          hint: "Items, brands and suppliers",
                        },
                        {
                          to: "/goods/receive",
                          title: "Receive new store stock",
                          hint: "Evidenced goods receiving",
                        },
                      ]
                }
              />
            </Step>

            <Step
              n={4}
              title="Approve readiness and pair the counter"
              state={counterState}
              failing={failing.counter}
              who={["Owner approves", "Manager pairs"]}
              summary="Check the scanner and printer, pair the registered counter, then approve the store for selling."
              details={
                <p>
                  Complete scanner and printer checks, configured discounts and registered-counter
                  pairing. Online sales show success and allow printing only after the server
                  accepts the bill.
                </p>
              }
            >
              <Tasks
                items={[
                  {
                    to: "/sell/till",
                    title: "Counter pairing and checks",
                    hint: "Scanner, printer and till",
                  },
                  {
                    to: "/setup/organisation?panel=sites",
                    title: "Approve store readiness",
                    hint: "Turns selling on",
                  },
                  ...(setup.setup_complete
                    ? [
                        {
                          to: "/sell",
                          title: "Open the assigned counter",
                          hint: "Start billing",
                          cta: true,
                        },
                      ]
                    : []),
                ]}
              />
            </Step>
          </ol>
        </>
      )}
    </div>
  );
}
