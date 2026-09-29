// Home's Alerts surface (#77, #84 story 9): news that finds you, needing no
// decision. Two blocks, each behind the gate its old screen had:
//
// - Coming up: deadlines the daily job raises (`alerts.checks`) - stock stuck
//   in transit, and a brand's return window closing at 30/15/7 days left. Each
//   clears itself the run its condition stops being true. A later kind is
//   another row here, never a new screen - the criterion #77 itself draws.
// - Notifications: the goods notification feed (E179/E180/E187) - a new
//   problem landing on your team, a PT sent back - grouped by day, read or
//   unread. Reading one never resolves the problem; that happens on Action
//   Needed.
//
// Since 23 Sep 2026 every row carries a topic chip and a plain sentence
// (`lib/actionCatalogue.ts`), so the two blocks read the same way Action
// Needed does.
import { CheckCircle2 } from "lucide-react";

import { useAuth } from "../auth/AuthContext";
import { useList } from "../lib/hooks";
import { deadlineTopic } from "../lib/actionCatalogue";
import { TopicChip } from "../components/ActionBits";
import { canSeeExceptions, GoodsNotificationsFeed } from "./GoodsExceptions";
import { PageHeader } from "../components/PageHeader";
import {
  AlertTitle,
  alertWhere,
  daysLeftLabel,
  daysLeftTone,
  fmtAlertWhen,
  type AlertT,
} from "../components/alert";

export function AlertsPage() {
  const { user, session } = useAuth();
  // Each block keeps the gate its old screen had: deadlines behind Home, the
  // goods notifications behind the exception grants.
  const canSeeDeadlines = !!user?.is_superuser || (user?.capabilities?.home ?? "none") !== "none";
  const canSeeGoods = canSeeExceptions(session);

  return (
    <div className="page-pad">
      <PageHeader lead="News for you. Nothing here needs a decision: your to-do list is on Action Needed." />

      {canSeeDeadlines && (
        <section data-testid="alerts-deadlines">
          <div className="an-section-head">
            <h2 className="h3">Coming up</h2>
            <span className="an-section-hint">
              Deadlines. Each one clears itself once it stops being true.
            </span>
          </div>
          <DeadlineAlerts />
        </section>
      )}

      {canSeeGoods && (
        <section data-testid="alerts-goods" className={canSeeDeadlines ? "an-section" : undefined}>
          <div className="an-section-head">
            <h2 className="h3">Notifications</h2>
            <span className="an-section-hint">
              What happened. Open one to see it on Action Needed.
            </span>
          </div>
          <GoodsNotificationsFeed />
        </section>
      )}
    </div>
  );
}

/** The in-transit and return-window alerts (#77), read from `/alerts`. */
function DeadlineAlerts() {
  const { data, loading } = useList<AlertT>("/alerts");

  if (loading) return <p className="lead">Loading…</p>;
  if (data.length === 0) {
    return (
      <p className="an-empty-line" data-testid="alerts-empty">
        <CheckCircle2 size={16} aria-hidden /> No deadlines coming up.
      </p>
    );
  }
  return (
    <ul className="an-deadlines" data-testid="alerts-table">
      {data.map((a) => (
        <li key={a.id} className="an-deadline" data-testid={`alert-row-${a.id}`}>
          <TopicChip topic={deadlineTopic(a.kind)} />
          <span className="an-deadline-main">
            <span className="an-deadline-kind">{a.kind_label}</span>
            <AlertTitle alert={a} />
            <span className="an-note-where"> · {alertWhere(a)}</span>
          </span>
          <span className={`chip chip-${daysLeftTone(a.days_left)}`}>
            {daysLeftLabel(a.days_left)}
          </span>
          <span className="an-note-time">Raised {fmtAlertWhen(a.created_at)}</span>
        </li>
      ))}
    </ul>
  );
}

export default AlertsPage;
