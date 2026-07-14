import { test, expect } from '@playwright/test'

// 操盘终端冒烟 (ECC e2e-testing)
// 验证: SPA 启动 → 路由到 /operator-terminal → 配额状态栏渲染 (后端不可用也显示 '?')
test.describe('操盘终端 OperatorTerminal', () => {
  test('应用启动并可路由到操盘终端', async ({ page }) => {
    // 基页可加载 (SPA 启动, 非白屏)
    await page.goto('/')
    await expect(page.locator('#root')).not.toBeEmpty()

    // 直接路由到操盘终端 (懒加载 chunk 挂载)
    await page.goto('/operator-terminal')
    await expect(
      page.getByRole('heading').first(),
      '操盘终端应有可见标题',
    ).toBeVisible({ timeout: 20000 })
  })

  test('API 配额状态栏渲染 (后端不可用回退 "?")', async ({ page }) => {
    await page.goto('/operator-terminal')
    // 配额状态栏标签是静态渲染, 后端挂了也显示 '?', 不白屏
    await expect(
      page.getByText('API配额', { exact: true }),
      '配额状态栏标签应渲染',
    ).toBeVisible({ timeout: 20000 })
  })
})
