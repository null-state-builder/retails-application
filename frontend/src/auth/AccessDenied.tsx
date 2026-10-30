import { Link } from "react-router-dom";

import { PageHeader } from "../components/PageHeader";
import { EmptyState } from "../components/States";

// Shown inside the AppShell when a signed-in user lands on a route their role
// can't access (issue #37). Keeps the URL visible for support screenshots
// rather than silently redirecting. The crumb still names the screen that was
// asked for, so "no access" always says to what.
export function AccessDenied() {
  return (
    <div className="page-pad">
      <PageHeader title="No access" />
      <EmptyState
        heading="You don't have access to this page"
        action={
          <Link className="btn btn-primary" to="/" data-testid="access-denied-home">
            Back to Home
          </Link>
        }
        testId="access-denied"
      >
        This page is limited to certain roles. If you believe you should have access, ask an
        administrator to review your role.
      </EmptyState>
    </div>
  );
}
