import { Link } from "react-router-dom";
import { ShieldAlert } from "lucide-react";

// Shown inside the AppShell when a signed-in user lands on a route their role
// can't access (issue #37). Keeps the URL visible for support screenshots
// rather than silently redirecting.
export function AccessDenied() {
  return (
    <div className="page-pad">
      <p className="eyebrow">Access</p>
      <h1 className="h1" style={{ marginBottom: 20 }}>No access</h1>
      <div
        className="card"
        style={{ padding: "44px 38px", borderTop: "3px solid var(--layer-controls)" }}
        data-testid="access-denied"
      >
        <div
          style={{
            width: 56,
            height: 56,
            borderRadius: 14,
            background: "var(--inner)",
            border: "1px solid var(--hairline)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            color: "var(--layer-controls)",
            marginBottom: 18,
          }}
        >
          <ShieldAlert size={26} />
        </div>
        <h3 className="h3" style={{ marginBottom: 8 }}>You don't have access to this page</h3>
        <p className="lead" style={{ maxWidth: 560, lineHeight: 1.65 }}>
          This page is limited to certain roles. If you believe you should have access, ask an
          administrator to review your role.
        </p>
        <div style={{ marginTop: 18 }}>
          <Link className="btn btn-primary" to="/" data-testid="access-denied-home">
            Back to Home
          </Link>
        </div>
      </div>
    </div>
  );
}
