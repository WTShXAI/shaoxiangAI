#!/usr/bin/env python
"""
scripts/calibration_report.py
=============================
P3 报表入口: 生成"模型概率校准检验报告"。

输出:
  deliverables/calibration_report_<YYYYMMDD>.html   自包含 HTML(内联 SVG 可靠性图)
  deliverables/calibration_report_<YYYYMMDD>.json   结构化数据

用法:
  python scripts/calibration_report.py
"""
import sqlite3
import json
import os
import sys
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
DB = os.path.join(PROJECT_ROOT, "data", "football_data.db")

from pipeline.calibration import build_calibration, render_html


def main():
    out_dir = "deliverables"
    os.makedirs(out_dir, exist_ok=True)
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cal = build_calibration(cur)
    con.close()

    stamp = datetime.now().strftime("%Y%m%d")
    html_path = os.path.join(out_dir, f"calibration_report_{stamp}.html")
    json_path = os.path.join(out_dir, f"calibration_report_{stamp}.json")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(render_html(cal))
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(cal, f, ensure_ascii=False, indent=2, default=str)

    lv, hs = cal["live"], cal["historical"]
    print("校准报告已生成:")
    print(f"  HTML: {html_path}")
    print(f"  JSON: {json_path}")
    print(f"\n[A] Live 决策闭环: n={lv['n']} · Brier={lv['brier']} · ECE={lv['ece']} · 置信={lv['confidence']}")
    print(f"[B] Historical 共识: n={hs['n']:,} · Brier={hs['brier']} · "
          f"LogLoss={hs['log_loss']} · ECE={hs['ece']} · 斜率={hs['slope']}")
    return html_path, json_path


if __name__ == "__main__":
    main()
