import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    // Local development only. Production is Vercel (frontend) + Render (backend): the build
    // calls the backend at VITE_API_BASE_URL, so neither this proxy nor a tunnel is used there.
    // Optional local debugging through a Cloudflare quick tunnel (*.trycloudflare.com); `vite
    // preview` inherits this and the /api proxy below, so the site and its API share one origin.
    allowedHosts: [".trycloudflare.com"],
    proxy: {
      "/api": {
        // Backend to proxy to; override with TALKSHOP_API (e.g. when :8000 is taken)
        target: process.env.TALKSHOP_API ?? "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});
