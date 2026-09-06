# -*- coding: utf-8 -*-
"""WS3 复盘自动复核引擎: 对 match_analysis_cache 全部已缓存行重算
最强信号/偏差%/ROI/标签 + 传统维度(verdict_hit/score_err/stake_pnl),
闭合赛果复盘数据闭环。纯数据层, 不改调度/前端。

用法:
  .venv/Scripts/python.exe scripts/recheck_analysis.py            # 干跑统计
  .venv/Scripts/python.exe scripts/recheck_analysis.py --apply    # 执行写库
"""
import sys, os, argparse, sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gq.db as db


def recheck_cs(apply):
    """赛前波胆(CS)归档 + 赛果验证 (类似31K)。

    - 未开赛(scheduled) → freeze_pre_match_cs
    - 已完场(finished)+比分 → verify_cs
    """
    db.ensure_cs_tables()
    with db.conn() as c:
        c.row_factory = sqlite3.Row
        sched = [r[0] for r in c.execute(
            "SELECT match_key FROM matches WHERE status='scheduled'").fetchall()]
        fin = [r[0] for r in c.execute(
            "SELECT match_key FROM matches WHERE status='finished' AND score_home IS NOT NULL").fetchall()]

    n_frozen = n_verified = n_no_market = 0
    if apply:
        for mk in sched:
            if db.freeze_pre_match_cs(mk):
                n_frozen += 1
        for mk in fin:
            vr = db.verify_cs(mk, source='recheck')
            if vr is None:
                n_no_market += 1
            else:
                n_verified += 1
    else:
        # 干跑: 用查询估覆盖
        n_frozen = len(db.query_pre_match_cs(limit=100000))
        n_verified = len(db.query_cs_verification(limit=100000))

    print(f"[recheck-cs] 未开赛冻结: {n_frozen} | 赛果验证: {n_verified} | 无赛前盘口: {n_no_market}")
    return n_frozen, n_verified


