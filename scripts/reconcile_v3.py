# -*- coding: utf-8 -*-
"""
reconcile_v3.py — GQ 比分联网核对与校正 (权威源: 500网 live.500.com/wanchang.php)

v3 相对 v2 的三处升级
---------------------
1) 联赛映射三通道: ①队名自举 ②开赛时刻集合重叠 ③队名集合交叉验证(防小样本误配)
2) 桶内全局一对一指派: 按开赛时刻分桶, 桶内 GQ场×500场 打分后贪心一对一匹配,
   彻底避免"多场友谊赛互相抢同一条500记录"
3) 综合评分 = 0.72*队名分 + 0.28*联赛一致  —— 联赛映射缺失可靠队名救回,
   队名译名差异大可靠联赛救回

主客口径
--------
GQ 与 500网在友谊赛/中立场上主客常相反(队名 1.00 匹配但左右互换)。
本库全部盘口(让球/大小/波胆)均按 GQ 主客存储, 故落库一律以 GQ 主客为准,
swap 场写入时交换 500网比分, 保证内部一致。

用法
----
  python scripts/reconcile_v3.py                     # dry-run
  python scripts/reconcile_v3.py --apply             # 落库(备份+审计)
  python scripts/reconcile_v3.py --apply --levels A,B  # 只落 A/B 级
"""
import argparse
import collections
import datetime
import json
import os
import re
import shutil
import sqlite3
from difflib import SequenceMatcher

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "events.db")
SRC = os.path.join(ROOT, "data", "results_500.jsonl")
REPORT = os.path.join(ROOT, "data", "reconcile_v3_report.json")
AUDIT = os.path.join(ROOT, "data", "web_verify_audit.jsonl")
LGMAP_OUT = os.path.join(ROOT, "data", "league_map_500.json")
ALIAS_OUT = os.path.join(ROOT, "data", "team_alias_500.json")

VIRT = re.compile(r"\(\s*\d+\s*分钟\s*\)|VS-|模拟|梦幻对垒")
_ROMAN = {"Ⅰ": "1", "Ⅱ": "2", "Ⅲ": "3", "Ⅳ": "4", "Ⅴ": "5"}
_DROP = ["足球俱乐部", "俱乐部", "足球会", "足球队", "竞技会", "体育会", "队"]

W_NAME, W_LEAGUE = 0.72, 0.28


# ---------------------------------------------------------------- 基础工具
def norm(s):
    s = (s or "").strip()
    for k, v in _ROMAN.items():
        s = s.replace(k, v)
    s = re.sub(r"[（(]\s*女[子足]?\s*[)）]|女子|女足", "女", s)
    s = re.sub(r"\s+", "", s).replace("（", "(").replace("）", ")")
    for t in _DROP:
        s = s.replace(t, "")
    return s


_simcache = {}


def sim(a, b):
    key = (a, b)
    if key in _simcache:
        return _simcache[key]
    na, nb = norm(a), norm(b)
    if not na or not nb:
        v = 0.0
    elif na == nb:
        v = 1.0
    else:
        v = 0.0
        if len(na) >= 2 and len(nb) >= 2 and (na in nb or nb in na):
            sh, lo = (na, nb) if len(na) < len(nb) else (nb, na)
            if len(sh) / len(lo) >= 0.45:
                v = 0.88
        r = SequenceMatcher(None, na, nb).ratio()
        sa, sb = set(na), set(nb)
        j = len(sa & sb) / max(1, len(sa | sb))
        v = max(v, r, j)
    _simcache[key] = v
    return v


def real_dt_key(rec):
    try:
        dp, tp = rec["time"].split(" ")
        mm, dd = dp.split("-")
        hh, mi = tp.split(":")
        y = int(rec["date"][:4])
        base = datetime.datetime.strptime(rec["date"], "%Y-%m-%d")
        d = datetime.datetime(y, int(mm), int(dd), int(hh), int(mi))
        if (d - base).days > 200:
            d = d.replace(year=y - 1)
        if (base - d).days > 200:
            d = d.replace(year=y + 1)
        return d.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return None


