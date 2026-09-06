#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
team_name_resolver.py — 中文↔母语队名解析器 v1
================================================
将「整轮赛果卡」(母语队名 + 终比分 + 开球) 反向映射到 events.db 的 mid。

设计原则 (对齐哨响铁律 IR-03/IR-30 宁缺勿错):
  - 仅按「队名身份」匹配, 绝不按 GQ league 字符串整体套用赛果卡
    (GQ '波兰丙级联赛' 等字符串常混合多个真实子联赛)。
  - 只输出「双队均在字典中且唯一命中 GQ 未填 mid」的候选; 其余报 unmatched。
  - 本脚本只产出 draft mapping JSON, 不写库; 核验后由 web_backfill_scores.py --apply 落库。

用法:
  python team_name_resolver.py --card cards/group1_2026-08.json --out draft_batch.json
  (--card 为整轮卡 JSON: [{"league_key","date","home_mt","away_mt","sh","sa","ht_sh","ht_sa"}, ...])
"""
import argparse, json, sqlite3, sys, os, unicodedata

DB_PATH = os.environ.get("GQ_DB", "D:/Architecture/data/events.db")
MAP_PATH = os.path.join(os.path.dirname(__file__), "team_name_map.json")


def load_map():
    with open(MAP_PATH, encoding="utf-8") as f:
        return json.load(f)


def norm(s):
    """小写 + 去变音符号 (波兰语 łóąęśżźń 等 -> 无重音), 便于卡面字符串与字典互比。"""
    s = (s or "").strip().lower()
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def reverse_league(league_block):
    """母语 -> 中文 (键统一 norm)"""
    return {norm(v): k for k, v in league_block.items()}


def resolve_card(card_rows, gq_con):
    mp = load_map()
    cur = gq_con.cursor()
    matched, unmatched_teams = [], set()

    # 预载未填比赛 (home, away, kickoff_date) -> [(mid, kickoff)]
    cur.execute(
        "SELECT mid, home, away, league, kickoff FROM matches "
        "WHERE score_home IS NULL OR score_away IS NULL"
    )
    rows = cur.fetchall()
    # 索引: (home_cn, away_cn) -> list of (mid, kickoff, league)
    by_pair = {}
    for mid, home, away, league, kickoff in rows:
        by_pair.setdefault((home, away), []).append((mid, kickoff, league))

    for r in card_rows:
        lk = r.get("league_key")
        if lk not in mp:
            print(f"[skip] 无字典子联赛: {lk}", file=sys.stderr)
            continue
        rev = reverse_league(mp[lk])
        sh_cn = rev.get(norm(r["home_mt"]))
        sa_cn = rev.get(norm(r["away_mt"]))
        if not sh_cn or not sa_cn:
            if norm(r["home_mt"]) not in rev:
                unmatched_teams.add(f"{r['home_mt']} (league={lk})")
            if norm(r["away_mt"]) not in rev:
                unmatched_teams.add(f"{r['away_mt']} (league={lk})")
            continue
        cand = by_pair.get((sh_cn, sa_cn))
        if not cand:
            # 反向: 也许卡里的主客顺序与 GQ 相反
            cand = by_pair.get((sa_cn, sh_cn))
            if cand:
                # 顺序相反 -> 比分需交换
                r = dict(r)
                r["sh"], r["sa"] = r["sa"], r["sh"]
                r["ht_sh"], r["ht_sa"] = r["ht_sa"], r["ht_sh"]
                sh_cn, sa_cn = sa_cn, sh_cn
        if not cand:
            unmatched_teams.add(f"{r['home_mt']} vs {r['away_mt']} (league={lk}, 无GQ未填mid)")
            continue
        # 取该对唯一/首个未填 mid
        mid, kickoff, league = cand[0]
        matched.append({
            "mid": mid,
            "sh": r["sh"], "sa": r["sa"],
            "ht_sh": r.get("ht_sh"), "ht_sa": r.get("ht_sa"),
            "source": f"round-card({lk}) + {r.get('source','')}".strip(" +"),
            "note": f"{sh_cn} {r['sh']}-{r['sa']} {sa_cn} (HT {r.get('ht_sh')}-{r.get('ht_sa')}) | GQ kickoff {kickoff}",
            "_gq_home": sh_cn, "_gq_away": sa_cn, "_gq_kickoff": kickoff,
        })
    return matched, sorted(unmatched_teams)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--card", required=True, help="整轮赛果卡 JSON 路径")
    ap.add_argument("--out", required=True, help="产出的 draft mapping JSON 路径")
    ap.add_argument("--db", default=DB_PATH)
    args = ap.parse_args()

    with open(args.card, encoding="utf-8") as f:
        card = json.load(f)
    con = sqlite3.connect(args.db)
    matched, unmatched = resolve_card(card, con)
    con.close()

    # 去掉内部辅助字段
    for m in matched:
        for k in list(m.keys()):
            if k.startswith("_"):
                del m[k]
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(matched, f, ensure_ascii=False, indent=2)

    print(f"[ok] 命中 {len(matched)} 场 -> {args.out}")
    for m in matched:
        print(f"  mid={m['mid']} {m['_gq_home'] if False else ''}{m['note']}")
    if unmatched:
        print(f"\n[unmatched] {len(unmatched)} 队/对未映射 (需补字典或换正确子联赛卡):")
        for u in unmatched:
            print(f"  - {u}")


if __name__ == "__main__":
    main()
