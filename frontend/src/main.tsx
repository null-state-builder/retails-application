import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
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
    <App />
  </StrictMode>,
);
