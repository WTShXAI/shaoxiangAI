# -*- coding: utf-8 -*-
"""阶段1: 双源融合构建滚球神器训练集 + 首轮规则分析
源:
  A) GQ.match_outcomes  -> 初盘1X2/OU/AH/CS + 半场 + 赛果 (6931场, 你列的四项全在)
  B) football_data.historical_matches -> 1X2开/收 + 赛果 + 实际总球 (312K, 海量+漂移)
记录(精简): 初盘1X2(A/D/H) + 初盘OU(line/over/under) + 初盘AH(line/home/away) + 初盘CS(波胆top10)
            + 收盘1X2(仅B) + 半场(仅A) + 赛果标签
派生(只用法允不变量: 去水概率/漂移/跨市场残差):
  1X2去水概率 p_h/p_d/p_a, 抽水 margin
  OU去水概率 p_over/p_under
  AH去水概率(单边)
  CS最热比分赔率/0-0赔率
  1X2开->收漂移 drift_h/d/a (仅B)
标签: result(H/D/A), is_draw, total_goals, over_ou(总球>OU线, 仅A有OU线)
"""
import sqlite3, json, math, os

FD = "data/football_data.db"
GQ = "data/events.db"
OUT = "data/rollball_training.db"

def con(path):
    c = sqlite3.connect(path, timeout=30)
    c.execute("PRAGMA busy_timeout=30000")
    return c

def margin_3(h, d, a):
    if not (h and d and a): return None
    s = 1.0/h + 1.0/d + 1.0/a
    if s <= 0: return None
    return s - 1.0

def implied(h, d, a):
    m = margin_3(h, d, a)
    if m is None: return (None, None, None, None)
    ov = 1.0 + m
    return (1.0/h/ov, 1.0/d/ov, 1.0/a/ov, m)

def ou_implied(over, under):
    if not (over and under): return (None, None, None)
    io, iu = 1.0/over, 1.0/under
    s = io + iu
    if s <= 0: return (None, None, None)
    return (io/s, iu/s, s-1.0)

def parse_cs(s):
    """返回 (最热比分赔率, 最热比分, 0-0赔率, 总球<=2.5格子均赔)"""
    if not s: return (None, None, None, None)
    try:
        arr = json.loads(s)
    except Exception:
        return (None, None, None, None)
    if not arr: return (None, None, None, None)
    # 找最热(最小赔率)
    best = min(arr, key=lambda x: x[1])
    fav_odds, fav_score = best[1], best[0]
    cs00 = None
    low_sum = []
    for sc, od in arr:
        if sc == "0-0": cs00 = od
        try:
            a, b = sc.split(":")
            if int(a) + int(b) <= 2: low_sum.append(od)
        except Exception:
            continue
    low_avg = sum(low_sum)/len(low_sum) if low_sum else None
    return (fav_odds, fav_score, cs00, low_avg)

rows = []

# ---- 源B: football_data (312K) ----
fdc = con(FD)
cur = fdc.execute("""SELECT home_team, away_team, league_name, match_date,
  open_home_odds, open_draw_odds, open_away_odds,
  close_home_odds, close_draw_odds, close_away_odds,
  home_score, away_score, final_result, total_goals
  FROM historical_matches
  WHERE open_home_odds IS NOT NULL AND open_draw_odds IS NOT NULL AND open_away_odds IS NOT NULL
    AND final_result IS NOT NULL""")
nb = 0
for home, away, lg, date, oh, od, oa, ch, cd, ca, hs, aws, res, tg in cur:
    nb += 1
    p_h, p_d, p_a, m = implied(oh, od, oa)
    drift_h = (ch-oh)/oh if (ch and oh) else None
    drift_d = (cd-od)/od if (cd and od) else None
    drift_a = (ca-oa)/oa if (ca and oa) else None
    rows.append({
        "src": "fd", "home": home, "away": away, "league": lg, "date": date,
        "op_h": oh, "op_d": od, "op_a": oa,
        "cl_h": ch, "cl_d": cd, "cl_a": ca,
        "ou_line": None, "ou_over": None, "ou_under": None,
        "ah_line": None, "ah_home": None, "ah_away": None,
        "cs_fav_odds": None, "cs00_odds": None, "cs_low_avg": None,
        "ht_h": None, "ht_a": None,
        "p_h": p_h, "p_d": p_d, "p_a": p_a, "margin": m,
        "p_over": None, "p_under": None,
        "drift_h": drift_h, "drift_d": drift_d, "drift_a": drift_a,
        "result": res, "is_draw": 1 if res == "D" else 0,
        "total_goals": tg, "over_ou": None,
    })
