"""In-page fetch test: trigger getFixtures directly from browser context
and observe the response. Also check global app state."""
import json, time
from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:9000/"

def main():
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = browser.contexts[0]
        page = ctx.new_page()
        page.goto(URL, wait_until="networkidle")
        page.wait_for_timeout(3000)

        # Direct fetch test from page context (same origin)
        result = page.evaluate("""async () => {
            const SK = '球会友谊赛';
            const url = '/api/leagues/' + encodeURIComponent(SK) + '/fixtures';
            try {
                const t0 = performance.now();
                const r = await fetch(url, { credentials: 'same-origin' });
                const dt = performance.now() - t0;
                const text = await r.text();
                let parsed = null;
                try { parsed = JSON.parse(text); } catch {}
                const fixtures = parsed?.data?.fixtures || parsed?.fixtures || [];
                return { ok: r.ok, status: r.status, url, dtMs: Math.round(dt), bodyLen: text.length, fixturesCount: fixtures.length, sample: fixtures.slice(0,1).map(f => ({home:f.home, away:f.away})) };
            } catch (e) {
                return { error: String(e), url };
            }
        }""")
        print("DIRECT FETCH (球会友谊赛):")
        print(json.dumps(result, ensure_ascii=False, indent=2))

        # Also try a second league with no spaces
        result2 = page.evaluate("""async () => {
            const SK = '美国职业大联盟';
            const url = '/api/leagues/' + encodeURIComponent(SK) + '/fixtures';
            try {
                const r = await fetch(url, { credentials: 'same-origin' });
                const text = await r.text();
                const parsed = JSON.parse(text);
                const fixtures = parsed?.data?.fixtures || [];
                return { ok: r.ok, status: r.status, bodyLen: text.length, fixturesCount: fixtures.length };
            } catch (e) { return { error: String(e) }; }
        }""")
        print("\nDIRECT FETCH (美国职业大联盟):")
        print(json.dumps(result2, ensure_ascii=False, indent=2))

        # Now poke refresh via window if exposed, OR inspect the rendered DOM
        # Look at any evidence of fixture rows in DOM
        dom = page.evaluate("""() => {
            return {
                analysisButtons: Array.from(document.querySelectorAll('button')).filter(b => b.textContent.trim()==='分析').length,
                // rows that look like fixture matches — usually have a home/away pair
                rowsWithTwoTeams: Array.from(document.querySelectorAll('[class*="row"], tr, li')).filter(el => {
                    const t = el.textContent || '';
                    return /\\d+[:：]\\d+|vs|VS/.test(t) && t.length > 20 && t.length < 200;
                }).length,
                // explicit "今日 N 场" badge near timeline
                todayBadge: Array.from(document.querySelectorAll('span')).map(s=>s.textContent||'').find(t=>/今日\\s*\\d+\\s*场/.test(t)) || '',
                leagueBadge: Array.from(document.querySelectorAll('span')).map(s=>s.textContent||'').find(t=>/个联赛/.test(t)) || '',
            };
        }""")
        print("\nDOM STATE:")
        print(json.dumps(dom, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
