// ═══════════════════════════════════════════════════════════════════════════
// Playwright Configuration — Paper Trading Dashboard E2E Tests
// ═══════════════════════════════════════════════════════════════════════════

const { defineConfig, devices } = require('@playwright/test');

module.exports = defineConfig({
  testDir: './tests/e2e',
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: process.env.CI
    ? [['html', { outputFolder: 'test-results/e2e-report' }], ['github']]
    : [['html', { outputFolder: 'test-results/e2e-report' }], ['list']],

  timeout: 45_000,
  expect: {
    timeout: 15_000,
  },

  use: {
    baseURL: process.env.DASHBOARD_URL || 'http://localhost:5002',
    trace: process.env.CI ? 'on-first-retry' : 'on',
    screenshot: process.env.CI ? 'only-on-failure' : 'on',
    video: process.env.CI ? 'retain-on-failure' : 'off',
  },

  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});