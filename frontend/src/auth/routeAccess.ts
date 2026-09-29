// Route → authorization rules for the client PWA.
//
// ProtectedRoute only checks that a user is authenticated, so any logged-in
// user could load any page shell by typing the URL. The backend already 403s
// the data, so this is defense-in-depth + honest UX: mirror the server gates
// on the client so scoped users don't land on pages they have no business on.
//
// Since #87 there is one authority for both the menu and the guard: the section
// manifest. Every rule below is *derived* from it — a screen's URL sits under
// its section's items, so what the sidebar hides, the URL bar hides too, and
// the two can't drift. Whether the user holds a section is the server's answer
// (`capabilities`, the SIDEBAR RBAC contract #85), never inferred here.
import {
  dataGateOpen,
  foldOwning,
  foldTabsFor,
  itemOwning,
  itemPath,
  itemVisible,
  NO_STORE_FEATURES,
  normalizePath,
  orGoodsVisible,
  sectionGrant,
  storeFeatureOpen,
} from "../shell/navConfig";
import type { User } from "./AuthContext";

export function canAccess(
  pathname: string,
  user: User,
  goodsActions: readonly string[] = [],
  featuresOn: ReadonlySet<string> = NO_STORE_FEATURES,
): boolean {
  // The screen this URL belongs to, longest match first — so /money/vendor's
  // finance-only gate beats plain /money, and a mixed-case or trailing-slash
  // URL can't slip past into the default-allow branch.
  const screen = itemOwning(pathname);
  // A screen of a store feature that is off where this person works (ST-OPS-6)
  // is closed to everybody, break-glass included: the server refuses it too.
  if (screen && !storeFeatureOpen(screen, featuresOn)) return false;
  if (user.is_superuser) return true;
  // A folded page (#170) owns its URL outright - no menu entry points at it, so
  // `itemOwning` finds nothing and the default-allow branch would let anyone in.
  // It opens for whoever can see at least one of its tabs, and every tab carries
  // the gate of the screen it draws: a page with nothing on it is not a page.
  const fold = foldOwning(pathname);
  // A goods line drawn as a fold tab opens its own screen, never this page, so
  // it does not count as something here to see.
  if (fold) return foldTabsFor(fold, user, goodsActions, featuresOn).some((t) => !t.link);
  // Unknown path: no screen claims it, so there is nothing to protect. It is
  // either a legacy URL on its way to a redirect (resolved by the router, then
  // guarded at its new home) or a 404-ish stub.
  if (!screen) return true;
  // A goods-v1 screen opens on the session's own action grants and nothing
  // else (GSA-T02, `NavItem.goodsActions`): the same rule that draws its menu
  // line, so the URL and the sidebar cannot disagree. A legacy section never
  // opens it; the server still refuses any read or command the grant's scope
  // does not reach.
  if (screen.goodsActions)
    return itemVisible(screen, undefined, "", false, goodsActions, featuresOn);
  // A legacy screen a goods grant also opens (`NavItem.orGoodsActions`).
  if (orGoodsVisible(screen, goodsActions)) return true;
  // A heading the server never sends answers to the section that opens it.
  const held = user.capabilities?.[sectionGrant(screen.section)];
  if ((held ?? "none") === "none") return false;
  // A screen moved into this section keeps the gate its data answers to
  // (`NavItem.dataGate`, store operations ticket 10).
  if (!dataGateOpen(screen, user.capabilities, false)) return false;
  // A URL strictly under the item's own path — a document, not the list/create
  // screen itself — answers to `childMinCapability` where the item sets one
  // (#119: a PT stays readable below the rung its own making screen needs).
  const isChild = itemPath(screen) !== normalizePath(pathname);
  const gate =
    isChild && screen.childMinCapability
      ? { ...screen, minCapability: screen.childMinCapability }
      : screen;
  return itemVisible(gate, held, user.role?.code ?? "", false, [], featuresOn);
}
