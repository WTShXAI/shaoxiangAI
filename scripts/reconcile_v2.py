# -*- coding: utf-8 -*-
"""
reconcile_v2.py — GQ 比分联网核对与校正 (权威源: 500网 live.500.com/wanchang.php)

设计要点
--------
1) 时间主键: 用 500网行内 td[2] 的真实开赛时刻(MM-DD HH:MM)还原 datetime, 与 GQ.matches.kickoff 精确对齐
   (实测 3345/3494 非虚拟场存在精确同时刻候选, 时间口径高度一致)
2) 联赛映射自举: 先用"双边队名高相似"得到高置信对, 统计 GQ联赛全称 -> 500网联赛简称 的映射
3) 队名别名自举: 从高置信对中提取 (GQ队名 -> 500队名) 别名, 二轮匹配时直接命中
4) 主客颠倒检测: 大量友谊赛 GQ 与 500网 主客相反(队名 1.00 匹配)。
   ** 落库一律以 GQ 主客口径为准 ** —— 因为本库所有盘口/让球/大小都是按 GQ 主客存的,
   若按 500网口径写比分, 让球方向会全错。故 swap 场写入时交换 500网比分。
5) 分级置信: A/B/C 落库, SUSPECT 与 NOMATCH 只报告不落库

用法
----
  python scripts/reconcile_v2.py                # dry-run, 出报告
  python scripts/reconcile_v2.py --apply        # 落库(自动备份 + 审计)
"""
import argparse
import collections
import datetime
import json
import os
import re
import shutil
import sqlite3
import sys
from difflib import SequenceMatcher

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "events.db")
SRC = os.path.join(ROOT, "data", "results_500.jsonl")
REPORT = os.path.join(ROOT, "data", "reconcile_v2_report.json")
AUDIT = os.path.join(ROOT, "data", "web_verify_audit.jsonl")
ALIAS_OUT = os.path.join(ROOT, "data", "team_alias_500.json")
LGMAP_OUT = os.path.join(ROOT, "data", "league_map_500.json")

VIRT = re.compile(r"\(\s*\d+\s*分钟\s*\)|VS-|模拟|梦幻对垒")
_ROMAN = {"Ⅰ": "1", "Ⅱ": "2", "Ⅲ": "3", "Ⅳ": "4", "Ⅴ": "5"}
_DROP = ["足球俱乐部", "俱乐部", "足球会", "足球队", "竞技会", "体育会", "队"]


def norm(s):
    """队名归一化: 女足口径统一 / 去噪词 / 全半角 / 罗马数字"""
    s = (s or "").strip()
    for k, v in _ROMAN.items():
        s = s.replace(k, v)
    s = re.sub(r"[（(]\s*女[子足]?\s*[)）]|女子|女足", "女", s)
    s = re.sub(r"\s+", "", s)
    s = s.replace("（", "(").replace("）", ")")
    for t in _DROP:
        s = s.replace(t, "")
    return s


def sim(a, b):
    """相似度 = max(序列比, 字符集 Jaccard, 子串包含分)"""
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # 子串包含(如 陕西联合 ⊂ 陕西联合月亮泊)
    if len(a) >= 2 and len(b) >= 2 and (a in b or b in a):
        shorter, longer = (a, b) if len(a) < len(b) else (b, a)
        if len(shorter) / len(longer) >= 0.45:
            return 0.88
    r = SequenceMatcher(None, a, b).ratio()
    sa, sb = set(a), set(b)
    j = len(sa & sb) / max(1, len(sa | sb))
    return max(r, j)


def real_dt(rec):
    """由 500网 date + td[2] 的 'MM-DD HH:MM' 还原真实开赛 datetime(跨年保护)"""
    try:
        dpart, tpart = rec["time"].split(" ")
        mm, dd = dpart.split("-")
        hh, mi = tpart.split(":")
        y = int(rec["date"][:4])
        base = datetime.datetime.strptime(rec["date"], "%Y-%m-%d")
        d = datetime.datetime(y, int(mm), int(dd), int(hh), int(mi))
        if (d - base).days > 200:
            d = d.replace(year=y - 1)
        if (base - d).days > 200:
            d = d.replace(year=y + 1)
        return d
    except Exception:
        return None


