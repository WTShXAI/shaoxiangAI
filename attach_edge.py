import json, time, base64, gzip, zlib, threading
from playwright.sync_api import sync_playwright
def log(*a): print(" ".join(str(x) for x in a), flush=True)

ODDS = "getMatchBaseInfoByOddsPB"
OUT = r"D:\Architecture\early_cs_all.json"
MIDS_OUT = r"D:\Architecture\early_real_mids.json"

def decode_data(t):
    try: j = json.loads(t)
    except: return None
    if not isinstance(j, dict) or j.get('code') != '0000000' or not j.get('data'): return None
    raw = base64.b64decode(j['data'])
    for fn in (lambda r: gzip.decompress(r), lambda r: zlib.decompress(r, -zlib.MAX_WBITS), lambda r: zlib.decompress(r)):
        try: return json.loads(fn(raw).decode('utf-8'))
        except: pass
    return None

def extract_cs(obj):
    cs = []
    for x in (obj.get('playData') or []):
        if not isinstance(x, dict): continue
        if x.get('hpn') == '全场波胆' or str(x.get('topKey')) == '7' or str(x.get('hpid')) == '7':
            for line in (x.get('hl') or []):
                if not isinstance(line, dict): continue
                for o in (line.get('ol') or []):
                    if not isinstance(o, dict): continue
                    sc = o.get('ot') or o.get('otv'); ov = o.get('ov')
                    if sc is not None and ov is not None:
                        cs.append({'score': str(sc).replace(' ', ''), 'odds': ov})
    return cs

