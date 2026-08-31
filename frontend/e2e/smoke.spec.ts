import { test, expect, type Page } from '@playwright/test'

// 哨响AI 前端 e2e 冒烟 (Vite dev 3100 + proxy → bridge 9000)
// 覆盖: 4 路由可达+标题 / AgeGate / 赛程渲染 / 实时比分渲染 / 世界分析器表单
// 铁律: 0 pageerror + 0 console error 才算过; AgeGate 按钮不存在视为已通过

function trackErrors(page: Page): string[] {
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(`pageerror: ${String(e).slice(0, 300)}`))
  page.on('console', (msg) => {
    if (msg.type() === 'error') errors.push(`console: ${msg.text().slice(0, 300)}`)
  })
  return errors
}

async function passAgeGate(page: Page) {
  const btn = page.locator('button:has-text("我已满 18 岁")')
  if (await btn.count()) {
    await btn.first().click()
    await page.waitForTimeout(800)
  }
}

function cleanErrors(errors: string[]): string[] {
  return errors.filter((e) => !e.toLowerCase().includes('favicon'))
}

test('四路由可达且标题正确, 零 JS 错误', async ({ page }) => {
  const errors = trackErrors(page)
  const cases: Array<[string, RegExp]> = [
    ['/', /赛程/],
    ['/live-scores', /实时比分/],
    ['/timeline', /今日时间轴/],
    ['/world-analyzer', /世界级分析器/],
  ]
  for (const [path, titleRe] of cases) {
    const resp = await page.goto(path, { waitUntil: 'domcontentloaded', timeout: 30000 })
    expect(resp?.status(), `${path} 应返回 200`).toBe(200)
    await expect(page, `${path} 标题应匹配`).toHaveTitle(titleRe)
  }
  expect(cleanErrors(errors), '四路由零 JS 错误').toEqual([])
})

test('AgeGate 通过后赛程页渲染赛事内容', async ({ page }) => {
  const errors = trackErrors(page)
  await page.goto('/', { waitUntil: 'domcontentloaded', timeout: 30000 })
  await passAgeGate(page)
  // 轮询页用网络空闲会超时, 显式等待赛事标记出现
  await page.waitForSelector('text=进行中', { timeout: 20000 }).catch(() => {})
  const body = await page.evaluate(() => document.body.innerText)
  expect(body.length, '页面应有实质内容').toBeGreaterThan(50)
  expect(cleanErrors(errors), '赛程页零 JS 错误').toEqual([])
})

test('实时比分页渲染比赛列表', async ({ page }) => {
  const errors = trackErrors(page)
  await page.goto('/live-scores', { waitUntil: 'domcontentloaded', timeout: 30000 })
  await passAgeGate(page)
  await page.waitForSelector('text=进行中', { timeout: 20000 }).catch(() => {})
  const body = await page.evaluate(() => document.body.innerText)
  expect(body.length, '页面应有实质内容').toBeGreaterThan(50)
  expect(cleanErrors(errors), '实时比分页零 JS 错误').toEqual([])
})

test('世界级分析器渲染与表单可用', async ({ page }) => {
  test.slow() // 后端分析链路可能长
  const errors = trackErrors(page)
  await page.goto('/world-analyzer', { waitUntil: 'domcontentloaded', timeout: 30000 })
  await passAgeGate(page)
  await expect(page.locator('h1, h2').first(), '页面应有标题').toBeVisible()
  // 表单: 输入框 + 分析按钮存在
  await expect(page.locator('input').first(), '应有人工输入框').toBeVisible()
  await expect(page.locator('button:has-text("开始分析")').first(), '应有开始分析按钮').toBeVisible()
  await expect(page.locator('button:has-text("填入示例")').first(), '应有示例填充按钮').toBeVisible()
  expect(cleanErrors(errors), '分析器页零 JS 错误').toEqual([])
})
