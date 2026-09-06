# -*- coding: utf-8 -*-
"""
long_images_v2.py — 对 v1 漏判的 237 张 "other" 截图做二次精修:
  1) 重新 OCR (带坐标 bbox, v1 的 raw_ocr_json 只存了 text+conf 无坐标, 无法做坐标解析)
  2) 重分类: cross_book(多机构胜平负表=跨庄edge源) / odds_trend(走势) /
              lineup(阵容) / text_live(文字直播) / live_odds(赔率展示页) / noise / other
  3) 抽取 cross_book 多机构 1X2 赔率 + 凯利, 入库 cross_book_odds 表
  4) 对 live_odds 展示页重抽市场赔率(1X2/AH/OU)补 image_odds
  5) 生成跨庄离散度报告 = 哨响AI 缺失的"真edge"数据源

用法:
  python long_images_v2.py --limit 20      # 调试
  python long_images_v2.py                 # 全量 237
"""
from __future__ import annotations
import argparse, json, re, sqlite3, sys, time
from pathlib import Path

import long_images_ingest as V1   # 复用纯函数: extract_market_odds / find_team_pair_by_coords / NAV_TOKENS

DB_PATH = Path("D:/Architecture/data/long_images.db")
LONG_DIR = Path("D:/Architecture/long")

# ---------------- OCR (RapidOCR, 带 bbox) ----------------
_OCR = None
def get_ocr():
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR
        _OCR = RapidOCR()
    return _OCR

# ---------------- 重分类关键词 ----------------
KW_CROSS = ["机构", "胜平负"]          # 分析页多机构表强信号
KW_TREND = ["走势"]
KW_LINEUP = ["首发阵容", "4-2-3-1", "4-3-3", "4-4-2", "3-5-2", "本场替补"]
KW_TEXTLIVE = ["文字直播"]
KW_LIVEODDS = ["全场独赢", "全场让球", "全场大小", "上半场独赢"]
NOISE_TOKENS = ["贷款金额", "贷款详情", "还款计划", "年化利率", "还款方式", "未还本金"]

# 已知机构名(部分匹配, 含 * 掩码)
KNOWN_BOOKS = ["Inter", "Victor", "bet", "S28", "国竞", "必发", "马", "10", "12", "18",
               "36", "澳", "盈", "立", "威", "易", "利", "Ma", "RBK"]
MARKET_LABELS = {"让步","大小","胜平负","角球","必发","走势","日期","时间","比分","机构",
                 "赛前","中场","上半场","下半场","所有投注"}

BOOK_RE = re.compile(r"^[\u4e00-\u9fa5A-Za-z0-9*·]{2,10}$")
NUM_RE = re.compile(r"^\d+(\.\d+)?$")
ODDS_RANGE = (1.01, 200.0)     # 赔率合法区间
KELLY_RANGE = (0.01, 1.30)     # 凯利指数合法区间

def classify_v2(lines):
    texts = [l["text"].strip() for l in lines]
    joined = "\n".join(texts)
    if any(t in joined for t in NOISE_TOKENS):
        return "noise"
    if "文字直播" in joined:
        return "text_live"
    # 阵容: 含阵型 token 或"首发阵容"
    if "首发阵容" in joined or any(re.search(r"4-\d-\d|3-5-2|4-4-2", t) for t in texts):
        return "lineup"
    # 多机构胜平负表: 同时有 机构 + (胜平负 或 多家机构名)
    has_org = "机构" in texts
    has_booknames = sum(1 for t in texts if (("*" in t and 2 <= len(t) <= 8) or
                                             any(t.startswith(b) for b in KNOWN_BOOKS)))
    if has_org and (has_booknames >= 3):
        return "cross_book"
    if has_org and "胜平负" in joined:
        return "cross_book"
    # 走势: 含走势 + 机构名 + 日期列
    if "走势" in joined and (has_booknames >= 2 or re.search(r"\d{2}-\d{2} \d{2}:\d{2}", joined)):
        return "odds_trend"
    if has_booknames >= 2 and re.search(r"\d{2}-\d{2} \d{2}:\d{2}", joined):
        return "odds_trend"
    # 赔率展示页(无滚球时间): 含市场标签 + 团队
    if any(k in joined for k in KW_LIVEODDS):
        return "live_odds"
    return "other"

