r"""data_integrity_check.py — 每日自动数据完整性检查（文档 SYSTEM_BLUEPRINT §1/P-INTEGRITY）

只读 events.db (mode=ro)，输出 reports/data_integrity.json + 摘要。覆盖:
  C1 缺盘: 完场+有比分 但 0 条 odds_changes（从未记录赔率）
  C2 稀疏tick: 近120天完场 但 odds_changes < 5 条（采集稀疏/跳tick）
  C3 ID冲突: 同 match_key 终场比分不一致（数据冲突）
  C4 时钟偏移: first_seen > last_seen（采集时钟倒挂）
  C5 补时/分钟异常: 完场场 odds_changes 中 score_at 非空但 minute_at 空（INFO）
  INFO 已知缺口: finished+score 无 prematch_conclusion（诚实损失, 不修）

铁律(DISCIPLINE.md §4): 只读, 不写 events.db, 不 VACUUM, 不 rm。
用法: .venv/Scripts/python.exe scripts/data_integrity_check.py
"""
import os
import sqlite3
import time
import json
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
SRC = os.path.join(DATA, "events.db")
OUT = os.path.join(ROOT, "reports", "data_integrity.json")


def ro(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def main():
    t0 = time.time()
    con = ro(SRC)
    cur = con.cursor()
    now = time.time()
    win = now - 120 * 86400

    def q(sql, *a):
        return cur.execute(sql, a).fetchall()

    # 总览
    totals = {
        "matches": q("SELECT COUNT(*) FROM matches")[0][0],
        "odds_changes": q("SELECT COUNT(*) FROM odds_changes")[0][0],
        "odds_snapshots": q("SELECT COUNT(*) FROM odds_snapshots")[0][0],
        "prematch_conclusion": q("SELECT COUNT(*) FROM prematch_conclusion")[0][0],
    }

    # C1 缺盘（限定实时采集窗口, 排除迁移历史库无赔率场次）
    c1 = q(
        """SELECT COUNT(*) FROM matches m
           WHERE m.score_home IS NOT NULL AND COALESCE(m.score_missing,0)=0
             AND m.first_seen > ?
             AND NOT EXISTS (SELECT 1 FROM odds_changes o WHERE o.match_key=m.match_key)""",
        win,
    )[0][0]

    # C2 稀疏 tick（近120天完场）
    c2 = q(
        """SELECT COUNT(*) FROM matches m
           WHERE m.score_home IS NOT NULL AND COALESCE(m.score_missing,0)=0
             AND m.first_seen > ?
             AND (SELECT COUNT(*) FROM odds_changes o WHERE o.match_key=m.match_key) < 5""",
        win,
    )[0][0]

    # C3 ID 冲突
    c3 = q(
        """SELECT COUNT(*) FROM (
             SELECT match_key FROM matches
             WHERE score_home IS NOT NULL
             GROUP BY match_key
             HAVING COUNT(DISTINCT score_home||'-'||score_away) > 1)"""
    )[0][0]

    # C4 时钟偏移
    c4 = q(
        "SELECT COUNT(*) FROM matches WHERE first_seen IS NOT NULL AND last_seen IS NOT NULL AND first_seen > last_seen"
    )[0][0]

    # C5 补时/分钟异常 (INFO)
    c5 = q(
        """SELECT COUNT(*) FROM odds_changes o
           JOIN matches m ON m.match_key=o.match_key
           WHERE m.score_home IS NOT NULL AND o.score_at IS NOT NULL AND o.minute_at IS NULL"""
    )[0][0]

    # INFO 已知缺口
    info_gap = q(
        """SELECT COUNT(*) FROM matches m
           WHERE m.score_home IS NOT NULL AND COALESCE(m.score_missing,0)=0
             AND NOT EXISTS (SELECT 1 FROM prematch_conclusion p WHERE p.match_key=m.match_key)"""
    )[0][0]

    con.close()

    checks = {
        "C1_unmonitored_finished": {"count": c1, "level": "INFO",
            "note": "完场+比分但0条odds_changes; 多为迁移历史库(football_data/GQ)无赔率场次, 非实时故障"},
        "C2_monitored_sparse_120d": {"count": c2, "level": "INFO",
            "note": "近120天完场但有赔率且<5tick; 采集稀疏/采样设计, 基线指标非告警"},
        "C3_id_conflict": {"count": c3, "threshold": 0, "level": "FAIL" if c3 > 0 else "OK",
            "note": "同match_key终场比分不一致=真数据冲突"},
        "C4_clock_skew": {"count": c4, "threshold": 0, "level": "FAIL" if c4 > 0 else "OK",
            "note": "first_seen>last_seen=采集时钟倒挂"},
        "C5_stoppage_minute_info": {"count": c5, "level": "INFO",
            "note": "完场场odds_changes中score_at非空但minute_at空"},
        "INFO_known_gap_no_conclusion": {"count": info_gap, "level": "INFO",
            "note": "诚实损失, 不可回填(防未来信息)"},
    }
    levels = [v["level"] for v in checks.values() if v.get("level") in ("FAIL", "WARN")]
    overall = "FAIL" if "FAIL" in levels else ("WARN" if "WARN" in levels else "OK")

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "events.db (READ-ONLY)",
        "totals": totals,
        "overall": overall,
        "checks": checks,
        "elapsed_s": round(time.time() - t0, 1),
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"[数据完整性] overall={overall} ({report['elapsed_s']}s)")
    for k, v in checks.items():
        print(f"  {k}: {v.get('count')} [{v.get('level')}]")
    print(f"→ {OUT}")


if __name__ == "__main__":
    main()
