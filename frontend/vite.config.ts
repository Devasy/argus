import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Override with VITE_DEV_BACKEND=http://host:port to point the dev server at
// a remote backend instead of a locally-running one.
const backend = process.env.VITE_DEV_BACKEND ?? "http://localhost:8100";
const backendWs = backend.replace(/^http/, "ws");

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: backend,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
      "/ws": {
        target: backendWs,
        ws: true,
      },
    },
  },
});