def load_500():
    by_time = collections.defaultdict(list)
    n = 0
    with open(SRC, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if r.get("ft_home") is None:
                continue
            d = real_dt(r)
            if not d:
                continue
            r["_dt"] = d
            by_time[d.strftime("%Y-%m-%d %H:%M")].append(r)
            n += 1
    return by_time, n


def load_gq():
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT mid,league,home,away,kickoff,status,score_home,score_away,"
        "ht_score_home,ht_score_away,minute FROM matches "
        "WHERE kickoff!='' AND kickoff IS NOT NULL"
    ).fetchall()
    con.close()
    keys = ["mid", "league", "home", "away", "kickoff", "status",
            "sh", "sa", "hh", "ha", "minute"]
    return [dict(zip(keys, r)) for r in rows]


def pair_score(g, c, alias=None):
    """返回 (best_score, orientation) ; orientation in {'direct','swap'}"""
    def s(x, y):
        v = sim(x, y)
        if alias:
            ax = alias.get(norm(x))
            if ax:
                v = max(v, sim(ax, y))
        return v
    d = min(s(g["home"], c["home"]), s(g["away"], c["away"]))
    w = min(s(g["home"], c["away"]), s(g["away"], c["home"]))
    return (d, "direct") if d >= w else (w, "swap")


def bootstrap(gq, by_time):
    """一轮高置信匹配 -> 学联赛映射 + 队名别名"""
    lg = collections.defaultdict(collections.Counter)
    al = collections.defaultdict(collections.Counter)
    for g in gq:
        if VIRT.search(g["league"] or ""):
            continue
        best, bs, bo = None, 0.0, "direct"
        for c in by_time.get(g["kickoff"][:16], []):
            sc, o = pair_score(g, c)
            if sc > bs:
                bs, bo, best = sc, o, c
        if best and bs >= 0.75:
            lg[g["league"]][best["league"]] += 1
            if bo == "direct":
                al[norm(g["home"])][best["home"]] += 1
                al[norm(g["away"])][best["away"]] += 1
            else:
                al[norm(g["home"])][best["away"]] += 1
                al[norm(g["away"])][best["home"]] += 1
    LG = {k: v.most_common(1)[0][0] for k, v in lg.items()
          if v.most_common(1)[0][1] / sum(v.values()) >= 0.8}
    AL = {k: v.most_common(1)[0][0] for k, v in al.items()
          if v.most_common(1)[0][1] >= 1}
    return LG, AL


