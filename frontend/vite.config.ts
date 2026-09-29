import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";
import { PWA_OPTIONS } from "./src/pwa/config";

export default defineConfig({
  plugins: [react(), VitePWA(PWA_OPTIONS)],
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
