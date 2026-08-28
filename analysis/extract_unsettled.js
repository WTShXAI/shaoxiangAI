(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const byText = t => [...document.querySelectorAll('*')].find(e => e.textContent.trim() === t && e.children.length === 0);
  const clickTab = t => { const e = byText(t); if (e) { e.click(); return true; } return false; };
  const nextBtn = () => [...document.querySelectorAll('button')].find(b => /Next page/i.test(b.textContent) && !b.disabled);
  const collect = () => Array.from(document.querySelectorAll('table tbody tr')).map(r => Array.from(r.querySelectorAll('td')).map(td => td.innerText.replace(/\n+/g, ' ').trim()));
  const summary = () => {
    const t = document.body.innerText;
    return {
      total: (t.match(/总投注单数：\s*(\d+)/) || [])[1],
      amt: (t.match(/总投注额：\s*([\d.,]+)/) || [])[1],
      win: (t.match(/总输赢：\s*([-\d.,]+)/) || [])[1]
    };
  };
  const out = {};
  // 未结算
  clickTab('未结算注单'); await sleep(1000);
  out.unsettled = { summary: summary(), rows: collect() };
  // 彩票 tab then settled
  const lottery = byText('彩票'); if (lottery) lottery.click(); await sleep(800);
  clickTab('已结算注单'); await sleep(1000);
  out.lottery_settled = { summary: summary(), rows: collect() };
  const lottery_un = byText('彩票'); if (lottery_un) lottery_un.click(); await sleep(800);
  clickTab('未结算注单'); await sleep(1000);
  out.lottery_unsettled = { summary: summary(), rows: collect() };
  return JSON.stringify(out);
})()
