import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { VitePWA } from "vite-plugin-pwa";
import path from "path";

const backendTarget = process.env.VITE_API_URL || "http://localhost:8001";
const backendProxy = {
  "/api": backendTarget,
  "/_allauth": backendTarget,
  "/dashboard": backendTarget,
  "/admin": backendTarget,
  "/accounts": backendTarget,
  "/static": backendTarget,
  "/media": backendTarget,
};

export default defineConfig({
  build: {
    rolldownOptions: {
      output: {
        codeSplitting: {
          groups: [
            {
              name: "portal-shared",
              // These modules already share the initial import closure. Keep
              // them together to avoid cross-chunk imports in every role page.
              test: /node_modules\/(?:lucide-react|@base-ui)\/|src\/components\/portal\//,
            },
          ],
        },
      },
    },
  },
  plugins: [
    react(),
    tailwindcss(),
    VitePWA({
      strategies: "injectManifest",
      srcDir: "src",
      filename: "sw.ts",
      registerType: "autoUpdate",
      injectManifest: {
        globPatterns: ["**/*.{js,css,html,ico,png,svg,woff2}"],
      },
      manifest: {
        name: "CRM Jaguar",
        short_name: "Jaguar",
        lang: "ru",
        id: "/app",
        start_url: "/app",
        scope: "/",
        display: "standalone",
        theme_color: "#000000",
        icons: [
          { src: "/icon-192.png", sizes: "192x192", type: "image/png" },
          { src: "/icon-512.png", sizes: "512x512", type: "image/png" },
        ],
      },
    }),
  ],
  resolve: {
    alias: { "@": path.resolve(__dirname, "src") },
  },
  server: {
    port: 5173,
    host: true,
    allowedHosts: true,
    proxy: backendProxy,
  },
  preview: {
    proxy: backendProxy,
  },
});