fdc.close()
print(f"[B] football_data loaded: {nb}")

# ---- 源A: GQ.match_outcomes (6931) ----
gqc = con(GQ)
cur = gqc.execute("""SELECT home, away, league, kickoff,
  op_1x2_h, op_1x2_d, op_1x2_a,
  op_ah_line, op_ah_home, op_ah_away,
  op_ou_line, op_ou_over, op_ou_under, op_cs,
  ht_score_home, ht_score_away, score_home, score_away, result
  FROM match_outcomes WHERE is_valid=1 AND result IS NOT NULL""")
na = 0
for home, away, lg, kick, oh, od, oa, ahl, ahh, aha, oul, ouo, ouu, cs, hth, hta, sh, sa, res in cur:
    na += 1
    p_h, p_d, p_a, m = implied(oh, od, oa)
    p_over, p_under, om = ou_implied(ouo, ouu) if (ouo and ouu and ouo > 0 and ouu > 0) else (None, None, None)
    cs_fav, cs_fav_sc, cs00, cs_low = parse_cs(cs)
    tg = (sh + sa) if (sh is not None and sa is not None) else None
    over_ou = (tg > oul) if (tg is not None and oul and oul > 0) else None
    rows.append({
        "src": "gq", "home": home, "away": away, "league": lg, "date": kick,
        "op_h": oh, "op_d": od, "op_a": oa,
        "cl_h": None, "cl_d": None, "cl_a": None,
        "ou_line": oul if (oul and oul > 0) else None,
        "ou_over": ouo if (ouo and ouo > 0) else None,
        "ou_under": ouu if (ouu and ouu > 0) else None,
        "ah_line": ahl if (ahl is not None and ahl != 0) else None,
        "ah_home": ahh, "ah_away": aha,
        "cs_fav_odds": cs_fav, "cs00_odds": cs00, "cs_low_avg": cs_low,
        "ht_h": hth, "ht_a": hta,
        "p_h": p_h, "p_d": p_d, "p_a": p_a, "margin": m,
        "p_over": p_over, "p_under": p_under,
        "drift_h": None, "drift_d": None, "drift_a": None,
        "result": {"home": "H", "draw": "D", "away": "A"}.get(res, res),
        "is_draw": 1 if res == "draw" else 0,
        "total_goals": tg, "over_ou": over_ou,
    })
gqc.close()
print(f"[A] GQ loaded: {na}")
print(f"[TOTAL] unified rows: {len(rows)}")

# ---- 落库 ----
if os.path.exists(OUT):
    os.remove(OUT)
oc = sqlite3.connect(OUT)
oc.execute("""CREATE TABLE rb_matches (
  id INTEGER PRIMARY KEY AUTOINCREMENT, src TEXT, home TEXT, away TEXT, league TEXT, date TEXT,
  op_h REAL, op_d REAL, op_a REAL, cl_h REAL, cl_d REAL, cl_a REAL,
  ou_line REAL, ou_over REAL, ou_under REAL,
  ah_line REAL, ah_home REAL, ah_away REAL,
  cs_fav_odds REAL, cs00_odds REAL, cs_low_avg REAL,
  ht_h REAL, ht_a REAL,
  p_h REAL, p_d REAL, p_a REAL, margin REAL,
  p_over REAL, p_under REAL,
  drift_h REAL, drift_d REAL, drift_a REAL,
  result TEXT, is_draw INTEGER, total_goals INTEGER, over_ou INTEGER)""")
cols = ["src","home","away","league","date","op_h","op_d","op_a","cl_h","cl_d","cl_a",
        "ou_line","ou_over","ou_under","ah_line","ah_home","ah_away",
        "cs_fav_odds","cs00_odds","cs_low_avg","ht_h","ht_a",
        "p_h","p_d","p_a","margin","p_over","p_under","drift_h","drift_d","drift_a",
        "result","is_draw","total_goals","over_ou"]
