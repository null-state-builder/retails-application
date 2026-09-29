// Reports > Store Dashboard (store operations ticket 10, PRD §17): the store's
// Home (#174), reached from Reports as well. The same screen, not a second one:
// it reads `/api/store/dashboard`, which answers to `home: view` and to the store
// picked in the top bar. A person working across all their units is told to pick
// one, by the server, in its own words.

import { PageHeader } from "../components/PageHeader";
import { StoreDashboard } from "./StoreDashboard";
import "./Home.css";

export function StoreDashboardPage() {
  return (
    <div className="page-pad home">
      <PageHeader lead="One store's day: what it sold, what is waiting on somebody, what is in the shop now and the week behind it." />
      <StoreDashboard />
    </div>
  );
}
