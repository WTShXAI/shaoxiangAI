import { defineConfig, devices } from '@playwright/test'

// ECC e2e-testing 对齐: 操盘终端 (OperatorTerminal) 生产级冒烟
// webServer 自动拉起 vite preview (服务 dist/), 端口 4173
export default defineConfig({
  testDir: './e2e',
  timeout: 30000,
  expect: { timeout: 15000 },
  fullyParallel: true,
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: 'http://localhost:4173',
    trace: 'on-first-retry',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],
  webServer: {
    command: 'npm run preview -- --port 4173 --strictPort',
    url: 'http://localhost:4173',
    reuseExistingServer: true,
    timeout: 60000,
  },
})
