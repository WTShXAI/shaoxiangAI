"""
clean_outcomes.py — 赛果数据唯一干净入口 (SSoT)

背景 (2026-08-05 P0 数据事故):
  回测出现 IS/OOS 结论方向翻转, 追查发现 match_outcomes 有两层污染:

  [污染一] 电子盘/虚拟赛事
      瓦尔哈拉杯 2026 (8分钟) 143 场 + 瓦尔基里杯 2026 (8分钟) 85 场 = 228 场
      均进球 5.72, 零封率 0% —— 8 分钟一场的虚拟足球, 违反红线
      「电子盘落盘只进 data/electronic_poll_*」。且含「杯」字污染 Cup 分类回测。

  [污染二] 采集截断 (影响远大于污染一)
      source='gq' 且 半场比分缺失 的 1307 场 (占 gq 源 41.2%),
      终场比分被冻结在中途:
          有半场 n=1865  均进球 2.913  0-0率  7.08%  大2.5率 55.4%  <- 真实基准
          无半场 n=1307  均进球 1.951  0-0率 29.38%  大2.5率 32.1%  <- 截断
      同联赛同日配对对照: 差 +1.377 球, Wilcoxon p=3.4e-05 (非联赛混淆)。

      >>> 这是「小球策略看起来盈利」的根本原因: 41% 的比分被系统性压低。

  注意 source='wc' (历史世界杯回填) 本就无半场字段, 但均进球 2.957 / 0-0率 9.15%
  完全正常, 不能按同一规则剔除 —— 截断判定必须 source-aware。

  [2026-08-18 复核 + 解耦]
    原 `is_truncated = ht_missing & (source 非 wc)` 的 ht 代理已失效:
    ht_score 采集 clobber 修复 + 回填后, gq 无HT组终场分布恢复正常
    (均进球 2.940 / 0-0率 14.8% / 大2.5 50.2%), 且与 live `matches` 终场比分
    100% 一致(3673/3673, 无一例 archived 低于 live)。即"终场冻结中途"的截断污染
    已不存在。继续用 ht 缺失作截断代理会静默丢弃 ~67% (3871/5961) 的 gq 真实样本,
    属隐性训练数据流失。故 2026-08-18 起 `is_truncated` 不再由 ht_missing 触发
    (恒 False), `ht_missing` 仅保留为信息列。见 mark_quality 内注释。

用法 (所有回测/训练一律走这里, 禁止直接 read_sql match_outcomes):
    from pipeline.clean_outcomes import load_clean_outcomes
    df = load_clean_outcomes()                    # 默认最严: 剔虚拟 + 剔截断
    df = load_clean_outcomes(drop_truncated=False)  # 只剔虚拟(仅用于对照实验)
    df, rep = load_clean_outcomes(return_report=True)

CLI:
    python pipeline/clean_outcomes.py     # 打印各档数据的健康度体检表

[2026-08-31 第二层 SSoT: events.db matches 表 假 0-0 口径]
  match_outcomes 层处理"虚拟+截断"; matches 层处理"假 0-0" (score_missing 打标)。
  背景: 冷门联赛采集器 _sweep_finished 没抓到比分却照翻 finished 写 0:0,
  假 0:0=平局 → 主客/让球回测被压低, OU-over 更惨 (干净组 42.98% vs 脏组 38.64%)。
  治理: 08-30 purge (打标+置NULL 1277场) + 08-31 gov (只打标 407场) = 1686 场。
  用法:
    from pipeline.clean_outcomes import clean_where, is_clean, has_inplay_evidence
    rows = con.execute(f"SELECT ... FROM matches WHERE status='finished' AND {clean_where()}")
    if not is_clean(con, match_key): ...   # 单场判定
  任何用 matches.score_* 结算的回测/训练必须过此口径 (IR-04), 禁止裸读。
"""
from __future__ import annotations

import os
import sqlite3
import logging
import pandas as pd

from pipeline.virtual_match_filter import is_virtual

log = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GQ_DB = os.path.join(ROOT, "data", "events.db")

