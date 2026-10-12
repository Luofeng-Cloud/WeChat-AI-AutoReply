import { defineConfig, externalizeDepsPlugin } from "electron-vite";
import { resolve } from "node:path";

export default defineConfig({
  main: {
    plugins: [externalizeDepsPlugin()],
    build: {
      outDir: "dist/main",
      lib: { entry: resolve(__dirname, "src/main/index.ts"), formats: ["cjs"] },
      rollupOptions: { output: { entryFileNames: "index.js" } },
    },
  },
  preload: {
    plugins: [externalizeDepsPlugin()],
    build: {
      outDir: "dist/preload",
      lib: { entry: resolve(__dirname, "src/preload/index.ts"), formats: ["cjs"] },
      rollupOptions: { output: { entryFileNames: "index.js" } },
    },
  },
  renderer: {
    root: resolve(__dirname, "src/renderer"),
    build: {
      outDir: resolve(__dirname, "dist/renderer"),
      emptyOutDir: true,
      rollupOptions: { input: resolve(__dirname, "src/renderer/index.html") },
    },
  },
});
