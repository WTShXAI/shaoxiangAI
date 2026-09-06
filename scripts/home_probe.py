"""Deep probe of Home (LeagueSchedule) route.
Uses the same sync_playwright pattern as frontend_audit.py."""
import json, time, sys
from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:9000/"
SETTLE_MS = 18000

def main():
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = browser.contexts[0]
        page = ctx.new_page()

        responses = []
        page.on("response", lambda r: responses.append({"status": r.status, "url": r.url, "type": r.request.resource_type}))
        requestfailed = []
        page.on("requestfailed", lambda r: requestfailed.append({"failure": r.failure, "url": r.url}))
        console_msgs = []
        page.on("console", lambda m: console_msgs.append({"type": m.type, "text": m.text}))

        cdp = ctx.new_cdp_session(page)
        cdp.send("Network.enable")
        cdp.send("Network.clearBrowserCache")
        cdp.send("Network.clearBrowserCookies")

        t0 = time.time()
        page.goto(URL, wait_until="domcontentloaded")
        print(f"[probe] goto @ {time.time()-t0:.1f}s")

        deadline = time.time() + SETTLE_MS / 1000
        last = -1
        while time.time() < deadline:
            state = page.evaluate("""() => {
                const chips = document.querySelectorAll('span.cursor-pointer').length;
                const analyses = Array.from(document.querySelectorAll('button')).filter(b => b.textContent.trim() === '分析').length;
                const badge = Array.from(document.querySelectorAll('span')).map(s => s.textContent||'').find(t => /个联赛/.test(t)) || '';
                const empty = Array.from(document.querySelectorAll('p,div')).filter(el => /无赛程|暂无进行|加载中/.test(el.textContent||'')).map(el => el.textContent.trim().slice(0,60));
                return {chips, analyses, badge, empty: empty.slice(0,3)};
            }""")
            if state["chips"] != last or state["analyses"] > 0:
                print(f"[probe] t={time.time()-t0:.1f}s chips={state['chips']} analyses={state['analyses']} badge='{state['badge']}' empty={state['empty']}")
                last = state["chips"]
            if state["analyses"] > 5 or state["chips"] > 20:
                print(f"[probe] POPULATED @ {time.time()-t0:.1f}s")
                break
            time.sleep(0.5)

        final = page.evaluate("""() => {
            const body = document.body.innerText || '';
            const chips = document.querySelectorAll('span.cursor-pointer').length;
            const analyses = Array.from(document.querySelectorAll('button')).filter(b => b.textContent.trim() === '分析').length;
            const badge = Array.from(document.querySelectorAll('span')).map(s => s.textContent||'').find(t => /个联赛/.test(t)) || '';
            return {bodyLen: body.length, chips, analyses, badge, bodyTail: body.slice(-300)};
        }""")

        page.screenshot(path=r"D:\Architecture\.edge_agent_profile\home_probe.png", full_page=False)

        api_reqs = [r for r in responses if "/api/" in r.get("url","")]
        api_failures = [r for r in api_reqs if r["status"] >= 400]
        # unique api endpoints hit with their final status
        seen = {}
        for r in api_reqs:
            path = r["url"].split("?")[0].split("//",1)[-1].split("/",1)[-1]
            path = "/" + path
            seen[path] = r["status"]

        result = {
            "final": final,
            "elapsed_sec": round(time.time()-t0, 1),
            "total_api_responses": len(api_reqs),
            "api_failures": api_failures[:10],
            "requestfailed": requestfailed[:5],
            "console_errors": [c for c in console_msgs if c["type"] in ("error",)][:10],
            "console_warnings": [c for c in console_msgs if c["type"] == "warning"][:5],
            "console_total": len(console_msgs),
            "api_endpoints_hit": seen,
        }
        print("\n=== RESULT ===")
        print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
