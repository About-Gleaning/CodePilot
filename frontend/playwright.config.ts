import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  globalSetup: './e2e/global-setup.ts',
  timeout: 30_000,
  workers: 1,
  use: {
    baseURL: 'https://127.0.0.1:5443',
    browserName: 'chromium',
    channel: 'chrome',
    ignoreHTTPSErrors: true,
    trace: 'retain-on-failure',
  },
  outputDir: 'output/playwright',
});
