import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  plugins: [
    // VitePWA 已禁用 (2026-08-05 修复反复缓存旧版本问题) — 后续如需PWA, 改用 server-side cache 控制
    react(),
  ],
  resolve: {
    alias: {
      "@": resolve(__dirname, "./src"),
    },
  },
  server: {
    host: '127.0.0.1',
    port: 3000,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:9000",
        changeOrigin: true,
      },
      "/health": {
        target: "http://127.0.0.1:9000",
        changeOrigin: true,
      },
      "/ws": {
        target: "ws://127.0.0.1:9000",
        ws: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
    cssCodeSplit: true,
    rollupOptions: {
      output: {
        manualChunks: (id) => {
          // vendor split — 借鉴bocai的分包粒度
          if (id.includes('node_modules/react') || id.includes('node_modules/react-dom') || id.includes('node_modules/react-router')) return 'vendor-react';
          if (id.includes('node_modules/@tanstack')) return 'vendor-query';
          if (id.includes('node_modules/framer-motion')) return 'vendor-motion';
          // page split — 每个页面独立chunk, 类bocai的按需加载 (Schedule 为首页, 留主包保首屏)
          if (id.includes('/pages/LiveScores')) return 'page-live';
          if (id.includes('/pages/Timeline')) return 'page-timeline';
          if (id.includes('/pages/WorldAnalyzer')) return 'page-world';
          // component split
          if (id.includes('/components/layout')) return 'comp-layout';
        },
      },
    },
  },
});
