import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": resolve(__dirname, "./src"),
    },
  },
  server: {
    port: 3000,
    proxy: {
      "/api": {
        target: "http://localhost:9000",
        changeOrigin: true,
      },
      "/ws": {
        target: "ws://localhost:9000",
        ws: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
