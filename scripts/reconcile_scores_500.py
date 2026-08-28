# -*- coding: utf-8 -*-
"""
reconcile_scores_500.py — 用 500网权威赛果核对/校正 events.db 终场+半场比分

流程
====
  1. 载入 data/results_500.jsonl (由 scripts/web_results_500.py 抓取)
  2. 与 events.db matches 做匹配: (日期±1, 归一化主队, 归一化客队)
     - L1 精确: 归一化队名全等
     - L2 模糊: 同日同联赛下, 主客队名相似度(SequenceMatcher) 双双 >= 阈值
  3. 比对 FT/HT, 生成差异报告
  4. --apply 时以 500 网为准覆盖 matches + match_outcomes, 全部写审计

安全设计 (可回退)
====
  - 默认 dry-run, 只出报告, 不写库
  - 只在"500 网该场有明确比分 + 状态为完场"时才覆盖
  - 每笔覆盖写 data/web_verify_audit.jsonl (含 our/web 双值), 可反向回滚
  - --apply 前自动备份 events.db → data/events.db.bak_YYYYmmdd_HHMMSS

用法
====
  python scripts/reconcile_scores_500.py                 # dry-run 报告
  python scripts/reconcile_scores_500.py --fuzzy 0.86    # 调模糊阈值
  python scripts/reconcile_scores_500.py --apply         # 落库
"""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import re
import shutil
import sqlite3
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "events.db"
SRC = ROOT / "data" / "results_500.jsonl"
AUDIT = ROOT / "data" / "web_verify_audit.jsonl"
REPORT = ROOT / "data" / "reconcile_500_report.json"

# 队名归一化: 两站译名差异极大(米拉索尔/迈拉索尔SP、博伊西/博伊西AC、
# 艾云达尔女足/艾文代尔(女)、摩顿城精英II队U23/莫顿城U23), 必须重度归一。
_DROP = ["足球俱乐部", "俱乐部", "足球会", "足球队", "竞技会", "体育会",
         "精英", "(中)", "（中）", "队"]
# 巴西州名/常见队名后缀 (仅在词尾剥离, 避免误伤队名主体)
_SUFFIX = ["SP", "RS", "MG", "RJ", "PR", "SC", "BA", "CE", "GO", "PE", "AL",
           "AC", "FC", "CF", "AFC", "CD", "SK", "IF", "FK", "BK"]
_FINISHED_500 = {"完", "完场"}


def norm(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"\[\d+\]", "", s)
    # 女足标记统一: (女)/女足/女子 → 女
    s = re.sub(r"[（(]\s*女\s*[)）]|女足|女子", "女", s)
    # 罗马数字统一
    for a, b in [("Ⅰ", "I"), ("Ⅱ", "II"), ("Ⅲ", "III"), ("Ⅳ", "IV")]:
        s = s.replace(a, b)
    # 保护年龄段标记(U23/U20/U19 是关键区分位), 先摘出再还原
    age = "".join(re.findall(r"U\d{2}", s, flags=re.I)).upper()
    s = re.sub(r"U\d{2}", "", s, flags=re.I)
    s = re.sub(r"II队|I{1,3}队|B队|II|III", "", s)
    for t in _DROP:
        s = s.replace(t, "")
    up = s.upper()
    for suf in _SUFFIX:
        if up.endswith(suf) and len(s) > len(suf) + 1:
            s = s[: -len(suf)]
            up = s.upper()
    return s + age


def sim(a: str, b: str) -> float:
    """中文音译差异大, 取 序列相似度 与 字符集 Jaccard 的较大者。"""
    if not a or not b:
        return 0.0
    seq = SequenceMatcher(None, a, b).ratio()
    sa, sb = set(a), set(b)
    jac = len(sa & sb) / max(1, len(sa | sb))
    return max(seq, jac)


def pair_score(h1, a1, h2, a2):
    """对阵相似度: 均分为主, 但任一侧过低则判负(防张冠李戴)。"""
    sh, sa = sim(h1, h2), sim(a1, a2)
    if min(sh, sa) < 0.34:
        return 0.0
    return (sh + sa) / 2.0


def shift(d: str, n: int) -> str:
    return (datetime.date.fromisoformat(d) + datetime.timedelta(days=n)).isoformat()


# 500网 td[2] 是真实开赛 "MM-DD HH:MM"; 顶部 date 只是页面归属日, 二者常差一天。
# 必须用 td[2] 还原真实 datetime, 否则跨零点场次会错配到隔天同名对阵。
_T_RE = re.compile(r"(\d{2})-(\d{2})\s+(\d{2}):(\d{2})")


def real_dt(rec: dict):
    """由 rec['date'](页面日) + rec['time']('MM-DD HH:MM') 还原真实开赛 datetime。"""
    m = _T_RE.search(rec.get("time", "") or "")
    if not m:
        return None
    mm, dd, hh, mi = (int(x) for x in m.groups())
    base = datetime.date.fromisoformat(rec["date"])
    year = base.year
    # 跨年保护: 页面日在 12 月而比赛月为 1 月 → 次年
    if base.month == 12 and mm == 1:
        year += 1
    elif base.month == 1 and mm == 12:
        year -= 1
    try:
        return datetime.datetime(year, mm, dd, hh, mi)
    except ValueError:
        return None


