import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Proxy targets are compose/host wiring for the Vite *server*, not VITE_ client knobs.
// The only allowed VITE_ knob is VITE_API_BASE_URL (Order API origin). Leave it unset
// so the browser hits same-origin `/snapshot` and this proxy forwards it.
const api = process.env.API_PROXY_TARGET ?? "http://127.0.0.1:8000";
const loadgen = process.env.LOADGEN_PROXY_TARGET ?? "http://127.0.0.1:8090";
const rsim = process.env.RSIM_PROXY_TARGET ?? "http://127.0.0.1:8081";
const csim = process.env.CSIM_PROXY_TARGET ?? "http://127.0.0.1:8082";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 5173,
    strictPort: true,
    proxy: {
      "/snapshot": { target: api, changeOrigin: true },
      "/work-items": { target: api, changeOrigin: true },
      "/loadgen": {
        target: loadgen,
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/loadgen/, ""),
      },
      "/rsim": {
        target: rsim,
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/rsim/, ""),
      },
      "/csim": {
        target: csim,
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/csim/, ""),
      },
    },
  },
});
