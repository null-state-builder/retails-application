import { Link } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";
import { OperationsPage } from "../components/OperationsPage";
import { PageHeader } from "../components/PageHeader";
import { visibleSections } from "../shell/navConfig";
import { PLANNED_PAGES } from "./plannedPages";

/** Planned destinations retain the same section, action and feature gates as
 * their direct bookmarks. The roadmap lists promises separately from work. */
export function ApplicationRoadmapPage() {
  const { user, session, featuresOn } = useAuth();
  const sections = user
    ? visibleSections(user, session?.display_actions ?? [], featuresOn)
        .map((section) => ({ ...section, items: section.items.filter((item) => item.planned) }))
        .filter((section) => section.items.length > 0)
    : [];
  return (
    <OperationsPage>
      <PageHeader
        title="Application roadmap"
        lead="Planned features are listed here separately from daily work. These pages describe future functionality."
      />
      {sections.length === 0 && (
        <p className="muted">
          There are no planned destinations within your current navigation access.
        </p>
      )}
      {sections.map((section) => (
        <section
          key={section.def.code}
          className="card section-card"
          data-testid={`roadmap-${section.def.code}`}
        >
          <h2 className="h3">{section.label}</h2>
          <ul>
            {section.items.map((item) => (
              <li key={item.to}>
                <Link to={item.to}>{item.label}</Link> — planned.
                {PLANNED_PAGES[item.to]?.summary && (
                  <p className="lead">{PLANNED_PAGES[item.to]?.summary}</p>
                )}
              </li>
            ))}
          </ul>
        </section>
      ))}
    </OperationsPage>
  );
}
