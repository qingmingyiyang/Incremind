import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

const __BUILD_DATE__ = new Date().toISOString().slice(0, 10);
const backendProxyOrigin = "http://127.0.0.1:8001";

export default defineConfig({
  // Electron 打包用 loadFile(file://) 加载 index.html。资源必须相对当前 HTML，
  // 不能解析为 file:///assets/...；该字段必须位于 Vite 顶层而不是 build 内。
  base: "./",
  plugins: [react()],
  define: {
    __BUILD_DATE__: JSON.stringify(__BUILD_DATE__),
    __BACKEND_PROXY_ORIGIN__: JSON.stringify(backendProxyOrigin),
  },
  resolve: {
    alias: {
      "@src": path.resolve(__dirname, "src"),
      "@testing-library/react": path.resolve(__dirname, "node_modules/@testing-library/react"),
      "axe-core": path.resolve(__dirname, "node_modules/axe-core"),
      react: path.resolve(__dirname, "node_modules/react"),
      "react-dom": path.resolve(__dirname, "node_modules/react-dom"),
    },
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes("node_modules")) {
            return undefined;
          }
          // React 核心：react、react-dom、scheduler 拆为独立 vendor-react chunk，
          // 提升长期缓存命中率（React 版本稳定，极少变动）
          if (
            id.includes("node_modules/react/") ||
            id.includes("node_modules/react-dom/") ||
            id.includes("node_modules/scheduler/")
          ) {
            return "vendor-react";
          }
          if (
            id.includes("react-markdown") ||
            id.includes("remark-gfm") ||
            id.includes("remark-") ||
            id.includes("rehype-") ||
            id.includes("unified") ||
            id.includes("micromark") ||
            id.includes("mdast") ||
            id.includes("hast") ||
            id.includes("unist") ||
            id.includes("vfile")
          ) {
            return "vendor-markdown";
          }
          if (id.includes("framer-motion")) {
            return "vendor-motion";
          }
          if (id.includes("lucide-react")) {
            return "vendor-icons";
          }
          return undefined;
        },
      },
    },
  },
  server: {
    host: "127.0.0.1",
    port: 4173,
    strictPort: true,
    fs: {
      allow: [path.resolve(__dirname, "../..")],
    },
    proxy: {
      "/api": {
        target: backendProxyOrigin,
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    testTimeout: 15000,
    pool: "forks",
    maxWorkers: 1,
    minWorkers: 1,
    include: ["../../tests/frontend/**/*.{test,spec}.{js,jsx,ts,tsx}"],
    setupFiles: "./src/testing/setupTests.js",
  },
});