for r in rows:
    oc.execute("INSERT INTO rb_matches (" + ",".join(cols) + ") VALUES (" +
               ",".join("?"*len(cols)) + ")",
               [r.get(c) for c in cols])
oc.commit()
oc.close()
print(f"[SAVE] {OUT}")

# ===================== 首轮规则分析 =====================
print("\n" + "="*70)
print("分析①: 初盘大2.5/3.5 比赛的平局识别 (源A, 有OU线)")
print("="*70)
ga = [r for r in rows if r["src"] == "gq" and r["ou_line"] and r["p_d"] is not None]
for line in [2.5, 2.75, 3.0, 3.25, 3.5]:
    sub = [r for r in ga if abs(r["ou_line"] - line) < 0.01]
    if not sub: continue
    n = len(sub); draws = sum(r["is_draw"] for r in sub)
    base = draws / n
    # 规则: 1X2去水平局概率 p_d 高 -> 平局
    print(f"\n  OU线={line}: 样本={n}, 平局数={draws}, 平局率={base:.3f}")
    # 用 p_d 阈值扫描, 找能"识别平局"的切分
    for thr in [0.28, 0.30, 0.32, 0.34, 0.36]:
        flagged = [r for r in sub if r["p_d"] >= thr]
        if not flagged: 
            print(f"    p_d>={thr}: 命中0场"); continue
        fd_ = sum(r["is_draw"] for r in flagged); prec = fd_/len(flagged)
        # recall: 覆盖了多少真实平局
        rec = fd_/draws if draws else 0
        print(f"    p_d>={thr}: 标记{len(flagged)}场, 其中平局{fd_}场, 精确率={prec:.3f}, 召回={rec:.3f}")

print("\n" + "="*70)
print("分析②: 大于/小于初盘 (over_ou) 分离 (源A)")
print("="*70)
ov = [r for r in rows if r["src"] == "gq" and r["over_ou"] is not None and r["p_over"] is not None]
n = len(ov); over_hits = sum(r["over_ou"] for r in ov)
print(f"  有OU+赛果样本={n}, 大球命中={over_hits}, 大球率={over_hits/n:.3f}")
print(f"  庄家OU去水大球概率 p_over 均值={sum(r['p_over'] for r in ov)/n:.3f} (理论应~0.5)")
# 校准: p_over 分箱 vs 实际大球率
buckets = [(0.40,0.45),(0.45,0.50),(0.50,0.55),(0.55,0.60),(0.60,0.65)]
for lo, hi in buckets:
    b = [r for r in ov if lo <= r["p_over"] < hi]
    if not b: continue
    hr = sum(r["over_ou"] for r in b)/len(b)
    print(f"  p_over[{lo:.2f},{hi:.2f}): n={len(b)}, 实际大球率={hr:.3f}")

print("\n" + "="*70)
print("分析③: 1X2 去水概率 vs 实际结果 (全量312K, 源B)")
print("="*70)
fb = [r for r in rows if r["src"] == "fd" and r["p_h"] is not None]
n = len(fb)
# argmax 去水概率 命中率
hit = sum(1 for r in fb if (r["result"]=="H" and r["p_h"]>=r["p_d"] and r["p_h"]>=r["p_a"])
          or (r["result"]=="D" and r["p_d"]>=r["p_h"] and r["p_d"]>=r["p_a"])
          or (r["result"]=="A" and r["p_a"]>=r["p_h"] and r["p_a"]>=r["p_d"]))
print(f"  样本={n}, 去水概率argmax命中率={hit/n:.4f}")
# 平局: 去水平局概率是否准
db_ = [r for r in fb if r["is_draw"]==1]; nd=len(db_)
print(f"  平局样本={nd}, 平局去水均值p_d={sum(r['p_d'] for r in db_)/nd:.3f}")
print(f"  非平局去水均值p_d={sum(r['p_d'] for r in fb if r['is_draw']==0)/(n-nd):.3f}")
print("\n[DONE] 阶段1 数据+首轮分析完成")
