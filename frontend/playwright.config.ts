import { defineConfig, devices } from '@playwright/test'

// 前端 e2e 冒烟: dev server(3100) + vite proxy → bridge 9000
// ⚠️ 必须用 dev server 而非 preview(4173): preview 无 proxy, /api 请求 404,
//    AgeGate 放行后赛事数据/轮询全挂 (2026-08-31 实测坑, 已固化)
export default defineConfig({
  testDir: './e2e',
  timeout: 30000,
  expect: { timeout: 15000 },
  fullyParallel: false, // 轮询页共享 dev server + 后端, 串行更稳
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: 'http://localhost:3100',
    trace: 'on-first-retry',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],
  webServer: {
    command: 'npm run dev -- --port 3100 --strictPort',
    url: 'http://localhost:3100',
    reuseExistingServer: true,
    timeout: 60000,
  },
})