# 真实足球健康度基准 (用于体检断言)
# BENCH_AVG_GOALS: 终场均进球合理区间。截断污染会击穿下界(<2.4 且零封飙升)。
# BENCH_ZERO_RATE: 0-0 率区间。原上界 0.12 是 2026-08-05 按 HAS_HT 子集(人为干净)标定;
#   2026-08-18 解耦 ht 后, 清洗集纳入大量"不报半场"的 obscure 联赛, 0-0率升至 ~13% 但
#   均进球仍 2.7+ (非截断特征)。故上界放宽至 0.15 —— 仍能捕获 29% 量级的真实截断,
#   但不再对健康的混合联赛数据误报。截断判定须 avg 与 zero 同时越界, 单看 zero 偏高不算。
BENCH_AVG_GOALS = (2.40, 3.10)
BENCH_ZERO_RATE = (0.05, 0.15)

# 半场缺失不代表截断的数据源 (历史回填本就没有半场字段)
SOURCES_WITHOUT_HT = {"wc"}


def mark_quality(df: pd.DataFrame) -> pd.DataFrame:
    """给 DataFrame 打质量标记列, 不做过滤。

    新增列:
        is_virtual   : 虚拟/电子盘赛事
        ht_missing   : 半场比分缺失 (含被污染后置空的, 见 ht_contaminated)
        ht_contaminated : 半场总进球 >= 全场总进球 (ht 被回填为全场, 不可用,
                         由 gq/db.py clean_ht_score 规则判定; 命中后 ht 列置空)
        is_truncated : 采集截断 (见下方解耦说明, 当前恒 False)
        is_clean     : 非虚拟 且 非截断
    """
    d = df.copy()
    d["is_virtual"] = d["league"].map(is_virtual) if "league" in d.columns else False

    if {"ht_score_home", "ht_score_away", "score_home", "score_away"} <= set(d.columns):
        _hh, _ha = d["ht_score_home"], d["ht_score_away"]
        _fh, _fa = d["score_home"], d["score_away"]
        _ht_tot = _hh.fillna(0) + _ha.fillna(0)
        _ft_tot = _fh.fillna(0) + _fa.fillna(0)
        # 污染判定: 半场总进球 >= 全场总进球 且 ht 非缺失 → 被回填为全场, 不可用
        # (GQ ht_score 约 66% 存在此污染, 见 gq/db.py clean_ht_score 注释)
        d["ht_contaminated"] = (_hh.notna() & _ha.notna() & (_ht_tot >= _ft_tot))
        # 污染 ht 置空: 下游视为缺失(真实半场未知), 而非错误值渗入训练
        d.loc[d["ht_contaminated"], ["ht_score_home", "ht_score_away"]] = pd.NA, pd.NA
        d["ht_missing"] = d["ht_score_home"].isna() | d["ht_score_away"].isna()
    else:
        d["ht_contaminated"] = False
        d["ht_missing"] = False

    # [2026-08-18 解耦 is_truncated 与 ht_missing]
    #   旧逻辑: is_truncated = ht_missing & ht_expected(source 非 wc)。
    #   旧依据(2026-08-05 P0): 当时 ht 缺失 与 终场截断强相关 —— 无HT组均进球 1.951、
    #   0-0率 29.38%, 终场比分被冻结在中途。故用 ht 缺失作截断代理, 剔除整组。
    #   现状(2026-08-18 复盘): ht_score 采集 clobber 已修复并回填, 无HT组终场分布已恢复
    #   正常 —— gq 无HT n=3871 均进球 2.940 / 0-0率 14.8% / 大2.5 50.2%; 且与 live 表
    #   matches 终场比分 100% 一致(3673/3673, 无一例 archived 低于 live)。即"终场冻结中途"
    #   的截断污染已不存在, ht 缺失只剩"该场未采集到半场"的信息意义。
    #   若仍用 ht_missing 作截断代理, 会静默丢弃 ~67% (3871/5961) 的 gq 真实样本 —— 这是
    #   隐性训练数据流失, 必须解除。
    #   因此 is_truncated 不再由 ht_missing 触发, 恒为 False; ht_missing 仅保留为信息列。
    #   未来若需重启用截断检测, 应改用跨表硬证据(archived 终场 < live matches 终场),
    #   而非依赖半场字段。
    d["is_truncated"] = pd.Series(False, index=d.index)

    d["is_clean"] = (~d["is_virtual"]) & (~d["is_truncated"])
    return d


