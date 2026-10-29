import { QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import { queryClient } from "./lib/queryCache";
import { initTheme } from "./theme/theme";
import "./index.css";
import "./theme/fonts.css";
import "./theme/counter.css";

initTheme();

const rootElement = document.getElementById("root");
if (!rootElement) {
  throw new Error("Root element #root not found");
}

createRoot(rootElement).render(
  <StrictMode>
    {/* The back office's in-memory server cache (app loading and caching plan,
        Phase 1). The till does not read from it. */}
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
);