# ---------------------------------------------------------------- 载入
def load_500():
    buckets = collections.defaultdict(list)
    n = 0
    with open(SRC, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if r.get("ft_home") is None:
                continue
            k = real_dt_key(r)
            if not k:
                continue
            r["_key"] = k
            buckets[k].append(r)
            n += 1
    return buckets, n


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
    out = []
    for r in rows:
        g = dict(zip(keys, r))
        if VIRT.search(g["league"] or ""):
            continue
        g["_key"] = g["kickoff"][:16]
        out.append(g)
    return out


# ---------------------------------------------------------------- 联赛映射
def name_pair(g, c, alias=None):
    """(best, orientation) —— 队名双边最小相似度, 取 direct/swap 较优者"""
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


def build_maps(gq, buckets):
    """通道1: 队名高置信自举 -> 联赛映射 + 队名别名"""
    lg = collections.defaultdict(collections.Counter)
    al = collections.defaultdict(collections.Counter)
    for g in gq:
        best, bs, bo = None, 0.0, "direct"
        for c in buckets.get(g["_key"], []):
            s, o = name_pair(g, c)
            if s > bs:
                bs, bo, best = s, o, c
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
    AL = {k: v.most_common(1)[0][0] for k, v in al.items()}
    return LG, AL, set(LG)          # 第三返回值 = 高置信(队名自举)映射的联赛集合


def extend_maps_by_time(gq, buckets, LG, AL):
    """通道2+3: 队名集合重合率主导 + 时刻集合重叠辅助

    注: 不能只靠时刻重叠 —— 实测 MLS(美国职业大联盟->美职联) 队名 100% 重合
    但开赛时刻普遍差 10 分钟, 时刻重叠仅 0.08。故以队名重合率为主判据。
    """
    t_gq = collections.defaultdict(set)
    n_gq = collections.defaultdict(set)
    d_gq = collections.defaultdict(set)
    for g in gq:
        t_gq[g["league"]].add(g["_key"])
        n_gq[g["league"]].update([g["home"], g["away"]])
        d_gq[g["league"]].add(g["_key"][:10])
    t_5 = collections.defaultdict(set)
    n_5 = collections.defaultdict(set)
    d_5 = collections.defaultdict(set)
    for k, lst in buckets.items():
        for c in lst:
            t_5[c["league"]].add(k)
            n_5[c["league"]].update([c["home"], c["away"]])
            d_5[c["league"]].add(k[:10])

    added = {}
    for lg, names in n_gq.items():
        if lg in LG or len(t_gq[lg]) < 2:
            continue
        best, brate, bov = None, 0.0, 0.0
        for l5, n5 in n_5.items():
            if not (d_gq[lg] & d_5[l5]):
                continue
            ratio = len(t_5[l5]) / max(1, len(t_gq[lg]))
            if not (0.25 <= ratio <= 8):
                continue
            hit = sum(1 for nm in names if any(sim(nm, x) >= 0.62 for x in n5))
            rate = hit / max(1, len(names))
            ov = len(t_gq[lg] & t_5[l5]) / max(1, len(t_gq[lg]))
            sc = rate + 0.25 * ov
            if sc > brate + 0.25 * bov:
                best, brate, bov = l5, rate, ov
        # 队名重合率主导; 时刻高度重叠时可略放宽
        if best and (brate >= 0.45 or (brate >= 0.30 and bov >= 0.7)):
            added[lg] = best
    LG.update(added)
    return added


# ---------------------------------------------------------------- 桶内指派
def assign_bucket(gs, cs, LG, AL, min_total=0.20):
    """桶内 GQ场 × 500场 全局贪心一对一指派, 返回 [(g, c, name_score, orient, lg_ok, total)]"""
    pairs = []
    for gi, g in enumerate(gs):
        tgt = LG.get(g["league"])
        for ci, c in enumerate(cs):
            ns, o = name_pair(g, c, AL)
            lg_ok = 1 if (tgt and c["league"] == tgt) else 0
            total = W_NAME * ns + W_LEAGUE * lg_ok
            if total < min_total:
                continue
            pairs.append((total, ns, lg_ok, o, gi, ci))
    pairs.sort(key=lambda x: -x[0])
    used_g, used_c, out = set(), set(), []
    for total, ns, lg_ok, o, gi, ci in pairs:
        if gi in used_g or ci in used_c:
            continue
        used_g.add(gi)
        used_c.add(ci)
        out.append((gs[gi], cs[ci], ns, o, lg_ok, total))
    return out


def _dt(s):
    return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M")


def assign_window(gs, cs, LG, AL, max_min=20):
    """二轮: 对一轮未配对的场, 在 ±max_min 分钟窗口内做一对一指派
    (实测 MLS 等联赛两边开赛时刻普遍差 10 分钟)"""
    pairs = []
    for gi, g in enumerate(gs):
        tgt = LG.get(g["league"])
        gd = _dt(g["_key"])
        for ci, c in enumerate(cs):
            dm = abs((_dt(c["_key"]) - gd).total_seconds()) / 60.0
            if dm > max_min:
                continue
            ns, o = name_pair(g, c, AL)
            lg_ok = 1 if (tgt and c["league"] == tgt) else 0
            total = W_NAME * ns + W_LEAGUE * lg_ok - 0.004 * dm
            if total < 0.34:          # 二轮门槛更高, 防跨场误配
                continue
            pairs.append((total, ns, lg_ok, o, gi, ci))
    pairs.sort(key=lambda x: -x[0])
    used_g, used_c, out = set(), set(), []
    for total, ns, lg_ok, o, gi, ci in pairs:
        if gi in used_g or ci in used_c:
            continue
        used_g.add(gi)
        used_c.add(ci)
        out.append((gs[gi], cs[ci], ns, o, lg_ok, total))
    return out


def grade(ns, lg_ok, g, c, n_same_league_in_bucket, strong=False):
    """置信分级
    strong=True 表示该场的联赛映射来自"队名自举"(高置信), 而非"队名重合率推断"。
    只有 strong 映射才允许"同时刻同联赛唯一场次"这种不看队名的 B 级判定。
    """
    if ns >= 0.62:
        return "A", "队名强匹配"
    if lg_ok and ns >= 0.42:
        return "A", "联赛一致+队名中匹配"
    if lg_ok and strong and n_same_league_in_bucket == 1:
        if ns >= 0.15:
            return "B", "同时刻同联赛唯一场次"
        return "SUSPECT", "同时刻同联赛唯一但队名完全不符"
    if lg_ok and strong and ns >= 0.28:
        return "C", "联赛一致+队名弱匹配"
    if lg_ok and ns >= 0.38:
        return "C", "联赛(推断)一致+队名弱匹配"
    if ns >= 0.5:
        return "C", "队名中匹配(无联赛映射)"
    return "NOMATCH", "证据不足"


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--levels", default="A,B,C")
    args = ap.parse_args()
    levels = set(x.strip().upper() for x in args.levels.split(","))

    buckets, n500 = load_500()
    gq = load_gq()
    print("[SRC ] 500网完场记录 %d / GQ 待核对(已剔虚拟赛事) %d" % (n500, len(gq)))

    LG, AL, LG_STRONG = build_maps(gq, buckets)
    print("[MAP1] 队名自举联赛映射 %d, 队名别名 %d" % (len(LG), len(AL)))
    added = extend_maps_by_time(gq, buckets, LG, AL)
    print("[MAP2] 时刻重叠+队名验证新增映射 %d -> 合计 %d" % (len(added), len(LG)))
    if added:
        for k, v in list(added.items())[:12]:
            print("        %-30s -> %s" % (k, v))
    json.dump(LG, open(LGMAP_OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(AL, open(ALIAS_OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    gq_by_key = collections.defaultdict(list)
    for g in gq:
        gq_by_key[g["_key"]].append(g)

    stat = collections.Counter()
    plans, suspects, nomatch = [], [], []
    lgcnt_all = {k: collections.Counter(c["league"] for c in lst) for k, lst in buckets.items()}
    used_c, decided = set(), {}
    leftover_g = []

    def cid(c):
        return (c["date"], c["mid500"])

    def take(g, c, ns, o, lg_ok, nlg):
        lv, note = grade(ns, lg_ok, g, c, nlg, g["league"] in LG_STRONG)
        decided[g["mid"]] = True
        stat[lv] += 1
        if lv in ("A", "B", "C"):
            used_c.add(cid(c))
            if o == "direct":
                ft, ht = (c["ft_home"], c["ft_away"]), (c["ht_home"], c["ht_away"])
            else:
                ft, ht = (c["ft_away"], c["ft_home"]), (c["ht_away"], c["ht_home"])
                stat["_swap"] += 1
            plans.append({"g": g, "c": c, "lv": lv, "ns": round(ns, 3),
                          "orient": o, "note": note, "ft": ft, "ht": ht})
        elif lv == "SUSPECT":
            suspects.append({"mid": g["mid"], "league": g["league"],
                             "gq": "%s vs %s" % (g["home"], g["away"]),
                             "w500": "%s vs %s (%s)" % (c["home"], c["away"], c["league"]),
                             "kickoff": g["kickoff"], "ns": round(ns, 3), "note": note})
        else:
            nomatch.append({"mid": g["mid"], "league": g["league"],
                            "gq": "%s vs %s" % (g["home"], g["away"]),
                            "kickoff": g["kickoff"], "note": note})

    # ---- 一轮: 精确时刻桶指派 ----
    for key, gs in gq_by_key.items():
        cs = buckets.get(key, [])
        if not cs:
            leftover_g.extend(gs)
            continue
        res = assign_bucket(gs, cs, LG, AL)
        got = set()
        for g, c, ns, o, lg_ok, total in res:
            got.add(g["mid"])
            take(g, c, ns, o, lg_ok, lgcnt_all[key][c["league"]])
        leftover_g.extend([g for g in gs if g["mid"] not in got])

    # ---- 二轮: ±20 分钟时间窗补救 ----
    stat["_r1_matched"] = len(plans)
    free_c = [c for lst in buckets.values() for c in lst if cid(c) not in used_c]
    win_g = collections.defaultdict(list)
    win_c = collections.defaultdict(list)
    for g in leftover_g:
        win_g[g["_key"][:13]].append(g)          # 按"小时"粗分组
    for c in free_c:
        h = _dt(c["_key"])
        for off in (-1, 0, 1):
            win_c[(h + datetime.timedelta(hours=off)).strftime("%Y-%m-%d %H")].append(c)
    r2_got = set()
    for hk, gs in win_g.items():
        cs = win_c.get(hk, [])
        if not cs:
            continue
        cs = [c for c in cs if cid(c) not in used_c]
        if not cs:
            continue
        for g, c, ns, o, lg_ok, total in assign_window(gs, cs, LG, AL):
            if cid(c) in used_c:
                continue
            r2_got.add(g["mid"])
            take(g, c, ns, o, lg_ok, lgcnt_all[c["_key"]][c["league"]])
    stat["_r2_matched"] = len(plans) - stat["_r1_matched"]

    for g in leftover_g:
        if g["mid"] in decided:
            continue
        stat["UNPAIRED"] += 1
        nomatch.append({"mid": g["mid"], "league": g["league"],
                        "gq": "%s vs %s" % (g["home"], g["away"]),
                        "kickoff": g["kickoff"], "note": "无可配对500记录(疑500网未收录)"})

    # ---- 差异统计 ----
    d = collections.Counter()
    flips = []
    for p in plans:
        g, ft, ht = p["g"], p["ft"], p["ht"]
        if g["sh"] is None or g["sa"] is None:
            d["ft_fill_null"] += 1
        elif (g["sh"], g["sa"]) != ft:
            d["ft_diff"] += 1
            ours, web = g["sh"] + g["sa"], ft[0] + ft[1]
            d["dir_lower" if ours < web else ("dir_higher" if ours > web else "dir_eqsum")] += 1
            r1 = (g["sh"] > g["sa"]) - (g["sh"] < g["sa"])
            r2 = (ft[0] > ft[1]) - (ft[0] < ft[1])
            if r1 != r2:
                d["result_flip"] += 1
                if len(flips) < 25:
                    flips.append("%s %s | %s %d-%d %s => %d-%d [%s%s]" % (
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
        else:
            d["ht_none_in_web"] += 1
        if g["status"] != "finished":
            d["status_fix"] += 1

    tot = len(gq)
    print("\n" + "=" * 70)
    print("[MATCH] 可落库 %d / %d = %.1f%%   (A=%d B=%d C=%d) | 主客颠倒 %d" % (
        len(plans), tot, 100.0 * len(plans) / tot, stat["A"], stat["B"], stat["C"], stat["_swap"]))
    print("        一轮精确时刻 %d + 二轮±20分钟窗 %d" % (stat["_r1_matched"], stat["_r2_matched"]))
    print("[SKIP ] 可疑不落库 %d | 证据不足 %d | 500网未收录 %d" % (
        stat["SUSPECT"], stat["NOMATCH"], stat["UNPAIRED"]))
    print("-" * 70)
    print("  终场  空值补录 %-5d  不一致纠正 %-5d  本来一致 %-5d" % (
        d["ft_fill_null"], d["ft_diff"], d["ft_same"]))
    print("        方向: 我方偏低(漏球) %-5d  偏高 %-5d  总球同但比分错 %d" % (
        d["dir_lower"], d["dir_higher"], d["dir_eqsum"]))
    print("  半场  补录 %-5d  纠正 %-5d  本来一致 %-5d  (500网无半场 %d)" % (
        d["ht_fill"], d["ht_diff"], d["ht_same"], d["ht_none_in_web"]))
    print("  状态  live/scheduled -> finished  %d 场" % d["status_fix"])
    print("  ** 胜平负方向被改写: %d 场 **" % d["result_flip"])
    print("=" * 70)
    print("\n胜负翻转样例:")
    for x in flips[:15]:
        print("  ", x)

    rep = {"ts": datetime.datetime.now().isoformat(), "src_500": n500, "gq_total": tot,
           "levels": {k: stat[k] for k in ("A", "B", "C", "SUSPECT", "NOMATCH", "NOCAND", "UNPAIRED")},
           "swap": stat["_swap"], "diff": dict(d), "flip_samples": flips,
           "suspect_samples": suspects[:300], "nomatch_samples": nomatch[:300],
           "nomatch_by_league": collections.Counter(x["league"] for x in nomatch).most_common(50)}
    json.dump(rep, open(REPORT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n[REPORT] %s" % REPORT)

    if not args.apply:
        print("[DRY-RUN] 未写库。确认后加 --apply")
        return

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
        cur.execute("UPDATE matches SET score_home=?,score_away=?,ht_score_home=?,"
                    "ht_score_away=?,status='finished',minute=90 WHERE mid=?",
                    (ft[0], ft[1], ht[0], ht[1], g["mid"]))
        nm += cur.rowcount
        # match_outcomes.result 规范编码 = home/draw/away (见 gq/db.py:40 表定义注释)。
        # 曾误写 '3'/'1'/'0', 导致同库两套编码并存, 下游 group by result 会把
        # 同一种结果拆成两类。2026-08-03 已统一存量 1066 行并在此堵源。
        res = "home" if ft[0] > ft[1] else ("draw" if ft[0] == ft[1] else "away")
        cur.execute("UPDATE match_outcomes SET score_home=?,score_away=?,ht_score_home=?,"
                    "ht_score_away=?,result=? WHERE mid=?",
                    (ft[0], ft[1], ht[0], ht[1], res, g["mid"]))
        no += cur.rowcount
        aud.write(json.dumps({
            "mid": g["mid"], "league": g["league"], "home": g["home"], "away": g["away"],
            "kickoff": g["kickoff"], "our_ft": [g["sh"], g["sa"]], "web_ft": list(ft),
            "our_ht": [g["hh"], g["ha"]], "web_ht": list(ht), "level": p["lv"],
            "name_score": p["ns"], "orient": p["orient"],
            "w500": "%s vs %s" % (p["c"]["home"], p["c"]["away"]),
            "action": "correct", "source": "500.com/wanchang", "tool": "reconcile_v3",
            "ts": datetime.datetime.now().isoformat()}, ensure_ascii=False) + "\n")
    con.commit()
    con.close()
    aud.close()
    print("[APPLY] matches %d 行 / match_outcomes %d 行 已更新, 审计已追加" % (nm, no))


if __name__ == "__main__":
    main()
