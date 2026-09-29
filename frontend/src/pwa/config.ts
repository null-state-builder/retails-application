// What the installed counter is, and what its service worker may and may not do.
//
// In its own module rather than inline in `vite.config.ts` for one reason: the
// rule below - **the service worker never caches an API response** - is a money
// rule, and a money rule wants a test. A config object a test can import is the
// only seam a bundler plugin offers.
//
// The rule itself is the whole shape of this feature. The till is offline-first
// already: it bills from IndexedDB, not from HTTP, and everything it needs is in
// the local dataset before the network goes away. So the only thing a service
// worker has to add is the *app shell* - the HTML, the JavaScript and the CSS
// that let a Chrome with no line open the counter at all. Caching an API
// response on top of that would put a second, staler copy of the world beside
// the one the till reasons about, and a counter reading yesterday's stock from a
// cache while its own database says otherwise is a bug nobody could explain
// standing at a shop counter.
//
// Hence: precache the built shell by extension, never intercept `/api`, and no
// runtime caching at all.

import type { ManifestOptions, VitePWAOptions } from "vite-plugin-pwa";

/** Everything under this path is the server's answer, never the cache's. */
export const API_PREFIX = "/api";

/** What Chrome is told the counter is. Exported on its own because
 *  `VitePWAOptions["manifest"]` is `Partial<ManifestOptions> | false`, and a
 *  reader that has to rule out `false` first cannot say anything plainly. */
export const MANIFEST: Partial<ManifestOptions> = {
  name: "KDPS Operating System",
  short_name: "KDPS",
  description: "The counter and back office for KDPS Lifestyle.",
  // The app's own front door, not the counter's. A manifest is one file for the
  // whole product - a warehouse or head-office person installs the same one -
  // and the server already knows where each login belongs (`landing_page`).
  // Starting everybody on a Sell route would open a screen most of them do not
  // hold, and pinning `orientation` would lock a manager's tablet to landscape
  // to suit a counter nobody else uses.
  start_url: "/",
  scope: "/",
  display: "standalone",
  background_color: "#faf7f2",
  theme_color: "#faf7f2",
  lang: "en-IN",
  icons: [
    { src: "/icon-192.png", sizes: "192x192", type: "image/png", purpose: "any" },
    { src: "/icon-512.png", sizes: "512x512", type: "image/png", purpose: "any" },
    { src: "/icon-maskable-512.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
  ],
};

export const PWA_OPTIONS: Partial<VitePWAOptions> = {
  // Chrome takes the new worker at the next visit without asking. A counter is
  // reloaded between customers all day, and a prompt nobody understands ("a new
  // version is available") would be answered by whoever is nearest.
  registerType: "autoUpdate",
  // The generated worker registers itself, so nothing in `main.tsx` has to know
  // it exists - and a build with the plugin removed loses the worker rather than
  // throwing at a missing module.
  injectRegister: "auto",
  includeAssets: ["apple-touch-icon.png"],
  manifest: MANIFEST,
  workbox: {
    // The app shell, by extension. Nothing here can match an API response: these
    // are files Vite wrote into `dist`, and `/api` is not one of them.
    globPatterns: ["**/*.{js,css,html,svg,png,ico,woff,woff2}"],
    // A store's price list is 20,000 pieces of JavaScript-sized bundle; the
    // default 2 MB limit would silently drop the main chunk out of the precache
    // and leave an "installed" app that will not open offline.
    maximumFileSizeToCacheInBytes: 6 * 1024 * 1024,
    // Every in-app URL is served by index.html - except anything under /api,
    // which must reach the server or fail honestly. Without this denylist a
    // navigation-shaped API request would be answered with the app shell, and
    // the caller would parse HTML as JSON.
    navigateFallback: "/index.html",
    navigateFallbackDenylist: [new RegExp(`^${API_PREFIX}`)],
    // **No runtime caching, deliberately.** See the module comment: the till's
    // offline copy is IndexedDB, and a second cached copy of the same facts is
    // not redundancy, it is a disagreement.
    runtimeCaching: [],
    cleanupOutdatedCaches: true,
    clientsClaim: true,
  },
  devOptions: {
    // Off in `vite dev`: a service worker in front of HMR serves yesterday's
    // module graph and turns every "it did not update" into an hour. The worker
    // is a property of the built app, and that is where it is tested.
    enabled: false,
  },
};