def health_check(df: pd.DataFrame, label: str = "") -> dict:
    """算一段赛果数据的健康度指标, 并与真实足球基准比对。"""
    d = df.dropna(subset=["score_home", "score_away"])
    if not len(d):
        return {"label": label, "n": 0, "ok": False}
    tot = d["score_home"] + d["score_away"]
    avg = float(tot.mean())
    zero = float((tot == 0).mean())
    ok = (BENCH_AVG_GOALS[0] <= avg <= BENCH_AVG_GOALS[1]
          and BENCH_ZERO_RATE[0] <= zero <= BENCH_ZERO_RATE[1])
    return {
        "label": label,
        "n": int(len(d)),
        "avg_goals": round(avg, 3),
        "zero_rate": round(zero, 4),
        "over25_rate": round(float((tot > 2.5).mean()), 4),
        "ok": bool(ok),
    }


def load_clean_outcomes(
    db_path: str | None = None,
    drop_virtual: bool = True,
    drop_truncated: bool = True,
    require_scores: bool = True,
    return_report: bool = False,
):
    """读取并清洗 match_outcomes。这是唯一允许的赛果入口。

    Args:
        drop_virtual   : 剔除电子盘/虚拟赛事 (默认 True, 不要关)
        drop_truncated : 剔除采集截断场次 (默认 True, 关掉只用于对照实验)
        require_scores : 只要有终场比分的场次
        return_report  : 同时返回清洗前后的健康度报告
    """
    path = db_path or GQ_DB
    conn = sqlite3.connect(path)
    try:
        df = pd.read_sql_query("SELECT * FROM match_outcomes", conn)
    finally:
        conn.close()

    if require_scores:
        df = df.dropna(subset=["score_home", "score_away"])

    d = mark_quality(df)
    before = health_check(d, "清洗前(全库)")

    out = d
    if drop_virtual:
        out = out[~out["is_virtual"]]
    if drop_truncated:
        out = out[~out["is_truncated"]]
    out = out.copy()

    after = health_check(out, "清洗后")
    report = {
        "db": path,
        "before": before,
        "after": after,
        "dropped_virtual": int(d["is_virtual"].sum()) if drop_virtual else 0,
        "dropped_truncated": int((d["is_truncated"] & ~d["is_virtual"]).sum()) if drop_truncated else 0,
        "benchmark": {"avg_goals": BENCH_AVG_GOALS, "zero_rate": BENCH_ZERO_RATE},
    }

    if not after["ok"]:
        log.warning(
            "[clean_outcomes] 清洗后数据仍偏离真实足球基准: "
            f"均进球={after['avg_goals']} 零封={after['zero_rate']:.2%} "
            f"(基准 {BENCH_AVG_GOALS} / {BENCH_ZERO_RATE}) —— 可能还有未识别的污染源"
        )

    if return_report:
        return out, report
    return out


# ═════════════════════════════════════════════════════════════════════
# 第二层 SSoT: events.db matches 表 假 0-0 口径 (2026-08-31 治理落地)
# 与上层 match_outcomes 层互补: 上层处理虚拟/截断, 本层处理 score_missing 假 0-0。
# ═════════════════════════════════════════════════════════════════════


def clean_where() -> str:
    """SQL WHERE 片段: 排除已打标假 0-0 (matches.score_missing=1). 用法:
    con.execute(f"SELECT ... FROM matches WHERE status='finished' AND {clean_where()}")"""
    return "(score_missing IS NULL OR score_missing != 1)"


def is_clean(con: sqlite3.Connection, match_key: str) -> bool:
    """单场主口径: 未打标假 0-0 即干净. 查无此场→False; 旧库无 score_missing 列→True(兼容)."""
    try:
        row = con.execute(
            "SELECT score_missing FROM matches WHERE match_key=? LIMIT 1", (match_key,)
        ).fetchone()
        if row is None:
            return False  # 查无此场
        return row[0] != 1
    except sqlite3.OperationalError:
        # 列不存在 (旧库/其他 DB) → 保守视为干净 (该库无打标体系)
        return True


