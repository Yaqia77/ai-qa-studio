import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// 开发期把 /api 代理到本地 FastAPI、/agent 代理到本地 qa-agent，生产由 Nginx 同域反代（免 CORS）
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      // AI 生成直连独立 Agent 服务（前端去 /agent 前缀 → /v1/*）；
      // 落库预览/提交仍走 /api/agent/*（平台侧校验闸门），两条链路职责分离
      "/agent": {
        target: "http://127.0.0.1:8100",
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/agent/, ""),
      },
    },
  },
});
