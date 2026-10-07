import react from "@vitejs/plugin-react";
import { readFileSync } from "node:fs";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [
    react(),
    {
      name: "training-runbook",
      resolveId(id) {
        if (id === "virtual:training-runbook") return "\0virtual:training-runbook";
      },
      load(id) {
        if (id === "\0virtual:training-runbook") {
          const source = readFileSync(
            new URL("../../docs/runbooks/sft-training.md", import.meta.url),
            "utf8",
          );
          return `export default ${JSON.stringify(source)}`;
        }
      },
    },
  ],
  build: { outDir: "dist", sourcemap: false },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
  },
});
