"""
波胆 + 角球 采集试验 — 在比赛列表直接点二级tab
"""
import asyncio, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from gq.auto_collector import GQCollectorV2, MatchData

async def main():
    c = GQCollectorV2(headless=True)
    ok = await c.login()
    if not ok: print("Login failed"); return
    p = c._page
    ok = await c.ensure_football()
    if not ok: print("Football failed"); return
    await c.scroll_all()

    matches = await c.scrape_matches()
    print(f"共 {len(matches)} 场比赛")

    target = matches[0]  # 先试第一场
    print(f"\n=== 目标: {target.key} ===")

    # 点击比赛的"波胆"二级tab
    clicked = await p.evaluate(f"""() => {{
        // 找比赛的父容器
        const containers = document.querySelectorAll('.odd-list.match-indent');
        for (const el of containers) {{
            const t = el.innerText || '';
            if (t.includes('{target.home}') && t.includes('{target.away}')) {{
                // 在它下面找波胆tab
                const parent = el.parentElement;
                if (!parent) continue;
                const tabs = parent.querySelectorAll('.tab-item-h, .secondary-game-play *');
                for (const tab of tabs) {{
                    const txt = tab.innerText || '';
                    if (txt.includes('\\u6ce2\\u80c6') && !txt.includes('\\u5168\\u573a') && tab.offsetHeight > 0) {{
                        tab.click();
                        return 'cs_clicked';
                    }}
                }}
                // 也试试在同一层级的兄弟
                const siblings = el.parentElement?.querySelectorAll('div,span');
                if (siblings) {{
                    for (const sib of siblings) {{
                        const txt = sib.innerText || '';
                        if (txt.trim() === '\\u6ce2\\u80c6' && sib.offsetHeight > 0) {{
                            sib.click();
                            return 'cs_sibling';
                        }}
                    }}
                }}
                return 'found_match_but_no_cs_tab';
            }}
        }}
        return 'match_not_found';
    }}""")
    print(f"点击结果: {clicked}")
    await asyncio.sleep(3)

    # 看看页面变了什么
    body = await p.evaluate("document.body.innerText") or ""
    # 找波胆区域
    lines = body.split('\n')
    cs_lines = []
    in_cs = False
    for i, line in enumerate(lines):
        l = line.strip()
        if '波胆' in l or '正确比分' in l: in_cs = True; continue
        if in_cs:
            if '全场独赢' in l or '全场让球' in l or '晋级' in l: break
            if i < len(lines) - 1:
                next_line = lines[i+1].strip()
                # 比分格式: X-Y, 下一行数字是赔率
                import re
                if re.match(r'^\d+-\d+$', l) and re.match(r'^\d+(\.\d+)?$', next_line):
                    cs_lines.append((l, next_line))
    
    print(f"\n波胆数据 ({len(cs_lines)}):")
    for score, odds in cs_lines:
        print(f"  {score}: {odds}")

    if not cs_lines:
        # 打印所有可能在波胆区域的行
        print("\n波胆附近文本:")
        for i, l in enumerate(lines):
            if any(kw in l for kw in ['1-0', '0-0', '2-1', '波胆']):
                start = max(0, i-2)
                end = min(len(lines), i+4)
                for j in range(start, end):
                    print(f"  [{j}] {lines[j].strip()}")
                print("  ---")

    # 重新确保在比赛列表
    await p.evaluate('window.location.hash = "#/match"')
    await asyncio.sleep(2)
    await c.ensure_football()

    # 试角球
    print(f"\n=== 试角球 ({target.key}) ===")
    clicked2 = await p.evaluate(f"""() => {{
        const containers = document.querySelectorAll('.odd-list.match-indent');
        for (const el of containers) {{
            const t = el.innerText || '';
            if (t.includes('{target.home}') && t.includes('{target.away}')) {{
                const parent = el.parentElement;
                if (!parent) continue;
                const tabs = parent.querySelectorAll('.tab-item-h, .secondary-game-play *, div, span');
                for (const tab of tabs) {{
                    const txt = tab.innerText || '';
                    if (txt.trim() === '\\u89d2\\u7403' && tab.offsetHeight > 0) {{
                        tab.click();
                        return 'corner_clicked';
                    }}
                }}
                return 'found_but_no_corner';
            }}
        }}
        return 'not_found';
    }}""")
    print(f"角球点击: {clicked2}")
    await asyncio.sleep(3)

    body2 = await p.evaluate("document.body.innerText") or ""
    lines2 = body2.split('\n')
    corner_lines = []
    in_corner = False
    for i, l in enumerate(lines2):
        l = l.strip()
        if '角球' in l and '全场' not in l: in_corner = True; continue
        if in_corner and ('全场独赢' in l or '全场让球' in l or '波胆' in l): break
        if in_corner and i < len(lines2) - 1:
            nl = lines2[i+1].strip()
            if l.startswith('大 ') and nl.replace('.','').isdigit():
                corner_lines.append(('over', l[2:], nl))
            if l.startswith('小 ') and nl.replace('.','').isdigit():
                corner_lines.append(('under', l[2:], nl))

    print(f"角球数据 ({len(corner_lines)}):")
    for t, line, odds in corner_lines:
        print(f"  {t} {line}: {odds}")

    await c.close()

asyncio.run(main())
