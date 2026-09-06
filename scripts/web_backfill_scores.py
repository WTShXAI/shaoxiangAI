# -*- coding: utf-8 -*-
"""
web_backfill_scores.py — 网络查证比分回填 (GQ 清库窗口兜底)

仅回填【已通过网络多源查证】的终比分。本脚本不假设任何未验证数据:
- 读取 scripts/web_score_mapping.json (mid, sh, sa, ht_sh, ht_sa, source)
- 幂等 UPSERT: matches(score/status=finished/minute=90) + match_outcomes(record/fill)
- 尊重 is_override 锁; 事务 + 针对性备份 + 校验(null_score_cnt=0, inconsistency=0)
- 默认 dry-run; --apply 才写库。

铁律: 只写 web_score_mapping.json 里、且 source 可靠的条目。未在映射里的比赛一律不碰。
"""
from __future__ import annotations
import argparse, json, os, sys, time
from datetime import datetime

# 让 import gq 可解析
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gq.db import conn as _gq_conn, record_match_outcome


def derive_result(sh, sa) -> str:
    if sh > sa:
        return "home"
    if sh == sa:
        return "draw"
    return "away"


def backup_rows(mids, path):
    with _gq_conn(readonly=True) as c:
        ph = ",".join("?" * len(mids))
        mrows = [dict(r) for r in c.execute(f"SELECT * FROM matches WHERE mid IN ({ph})", mids)]
        orows = [dict(r) for r in c.execute(f"SELECT * FROM match_outcomes WHERE mid IN ({ph})", mids)]
    json.dump({"ts": time.time(), "mids": mids, "matches": mrows, "match_outcomes": orows},
              open(path, "w", encoding="utf-8"), ensure_ascii=False, default=str)
    print(f"[backup] {len(mrows)} matches + {len(orows)} match_outcomes -> {path}")


def write_one(mid, sh, sa, ht_sh, ht_sa, source):
    """返回 (status, msg)。status in inserted/filled/skipped_existing/error。"""
    result = derive_result(sh, sa)
    with _gq_conn() as c:
        m = c.execute("SELECT home, away, league, is_override FROM matches WHERE mid=?", (mid,)).fetchone()
        if not m:
            return ("error", f"matches 无此 mid={mid}")
        home, away, league, is_ov = m["home"], m["away"], m["league"] or "", m["is_override"]
        if is_ov:
            return ("skipped_existing", f"被 is_override 锁定, 跳过 mid={mid}")
        # matches: 终比分 + finished
        c.execute(
            "UPDATE matches SET score_home=?, score_away=?, ht_score_home=?, ht_score_away=?,"
            " status='finished', minute=90, last_seen=? WHERE mid=? AND (is_override IS NULL OR is_override=0)",
            (sh, sa, ht_sh, ht_sa, time.time(), mid))
        # match_outcomes
        exist = c.execute("SELECT score_home, is_valid, is_override FROM match_outcomes WHERE mid=?", (mid,)).fetchone()
        if exist is None:
            rec = record_match_outcome(mid, home, away, league, "", sh, sa, ht_sh, ht_sa)
            if rec is None:
                return ("error", f"record_match_outcome 返回 None mid={mid}")
            return ("inserted", f"{home} vs {away} {sh}-{sa} ({result}) src={source}")
        if exist["is_override"]:
            return ("skipped_existing", f"match_outcomes 被锁定 mid={mid}")
        if exist["score_home"] is not None:
            return ("skipped_existing", f"已有终比分 mid={mid}")
        c.execute(
            "UPDATE match_outcomes SET score_home=?, score_away=?, ht_score_home=?, ht_score_away=?,"
            " result=?, is_valid=1, source='web_backfill', archived_at=? WHERE mid=?",
            (sh, sa, ht_sh, ht_sa, result, time.time(), mid))
        return ("filled", f"{home} vs {away} {sh}-{sa} ({result}) [补 NULL 行] src={source}")


def validate(mids):
    ph = ",".join("?" * len(mids)) if mids else "''"
    with _gq_conn(readonly=True) as c:
        null_score = c.execute(
            f"SELECT COUNT(*) FROM match_outcomes WHERE mid IN ({ph}) AND (score_home IS NULL OR result IS NULL)", mids).fetchone()[0] if mids else 0
        incons = c.execute(
            f"""SELECT COUNT(*) FROM match_outcomes WHERE mid IN ({ph})
                AND score_home IS NOT NULL AND result IS NOT NULL
                AND ((score_home>score_away AND result<>'home') OR (score_home<score_away AND result<>'away')
                  OR (score_home=score_away AND result<>'draw'))""", mids).fetchone()[0] if mids else 0
    return {"targets": len(mids), "null_score_cnt": null_score, "score_result_inconsistent": incons}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--mapping", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_score_mapping.json"))
    args = ap.parse_args()

    mapping = json.load(open(args.mapping, encoding="utf-8"))
    print(f"[load] web_score_mapping.json: {len(mapping)} 条")

    if not args.apply:
        print("[DRY-RUN] 不写库。预览:")
        for e in mapping:
            print(f"   mid={e['mid']} -> {e['sh']}-{e['sa']}  ({e['note']})")
        print("[DRY-RUN] 加 --apply 才真正写库。")
        return

    mids = [e["mid"] for e in mapping]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"web_backfill_backup_{ts}.json")
    backup_rows(mids, backup_path)

    inserted = filled = errors = skipped = 0
    for e in mapping:
        try:
            status, msg = write_one(e["mid"], e["sh"], e["sa"], e.get("ht_sh"), e.get("ht_sa"), e.get("source", ""))
        except Exception as ex:
            errors += 1
            print(f"[ERROR] mid={e['mid']}: {ex}")
            continue
        if status == "inserted":
            inserted += 1
        elif status == "filled":
            filled += 1
        elif status == "skipped_existing":
            skipped += 1
        else:
            errors += 1
        print(f"  [{status}] {msg}")

    print(f"\n[APPLY] inserted={inserted}, filled={filled}, skipped_existing={skipped}, errors={errors}")
    v = validate(mids)
    print(f"[VALIDATION] {json.dumps(v, ensure_ascii=False)}")
    if v["score_result_inconsistent"] > 0:
        print(f"[VALIDATION][WARN] {v['score_result_inconsistent']} 条 score/result 不一致!")
    print(f"[APPLY] 备份: {backup_path}")


if __name__ == "__main__":
    main()
