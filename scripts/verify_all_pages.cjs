const { chromium } = require('D:/Architecture/frontend/node_modules/playwright');

const EDGE = 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const BASE = 'http://localhost:9000';
const OUT = 'D:/Architecture/_verify';

const PAGES = [
  { name: 'home', path: '/', timeout: 25000, waitFor: '#root', desc: '首页' },
  { name: 'analysis_center', path: '/analysis-center', timeout: 120000, waitFor: 'text=重新扫描', desc: '分析中心(含90s扫描)' },
  { name: 'league_schedule', path: '/league-schedule', timeout: 30000, waitFor: 'text=赛程', desc: '联赛赛程' },
  { name: 'live_scores', path: '/live-scores', timeout: 30000, waitFor: 'text=实时', desc: '实时比分' },
  { name: 'match_results', path: '/match-results', timeout: 30000, waitFor: 'text=结果', desc: '赛果查询' },
];

(async () => {
  const browser = await chromium.launch({
    executablePath: EDGE,
    headless: true,
    args: ['--no-sandbox', '--disable-gpu']
  });

  const results = [];
  const fatal = [];

  for (const pg of PAGES) {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const errors = [];
    page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
    page.on('pageerror', (e) => errors.push('PAGEERROR: ' + e.message));

    const start = Date.now();
    let ok = false;
    let detail = '';

    try {
      await page.goto(BASE + pg.path, { waitUntil: 'domcontentloaded', timeout: pg.timeout });
      // Wait for key element
      try {
        await page.waitForSelector(pg.waitFor, { timeout: pg.timeout });
        detail = `元素已渲染`;
      } catch (e) {
        detail = `未等到关键元素: ${e.message.slice(0,60)}`;
      }
      // Check #root has content
      const rootLen = await page.$eval('#root', el => el.innerHTML.length).catch(() => 0);
      if (rootLen > 100) detail += ` | root=${rootLen}字节`;
      else detail += ' | root内容异常';

      await page.screenshot({ path: OUT + '/shot_' + pg.name + '.png', fullPage: false });
      ok = rootLen > 100 && errors.length === 0;
    } catch (e) {
      detail = '导航失败: ' + e.message.slice(0, 80);
    }

    results.push({
      page: pg.desc,
      ok,
      time_ms: Date.now() - start,
      errors: errors.length,
      detail,
    });
    console.log((ok ? '✅' : '❌') + ` ${pg.desc} | ${detail} | ${errors.length} errors`);

    if (errors.length > 0 && !ok) fatal.push({ page: pg.desc, errors });
    await page.close();
  }

  await browser.close();

  // Summary
  const pass = results.filter(r => r.ok).length;
  const fail = results.length - pass;
  console.log(`\n=== 前端全页验证: ${pass}/${results.length} PASS, ${fail} FAIL ===`);
  if (fail > 0) {
    console.log('\nFAILED PAGES:');
    fatal.forEach(f => console.log(`  ${f.page}: ${f.errors.join('; ')}`));
    process.exit(1);
  }
  console.log('ALL PAGES PASS ✅');
})().catch((e) => { console.error('FATAL', e); process.exit(1); });
