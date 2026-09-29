import { useLocation } from "react-router-dom";
import { Compass } from "lucide-react";

import { PageHeader } from "../components/PageHeader";
import { NAV_ITEMS, SECTIONS, itemPath, normalizePath } from "../shell/navConfig";
import { PLANNED_PAGES, STAGE_LABEL, STAGE_TONE } from "./plannedPages";
import "./PlannedPage.css";

/** The screen and section a bare address belongs to. Exact match, not
 *  longest-prefix: an address nobody named must not borrow its parent's promise. */
function screenAt(pathname: string) {
  const target = normalizePath(pathname);
  const screen = NAV_ITEMS.find((i) => !i.deepLink && itemPath(i) === target);
  const section = screen ? SECTIONS.find((s) => s.code === screen.section) : undefined;
  return { target, screen, section };
}

/** A screen we have promised KDPS but have not built (issue #89).
 *
 *  It says three things and no more: what will live here, what it is waiting on,
 *  and which module of the client's build-status report it belongs to. The copy
 *  is the report's own, from `plannedPages.ts` — so a demo of the sidebar and the
 *  report the client is holding cannot tell two different stories. */
export function PlannedPage() {
  const { pathname } = useLocation();
  const { target, screen, section } = screenAt(pathname);
  const planned = PLANNED_PAGES[target];
  if (!planned) return <NotFound />;

  const Icon = section?.icon ?? Compass;
  const layer = section?.layer ?? "documents";

  return (
    <div className="page-pad">
      <PageHeader
        title={screen?.label}
        actions={
          <span className={`chip chip-${STAGE_TONE[planned.module.stage]}`}>
            {STAGE_LABEL[planned.module.stage]}
          </span>
        }
      />
      <p className="lead planned-summary">{planned.summary}</p>

      <div
        className="card planned-card"
        style={{ borderTop: `3px solid var(--layer-${layer})` }}
        data-testid="planned-page"
      >
        <div className="planned-medallion" style={{ color: `var(--layer-${layer})` }}>
          <Icon size={24} />
        </div>
        <h3 className="h3">What will live here</h3>
        <ul className="planned-list">
          {planned.contains.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>

        {planned.notes && (
          <div className="planned-notes">
            <p className="planned-notes-head">Worth knowing</p>
            {planned.notes.map((note) => (
              <p key={note}>{note}</p>
            ))}
          </div>
        )}
      </div>

      <p className="planned-stamp">
        Not built yet — part of <b>{planned.module.name}</b> in the module-wise build-status
        report sent to KDPS management on 21 July 2026.
      </p>
    </div>
  );
}

/** An address nobody built and nobody promised — the 404 corner. Kept separate
 *  from the planned page on purpose: "we will build this, here is what it does"
 *  and "there is nothing here" are different messages. */
export function NotFound() {
  return (
    <div className="page-pad">
      <h1 className="h1">Nothing at this address</h1>
      <div className="card planned-card" data-testid="not-found">
        <div className="planned-medallion" style={{ color: "var(--muted)" }}>
          <Compass size={24} />
        </div>
        <p className="lead" style={{ maxWidth: 560 }}>
          This is not a screen. Use the sidebar, or the search box at the top, to get where you
          were going.
        </p>
      </div>
    </div>
  );
}
