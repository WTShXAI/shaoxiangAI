#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
frontend_probe.py — 用 Playwright 经 CDP 连接本机 Edge(远程调试端口),
实操哨响AI前端: 截图 + 抓控制台/页面错误 + 抽 DOM 结构, 用于定位交互/渲染问题。

用法:
  python frontend_probe.py <url> [out_png]
依赖: 本机 Edge 已以 --remote-debugging-port=9222 启动(独立 profile)。
注意: 不调用 browser.close(), 避免关掉用户 Edge; 只关本次 page。
"""
import sys
import json
import os

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:9000"
OUT = sys.argv[2] if len(sys.argv) > 2 else "D:/Architecture/.edge_agent_profile/probe.png"

from playwright.sync_api import sync_playwright

console_msgs = []
page_errors = []


def main():
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        page.on("console", lambda m: console_msgs.append(f"{m.type}: {m.text}"))
        page.on("pageerror", lambda e: page_errors.append(str(e)))
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            print(f"[goto error] {e}")
        # 给 SPA 一点时间渲染
        page.wait_for_timeout(2500)

        title = page.title()
        nav = page.eval_on_selector_all(
            "a, button, [role=button], nav a",
            "els => els.slice(0,80).map(e=>({tag:e.tagName, text:(e.innerText||'').replace(/\\s+/g,' ').trim().slice(0,40), href:e.getAttribute('href')||''}))",
        )
        headings = page.eval_on_selector_all(
            "h1,h2,h3",
            "els => els.slice(0,25).map(e=>e.innerText.replace(/\\s+/g,' ').trim().slice(0,60))",
        )
        body_text = page.evaluate("document.body.innerText.replace(/\\s+/g,' ').trim().slice(0,1200)")
        # 视口尺寸 + 视口内是否空白(可见元素数)
        vis_stats = page.evaluate(
            "({vw: innerWidth, vh: innerHeight, "
            "visibleEls: document.querySelectorAll('*').length, "
            "mainText: (document.querySelector('main')||document.querySelector('#app')||document.body).innerText.replace(/\\s+/g,' ').trim().length})"
        )
        try:
            page.screenshot(path=OUT, full_page=False)
        except Exception as e:
            print(f"[screenshot error] {e}")

        result = {
            "url": page.url,
            "title": title,
            "viewport": vis_stats,
            "headings": headings,
            "nav": nav,
            "body_text": body_text,
            "console_count": len(console_msgs),
            "console": console_msgs[:40],
            "page_errors": page_errors[:20],
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print(f"\n[probe] screenshot -> {OUT}")
        page.close()


if __name__ == "__main__":
    main()
