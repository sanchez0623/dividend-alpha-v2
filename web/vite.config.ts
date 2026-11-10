import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// ★ 开发期把 API_PREFIX 代理到后端；前缀与后端 DA_API_PREFIX 对齐
const apiPrefix = process.env.VITE_API_PREFIX || "/da/v1";
// ★ 前端 dev 端口（默认 5178，避免与常见 5173 冲突），可用 VITE_PORT 覆盖
const frontPort = Number(process.env.VITE_PORT || 5178);

export default defineConfig({
  plugins: [react()],
  server: {
    port: frontPort,
    proxy: {
      [apiPrefix]: {
        target: process.env.VITE_API_TARGET || "http://127.0.0.1:8100",
        changeOrigin: true,
        ws: true,
      },
    },
  },
  build: { outDir: "dist" },
});
