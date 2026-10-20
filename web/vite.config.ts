import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// base: '/web-assets/' - one shared static prefix for every migrated page's
// JS/CSS, not tied to any single page slug. This is a single multi-page SPA
// (react-router routes for /chronology, /presence, etc., all in one bundle)
// serving the SAME index.html under every migrated slug - see
// src/webui_api.py's _serve_spa_index() and app.py's TabRoutingMiddleware,
// which forwards "web-assets" to the FastAPI app the same way "app-api" is.
export default defineConfig({
  base: "/web-assets/",
  plugins: [react(), tailwindcss()],
  build: {
    outDir: "dist",
  },
});
