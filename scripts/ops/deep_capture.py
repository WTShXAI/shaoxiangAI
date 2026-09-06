import io
import json, time, base64, gzip, re
from playwright.sync_api import sync_playwright

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
# 用之前 launch 返回的体育场馆 URL（已登录 token）
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

raw_cookies = [
    {"name": "X-API-UUID", "value": "3120eaa7-3e5f-4c84-8c45-32d3a461b5fc", "domain": "www.ylsvq5.vip", "path": "/"},
    {"name": "TRACK-HOUR", "value": "13", "domain": "www.ylsvq5.vip", "path": "/"},
    {"name": "X-API-TOKEN", "value": "d6381b31f1324c13f1a950009d11d08badee878fda9b7f0dfc01447a503e745f7c31887b86d85271efbe44c26f838cb9", "domain": "www.ylsvq5.vip", "path": "/"},
]

results = []
log = open(r"D:\Architecture\deep_capture.log", "w", encoding="utf-8")
def logi(*a):
    print(" ".join(str(x) for x in a), file=log, flush=True)

def try_decode(body):
    # 尝试 base64 + gzip (常见于这种站点)
    try:
        raw = base64.b64decode(body.strip())
        try:
            return gzip.decompress(raw).decode("utf-8", "ignore")
        except Exception:
            return raw.decode("utf-8", "ignore")
    except Exception:
        return None

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=CHROME, headless=False,
                                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
    ctx = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        ignore_https_errors=True)
    ctx.add_cookies(raw_cookies)
    page = ctx.new_page()

    def on_response(resp):
        try:
            u = resp.url
            if "realcpf.com" in u or "wnbtmel.com" in u or "realcpf" in u:
                ct = resp.headers.get("content-type", "")
                try:
                    body = resp.text()
                except Exception:
                    return
                logi(f"[RESP {resp.status}] {u}  ctype={ct} size={len(body)}")
                # attempt decode if looks encoded
                dec = try_decode(body) if ("wnbtmel" in u) else None
                if dec:
                    logi("   DECODED:", dec[:2000])
                    results.append({"url": u, "status": resp.status, "decoded": dec[:4000]})
                elif len(body) < 6000:
                    logi("   BODY:", body[:2000])
                    results.append({"url": u, "status": resp.status, "body": body[:4000]})
        except Exception as e:
            logi("err", e)

    page.on("response", on_response)
    logi("=== goto realcpf sports ===")
    page.goto(LAUNCH_URL, wait_until="networkidle", timeout=30000)
    time.sleep(5)
    logi("title:", page.title())
    try:
        logi("body text head:", page.inner_text("body")[:400])
    except Exception as e:
        logi("body err", e)

    # try to click into a sport / match to trigger odds API
    for sel in ["text=足球", "text=篮球", "text=早盘", "text=滚球", "text=今日",
                "[class*=sport-item]", "[class*=match]", "[class*=event]", "[class*=league]",
                "text=热门", "text=串关", "text=单关"]:
        try:
            els = page.locator(sel).all()
            if els:
                logi(f"CLICK '{sel}' ({len(els)})")
                els[0].click(timeout=3000)
                time.sleep(4)
        except Exception:
            pass

    time.sleep(8)
    logi(f"=== captured {len(results)} odds-related responses ===")
    with open(r"D:\Architecture\odds_apis.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    browser.close()
log.close()
print("DONE", len(results))
