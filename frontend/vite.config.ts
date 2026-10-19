import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        // Backend to proxy to; override with TALKSHOP_API (e.g. when :8000 is taken)
        target: process.env.TALKSHOP_API ?? "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});