with sync_playwright() as p:
    # 尝试连接正在运行的 Edge
    browser = None
    for port in [9222, 9223, 9221, 9229, 9220]:
        try:
            browser = p.chromium.connect_over_cdp(f"http://localhost:{port}")
            log(f"连接成功: 端口 {port}")
            break
        except Exception as e:
            log(f"端口 {port} 失败: {str(e)[:60]}")
    if not browser:
        log("所有端口都连不上，尝试查找 Edge 的 CDP pipe...")
        # 尝试直接用 ws 端点
        try:
            browser = p.chromium.connect_over_cdp("ws://127.0.0.1:9222/devtools/browser")
            log("WS 连接成功")
        except Exception as e:
            log("WS 也失败:", str(e)[:100])
            raise SystemExit(1)

    # 获取已有页面
    pages = browser.contexts[0].pages if browser.contexts else []
    log(f"已有页面数: {len(pages)}")
    for pg in pages:
        log(f"  页面: {pg.url[:120]}")
    
    # 找一个在 realcpf 域名下的页面
    page = None
    for pg in pages:
        if "realcpf" in pg.url or "ylsvq5" in pg.url:
            page = pg
            log(f"找到目标页面: {pg.url[:120]}")
            break
    if not page and pages:
        page = pages[0]
        log(f"使用第一个页面: {page.url[:120]}")
    
    if not page:
        # 新建页面
        page = browser.contexts[0].new_page()
        log("新建页面")
    
    # 检查是否登录（看 cookie）
    cookies = page.context.cookies()
    log(f"当前 cookies: {len(cookies)} 个")
    for c in cookies:
        if 'token' in c['name'].lower() or 'api' in c['name'].lower():
            log(f"  {c['name']}: {c['value'][:30]}... domain={c['domain']}")
    
    # 如果当前不在体育站，导航过去
    if "realcpf" not in page.url:
        page.goto("https://user-pc-new.realcpf.com/#/home", wait_until="networkidle", timeout=30000)
        time.sleep(5)
        log("已导航到体育站, URL:", page.url[:100])
    
    # 点"早盘"
    for sel in ["早盘", "早盘赛事", "Early", "早"]:
        try:
            loc = page.locator("div, span, a, button", has_text=sel).first
            if loc.count() and loc.is_visible(timeout=2000):
                loc.click(timeout=2000); log("点击:", sel); break
        except Exception as e:
            log("点击失败", sel, repr(e))
    time.sleep(4)
    
    # 展开全部赛事
    try:
        btn = page.locator("div, span, button", has_text="展开全部").first
        if btn.count() and btn.is_visible(timeout=2000):
            btn.click(timeout=2000); log("点击展开全部"); time.sleep(2)
    except Exception as e:
        log("无展开全部按钮", repr(e))
    
    # 点击赛事头
    league_headers = page.locator("div[class*='league']")
    lc = league_headers.count()
    log("赛事头:", lc)
    for i in range(lc):
        try: league_headers.nth(i).click(timeout=300)
        except: pass
    time.sleep(2)
    
    # 滚动收集 mid
    seen = set()
    page.evaluate("""() => {
        let best = null, bestDiff = 0;
        document.querySelectorAll('*').forEach(el => {
            try { const d = el.scrollHeight - el.clientHeight; if (d > 200 && el.clientHeight > 100 && d > bestDiff) { bestDiff = d; best = el; } }
            catch(e) {}
        });
        if (best) best.scrollTop = 0;
    }""")
    prev = 0; stall = 0
    for i in range(200):
        page.evaluate("""() => {
            let best = null, bestDiff = 0;
            document.querySelectorAll('*').forEach(el => {
                try { const d = el.scrollHeight - el.clientHeight; if (d > 200 && el.clientHeight > 100 && d > bestDiff) { bestDiff = d; best = el; } }
                catch(e) {}
            });
            if (best) best.scrollTop = best.scrollHeight;
            else window.scrollTo(0, document.body.scrollHeight);
        }""")
        time.sleep(0.3)
        ids = page.evaluate("""() => Array.from(document.querySelectorAll('div[id^="mid-"]')).map(e => e.id.replace('mid-',''))""")
        for m in ids: seen.add(m)
        if len(seen) == prev: stall += 1
        else: stall = 0; prev = len(seen)
        if stall >= 10:
            time.sleep(2)
            ids2 = page.evaluate("""() => Array.from(document.querySelectorAll('div[id^="mid-"]')).map(e => e.id.replace('mid-',''))""")
            for m in ids2: seen.add(m)
            if len(seen) == prev: log("触底"); break
            else: stall = 0
        if i % 15 == 0: log(f"scroll{i}, 卡片={len(seen)}")
    log(f"最终 mid: {len(seen)}")
    json.dump(sorted(seen, key=lambda x:-int(x)), open(MIDS_OUT,"w",encoding="utf-8"), ensure_ascii=False, indent=2)
    
    # 拦截 odds API 并抓波胆
    results = []; done = set(); lock = threading.Lock()
    q = [m for m in sorted(seen, key=lambda x:-int(x))]
    log(f"待抓波胆: {len(q)}")
    
    def handler(route, request):
        if ODDS not in request.url:
            try: route.continue_()
            except: pass
            return
        try: orig = json.loads(request.post_data or '{}')
        except: orig = {}
        cuid = orig.get('cuid','526002076777845380'); euid = orig.get('euid','3020101')
        while True:
            with lock:
                if not q: break
                mid = q.pop(0)
            body = {"cuid":cuid,"cos":0,"orpt":0,"euid":euid,"mid":mid,"mcid":0,"newUser":0}
            obj = None
            for attempt in range(8):
                try:
                    r = route.fetch(post_data=json.dumps(body)); t = r.text()
                    jc = ''
                    try: jc = json.loads(t).get('code','')
                    except: pass
                    if '0401038' in str(jc): time.sleep(15); continue
                    obj = decode_data(t)
                    if obj: break
                    else: time.sleep(2)
                except: time.sleep(2)
            if obj:
                with lock:
                    for m in (obj.get('data') or []):
                        if not isinstance(m,dict) or not m.get('mid'): continue
                        mid2 = str(m['mid']); done.add(mid2)
                        cs = extract_cs(obj)
                        status = m.get('mststi')
                        if cs and status in (0, None, 1):
                            results.append({'mid':mid2,'league':m.get('tnjc') or m.get('csna') or m.get('tn'),
                                'home':m.get('mhn') or m.get('frmhn'),'away':m.get('man') or m.get('frman'),
                                'status':status,'cs_count':len(cs),'cs':cs})
                            log(f"[OK 未开始+波胆] {mid2} 波胆={len(cs)} 累计={len(results)}")
                        elif cs: log(f"[跳过-已开赛] {mid2}")
                        else: log(f"[跳过-无波胆] {mid2}")
                    json.dump(results, open(OUT,"w",encoding="utf-8"), ensure_ascii=False, indent=2)
            time.sleep(1.2)
        try: route.continue_()
        except: pass
    
    page.route("**/getMatchBaseInfoByOddsPB**", handler)
    try:
        page.locator("div[id^='mid-']").first.click(timeout=6000)
        log("点击首场比赛")
    except Exception as e: log("点击失败", repr(e))
    start = time.time()
    while q and time.time()-start < 260:
        time.sleep(3)
    with lock: json.dump(results, open(OUT,"w",encoding="utf-8"), ensure_ascii=False, indent=2)
    log(f"完成: 未开始含波胆={len(results)} 剩余={len(q)}")
    browser.close()
log("DONE")
