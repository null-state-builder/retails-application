import { useLocation } from "react-router-dom";

import { PageHeader } from "../components/PageHeader";
import { Section } from "../components/Section";
import { EmptyState } from "../components/States";
import { Status } from "../components/Status";
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
  const { target, screen } = screenAt(pathname);
  const planned = PLANNED_PAGES[target];
  if (!planned) return <NotFound />;

  return (
    <div className="page-pad">
      <PageHeader
        {...(screen ? { title: screen.label } : {})}
        actions={
          // The build stage is a state: blue while it is scheduled, grey once it is "later".
          <Status tone={STAGE_TONE[planned.module.stage] === "blue" ? "blue" : "grey"}>
            {STAGE_LABEL[planned.module.stage]}
          </Status>
        }
      />
      <p className="lead planned-summary">{planned.summary}</p>

      <Section heading="What will live here" className="planned-card" testId="planned-page">
        <ul className="planned-list">
          {planned.contains.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>

        {planned.notes && (
          <Section level={3} heading="Worth knowing" className="planned-notes">
            {planned.notes.map((note) => (
              <p key={note}>{note}</p>
            ))}
          </Section>
        )}
      </Section>

      <p className="planned-stamp">
        Not built yet — part of <b>{planned.module.name}</b> in the module-wise build-status report
        sent to KDPS management on 21 July 2026.
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
      <EmptyState testId="not-found">
        This is not a screen. Use the sidebar, or the search box at the top, to get where you were
        going.
      </EmptyState>
    </div>
  );
}
