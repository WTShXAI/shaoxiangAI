(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const byText = t => [...document.querySelectorAll('*')].find(e => e.textContent.trim() === t && e.children.length === 0);
  const nextBtn = () => [...document.querySelectorAll('button')].find(b => /Next page/i.test(b.textContent) && !b.disabled);
  const firstRowId = () => { const r = document.querySelector('table tbody tr'); return r ? r.querySelector('td')?.innerText : null; };
  const collect = () => Array.from(document.querySelectorAll('table tbody tr')).map(r => Array.from(r.querySelectorAll('td')).map(td => td.innerText.replace(/\n+/g, ' ').trim()));

  // ensure 30天内 range
  const f = byText('30天内'); if (f) f.click();
  await sleep(1200);

  const all = [];
  let prev = null;
  for (let p = 0; p < 15; p++) {
    // wait for render (first row changes or stable)
    let waited = 0;
    while (waited < 4000) {
      const cur = firstRowId();
      if (cur && cur !== prev) break;
      await sleep(300); waited += 300;
    }
    const rows = collect();
    if (rows.length === 0) break;
    all.push(...rows);
    prev = firstRowId();
    const n = nextBtn();
    if (!n) break;
    n.click();
    await sleep(600);
  }
  const t = document.body.innerText;
  return JSON.stringify({
    rows: all,
    total: (t.match(/总投注单数：\s*(\d+)/) || [])[1],
    amt: (t.match(/总投注额：\s*([\d.,]+)/) || [])[1],
    win: (t.match(/总输赢：\s*([-\d.,]+)/) || [])[1]
  });
})()
