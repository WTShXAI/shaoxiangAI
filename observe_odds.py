import io
import json, time, base64, gzip
from playwright.sync_api import sync_playwright

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
def _gq_token():
    try:
        for _l in io.open(r"gq/.env", encoding="utf-8"):
            if _l.strip().startswith("GQ_REQUEST_ID="):
                return _l.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


LAUNCH_URL = ("https://user-pc-new.realcpf.com?token=" + _gq_token()
           + "&gr=b&tm=1&lg=zh&mk=0&stm=blue&skinColor=2")
cookies = [
    {"name":"X-API-UUID","value":"3120eaa7-3e5f-4c84-8c45-32d3a461b5fc","domain":"www.ylsvq5.vip","path":"/"},
    {"name":"TRACK-HOUR","value":"13","domain":"www.ylsvq5.vip","path":"/"},
    {"name":"X-API-TOKEN","value":"d6381b31f1324c13f1a950009d11d08badee878fda9b7f0dfc01447a503e745f7c31887b86d85271efbe44c26f838cb9","domain":"www.ylsvq5.vip","path":"/"},
]
seen_mids=set()
req_mids=[]
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path=CHROME, headless=False, args=["--no-sandbox","--disable-blink-features=AutomationControlled"])
    ctx=browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36", ignore_https_errors=True)
    ctx.add_cookies(cookies); page=ctx.new_page()
    def on_req(req):
        if 'getMatchBaseInfoByOddsPB' in req.url and 'structure' not in req.url:
            try: b=json.loads(req.post_data or '{}')
            except: return
            m=b.get('mid')
            if m: req_mids.append(str(m))
    def on_resp(resp):
        if 'getMatchBaseInfoByOddsPB' in resp.url and 'structure' not in resp.url:
            try: t=resp.text()
            except: return
            if len(t)<80: return
            try:
                j=json.loads(t); g=gzip.decompress(base64.b64decode(j['data'])); obj=json.loads(g.decode('utf-8'))
                d=obj.get('data') or []
                if d: seen_mids.add(str(d[0].get('mid')))
            except: pass
    page.on('request', on_req); page.on('response', on_resp)
    page.goto(LAUNCH_URL, wait_until="networkidle", timeout=30000); time.sleep(4)
    try: page.locator("div.item.ellipsis.yb-flex-center.button-bg-color", has_text="早盘").first.click(timeout=6000)
    except: pass
    time.sleep(5)
    # 滚动 40 次观察自发 odds 请求
    for i in range(40):
        page.evaluate("""()=>{const c=document.querySelector('.yb-match-list')||document.querySelector('.v-scroll-area')||document.querySelector('.q-scrollarea__container');if(c){c.scrollTop+=500;}else window.scrollBy(0,500);}""")
        time.sleep(0.8)
        if i%10==9: print(f"轮{i}: 请求mid数={len(req_mids)} 响应mid数={len(seen_mids)}", flush=True)
    print(f"最终: 请求mid={len(req_mids)} 去重={len(set(req_mids))} 响应mid={len(seen_mids)}", flush=True)
    print("响应mid样本:", sorted(seen_mids,key=lambda x:int(x))[:10], flush=True)
    browser.close()
json.dump({'req_mids':req_mids,'resp_mids':sorted(seen_mids,key=lambda x:int(x))}, open(r"D:\Architecture\observe_odds.json","w"), ensure_ascii=False, indent=2)
print("DONE")