def recheck_auto_review(apply):
    """2026-08-08 自动复核接入: 赛前KNN单结论 vs 赛后实际赛果, 写 match_analysis_cache。

    增量: 跳过已 auto_reviewed 的行 (force 用 --apply --force 重算全部)。
    全部 finished+score 比赛参与; 无赛前盘口的比赛 predicted_direction=NULL 不伪造。
    """
    if not apply:
        # 干跑: 统计可复核规模 + 已有覆盖
        with db.conn() as c:
            c.row_factory = __import__('sqlite3').Row
            total = c.execute(
                "SELECT COUNT(*) FROM matches WHERE status='finished' "
                "AND score_home IS NOT NULL AND score_away IS NOT NULL "
                f"AND ({db.virtual_league_sql('league')})").fetchone()[0]
            done = c.execute(
                "SELECT COUNT(*) FROM match_analysis_cache "
                "WHERE auto_reviewed_at IS NOT NULL").fetchone()[0]
        print(f"[recheck-auto] 可复核={total} 已复核={done} (干跑)")
        return
    stats = db.auto_review_all(limit=None, force=False)
    print(f"[recheck-auto] {stats}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="写入数据库 (否则仅统计)")
    ap.add_argument("--force", action="store_true", help="重算已复核行 (auto_review)")
    ap.add_argument("--limit", type=int, default=None, help="auto_review 上限(调试)")
    args = ap.parse_args()

    db.ensure_analysis_cache()
    with db.conn() as c:
        mids = [r[0] for r in c.execute(
            "SELECT mid FROM match_analysis_cache").fetchall()]

    total = len(mids)
    updated = 0
    no_outcome = 0
    with_signal = with_dev = with_roi = with_label = 0
    for mid in mids:
        if not args.apply:
            # 干跑: 仅统计现有覆盖
            row = db.get_cache_row(mid)
            if row:
                if row.get("strongest_signal") is not None: with_signal += 1
                if row.get("deviation_pct") is not None: with_dev += 1
                if row.get("roi") is not None: with_roi += 1
                if row.get("label") is not None: with_label += 1
            continue
        res = db.correct_analysis(mid)
        if res is None:
            no_outcome += 1
            continue
        updated += 1
        if res.get("strongest_signal") is not None: with_signal += 1
        if res.get("deviation_pct") is not None: with_dev += 1
        if res.get("roi") is not None: with_roi += 1
        if res.get("label") is not None: with_label += 1

    print(f"[recheck] cache 总行数: {total}")
    if args.apply:
        print(f"  重算更新: {updated} | 无赛果跳过: {no_outcome}")
    print(f"  维度覆盖: 最强信号={with_signal} 偏差%={with_dev} ROI={with_roi} 标签={with_label}")

    # 赛前波胆(CS)归档 + 赛果验证
    recheck_cs(args.apply)

    # 2026-08-08 自动复核: 赛前单结论 vs 赛后赛果 (每日 cron 增量)
    recheck_auto_review(args.apply)

    # P2 人工纠偏锁: 防御性重断言 (每日兜底, 确保锁定终场不被漂移破坏)
    recheck_overrides()

    # 缺口#7 自动护栏: 每日扫描 kickoff=0/应归档漏档并自动补档
    recheck_missing_outcomes(args.apply)

    if not args.apply:
        print("(干跑, 未写库。加 --apply 执行)")


def recheck_overrides():
    """防御性重断言: 用 override_data 重写所有 is_override=1 的比赛终场.
    仅触碰人工锁定行 (已是可信数据), 采集器本身被 upsert_match 守卫跳过,
    本阶段作为每日重断言兜底, 确保锁定终场长期有效."""
    n = db.reassert_overrides()
    print(f"[recheck-overrides] 重断言锁定终场: {n} 场")


def recheck_missing_outcomes(apply):
    """缺口#7 自动护栏 (2026-08-13): 每日扫描 '有终场比分 + 非虚拟 + 非友谊
    + 无 match_outcomes' 的应归档缺档并补档, 覆盖 kickoff=epoch0 脏时间漏档
    (8-12 手动 backfill 57 场的根因).

    安全设计
    --------
    - 候选=0 时不备份/不写, 静默 OK (护栏无副作用).
    - 候选>0 且 --apply 时自动补档(复用 gq.db.record_match_outcome, 幂等:
      已补的场次日不再出现在候选里, 不会重复写).
    - 候选>SAFETY_LIMIT 视为异常批量(如采集器大范围故障), 跳过并告警,
      不自动批量写库, 需人工确认(遵守铁律'先验证').
    - 落库前自动备份 events.db + 追加审计 data/backfill_outcomes_audit.jsonl.
    """
    SAFETY_LIMIT = 300
    _ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    DB = os.path.join(_ROOT, "data", "events.db")
    AUDIT = os.path.join(_ROOT, "data", "backfill_outcomes_audit.jsonl")
    VIRTUAL = ("瓦尔哈拉杯", "瓦尔基里杯", "(8分钟)", "（8分钟）")

    # db.conn() 是 contextmanager, 必须用 with; fetchall 已将所有行取到内存,
    # 列表推导转 dict 不依赖连接, 故 with 块仅包住读取即可.
    with db.conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT m.mid, m.league, m.home, m.away, m.kickoff,
                   m.score_home, m.score_away, m.ht_score_home, m.ht_score_away
            FROM matches m
            WHERE m.score_home IS NOT NULL AND m.score_away IS NOT NULL
              AND m.status = 'finished'
              AND m.league NOT LIKE '%友谊%'
              AND NOT EXISTS (SELECT 1 FROM match_outcomes o WHERE o.mid = m.mid)
            ORDER BY m.kickoff
            """
        ).fetchall()
    cands = [dict(r) for r in rows if not any(k in (r["league"] or "") for k in VIRTUAL)]
    n = len(cands)

    if n == 0:
        print("[recheck-missing] 无应归档缺档 (kickoff=0 漏档护栏 OK)")
        return 0
    print(f"[recheck-missing] 检测到 {n} 场应归档缺档 (含 kickoff=0 漏档)")

    if n > SAFETY_LIMIT:
        print(f"[recheck-missing][WARN] 超安全阈值 {SAFETY_LIMIT}, 疑似异常批量, "
              f"跳过自动补档, 需人工确认")
        return -1

    if not apply:
        return n

    # ── apply: 备份 + 审计 + 落库 ──
    import shutil, time, json
    ts = time.strftime("%Y%m%d_%H%M%S")
    bak = f"{DB}.bak_{ts}"
    shutil.copy2(DB, bak)
    ok = skip = fail = 0
    with open(AUDIT, "a", encoding="utf-8") as af:
        for g in cands:
            try:
                rec = db.record_match_outcome(
                    mid=str(g["mid"]), home=g["home"], away=g["away"],
                    league=g["league"] or "", kickoff=g["kickoff"] or "",
                    score_home=g["score_home"], score_away=g["score_away"],
                    ht_score_home=g["ht_score_home"], ht_score_away=g["ht_score_away"],
                )
                if rec:
                    ok += 1
                    af.write(json.dumps({
                        "mid": g["mid"], "league": g["league"], "home": g["home"],
                        "away": g["away"], "kickoff": g["kickoff"],
                        "ft": [g["score_home"], g["score_away"]],
                        "ht": [g["ht_score_home"], g["ht_score_away"]],
                        "tool": "recheck_missing_outcomes", "ts": ts,
                    }, ensure_ascii=False) + "\n")
                else:
                    skip += 1
            except Exception as e:  # noqa: BLE001
                fail += 1
                print(f"[recheck-missing][FAIL] mid={g['mid']} {g['home']} vs {g['away']}: {e!r}")
    print(f"[recheck-missing] 补档 写入{ok}/跳过{skip}/失败{fail} 备份{bak}")
    return ok


if __name__ == "__main__":
    main()
