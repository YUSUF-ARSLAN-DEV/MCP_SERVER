// playwright.config.ts - copy to the project root. Nothing here contacts any host.
import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  outputDir: "./evidence/_playwright",       // traces and failure screenshots
  timeout: 30_000,
  expect: { timeout: 7_000 },
  retries: 0,                                 // a flaky test should be fixed, not retried into a pass
  fullyParallel: true,
  reporter: [["list"], ["json", { outputFile: "evidence/results.json" }]],
  use: {
    trace: "on",                              // trace.zip per test: open with `npx playwright show-trace <file>`
    screenshot: "only-on-failure",
    video: "off",
    viewport: { width: 1280, height: 800 },
  },
});
