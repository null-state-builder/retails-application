import { BrowserRouter, Route, Routes } from "react-router-dom";

import { AuthProvider } from "./auth/AuthContext";
import { ProtectedRoute } from "./auth/ProtectedRoute";
import { ChangePassword } from "./pages/ChangePassword";
import { Login } from "./pages/Login";
import CustomerDisplayPage from "./pages/sell/CustomerDisplay";
import { PROTECTED_ROUTES } from "./routes";
import { LegacyRedirect } from "./shell/LegacyRedirect";

export function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Routes>
          <Route path="/login" element={<Login />} />
          {/* GSA-T03/ticket 03A: outside `ProtectedRoute` - a temporary-password
              session is signed in but confined to this one room server-side. */}
          <Route path="/change-password" element={<ChangePassword />} />
          {/* Ticket 09: the customer display, the till's second window. Outside
              `ProtectedRoute` so it wears no menu and no staff controls; the
              window asks the server itself whether this signed-in till login
              may show its store's display, and refuses otherwise. */}
          <Route path="/sell/display" element={<CustomerDisplayPage />} />
          <Route element={<ProtectedRoute />}>
            {PROTECTED_ROUTES.map((r) => (
              <Route key={r.id} path={r.path} element={r.element} />
            ))}
            {/* Anything the route table doesn't claim. */}
            <Route path="*" element={<LegacyRedirect />} />
          </Route>
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  );
}
