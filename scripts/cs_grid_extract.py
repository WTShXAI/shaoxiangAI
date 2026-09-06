"""CS 波胆网格精抽 — x/y 坐标聚类还原波胆赔率矩阵 (SSoT)

重 OCR page_type='cs_grid' 截图(带 bbox), 将 比分标签(r'^\\d+-\\d+$') 与
赔率(浮点) 按 列(cx 聚类) + 行(cy 邻近) 配对, 还原每张图的波胆 board。

布局观察(实测 iPhone 博彩 App 滚球/CS 页):
  6 个 cx 列 (~127/307/486/684/862/1043), 每列独立对应一个正确比分;
  比分行与赔率行交替(比分行 cy≈1966/2072/2178, 赔率行 cy≈1904/2010/2116)。

输出
----
  long_images.db.cs_grid_cells(image_id, score, odds, col, cy)
  data/cs_grid_report.json / .md
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from long_images_v2 import get_ocr  # 复用 RapidOCR 懒加载

DB = Path("data/long_images.db")
SCORE_RE = re.compile(r"^\d+-\d+$")
ODDS_RE = re.compile(r"^\d+(\.\d+)?$")


def cluster_cols(xs, gap=90):
    """x 坐标聚类成列: 间距<=gap 归同列, 返回每列中位 cx"""
    xs = sorted(set(round(x) for x in xs))
    if not xs:
        return []
    cols = [[xs[0]]]
    for x in xs[1:]:
        if x - cols[-1][-1] <= gap:
            cols[-1].append(x)
        else:
            cols.append([x])
    return [sum(c) / len(c) for c in cols]


def extract(image_path: Path):
    ocr = get_ocr()
    result, _ = ocr(str(image_path))
    toks = []
    for box, txt, conf in result:
        try:
            conf = float(conf)
        except (TypeError, ValueError):
            continue
        xs = [b[0] for b in box]
        ys = [b[1] for b in box]
        toks.append({
            "t": txt.strip(), "c": conf,
            "cx": (min(xs) + max(xs)) / 2, "cy": (min(ys) + max(ys)) / 2,
        })
    scores = [t for t in toks if SCORE_RE.match(t["t"])]
    odds = [t for t in toks if ODDS_RE.match(t["t"]) and 1.01 <= float(t["t"]) <= 200]
    if not scores:
        return []
    # 仅取 CS 网格区(比分所在 cy 带 ±60)做列聚类, 排除顶部 1X2/页眉噪声污染列中心
    scy = [s["cy"] for s in scores]
    lo, hi = min(scy) - 60, max(scy) + 60
    grid = [t for t in toks if lo <= t["cy"] <= hi]
    col_centers = cluster_cols([t["cx"] for t in grid])

    def col_of(x):
        return min(range(len(col_centers)), key=lambda i: abs(col_centers[i] - x))

    cells = []
    for s in scores:
        ci = col_of(s["cx"])
        best, bd = None, 1e9
        for o in odds:
            if not (lo <= o["cy"] <= hi):
                continue
            if col_of(o["cx"]) != ci:
                continue
            d = abs(o["cy"] - s["cy"])
            if d < bd:
                bd, best = d, o
        if best and bd < 130:
            cells.append({"score": s["t"], "odds": round(float(best["t"]), 2),
                          "col": ci, "cy": round(s["cy"])})
    # 去重同比分(取最高赔, 更保守的对庄值)
    seen: dict = {}
    for c in cells:
        if c["score"] not in seen or c["odds"] > seen[c["score"]]["odds"]:
            seen[c["score"]] = c
    return list(seen.values())


def main():
    conn = sqlite3.connect(str(DB))
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS cs_grid_cells (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      image_id INTEGER REFERENCES images(id) ON DELETE CASCADE,
      score TEXT, odds REAL, col INTEGER, cy INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_cgc_img ON cs_grid_cells(image_id);
    """)
    conn.execute("DELETE FROM cs_grid_cells")
    rows = conn.execute(
        "SELECT id, path FROM images WHERE page_type='cs_grid' ORDER BY id"
    ).fetchall()
    n = len(rows)
    print(f"[cs_grid] re-OCR + 聚类 {n} 张 ...", flush=True)
    t0 = time.time()
    total_cells = 0
    per_img = []
    for i, (img_id, path) in enumerate(rows, 1):
        p = Path(path)
        if not p.exists():
            continue
        try:
            cells = extract(p)
        except Exception as e:
            print(f"  [{i}/{n}] FAIL {p.name}: {e}", flush=True)
            cells = []
        for c in cells:
            conn.execute(
                "INSERT INTO cs_grid_cells(image_id, score, odds, col, cy) VALUES(?,?,?,?,?)",
                (img_id, c["score"], c["odds"], c["col"], c["cy"]))
            total_cells += 1
        # 回填 parsed_json
        conn.execute("UPDATE images SET parsed_json=? WHERE id=?",
                     (json.dumps({"league": None, "n_cs_cells": len(cells),
                                  "cells": cells}, ensure_ascii=False), img_id))
        per_img.append({"image_id": img_id, "n_cells": len(cells)})
        if i % 5 == 0 or i == n:
            print(f"  [{i}/{n}] rate={i/(time.time()-t0):.2f}/s cells={total_cells}", flush=True)
    conn.commit()

    rep = {
        "n_images": n,
        "total_cs_cells": total_cells,
        "avg_cells_per_image": round(total_cells / n, 1) if n else 0,
        "per_image": per_img,
    }
    Path("data/cs_grid_report.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    # md
    md = ["# CS 波胆网格精抽报告", "",
          f"- 处理截图: {n} 张",
          f"- 抽取波胆单元格: {total_cells} 个 (均值 {rep['avg_cells_per_image']}/张)", ""]
    for pi in per_img:
        md.append(f"- image_id={pi['image_id']}: {pi['n_cells']} 单元格")
    Path("data/cs_grid_report.md").write_text("\n".join(md), encoding="utf-8")
    print(f"[cs_grid] done in {time.time()-t0:.1f}s  total_cells={total_cells}")
    print(f"[cs_grid] 报告: data/cs_grid_report.json / .md")


if __name__ == "__main__":
    main()