def gq_dt(kickoff: str):
    try:
        return datetime.datetime.strptime(kickoff[:16], "%Y-%m-%d %H:%M")
    except Exception:
        return None


# GQ 里的虚拟/模拟赛事(8分钟制杯赛等), 真实世界不存在, 外部源永远查不到
_VIRTUAL_RE = re.compile(r"\(\s*\d+\s*分钟\s*\)|VS-|模拟")


def is_virtual(league: str) -> bool:
    return bool(_VIRTUAL_RE.search(league or ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="落库覆盖(默认只出报告)")
    ap.add_argument("--fuzzy", type=float, default=0.88, help="模糊匹配相似度阈值")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 场(调试)")
    args = ap.parse_args()

    recs = [json.loads(l) for l in open(SRC, encoding="utf-8")]
    # 只保留有比分的完场记录 (实测: 500网有比分的记录 status 全为 '完')
    recs = [r for r in recs if r["ft_home"] is not None and r["ft_away"] is not None
            and r.get("status") in _FINISHED_500]
    exact = collections.defaultdict(list)
    byday = collections.defaultdict(list)
    for r in recs:
        r["_h"], r["_a"] = norm(r["home"]), norm(r["away"])
        dt = real_dt(r)
        r["_dt"] = dt
        rd = dt.date().isoformat() if dt else r["date"]
        r["_rdate"] = rd
        exact[(rd, r["_h"], r["_a"])].append(r)
        byday[rd].append(r)
    print(f"[SRC] 500网完场记录: {len(recs)}")

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""SELECT mid, home, away, league, kickoff, status,
                          score_home, score_away, ht_score_home, ht_score_away
                   FROM matches
                   WHERE kickoff IS NOT NULL AND kickoff != ''
                     AND score_home IS NOT NULL AND mid IS NOT NULL""")
    gq = [dict(r) for r in cur.fetchall()]
    if args.limit:
        gq = gq[: args.limit]
    print(f"[GQ ] 待核对(有比分/有mid): {len(gq)}")

    matched, unmatched, virtual = [], [], []
    MAX_DT_MIN = 150  # 开球时间容差(分钟): 吸收两站开赛时间标注差异
    for g in gq:
        if is_virtual(g["league"]):
            virtual.append(g)
            continue
        d = g["kickoff"][:10]
        gdt = gq_dt(g["kickoff"])
        h, a = norm(g["home"]), norm(g["away"])
        cands = []
        for dd in (d, shift(d, -1), shift(d, 1)):
            cands.extend(byday.get(dd, []))
        # 时间闸门: 只在开球时间接近的候选里找, 杜绝隔天同名对阵错配
        if gdt is not None:
            near = [r for r in cands
                    if r["_dt"] is not None
                    and abs((r["_dt"] - gdt).total_seconds()) <= MAX_DT_MIN * 60]
        else:
            near = cands
        hit = None
        # L1 精确: 归一化队名全等
        for r in near:
            if r["_h"] == h and r["_a"] == a:
                hit = r
                hit["_level"] = "exact"
                break
        # L2 模糊: 时间闸门内取对阵相似度最高且过阈值者。
        #    开球时间已精确到分钟, 是极强约束, 故队名阈值可适度放宽;
        #    但保留 runner-up 差距校验, 两个候选咬得太紧时判为歧义 → 不匹配。
        if hit is None:
            scored = sorted(
                ((pair_score(h, a, r["_h"], r["_a"]), r) for r in near),
                key=lambda x: -x[0])
            if scored and scored[0][0] >= args.fuzzy:
                top, second = scored[0][0], (scored[1][0] if len(scored) > 1 else 0.0)
                if top - second >= 0.06 or top >= 0.9:
                    hit = scored[0][1]
                    hit["_level"] = f"fuzzy{top:.2f}"
        if hit is None:
            unmatched.append(g)
            continue
        matched.append((g, hit))

    denom = len(gq) - len(virtual)
    print(f"[SKIP ] 虚拟赛事(8分钟杯/模拟)跳过: {len(virtual)}")
    print(f"[MATCH] 命中 {len(matched)} / {denom} = {100*len(matched)/max(1,denom):.1f}%"
          f"  未命中 {len(unmatched)}")

    # 比对
    ft_diff, ht_fill, ht_diff, same = [], [], [], []
    for g, r in matched:
        og = (g["score_home"], g["score_away"])
        wb = (r["ft_home"], r["ft_away"])
        if og != wb:
            ft_diff.append((g, r))
        else:
            same.append((g, r))
        if g["ht_score_home"] is None and r["ht_home"] is not None:
            ht_fill.append((g, r))
        elif (g["ht_score_home"] is not None and r["ht_home"] is not None
              and (g["ht_score_home"], g["ht_score_away"]) != (r["ht_home"], r["ht_away"])):
            ht_diff.append((g, r))

    n = len(matched)
    print()
    print("=" * 62)
    print(f"  终场比分不一致 : {len(ft_diff):5d}  ({100*len(ft_diff)/max(1,n):.1f}%)")
    print(f"  终场比分一致   : {len(same):5d}  ({100*len(same)/max(1,n):.1f}%)")
    print(f"  半场缺失可补录 : {len(ht_fill):5d}")
    print(f"  半场不一致     : {len(ht_diff):5d}")
    print("=" * 62)

    # 方向性统计
    lo = hi = eqsum = 0
    flip = 0
    for g, r in ft_diff:
        og, oa = g["score_home"], g["score_away"]
        wh, wa = r["ft_home"], r["ft_away"]
        if og + oa < wh + wa:
            lo += 1
        elif og + oa > wh + wa:
            hi += 1
        else:
            eqsum += 1
        def res(x, y):
            return "H" if x > y else ("A" if x < y else "D")
        if res(og, oa) != res(wh, wa):
            flip += 1
    print(f"  其中 我方总进球偏低(漏球) {lo} / 偏高 {hi} / 总球数相同但比分错 {eqsum}")
    print(f"  ** 胜平负结果被改写(方向翻转): {flip} 场 **")
    print()
    print("--- 差异样例(前 15) ---")
    for g, r in ft_diff[:15]:
        print(f"  {g['mid']} [{r.get('_level','')}] {g['league'][:12]:<12} "
              f"{g['home'][:10]:<10} {g['score_home']}-{g['score_away']} "
              f"-> {r['ft_home']}-{r['ft_away']} (HT {r['ht_home']}-{r['ht_away']}) {g['away'][:10]}")

    report = {
        "ts": datetime.datetime.now().isoformat(),
        "src_500_records": len(recs),
        "gq_candidates": len(gq),
        "matched": len(matched),
        "unmatched": len(unmatched),
        "ft_diff": len(ft_diff), "ft_same": len(same),
        "ht_fill": len(ht_fill), "ht_diff": len(ht_diff),
        "dir_lower": lo, "dir_higher": hi, "dir_eqsum": eqsum, "result_flip": flip,
        "unmatched_sample": [
            {"mid": g["mid"], "home": g["home"], "away": g["away"],
             "league": g["league"], "kickoff": g["kickoff"]} for g in unmatched[:60]
        ],
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[REPORT] {REPORT}")

    if not args.apply:
        print("\n(dry-run: 未写库。确认后加 --apply 落库)")
        conn.close()
        return

    # ---- 落库 ----
    bak = DB.parent / f"events.db.bak_{datetime.datetime.now():%Y%m%d_%H%M%S}"
    shutil.copy2(DB, bak)
    print(f"[BAK] 已备份 → {bak}")

    audit = []
    todo = {}
    for g, r in ft_diff:
        todo[g["mid"]] = (g, r, "correct_ft")
    for g, r in ht_fill:
        if g["mid"] not in todo:
            todo[g["mid"]] = (g, r, "fill_ht")
    for g, r in ht_diff:
        if g["mid"] not in todo:
            todo[g["mid"]] = (g, r, "correct_ht")

    for mid, (g, r) in [(k, (v[0], v[1])) for k, v in todo.items()]:
        act = todo[mid][2]
        nh, na = r["ft_home"], r["ft_away"]
        hh, ha = r["ht_home"], r["ht_away"]
        result = "home" if nh > na else ("away" if nh < na else "draw")
        cur.execute("""UPDATE matches SET score_home=?, score_away=?,
                       ht_score_home=?, ht_score_away=?, status='finished'
                       WHERE mid=?""", (nh, na, hh, ha, mid))
        cur.execute("SELECT id FROM match_outcomes WHERE mid=?", (mid,))
        in_mo = cur.fetchone() is not None
        if in_mo:
            cur.execute("""UPDATE match_outcomes SET score_home=?, score_away=?,
                           result=?, ht_score_home=?, ht_score_away=?
                           WHERE mid=?""", (nh, na, result, hh, ha, mid))
        audit.append({
            "mid": mid, "league": g["league"], "home": g["home"], "away": g["away"],
            "our_ft": f"{g['score_home']}-{g['score_away']}", "web_ft": f"{nh}-{na}",
            "our_ht": (f"{g['ht_score_home']}-{g['ht_score_away']}"
                       if g["ht_score_home"] is not None else "--"),
            "web_ht": (f"{hh}-{ha}" if hh is not None else "--"),
            "result": result, "in_outcomes": in_mo, "action": act,
            "match_level": r.get("_level", ""),
            "source": f"500.com/wanchang {r['date']} mid500={r['mid500']}",
            "ts": datetime.datetime.now().isoformat(),
        })
    conn.commit()
    conn.close()
    with open(AUDIT, "a", encoding="utf-8") as f:
        for a in audit:
            f.write(json.dumps(a, ensure_ascii=False) + "\n")
    print(f"[APPLY] 已更新 {len(audit)} 场, 审计追加 → {AUDIT}")


if __name__ == "__main__":
    main()
