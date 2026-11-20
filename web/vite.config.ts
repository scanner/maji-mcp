// Builds the web client into dist/, which the control plane serves.
//
// Assets are never inlined as data: URIs, so fonts and images stay files the
// control plane's content security policy allows ("font-src 'self'"). In
// development, requests to the API go to a control plane on CONTROL (default
// http://localhost:8768, the demo); passkeys only work at the origin the control
// plane is configured with, so sign-in is tried against the built client it serves.

import { fileURLToPath, URL } from "node:url";

import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  build: {
    assetsInlineLimit: 0,
    sourcemap: false,
  },
  server: {
    proxy: { "/api": process.env.CONTROL ?? "http://localhost:8768" },
  },
  test: {
    environment: "jsdom",
    include: ["tests/**/*.test.ts"],
  },
});
