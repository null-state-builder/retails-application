// Bills, one screen with two scopes (desktop redesign Q1, 29 Oct 2026): this
// counter's bills by date (`/sell/bills`, which also reads the counter's own
// unsynced queue) and a search across every store this person can read
// (`/sell/customers`, a server screen). They stay two URLs because they keep two
// gates - the counter list needs `sell: operate`, the search only `sell: view` -
// and merging them behind one would lock a reader out or let one in. The switch
// shows only the scopes this person can open.

import { Link, useLocation } from "react-router-dom";

import { useAuth } from "../../auth/AuthContext";
import { canAccess } from "../../auth/routeAccess";

const SCOPES = [
  { to: "/sell/bills", counterLabel: "This counter", otherLabel: "By date" },
  { to: "/sell/customers", counterLabel: "All stores", otherLabel: "All stores" },
] as const;

export function BillScope() {
  const { user, session, featuresOn } = useAuth();
  const { pathname } = useLocation();
  if (!user) return null;
  // A counter is one store; for anybody else the dated list is not "this
  // counter", it is their whole scope by date.
  const counter = user.stores.length === 1;
  const open = SCOPES.filter((s) =>
    canAccess(s.to, user, session?.display_actions ?? [], featuresOn),
  );
  if (open.length < 2) return null;
  return (
    <div className="seg" role="group" aria-label="Which bills" data-testid="bill-scope">
      {open.map((s) => {
        const active = pathname === s.to;
        return (
          <Link
            key={s.to}
            to={s.to}
            className={`seg-btn${active ? " active" : ""}`}
            aria-current={active ? "page" : undefined}
            data-testid={`bill-scope-${s.to.split("/").pop()}`}
          >
            {counter ? s.counterLabel : s.otherLabel}
          </Link>
        );
      })}
    </div>
  );
}
