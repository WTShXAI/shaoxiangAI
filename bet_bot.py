from playwright.sync_api import sync_playwright # type: ignore

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=False,
        slow_mo=100
    )
    page = browser.new_page()
    page.goto("https://www.08a2zp.vip:9967/game/sport/ob?enName=YBTY")

    print("页面标题:", page.title())

    input("回车关闭浏览器...")
    browser.close()