# ---------------- 抽取: 顶部 match 身份( league / home / away ) ----------------
def extract_match_header(lines):
    out = {"league": None, "home_team": None, "away_team": None}
    # VS 单行(排除状态栏 y<150)
    for l in lines:
        t = l["text"].strip(); y = l["bbox"][0][1]
        if y < 150: continue
        if "VS" in t or "vs" in t:
            parts = re.split(r"\s*VS\s*|\s*vs\s*", t, flags=re.IGNORECASE)
            parts = [p.strip(" .") for p in parts if p.strip(" .")]
            if len(parts) >= 2:
                out["home_team"], out["away_team"] = parts[0], parts[1]
                for l2 in lines:
                    if l2["bbox"][0][1] < y + 60 and 2 <= len(l2["text"].strip()) <= 8 \
                       and re.search(r"[超甲联杯赛]", l2["text"]):
                        out["league"] = l2["text"].strip(); break
                return out
    # league: y[150,320], 含 超/甲/联/杯/赛 (首选顶部)
    for l in lines:
        t = l["text"].strip(); y = l["bbox"][0][1]
        if 150 <= y <= 320 and 2 <= len(t) <= 8 and re.search(r"[超甲联杯赛]", t):
            out["league"] = t; break
    # 兜底: 全图扫描含联赛后缀且非导航的短 token
    if out["league"] is None:
        for l in sorted(lines, key=lambda x: x["bbox"][0][1]):
            t = l["text"].strip()
            if 2 <= len(t) <= 8 and re.search(r"[超甲联杯赛]", t) and t not in V1.NAV_TOKENS:
                out["league"] = t; break
    # 左右团队: y[420,560], 左 x<450 / 右 x>650
    left = right = None
    for l in lines:
        t = l["text"].strip(); y = l["bbox"][0][1]
        x = (l["bbox"][0][0] + l["bbox"][2][0]) / 2
        if 420 <= y <= 560 and 2 <= len(t) <= 18 and t not in V1.NAV_TOKENS \
           and not t.isdigit() and not re.fullmatch(r"\d{1,2}:\d{2}", t):
            if x < 450 and left is None: left = t
            elif x > 650 and right is None: right = t
    if left: out["home_team"] = left
    if right: out["away_team"] = right
    return out

# ---------------- 抽取: cross_book 多机构 1X2 ----------------
def devig_implied(odds_list):
    """odds_list=[h,d,a]; 返回归一化隐含概率, 失败返回 None."""
    try:
        inv = [1.0 / o for o in odds_list]
        s = sum(inv)
        if s <= 0: return None
        return [i / s for i in inv]
    except Exception:
        return None

def extract_cross_book(lines, captured_at):
    """返回 list of dict: {bookmaker, h,d,a odds, h,d,a kelly, raw_numbers}."""
    # 找 机构 表头 y
    org_y = None
    for l in lines:
        if l["text"].strip() == "机构":
            org_y = l["bbox"][0][1]; break
    if org_y is None:
        return []
    # 机构名: y > org_y, x < 260, 形如已知机构
    book_rows = []
    for l in sorted(lines, key=lambda x: x["bbox"][0][1]):
        t = l["text"].strip()
        y = l["bbox"][0][1]; x = l["bbox"][0][0]
        if y <= org_y + 10: continue
        if x > 280: continue                       # 机构名在左列
        if t in MARKET_LABELS: continue
        if not BOOK_RE.match(t): continue
        if ("*" in t and 2 <= len(t) <= 9) or any(t.startswith(b) for b in KNOWN_BOOKS) or \
           re.match(r"^S\d+$", t) or t in ("国竞","必发"):
            book_rows.append((y, x, t))
    results = []
    for (y, x, name) in book_rows:
        # 收集同 y-band, x 在其右侧的所有数字(带 x 顺序)
        nums = []
        for l in lines:
            ly = l["bbox"][0][1]; lx = l["bbox"][0][0]
            if abs(ly - y) <= 35 and lx > x + 20:
                t = l["text"].strip().replace(",", "")
                if NUM_RE.match(t):
                    try: nums.append((lx, float(t)))
                    except ValueError: pass
        nums.sort()
        # 取 x 顺序中前 3 个 >1.01 的值作为 H/D/A 赔率
        odds_idx = [(i, v) for i, (lx, v) in enumerate(nums) if v >= 1.01]
        if len(odds_idx) < 3:
            continue
        hi, di, ai = odds_idx[0][0], odds_idx[1][0], odds_idx[2][0]
        h, d, a = odds_idx[0][1], odds_idx[1][1], odds_idx[2][1]
        # 原始 overround 校验: 合法 1X2 的 sum(1/o) 应在 [0.95, 1.18]
        # (注意 devig_implied 会归一化, 不能直接用其 sum 做阈值)
        raw_sum = 1.0/h + 1.0/d + 1.0/a
        if not (0.95 <= raw_sum <= 1.18):
            continue
        # 一致性闸门: 真实 1X2 三个赔率极差不会超过 8× (本数据集合法极大值比~3.07)
        # 漏判的错位行(如 25/1.05/6.0, 48/2.5/1.5, 1.1/10.5/10.5)在此剔除
        if max(h, d, a) / min(h, d, a) > 8:
            continue
        imp = devig_implied([h, d, a])
        # 凯利 = 剩余数值(排除已用的 3 个赔率下标)
        used = {hi, di, ai}
        rest = [v for i, (lx, v) in enumerate(nums) if i not in used]
        kelly = [v for v in rest if 0.01 <= v <= 1.30]
        hk = dk = ak = None
        if len(kelly) >= 3:
            hk, dk, ak = kelly[0], kelly[1], kelly[2]
        results.append({
            "bookmaker": name, "h_odds": h, "d_odds": d, "a_odds": a,
            "h_kelly": hk, "d_kelly": dk, "a_kelly": ak,
            "n_odds": len(odds_idx), "n_kelly": len(kelly),
            "raw": json.dumps([round(v,3) for _, v in nums]),
        })
    return results