def has_inplay_evidence(con: sqlite3.Connection, match_key: str,
                        kickoff_ts: float | None = None) -> bool:
    """补充层 (strict): 场次是否有开赛后滚球比分快照 (= 采集器真实跟踪过比赛).
    兜住"无 kickoff 未打标"的 0:0。kickoff_ts 缺失时退化为"存在任一 score_at 快照"."""
    if kickoff_ts is None:
        return con.execute(
            "SELECT 1 FROM odds_snapshots WHERE match_key=? "
            "AND score_at IS NOT NULL AND score_at != '' LIMIT 1", (match_key,)
        ).fetchone() is not None
    return con.execute(
        "SELECT 1 FROM odds_snapshots WHERE match_key=? "
        "AND score_at IS NOT NULL AND score_at != '' AND captured_at > ? LIMIT 1",
        (match_key, kickoff_ts - 300),
    ).fetchone() is not None


def matches_stats(con: sqlite3.Connection) -> dict:
    """matches 表治理统计 (只读). 主口径 score_missing!=1; 未打标 0:0 由 strict 层兜底."""
    def one(sql: str) -> int:
        return con.execute(sql).fetchone()[0]

    total = one("SELECT COUNT(*) FROM matches WHERE status='finished'")
    zero00 = one("SELECT COUNT(*) FROM matches WHERE status='finished' "
                 "AND score_home=0 AND score_away=0")
    tagged = one("SELECT COUNT(*) FROM matches WHERE score_missing=1")
    untagged_00 = one(f"SELECT COUNT(*) FROM matches WHERE status='finished' "
                      f"AND score_home=0 AND score_away=0 AND {clean_where()}")
    clean_total = one(f"SELECT COUNT(*) FROM matches WHERE status='finished' "
                      f"AND {clean_where()}")
    return {
        "finished_total": total,
        "finished_00_total": zero00,
        "score_missing_tagged": tagged,
        "finished_00_untagged": untagged_00,   # 真0:0 + 无 kickoff 无法判定
        "clean_finished": clean_total,
        "dirty_00_rate": round(zero00 / total, 4) if total else None,
        "clean_00_rate": round(untagged_00 / clean_total, 4) if clean_total else None,
        "note": "主口径 score_missing!=1; 未打标 0:0 由 has_inplay_evidence 兜底 (IR-04)",
    }


def _cli() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    conn = sqlite3.connect(GQ_DB)
    df = pd.read_sql_query("SELECT * FROM match_outcomes", conn)
    conn.close()
    df = df.dropna(subset=["score_home", "score_away"])
    d = mark_quality(df)

    rows = [
        health_check(d, "全库(污染)"),
        health_check(d[~d["is_virtual"]], "剔虚拟"),
        health_check(d[~d["is_truncated"]], "剔截断"),
        health_check(d[d["is_clean"]], "剔虚拟+剔截断 <<干净"),
        health_check(d[d["is_virtual"]], "  [被剔] 虚拟赛事"),
        health_check(d[d["is_truncated"]], "  [被剔] 采集截断"),
    ]
    print("=" * 78)
    print("赛果数据健康度体检 — match_outcomes")
    print("=" * 78)
    print(f"{'数据段':<24s}{'n':>7s}{'均进球':>9s}{'零封率':>9s}{'大2.5':>9s}{'达标':>7s}")
    for r in rows:
        if not r["n"]:
            continue
        print(f"{r['label']:<24s}{r['n']:>7d}{r['avg_goals']:>9.3f}"
              f"{r['zero_rate']:>9.2%}{r['over25_rate']:>9.2%}"
              f"{('OK' if r['ok'] else 'BAD'):>7s}")
    print(f"\n真实足球基准: 均进球 {BENCH_AVG_GOALS[0]}~{BENCH_AVG_GOALS[1]} / "
          f"零封率 {BENCH_ZERO_RATE[0]:.0%}~{BENCH_ZERO_RATE[1]:.0%}")

    print("\n-- 按源 --")
    for src, g in d.groupby(d["source"].fillna("(null)")):
        r = health_check(g, f"  {src} 全部")
        rc = health_check(g[g["is_clean"]], f"  {src} 干净")
        print(f"  {src:<8s} 全部 n={r['n']:<5d} 均={r['avg_goals']:.3f} 零封={r['zero_rate']:.2%}"
              f"   |  干净 n={rc['n']:<5d} 均={rc['avg_goals'] if rc['n'] else float('nan'):.3f} "
              f"零封={rc['zero_rate'] if rc['n'] else float('nan'):.2%}")


if __name__ == "__main__":
    import sys
    sys.path.insert(0, ROOT)
    _cli()
