#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
frontend_tour.py — 实操走查哨响AI前端4个主页面 + 触发分析弹窗 + 点击一场比赛,
输出截图与可机读 JSON 到 D:/Architecture/.edge_agent_profile/。
依赖: 本机 Edge 已以 --remote-debugging-port=9222 启动。
"""
import json
import os
from playwright.sync_api import sync_playwright

OUT = "D:/Architecture/.edge_agent_profile"
os.makedirs(OUT, exist_ok=True)

URLS = [
    ("home", "http://127.0.0.1:9000/"),
    ("live", "http://127.0.0.1:9000/live-scores"),
    ("timeline", "http://127.0.0.1:9000/timeline"),
]

findings = {"pages": {}, "interactions": {}}

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    ctx = browser.contexts[0]

    # ----- 1) home -----
    page = ctx.new_page()
    page.goto(URLS[0][1], wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2500)
    page.screenshot(path=f"{OUT}/tour_1_home.png")
    findings["pages"]["home"] = {
        "title": page.title(),
        "url": page.url,
    }

    # 1a) 点第一个"分析"按钮 -> 弹窗
    analyze_btn = page.locator("button:has-text('分析')").first
    if analyze_btn.count():
        try:
            analyze_btn.click()
            page.wait_for_timeout(1500)
            page.screenshot(path=f"{OUT}/tour_2_analyze.png")
            # 抓弹窗文本
            modal_text = ""
            for sel in ["[role=dialog]", ".ant-modal", ".modal", ".el-dialog"]:
                loc = page.locator(sel).first
                if loc.count():
                    modal_text = loc.inner_text()
                    break
            if not modal_text:
                modal_text = page.evaluate(
                    "document.body.innerText.slice(-800)"
                )
            findings["interactions"]["analyze_modal"] = modal_text[:600]
        except Exception as e:
            findings["interactions"]["analyze_modal_error"] = str(e)
        # 关弹窗
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)

    # 1b) 点今日赛程第一场比赛(点首个时间标签 "00:15" 之类) -> 看右侧面板是否填充
    try:
        first_time = page.locator("text=/^\\d{1,2}:\\d{2}$/").first
        if first_time.count():
            first_time.click()
            page.wait_for_timeout(1500)
            page.screenshot(path=f"{OUT}/tour_3_after_click_fixture.png")
            # 抽右侧面板文本
            right_panel = ""
            for sel in ["aside", "[class*=Sidebar]", "[class*=sidebar]", "[class*=Panel]"]:
                loc = page.locator(sel).first
                if loc.count():
                    right_panel = loc.inner_text()
                    break
            findings["interactions"]["right_panel_after_click"] = right_panel[:600]
    except Exception as e:
        findings["interactions"]["click_fixture_error"] = str(e)
    page.close()

    # ----- 2) live scores -----
    page = ctx.new_page()
    page.goto(URLS[1][1], wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2500)
    page.screenshot(path=f"{OUT}/tour_4_live.png")
    findings["pages"]["live"] = {
        "title": page.title(),
        "url": page.url,
        "main_text_excerpt": page.evaluate(
            "document.body.innerText.replace(/\\s+/g,' ').trim().slice(0,500)"
        ),
    }
    page.close()

    # ----- 3) timeline -----
    page = ctx.new_page()
    page.goto(URLS[2][1], wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2500)
    page.screenshot(path=f"{OUT}/tour_5_timeline.png")
    findings["pages"]["timeline"] = {
        "title": page.title(),
        "url": page.url,
        "main_text_excerpt": page.evaluate(
            "document.body.innerText.replace(/\\s+/g,' ').trim().slice(0,500)"
        ),
    }
    page.close()

    # ----- 4) quant demo -----
    page = ctx.new_page()
    page.goto(URLS[3][1], wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2500)
    page.screenshot(path=f"{OUT}/tour_6_quant.png")
    findings["pages"]["quant"] = {
        "title": page.title(),
        "url": page.url,
        "main_text_excerpt": page.evaluate(
            "document.body.innerText.replace(/\\s+/g,' ').trim().slice(0,500)"
        ),
    }
    page.close()

with open(f"{OUT}/tour_findings.json", "w", encoding="utf-8") as f:
    json.dump(findings, f, ensure_ascii=False, indent=2)
print("[tour] screenshots + tour_findings.json written to", OUT)