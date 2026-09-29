import { useEffect } from "react";
import { Navigate, Outlet, useLocation } from "react-router-dom";

import { KdpsLogo } from "../components/KdpsLogo";
import { roomAt } from "../routes";
import { AppShell } from "../shell/AppShell";
import { firstDestination, normalizePath, sidebarRows } from "../shell/navConfig";
import { TillProvider } from "../till/TillProvider";
import { AccessDenied } from "./AccessDenied";
import { useAuth } from "./AuthContext";
import { canAccess } from "./routeAccess";

export function ProtectedRoute() {
  const { user, session, loading, refreshSession, featuresOn } = useAuth();
  const location = useLocation();
  // Moving between screens re-reads the session (throttled), so the menu and
  // this guard follow current scoped assignments and policy.
  useEffect(() => {
    refreshSession();
  }, [location.pathname, refreshSession]);
  // The first frame after a refresh is the mark, not bare text: this is the
  // whole screen for as long as the session check takes.
  if (loading) {
    return (
      <div className="full-loader">
        <KdpsLogo variant="mark" height={44} title="" />
        <span>Loading KDPS…</span>
      </div>
    );
  }
  if (!user) return <Navigate to="/login" replace />;
  // GSA-T03/ticket 03A: a temporary-password session can reach only its own
  // session lifecycle and the change-password screen server-side
  // (`PASSWORD_CHANGE_REQUIRED`); send it there before drawing any other room.
  if (session?.user.must_change_password) return <Navigate to="/change-password" replace />;
  const actions = session?.display_actions ?? [];
  const allowed = canAccess(location.pathname, user, actions, featuresOn);
  // A person without Home in server navigation lands on their first authorised
  // screen rather than a blank Home and an otherwise usable menu.
  if (!allowed && normalizePath(location.pathname) === "/") {
    const destination = firstDestination(sidebarRows(user, actions, featuresOn));
    if (destination && normalizePath(destination) !== "/") {
      return <Navigate to={destination} replace />;
    }
  }
  const room = allowed ? roomAt(location.pathname) : undefined;
  const shell = (
    <AppShell {...(room ? { room } : {})}>{allowed ? <Outlet /> : <AccessDenied />}</AppShell>
  );
  // Billing's till must sit above the shell so the top bar can show its live
  // status. Other Sell screens keep their route-local provider.
  return room === "counter" ? <TillProvider>{shell}</TillProvider> : shell;
}