# ---------------- 主流程 ----------------
def process_lines(conn, img_id, lines):
    """对已结构化的 lines 做分类 + 抽取 + 入库. 返回 page_type."""
    ptype = classify_v2(lines)
    parsed = {}
    if ptype == "cross_book":
        hdr = extract_match_header(lines)
        parsed.update(hdr)
        cb = extract_cross_book(lines, None)
        for r in cb:
            conn.execute("""INSERT INTO cross_book_odds(
                image_id, league, home, away, bookmaker,
                h_odds, d_odds, a_odds, h_kelly, d_kelly, a_kelly,
                n_odds, n_kelly, raw_values, captured_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (img_id, hdr.get("league"), hdr.get("home_team"), hdr.get("away_team"),
                 r["bookmaker"], r["h_odds"], r["d_odds"], r["a_odds"],
                 r["h_kelly"], r["d_kelly"], r["a_kelly"],
                 r["n_odds"], r["n_kelly"], r["raw"], None))
        parsed["n_bookmakers"] = len(cb)
        parsed["bookmakers"] = [r["bookmaker"] for r in cb]
    elif ptype == "live_odds":
        parsed = V1.parse_live_odds("\n".join(l["text"] for l in lines), lines)
        mo = V1.extract_market_odds("\n".join(l["text"] for l in lines), lines)
        for m in mo:
            conn.execute("""INSERT INTO image_odds(image_id,market,line,selection,odds,raw_text)
                            VALUES(?,?,?,?,?,?)""",
                         (img_id, m["market_code"], m["line"], m["selection"], m["odds"],
                          f'{m["market_label"]} | {m.get("raw","")}'))
        parsed["n_market_odds"] = len(mo)
    conn.execute("UPDATE images SET page_type=?, parsed_json=? WHERE id=?",
                 (ptype, json.dumps(parsed, ensure_ascii=False), img_id))
    return ptype

def reprocess(conn, limit):
    ocr = get_ocr()
    rows = conn.execute(
        "SELECT id, path FROM images WHERE page_type='other' ORDER BY id"
    ).fetchall()
    if limit: rows = rows[:limit]
    n = len(rows)
    print(f"[v2] OCR+reprocess {n} 'other' images ...", flush=True)
    t0 = time.time()
    stats = {}
    for i, (img_id, path) in enumerate(rows, 1):
        p = Path(path)
        if not p.exists():
            continue
        try:
            result, _ = ocr(str(p))
        except Exception as e:
            print(f"  [{i}/{n}] OCR FAIL {p.name}: {e}", flush=True)
            continue
        if not result:
            conn.execute("UPDATE images SET page_type='noise' WHERE id=?", (img_id,))
            continue
        lines = []
        for box, txt, conf in result:
            try:
                conf = float(conf)
                bx = [[float(b[0]), float(b[1])] for b in box]
            except Exception:
                continue
            lines.append({"text": txt, "conf": conf, "bbox": bx})
        conn.execute("UPDATE images SET raw_ocr_json=? WHERE id=?",
                     (json.dumps([[l["text"], round(l["conf"],3),
                                   [[int(b[0]),int(b[1])] for b in l["bbox"]]] for l in lines],
                                  ensure_ascii=False), img_id))
        ptype = process_lines(conn, img_id, lines)
        stats[ptype] = stats.get(ptype, 0) + 1
        if i % 20 == 0 or i == n:
            dt = time.time() - t0
            print(f"  [{i}/{n}] rate={i/dt:.2f}/s  stats={stats}", flush=True)
    conn.commit()
    print(f"[v2] done in {time.time()-t0:.1f}s  stats={stats}")
    return stats

def reprocess_from_db(conn, limit):
    """直接从已存的 raw_ocr_json(带bbox) 重跑解析, 跳过 OCR(秒级)."""
    rows = conn.execute(
        "SELECT id, raw_ocr_json FROM images WHERE page_type='other' AND raw_ocr_json IS NOT NULL ORDER BY id"
    ).fetchall()
    if limit: rows = rows[:limit]
    n = len(rows)
    print(f"[v2-from-db] re-parsing {n} 'other' images (no OCR) ...", flush=True)
    t0 = time.time()
    stats = {}
    for i, (img_id, raw) in enumerate(rows, 1):
        try:
            data = json.loads(raw)
        except Exception:
            continue
        lines = []
        for item in data:
            if len(item) >= 3:
                lines.append({"text": item[0], "conf": item[1], "bbox": item[2]})
        ptype = process_lines(conn, img_id, lines)
        stats[ptype] = stats.get(ptype, 0) + 1
        if i % 50 == 0 or i == n:
            print(f"  [{i}/{n}] stats={stats}", flush=True)
    conn.commit()
    print(f"[v2-from-db] done in {time.time()-t0:.1f}s  stats={stats}")
    return stats

# ---------------- 报告: 跨庄离散度 ----------------
def build_report(conn, out_json, out_md):
    # 聚合 cross_book_odds 按 (league,home,away)
    rows = conn.execute("""
        SELECT league, home, away, bookmaker, h_odds, d_odds, a_odds, captured_at
        FROM cross_book_odds
        WHERE h_odds IS NOT NULL AND d_odds IS NOT NULL AND a_odds IS NOT NULL
    """).fetchall()
    from collections import defaultdict
    agg = defaultdict(list)
    for r in rows:
        key = (r[0], r[1], r[2])
        agg[key].append({"bk": r[3], "h": r[4], "d": r[5], "a": r[6]})
    matches = []
    for key, books in agg.items():
        league, home, away = key
        # 每个机构只取一条(最新/第一条)
        seen = {}
        for b in books:
            if b["bk"] not in seen:
                seen[b["bk"]] = b
        clean = list(seen.values())
        if len(clean) < 2:
            continue
        # 每机构 devig -> 隐含概率
        imp = {}
        for b in clean:
            raw_sum = 1.0/b["h"] + 1.0/b["d"] + 1.0/b["a"]
            if 0.95 <= raw_sum <= 1.18:
                prob = devig_implied([b["h"], b["d"], b["a"]])
                if prob:
                    imp[b["bk"]] = prob
        if len(imp) < 2:
            continue
        # 仅保留 devig 合法(隐含概率和 0.95-1.18)的机构
        hp = [p[0] for p in imp.values()]; dp = [p[1] for p in imp.values()]; ap = [p[2] for p in imp.values()]
        spread_h = max(hp) - min(hp); spread_d = max(dp) - min(dp); spread_a = max(ap) - min(ap)
        # 共识(均值)
        cons_h, cons_d, cons_a = sum(hp)/len(hp), sum(dp)/len(dp), sum(ap)/len(ap)
        # 离散最大方(与共识差最大)
        def farthest(probs, cons):
            fb = max(imp.items(), key=lambda kv: abs(kv[1][probs] - cons))
            return fb[0], fb[1][probs]
        fh, fhv = farthest(0, cons_h); fa, fav = farthest(2, cons_a); fd, fdv = farthest(1, cons_d)
        max_spread = max(spread_h, spread_d, spread_a)
        # 跨庄最佳赔率(最高价 = 投注者可获最大 value)
        bh = max(clean, key=lambda b: b["h"]); bh_bk, bh_o = bh["bk"], bh["h"]
        bd = max(clean, key=lambda b: b["d"]); bd_bk, bd_o = bd["bk"], bd["d"]
        ba = max(clean, key=lambda b: b["a"]); ba_bk, ba_o = ba["bk"], ba["a"]
        matches.append({
            "league": league, "home": home, "away": away,
            "n_books": len(imp), "books": list(imp.keys()),
            "consensus": {"h": round(cons_h,4), "d": round(cons_d,4), "a": round(cons_a,4)},
            "spread": {"h": round(spread_h,4), "d": round(spread_d,4), "a": round(spread_a,4)},
            "max_spread_pp": round(max_spread*100, 2),
            "best_price": {
                "h": {"book": bh_bk, "odds": round(bh_o,2)},
                "d": {"book": bd_bk, "odds": round(bd_o,2)},
                "a": {"book": ba_bk, "odds": round(ba_o,2)},
            },
            "outlier": {
                "home_fav": fh, "home_fav_p": round(fhv,4),
                "draw_fav": fd, "draw_fav_p": round(fdv,4),
                "away_fav": fa, "away_fav_p": round(fav,4),
            },
        })
    matches.sort(key=lambda m: m["max_spread_pp"], reverse=True)
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total_cross_book_rows": len(rows),
        "matches_with_cross_book": len(matches),
        "edge_candidates_top": matches[:15],
        "all_matches": matches,
    }
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    # Markdown
    md = ["# 跨庄赔率离散度报告 (long_images v2)\n",
          f"- 生成: {report['generated_at']}",
          f"- cross_book 抽取行数: {len(rows)}",
          f"- 有跨庄数据的比赛: {len(matches)} 场\n",
          "## Top 跨庄分歧(潜在 edge) — 按最大离散度排序\n"]
    for m in matches[:15]:
        md.append(f"\n### {m['league'] or '?'} | {m['home']} vs {m['away']}")
        md.append(f"- 机构数: {m['n_books']}  ({', '.join(m['books'])})")
        md.append(f"- 共识隐含概率: 主胜 {m['consensus']['h']*100:.1f}% / 平 {m['consensus']['d']*100:.1f}% / 客胜 {m['consensus']['a']*100:.1f}%")
        md.append(f"- 离散(最大-最小): 主胜 {m['spread']['h']*100:.1f}pp / 平 {m['spread']['d']*100:.1f}pp / 客胜 {m['spread']['a']*100:.1f}pp")
        md.append(f"- **最大离散 {m['max_spread_pp']}pp** — 离群机构: 主胜[{m['outlier']['home_fav']}={m['outlier']['home_fav_p']*100:.1f}%] 平[{m['outlier']['draw_fav']}={m['outlier']['draw_fav_p']*100:.1f}%] 客胜[{m['outlier']['away_fav']}={m['outlier']['away_fav_p']*100:.1f}%]")
        bp = m["best_price"]
        md.append(f"- **跨庄最佳价(可下注最高赔)**: 主胜 {bp['h']['odds']}@{bp['h']['book']} / 平 {bp['d']['odds']}@{bp['d']['book']} / 客胜 {bp['a']['odds']}@{bp['a']['book']}")
    with open(out_md, "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    print(f"[report] {out_json}\n[report] {out_md}")
    print(f"[report] matches_with_cross_book={len(matches)}, top_spread={matches[0]['max_spread_pp'] if matches else 0}pp")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--from-db", action="store_true",
                    help="跳过OCR, 直接从已存 raw_ocr_json(带bbox)重跑解析(秒级)")
    ap.add_argument("--reset-cb", action="store_true",
                    help="清空 cross_book_odds 并把 v2 已分类页重置回 other(便于全量重抽)")
    args = ap.parse_args()
    conn = sqlite3.connect(args.db)
    # 建 cross_book_odds 表(幂等)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS cross_book_odds (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      image_id INTEGER REFERENCES images(id) ON DELETE CASCADE,
      league TEXT, home TEXT, away TEXT,
      bookmaker TEXT,
      h_odds REAL, d_odds REAL, a_odds REAL,
      h_kelly REAL, d_kelly REAL, a_kelly REAL,
      n_odds INTEGER, n_kelly INTEGER,
      raw_values TEXT, captured_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_cbo_match ON cross_book_odds(league,home,away);
    CREATE INDEX IF NOT EXISTS idx_cbo_image ON cross_book_odds(image_id);
    """)
    conn.commit()
    if args.reset_cb:
        conn.execute("DELETE FROM cross_book_odds")
        conn.execute("DELETE FROM image_odds WHERE image_id IN (SELECT id FROM images WHERE page_type IN ('cross_book','live_odds','odds_trend','lineup','text_live','noise'))")
        conn.execute("UPDATE images SET page_type='other', parsed_json=NULL WHERE page_type IN ('cross_book','live_odds','odds_trend','lineup','text_live','noise')")
        conn.commit()
        print("[reset] cross_book_odds cleared, v2 page_types -> other")
    if args.from_db:
        reprocess_from_db(conn, args.limit)
    elif not args.skip_reprocess:
        reprocess(conn, args.limit)
    build_report(conn, "data/long_images_v2_report.json", "data/long_images_v2_report.md")

if __name__ == "__main__":
    main()
