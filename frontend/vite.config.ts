import { execSync } from "node:child_process";
import { readFileSync } from "node:fs";

import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";
import { PWA_OPTIONS } from "./src/pwa/config";

/** The build's identity, stamped on the back office's device cache so a new
 *  deploy never renders a copy an older build saved (caching plan Phase 2).
 *  `KDPS_BUILD_ID` wins (CI/deploy), then the commit, then the package version. */
function buildId(): string {
  const { version } = JSON.parse(readFileSync(new URL("./package.json", import.meta.url), "utf8"));
  if (process.env.KDPS_BUILD_ID) return `${version}+${process.env.KDPS_BUILD_ID}`;
  try {
    const sha = execSync("git rev-parse --short=12 HEAD", { stdio: ["ignore", "pipe", "ignore"] });
    return `${version}+${sha.toString().trim()}`;
  } catch {
    return String(version);
  }
}

export default defineConfig({
  plugins: [react(), VitePWA(PWA_OPTIONS)],
  define: { __KDPS_APP_VERSION__: JSON.stringify(buildId()) },
  envPrefix: ["VITE_", "REACT_APP_"],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
  preview: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
});
