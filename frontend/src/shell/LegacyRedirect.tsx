import { Navigate, useLocation } from "react-router-dom";

import { NotFound } from "../pages/PlannedPage";
import { resolveLegacyPath } from "./navConfig";

/** An address the manifest has moved. A pre-#87 URL - a bookmark, a link in
 *  someone's WhatsApp - redirects to that screen's new home, keeping its tail
 *  and query; anything else is an unbuilt corner and says so.
 *
 *  Rendered from two places, because a legacy path can sit in two kinds of
 *  place. Most fall through the router entirely and are caught by `App`'s
 *  catch-all. But one that lives where a document id lives (next to a `:id`
 *  route) is claimed by that dynamic route and never reaches the catch-all, so it needs a route of its own (routes.tsx `LEGACY`).
 *  Both render this, so `LEGACY_PREFIXES` stays the one statement of where an
 *  old URL goes. */
export function LegacyRedirect() {
  const { pathname, search, hash } = useLocation();
  const target = resolveLegacyPath(pathname);
  const query = target?.includes("?") && search ? `&${search.slice(1)}` : search;
  return target ? <Navigate to={target + query + hash} replace /> : <NotFound />;
}
