import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The built static files are served by the same FastAPI app in production,
// so all API calls are relative paths (no base URL). In dev, proxy /api to
// the backend so `npm run dev` works against a locally running server.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: "http://localhost:8080",
        changeOrigin: true,
        // Also proxies /api/ws's websocket upgrade - only affects requests
        // that actually carry an Upgrade header, so plain REST calls
        // through this same entry are unaffected.
        ws: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
