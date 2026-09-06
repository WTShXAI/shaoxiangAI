import json, sys, time
from playwright.sync_api import sync_playwright

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
URL = "https://www.ylsvq5.vip:9003/game/sport/ob?enName=YBTY"

raw_cookies = [
    {"name": "X-API-UUID", "value": "3120eaa7-3e5f-4c84-8c45-32d3a461b5fc", "domain": "www.ylsvq5.vip", "path": "/"},
    {"name": "TRACK-HOUR", "value": "13", "domain": "www.ylsvq5.vip", "path": "/"},
    {"name": "X-API-TOKEN", "value": "d6381b31f1324c13f1a950009d11d08badee878fda9b7f0dfc01447a503e745f7c31887b86d85271efbe44c26f838cb9", "domain": "www.ylsvq5.vip", "path": "/"},
]

captured = []
log = open(r"D:\Architecture\auto_capture.log", "w", encoding="utf-8")

def logi(*a):
    s = " ".join(str(x) for x in a)
    print(s, file=log, flush=True)

with sync_playwright() as p:
    browser = p.chromium.launch(
        executable_path=CHROME,
        headless=False,
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    )
    ctx = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        ignore_https_errors=True,
    )
    # inject cookies (need to be on the domain first; set via context after adding a blank page)
    page = ctx.new_page()
    # navigate to domain root to set cookies scope
    page.goto("https://www.ylsvq5.vip:9003/", wait_until="domcontentloaded", timeout=20000)
    ctx.add_cookies(raw_cookies)

    def on_response(resp):
        try:
            u = resp.url
            ct = resp.headers.get("content-type", "")
            if any(k in u.lower() for k in ["api", "odds", "sport", "game", "bet", "match", "play", "ob"]):
                if "text" in ct or "json" in ct or "javascript" in ct:
                    try:
                        body = resp.text()
                    except Exception:
                        body = ""
                    size = len(body)
                    logi(f"[RESP {resp.status}] {u}  ctype={ct} size={size}")
                    if size < 8000 and size > 0:
                        logi("   BODY:", body[:1500])
                    captured.append({"url": u, "status": resp.status, "size": size})
        except Exception as e:
            logi("   on_response err:", e)

    page.on("response", on_response)

    logi("=== goto odds page ===")
    page.goto(URL, wait_until="networkidle", timeout=30000)
    time.sleep(4)
    logi("=== page title:", page.title())
    # dump some visible text to know login state
    try:
        txt = page.inner_text("body")
        logi("=== body text (first 600):", txt[:600])
    except Exception as e:
        logi("body text err:", e)

    # try clicking elements that may open a match / odds board
    selectors_tried = []
    for sel in ["text=足球", "text=篮球", "text=体育", "text=投注", "text=盘口", "text=赛事",
                ".match-item", ".event-item", ".match-list li", "[class*=match]", "[class*=event]",
                "text=滚球", "text=今日", "text=早盘"]:
        try:
            els = page.locator(sel).all()
            if els:
                logi(f"FOUND selector '{sel}' -> {len(els)} els; clicking first")
                els[0].click(timeout=3000)
                time.sleep(3)
                selectors_tried.append(sel)
        except Exception:
            pass

    time.sleep(5)
    logi(f"=== captured {len(captured)} candidate requests ===")
    # save captured list
    with open(r"D:\Architecture\captured_apis.json", "w", encoding="utf-8") as f:
        json.dump(captured, f, ensure_ascii=False, indent=2)

    browser.close()
log.close()
print("DONE captured:", len(captured))
