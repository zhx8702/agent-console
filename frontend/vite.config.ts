import react from "@vitejs/plugin-react";
import { loadEnv } from "vite";
import { defineConfig } from "vitest/config";

export default defineConfig(({ mode }) => {
  const devApiTarget = loadEnv(mode, ".", "").VITE_DEV_API_TARGET || "http://127.0.0.1:8000";
  return {
    plugins: [react()],
    build: {
      rollupOptions: {
        output: {
          manualChunks(id) {
            if (id.indexOf("node_modules") !== -1) {
              return "vendor";
            }

            const pageMatch = id.match(/src\/pages\/([^/]+)\.tsx$/);
            if (pageMatch) {
              return `page-${pageMatch[1]}`;
            }
          }
        },
      },
    },
    server: {
      host: "0.0.0.0",
      port: 5173,
      proxy: {
        "/api": {
          target: devApiTarget,
          changeOrigin: true,
          cookiePathRewrite: "/api/",
          rewrite: (path) => path.replace(/^\/api(?=\/|$)/, "") || "/",
        },
        // FastAPI's generated Swagger page still references these absolute
        // support URLs. Application requests themselves always use /api.
        "/openapi.json": {
          target: devApiTarget,
          changeOrigin: true,
        },
        "/docs/oauth2-redirect": {
          target: devApiTarget,
          changeOrigin: true,
        },
      },
      watch: {
        usePolling: true,
        interval: 300,
      },
    },
    test: {
      environment: "jsdom",
      setupFiles: "./src/test/setup.ts",
      css: true,
      restoreMocks: true,
      exclude: ["e2e/**", "node_modules/**", "dist/**"],
    },
  };
});
