#!/usr/bin/env python
"""
scripts/roi_report.py
=====================
P2 报表入口: 从 bet_records + submarket_bets 生成决策闭环 ROI 报表。

输出:
  deliverables/roi_report_<YYYYMMDD>.html   自包含 HTML(内联 SVG 曲线)
  deliverables/roi_report_<YYYYMMDD>.json   结构化数据(供前端/二次分析)

用法:
  python scripts/roi_report.py
  python scripts/roi_report.py --out-dir deliverables
"""
import sqlite3
import json
import os
import sys
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
DB = os.path.join(PROJECT_ROOT, "data", "football_data.db")

from pipeline.roi_report import build_report, render_html


def main():
    out_dir = "deliverables"
    if "--out-dir" in sys.argv:
        out_dir = sys.argv[sys.argv.index("--out-dir") + 1]
    os.makedirs(out_dir, exist_ok=True)

    con = sqlite3.connect(DB)
    cur = con.cursor()
    report = build_report(cur)
    con.close()

    stamp = datetime.now().strftime("%Y%m%d")
    html_path = os.path.join(out_dir, f"roi_report_{stamp}.html")
    json_path = os.path.join(out_dir, f"roi_report_{stamp}.json")

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(render_html(report))
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)

    o = report["overall"]
    print(f"ROI 报表已生成:")
    print(f"  HTML: {html_path}")
    print(f"  JSON: {json_path}")
    print(f"  可结算注数 {o['bets']} | 本金 ¥{o['staked']:,.0f} | "
          f"净盈亏 ¥{o['pnl']:,.0f} | ROI {o['roi_pct']:+.2f}% | 胜率 {o['hit_rate']:.1f}%")
    return html_path, json_path


if __name__ == "__main__":
    main()
