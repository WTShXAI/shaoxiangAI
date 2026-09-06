#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
解析 D:/系统缓存备份/原来的c盘用户缓存/ShxAI/Desktop/世界杯 下的世界杯赔率截图，
用 RapidOCR 提取结构化赔率数据，写入 SQLite。
截图来源: 雷速/类似体育 App 的赛前赔率页。
"""
import os
import re
import sqlite3
import sys
import json
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OCR_VENV_PYTHON = os.path.join(ROOT, ".ocr_venv", "Scripts", "python.exe")
SRC_DIR = r"D:\系统缓存备份\原来的c盘用户缓存\ShxAI\Desktop\世界杯"
DB_PATH = os.path.join(ROOT, "data", "worldcup_screenshots.db")


def _import_rapidocr():
    try:
        from rapidocr_onnxruntime import RapidOCR
        return RapidOCR
    except ImportError:
        raise ImportError("RapidOCR 未安装，请用 .ocr_venv 运行本脚本")


def _to_float(s):
    if s is None:
        return None
    s = str(s).replace(",", "").strip()
    if s in ("-", "", "--"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_teams_from_filename(fname):
    """从文件名提取主客队，如 '加拿大VS波黑.png' -> ('加拿大','波黑')"""
    base = Path(fname).stem
    # 去掉微信图片前缀
    base = re.sub(r"微信图片_.*", "", base)
    for sep in ("VS", "vs", "Vs"):
        if sep in base:
            parts = base.split(sep, 1)
            return parts[0].strip(), parts[1].strip()
    return None, None


def ocr_image(engine, path):
    result, _ = engine(path)
    items = []
    for line in result:
        box, text, score = line
        pts = np.array(box)
        cx = float(pts[:, 0].mean())
        cy = float(pts[:, 1].mean())
        items.append({"text": text.strip(), "x": cx, "y": cy, "score": score})
    return items


def extract_top_section(items):
    """取顶部赔率区 (y < ~130 或到 '平' 行结束)"""
    # 找到左侧日期行 y 位置，作为截断参考
    date_items = [it for it in items if re.search(r"\d{2}月\d{2}日", it["text"])]
    cutoff_y = 130
    if date_items:
        cutoff_y = max(cutoff_y, max(it["y"] for it in date_items) + 40)
    return [it for it in items if it["y"] < cutoff_y]


def _is_odd(t):
    return bool(re.match(r"^\d+(\.\d+)?$", t)) and _to_float(t) is not None


def _is_handicap(t):
    return bool(re.match(r"^[+-]?\d+(\.\d+)?(/\d+(\.\d+)?)?$", t)) or t == "0"


def group_into_rows(items, y_tol=18):
    """按 y 坐标把项目聚成行"""
    if not items:
        return []
    items = sorted(items, key=lambda d: d["y"])
    rows = []
    cur = [items[0]]
    for it in items[1:]:
        if abs(it["y"] - cur[0]["y"]) <= y_tol:
            cur.append(it)
        else:
            rows.append(sorted(cur, key=lambda d: d["x"]))
            cur = [it]
    rows.append(sorted(cur, key=lambda d: d["x"]))
    return rows


# 基于 ~930px 宽截图的列 x 范围（label/line 列 与 odds 列分离，防跨列污染）
COL_RANGES = {
    # 全场 1X2: 标签 ~229, 赔率 ~293
    "ft_1x2_label": (180, 260),
    "ft_1x2_odds": (260, 330),
    # 全场 AH: handicap ~344, 赔率 ~415
    "ft_ah_handicap": (330, 395),
    "ft_ah_odds": (395, 465),
    # 全场 OU: line ~477, 赔率 ~549
    "ft_ou_line": (465, 530),
    "ft_ou_odds": (530, 590),
    # 半场 1X2: 标签 ~595, 赔率 ~658
    "ht_1x2_label": (560, 630),
    "ht_1x2_odds": (630, 695),
    # 半场 AH: handicap ~715, 赔率 ~780
    "ht_ah_handicap": (695, 760),
    "ht_ah_odds": (760, 825),
    # 半场 OU: line ~842, 赔率 ~914
    "ht_ou_line": (825, 890),
    "ht_ou_odds": (890, 950),
}


def assign_cols(items):
    out = []
    for it in items:
        x = it["x"]
        for col, (lo, hi) in COL_RANGES.items():
            if lo <= x < hi:
                out.append({**it, "col": col})
                break
    return out


def extract_odds(top_items):
    """列分配 + 行配对：1X2/AH/OU 分别在自己的列里配对"""
    assigned = assign_cols(top_items)
    rows = group_into_rows(assigned)

    ft_1x2 = {"home": None, "draw": None, "away": None}
    ht_1x2 = {"home": None, "draw": None, "away": None}
    ft_ah = []
    ht_ah = []
    ft_ou = []
    ht_ou = []

    for row in rows:
        # 1X2: 标签列内 主/客/平 与 同列/同y 的赔率列数字配对
        for d in row:
            t = d["text"]
            if t not in ("主", "客", "平"):
                continue
            prefix = d["col"].split("_")[0]  # ft or ht
            y = d["y"]
            odds_col = f"{prefix}_1x2_odds"
            nums = [r for r in row if r["col"] == odds_col and abs(r["y"] - y) < 22 and _is_odd(r["text"])]
            if not nums:
                continue
            odd = _to_float(min(nums, key=lambda r: abs(r["x"] - d["x"]))["text"])
            key = {"主": "home", "客": "away", "平": "draw"}[t]
            (ft_1x2 if prefix == "ft" else ht_1x2)[key] = odd

        # AH: handicap 列 与 同y 的 odds 列配对
        for d in row:
            t = d["text"]
            if not _is_handicap(t):
                continue
            prefix = d["col"].split("_")[0]
            if d["col"] not in (f"{prefix}_ah_handicap",):
                continue
            y = d["y"]
            odds_col = f"{prefix}_ah_odds"
            nums = [r for r in row if r["col"] == odds_col and abs(r["y"] - y) < 22 and _is_odd(r["text"])]
            if not nums:
                continue
            odd = _to_float(min(nums, key=lambda r: abs(r["x"] - d["x"]))["text"])
            entry = {"line": t, "odds": odd}
            (ft_ah if prefix == "ft" else ht_ah).append(entry)

        # OU: line 列 与 同y 的 odds 列配对
        for d in row:
            t = d["text"]
            side = None
            line = None
            if t.startswith("大") or t.startswith("小"):
                side = t[0]
                rest = t[1:].strip()
                if rest and re.match(r"^[\d./]+$", rest):
                    line = rest
                else:
                    # line 在右侧相邻 token
                    pass
            if side and line:
                prefix = d["col"].split("_")[0]
                y = d["y"]
                odds_col = f"{prefix}_ou_odds"
                nums = [r for r in row if r["col"] == odds_col and abs(r["y"] - y) < 22 and _is_odd(r["text"])]
                if not nums:
                    continue
                odd = _to_float(min(nums, key=lambda r: abs(r["x"] - d["x"]))["text"])
                entry = {"side": side, "line": line, "odds": odd}
                (ft_ou if prefix == "ft" else ht_ou).append(entry)

    return {
        "ft_1x2": ft_1x2,
        "ft_ah": ft_ah,
        "ft_ou": ft_ou,
        "ht_1x2": ht_1x2,
        "ht_ah": ht_ah,
        "ht_ou": ht_ou,
    }


def parse_datetime(items, year=2026):
    """从 OCR 项目里解析 '06月13日 03:00(GMT+8)'"""
    txt = " ".join([it["text"] for it in items])
    m = re.search(r"(\d{2})月(\d{2})日\s+(\d{2}):(\d{2})", txt)
    if m:
        mon, day, hr, mn = map(int, m.groups())
        try:
            dt = datetime(year, mon, day, hr, mn)
            return dt.isoformat()
        except ValueError:
            return None
    return None


def parse_image(engine, path):
    fname = os.path.basename(path)
    home, away = parse_teams_from_filename(fname)
    items = ocr_image(engine, path)
    top = extract_top_section(items)
    odds = extract_odds(top)
    dt = parse_datetime(top)
    return {
        "file": fname,
        "folder": os.path.basename(os.path.dirname(path)),
        "home": home,
        "away": away,
        "kickoff": dt,
        "ft_1x2_h": odds["ft_1x2"].get("home"),
        "ft_1x2_d": odds["ft_1x2"].get("draw"),
        "ft_1x2_a": odds["ft_1x2"].get("away"),
        "ht_1x2_h": odds["ht_1x2"].get("home"),
        "ht_1x2_d": odds["ht_1x2"].get("draw"),
        "ht_1x2_a": odds["ht_1x2"].get("away"),
        "ft_ah": json.dumps(odds["ft_ah"], ensure_ascii=False),
        "ft_ou": json.dumps(odds["ft_ou"], ensure_ascii=False),
        "ht_ah": json.dumps(odds["ht_ah"], ensure_ascii=False),
        "ht_ou": json.dumps(odds["ht_ou"], ensure_ascii=False),
        "raw_ocr": json.dumps([{"text": it["text"], "x": round(it["x"], 1), "y": round(it["y"], 1)} for it in items], ensure_ascii=False),
    }


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS wc_screenshot_matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file TEXT,
            folder TEXT,
            home TEXT,
            away TEXT,
            kickoff TEXT,
            ft_1x2_h REAL,
            ft_1x2_d REAL,
            ft_1x2_a REAL,
            ht_1x2_h REAL,
            ht_1x2_d REAL,
            ht_1x2_a REAL,
            ft_ah TEXT,
            ft_ou TEXT,
            ht_ah TEXT,
            ht_ou TEXT,
            score_home INTEGER,
            score_away INTEGER,
            ht_score_home INTEGER,
            ht_score_away INTEGER,
            result TEXT,
            source TEXT DEFAULT 'screenshot_virtual_wc',
            is_virtual INTEGER DEFAULT 1,
            raw_ocr TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_wcs_home_away ON wc_screenshot_matches(home, away)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_wcs_kickoff ON wc_screenshot_matches(kickoff)")
    conn.commit()
    return conn


def main():
    RapidOCR = _import_rapidocr()
    engine = RapidOCR()
    conn = init_db()
    c = conn.cursor()

    pngs = []
    for root, _, files in os.walk(SRC_DIR):
        for f in files:
            if f.lower().endswith(".png"):
                pngs.append(os.path.join(root, f))
    print(f"发现 {len(pngs)} 张截图")

    inserted = 0
    for p in sorted(pngs):
        try:
            rec = parse_image(engine, p)
            c.execute("""
                INSERT INTO wc_screenshot_matches
                (file, folder, home, away, kickoff, ft_1x2_h, ft_1x2_d, ft_1x2_a,
                 ht_1x2_h, ht_1x2_d, ht_1x2_a, ft_ah, ft_ou, ht_ah, ht_ou, raw_ocr)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                rec["file"], rec["folder"], rec["home"], rec["away"], rec["kickoff"],
                rec["ft_1x2_h"], rec["ft_1x2_d"], rec["ft_1x2_a"],
                rec["ht_1x2_h"], rec["ht_1x2_d"], rec["ht_1x2_a"],
                rec["ft_ah"], rec["ft_ou"], rec["ht_ah"], rec["ht_ou"], rec["raw_ocr"]
            ))
            inserted += 1
            print(f"[OK] {rec['folder']}/{rec['file']} | {rec['home']} vs {rec['away']} | FT {rec['ft_1x2_h']}/{rec['ft_1x2_d']}/{rec['ft_1x2_a']}")
        except Exception as e:
            print(f"[ERR] {p}: {e}")
    conn.commit()
    conn.close()
    print(f"\n入库完成: {inserted}/{len(pngs)} 写入 {DB_PATH}")


if __name__ == "__main__":
    main()