def classify(g, by_time, LG, AL):
    """返回 (level, cand, score, orientation, note)"""
    cands = by_time.get(g["kickoff"][:16], [])
    if not cands:
        return "NOCAND", None, 0.0, "direct", "该时刻500网无任何完场记录"
    tgt = LG.get(g["league"])
    sub = [c for c in cands if c["league"] == tgt] if tgt else []
    pool = sub if sub else cands
    scored = []
    for c in pool:
        sc, o = pair_score(g, c, AL)
        scored.append((sc, o, c))
    scored.sort(key=lambda x: -x[0])
    top = scored[0]
    second = scored[1][0] if len(scored) > 1 else 0.0

    if not tgt:
        # 联赛未在映射表 -> 只接受极高队名分
        if top[0] >= 0.75 and top[0] - second >= 0.10:
            return "B", top[2], top[0], top[1], "无联赛映射/队名强匹配"
        return "NOMATCH", top[2], top[0], top[1], "500网未收录该联赛"
    if not sub:
        return "NOMATCH", None, 0.0, "direct", "同时刻该联赛无500网记录"

    # 单方相似度, 用于识别"一方对上一方完全不对"的可疑场
    def one_side_max(c, o):
        if o == "direct":
            return max(sim(g["home"], c["home"]), sim(g["away"], c["away"])), \
                   min(sim(g["home"], c["home"]), sim(g["away"], c["away"]))
        return max(sim(g["home"], c["away"]), sim(g["away"], c["home"])), \
               min(sim(g["home"], c["away"]), sim(g["away"], c["home"]))

    hi, lo = one_side_max(top[2], top[1])
    if hi >= 0.6 and lo < 0.22:
        return "SUSPECT", top[2], top[0], top[1], "一方队名对上/另一方完全不符"
    if top[0] >= 0.50:
        return "A", top[2], top[0], top[1], "同联赛同时刻+双边队名"
    if len(sub) == 1 and top[0] >= 0.30:
        return "B", top[2], top[0], top[1], "同联赛同时刻唯一场次"
    if top[0] >= 0.35 and top[0] - second >= 0.15:
        return "C", top[2], top[0], top[1], "同联赛同时刻择优(领先次优)"
    if len(sub) == 1:
        return "SUSPECT", top[2], top[0], top[1], "唯一场次但队名极低"
    return "NOMATCH", top[2], top[0], top[1], "同联赛多场且无法区分"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="落库(默认 dry-run)")
    ap.add_argument("--levels", default="A,B,C", help="落库的置信等级")
    args = ap.parse_args()
    levels = set(x.strip().upper() for x in args.levels.split(","))

    by_time, n500 = load_500()
    gq = load_gq()
    print("[SRC ] 500网有比分记录 %d 条 / GQ 待核对 %d 场" % (n500, len(gq)))

    LG, AL = bootstrap(gq, by_time)
    print("[BOOT] 学到联赛映射 %d 条, 队名别名 %d 条" % (len(LG), len(AL)))
    json.dump(LG, open(LGMAP_OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(AL, open(ALIAS_OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    stat = collections.Counter()
    plans, suspects, nomatch = [], [], []
    for g in gq:
        if VIRT.search(g["league"] or ""):
            stat["VIRTUAL"] += 1
            continue
        lv, c, sc, o, note = classify(g, by_time, LG, AL)
        stat[lv] += 1
        if lv in ("A", "B", "C"):
            # 以 GQ 主客口径落库
            if o == "direct":
                ft = (c["ft_home"], c["ft_away"])
                ht = (c["ht_home"], c["ht_away"])
            else:
                ft = (c["ft_away"], c["ft_home"])
                ht = (c["ht_away"], c["ht_home"])
                stat["_swap"] += 1
            plans.append({"g": g, "c": c, "lv": lv, "score": round(sc, 3),
                          "orient": o, "note": note, "ft": ft, "ht": ht})
        elif lv == "SUSPECT":
            suspects.append({"mid": g["mid"], "league": g["league"],
                             "gq": "%s vs %s" % (g["home"], g["away"]),
                             "w500": "%s vs %s" % (c["home"], c["away"]) if c else "",
                             "kickoff": g["kickoff"], "score": round(sc, 3), "note": note})
        else:
            nomatch.append({"mid": g["mid"], "league": g["league"],
                            "gq": "%s vs %s" % (g["home"], g["away"]),
                            "kickoff": g["kickoff"], "note": note})

    # ---- 差异统计 ----
    d = collections.Counter()
    flips, ft_examples = [], []
    for p in plans:
        g, ft, ht = p["g"], p["ft"], p["ht"]
        if g["sh"] is None or g["sa"] is None:
            d["ft_fill_null"] += 1
        elif (g["sh"], g["sa"]) != ft:
            d["ft_diff"] += 1
            ours, web = g["sh"] + g["sa"], ft[0] + ft[1]
            if ours < web:
                d["dir_lower"] += 1
            elif ours > web:
                d["dir_higher"] += 1
            else:
                d["dir_eqsum"] += 1
            r1 = (g["sh"] > g["sa"]) - (g["sh"] < g["sa"])
            r2 = (ft[0] > ft[1]) - (ft[0] < ft[1])
            if r1 != r2:
                d["result_flip"] += 1
                if len(flips) < 20:
                    flips.append("%s %s | %s %d-%d %s  =>  %d-%d  [%s%s]" % (
                        g["mid"], g["league"], g["home"], g["sh"], g["sa"], g["away"],
                        ft[0], ft[1], p["lv"], "/主客换" if p["orient"] == "swap" else ""))
        else:
            d["ft_same"] += 1
        if ht[0] is not None:
            if g["hh"] is None:
                d["ht_fill"] += 1
            elif (g["hh"], g["ha"]) != ht:
                d["ht_diff"] += 1
            else:
                d["ht_same"] += 1
        if g["status"] != "finished":
            d["status_fix"] += 1

    print("\n" + "=" * 68)
    print("[MATCH] 可落库 %d 场 (A=%d B=%d C=%d) | 主客颠倒 %d" % (
        len(plans), stat["A"], stat["B"], stat["C"], stat["_swap"]))
    print("[SKIP ] 虚拟赛事 %d | 可疑不落库 %d | 无法核对 %d | 该时刻无记录 %d" % (
        stat["VIRTUAL"], stat["SUSPECT"], stat["NOMATCH"], stat["NOCAND"]))
    print("-" * 68)
    print("  终场: 空值补录 %-5d 不一致纠正 %-5d 一致 %-5d" % (
        d["ft_fill_null"], d["ft_diff"], d["ft_same"]))
    print("        方向 我方偏低(漏球) %-5d 偏高 %-5d 总球同但比分错 %d" % (
        d["dir_lower"], d["dir_higher"], d["dir_eqsum"]))
    print("  半场: 补录 %-5d 纠正 %-5d 一致 %d" % (d["ht_fill"], d["ht_diff"], d["ht_same"]))
    print("  状态: 由 live/scheduled 修正为 finished  %d 场" % d["status_fix"])
    print("  ** 胜平负方向被改写: %d 场 **" % d["result_flip"])
    print("=" * 68)
    print("\n胜负翻转样例:")
    for x in flips[:15]:
        print("  ", x)
    print("\n可疑(不落库)样例:")
    for x in suspects[:8]:
        print("   %s | %s | GQ:%s | 500:%s | %s" % (
            x["mid"], x["league"], x["gq"], x["w500"], x["note"]))

    rep = {"ts": datetime.datetime.now().isoformat(),
           "src_500": n500, "gq_total": len(gq),
           "levels": {k: stat[k] for k in ("A", "B", "C", "SUSPECT", "NOMATCH", "NOCAND", "VIRTUAL")},
           "swap": stat["_swap"], "diff": dict(d),
           "flip_samples": flips, "suspect_samples": suspects[:200],
           "nomatch_samples": nomatch[:200],
           "nomatch_by_league": collections.Counter(x["league"] for x in nomatch).most_common(40)}
    json.dump(rep, open(REPORT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n[REPORT] %s" % REPORT)

    if not args.apply:
        print("[DRY-RUN] 未写库。确认后加 --apply")
        return

    # ---------------- 落库 ----------------
    bak = DB + ".bak_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(DB, bak)
    print("[BACKUP] %s" % bak)
    con = sqlite3.connect(DB)
    cur = con.cursor()
    aud = open(AUDIT, "a", encoding="utf-8")
    nm = no = 0
    for p in plans:
        if p["lv"] not in levels:
            continue
        g, ft, ht = p["g"], p["ft"], p["ht"]
        cur.execute(
            "UPDATE matches SET score_home=?,score_away=?,ht_score_home=?,"
            "ht_score_away=?,status='finished',minute=90 WHERE mid=?",
            (ft[0], ft[1], ht[0], ht[1], g["mid"]))
        nm += cur.rowcount
        res = "3" if ft[0] > ft[1] else ("1" if ft[0] == ft[1] else "0")
        cur.execute(
            "UPDATE match_outcomes SET score_home=?,score_away=?,ht_score_home=?,"
            "ht_score_away=?,result=? WHERE mid=?",
            (ft[0], ft[1], ht[0], ht[1], res, g["mid"]))
        no += cur.rowcount
        aud.write(json.dumps({
            "mid": g["mid"], "league": g["league"], "home": g["home"], "away": g["away"],
            "kickoff": g["kickoff"], "our_ft": [g["sh"], g["sa"]], "web_ft": list(ft),
            "our_ht": [g["hh"], g["ha"]], "web_ht": list(ht),
            "level": p["lv"], "score": p["score"], "orient": p["orient"],
            "w500": "%s vs %s" % (p["c"]["home"], p["c"]["away"]),
            "action": "correct", "source": "500.com/wanchang",
            "ts": datetime.datetime.now().isoformat()}, ensure_ascii=False) + "\n")
    con.commit()
    con.close()
    aud.close()
    print("[APPLY] matches 更新 %d 行 / match_outcomes 更新 %d 行 / 审计已追加" % (nm, no))


if __name__ == "__main__":
    main()
