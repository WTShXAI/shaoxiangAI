"""
virtual_match_filter.py — 电子盘/虚拟赛事识别与隔离 (P0 数据保真)

事故:
  2026-08-05 回测时发现 match_outcomes 中 2026-08-04 的比赛
  平均进球 4.70 / 零进球率 0% / 大球率 81.3% —— 真实足球不可能。
  定位到污染源:
      瓦尔哈拉杯 2026 (8分钟)   143 场  均 5.66 球
      瓦尔基里杯 2026 (8分钟)    85 场  均 5.82 球
  这是 8 分钟一场的虚拟(电子)足球, 违反红线
  「电子盘落盘只进 data/electronic_poll_*, 绝不碰 events.db/match_outcomes/特征库」。
  且两者都含「杯」字 -> 直接污染 Cup 分类的回测结论。

识别规则 (保守, 宁可漏判不可误杀真实比赛):
  R1 时长标记 : 联赛名含 "(N分钟)" 且 N <= 20        -> 虚拟
                注意 "(2x40分钟)" 这类是真实青年/特殊赛制, 不能误杀
  R2 关键词   : EAFC / FIFA<两位数版本号> / 电子 / 虚拟 / eSoccer / Esport / Cyber / VS-
                注意: 裸 "FIFA" 会误杀 FIFA World Cup(真实世界杯), 已改为只匹配
                      "FIFA 23" 这类游戏版本号; 并加 WHITELIST 兜底
  R3 已知名单 : 瓦尔哈拉杯 / 瓦尔基里杯 (人工确认)
  R0 白名单   : 命中白名单的联赛无条件判为真实 (优先级最高)
  R4 统计异常 : 同一联赛 n>=20 且 (均进球 >= 4.5 且 零进球率 <= 0.02) -> 标记待人工复核
                (不自动过滤, 只报警, 防止误杀真实高进球联赛)

用法:
  from pipeline.virtual_match_filter import is_virtual, filter_real
  df_clean = filter_real(df)             # 剔除虚拟场次
  mask     = df["league"].map(is_virtual)

CLI:
  python pipeline/virtual_match_filter.py          # 扫描 events.db 并出报告
  python pipeline/virtual_match_filter.py --apply  # 在 match_outcomes 打 is_virtual 标记列
"""
import os
import re
import sys
import json
import sqlite3
import logging
import argparse
import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GQ_DB = os.path.join(ROOT, "data", "events.db")
OUT_DIR = os.path.join(ROOT, "data", "pricing_template")
os.makedirs(OUT_DIR, exist_ok=True)

# R1: (N分钟) 且 N<=20 -> 虚拟; (2x40分钟) 这种带乘号的是真实赛制
_DUR_RE = re.compile(r"\((\d+)\s*分钟\)")
_DUR_MULTI_RE = re.compile(r"\(\s*\d+\s*[x×]\s*\d+\s*分钟\s*\)")
VIRTUAL_MAX_MINUTES = 20

# R2 关键词
# 注意: 不能用裸 "FIFA" —— FIFA World Cup 2014/2018/2022/2026 共 328 场是真实世界杯,
#       2026-08-05 首版规则把它们全误杀了。改为只匹配游戏版本号 "FIFA 23"/"FIFA23"
#       (紧跟 2 位数字, 且后面不再跟数字, 因此不会命中 "FIFA World Cup 2026")。
_KW_RE = re.compile(
    r"EAFC"
    r"|FIFA\s*\d{2}(?!\d)"
    r"|电子竞技|电子足球|电竞|虚拟"
    r"|eSoccer|e-?Soccer|Esport|e-?Sports|Cyber"
    r"|^VS-",
    re.IGNORECASE,
)

# R3 已知虚拟赛事名单 (人工确认, 2026-08-05)
KNOWN_VIRTUAL = {
    "瓦尔哈拉杯",
    "瓦尔基里杯",
}

# R0 白名单: 无条件真实, 优先级高于所有规则 (防止关键词/时长规则误杀)
# ⚠️ 2026-08-13 修正: 移除裸 "世界杯" —— 该词过宽, 会让中文虚拟盘
#    "世界杯2026"(EAFC25/PANDA 包装) 被误判为真实, 污染 match_outcomes/训练数据。
#    真实世界杯英文标签 "FIFA World Cup 2026" 已由下方 "FIFA World Cup" 保护;
#    中文真实 WC2026(在加拿大、墨西哥&美国) 无 EAFC/VS- 标记, 默认判真实(不误杀)。
#    虚拟变体 "VS-世界杯2026...EAFC25" / "世界杯2026 EAFC25" 改由 R2 关键词捕获。
WHITELIST = (
    "FIFA World Cup",
    "FIFA Club World Cup",
    "FIFA Confederations Cup",
    "FIFA U-20 World Cup",
    "FIFA U-17 World Cup",
    "FIFA Women",
)

# R4 统计异常阈值 (仅报警不过滤)
ANOMALY_MIN_N = 20
ANOMALY_AVG_GOALS = 4.5
ANOMALY_MAX_ZERO_RATE = 0.02


def is_virtual(league: str | None) -> bool:
    """判定一个联赛名是否为虚拟/电子盘赛事."""
    if not league or not isinstance(league, str):
        return False
    s = league.strip()

    # R0 白名单 (最高优先级, 无条件真实)
    low = s.lower()
    for w in WHITELIST:
        if w.lower() in low:
            return False

    # R3 已知名单
    for kw in KNOWN_VIRTUAL:
        if kw in s:
            return True

    # R2 关键词
    if _KW_RE.search(s):
        return True

    # R1 时长: 先排除 (2x40分钟) 这类真实赛制
    if _DUR_MULTI_RE.search(s):
        return False
    m = _DUR_RE.search(s)
    if m:
        try:
            if int(m.group(1)) <= VIRTUAL_MAX_MINUTES:
                return True
        except ValueError:
            pass
    return False


def filter_real(df: pd.DataFrame, league_col: str = "league") -> pd.DataFrame:
    """返回剔除虚拟赛事后的 DataFrame."""
    if league_col not in df.columns:
        return df
    mask = df[league_col].map(is_virtual)
    return df[~mask].copy()


def detect_statistical_anomalies(df: pd.DataFrame) -> list:
    """R4: 找出统计上像虚拟盘但未被规则捕获的联赛 (只报警)."""
    if not {"league", "score_home", "score_away"} <= set(df.columns):
        return []
    d = df.dropna(subset=["score_home", "score_away"]).copy()
    d["tot"] = d["score_home"] + d["score_away"]
    d["flagged"] = d["league"].map(is_virtual)
    out = []
    for lg, g in d[~d["flagged"]].groupby("league"):
        if len(g) < ANOMALY_MIN_N:
            continue
        avg = float(g["tot"].mean())
        zr = float((g["tot"] == 0).mean())
        if avg >= ANOMALY_AVG_GOALS and zr <= ANOMALY_MAX_ZERO_RATE:
            out.append({"league": lg, "n": int(len(g)),
                        "avg_goals": round(avg, 2), "zero_rate": round(zr, 4)})
    return sorted(out, key=lambda x: -x["avg_goals"])


def scan(apply_flag: bool = False) -> dict:
    conn = sqlite3.connect(GQ_DB)
    df = pd.read_sql_query(
        """SELECT mid, home, away, league, kickoff, score_home, score_away
           FROM match_outcomes""", conn)

    df["is_virtual"] = df["league"].map(is_virtual)
    scored = df.dropna(subset=["score_home", "score_away"]).copy()
    scored["tot"] = scored["score_home"] + scored["score_away"]

    log.info("=" * 66)
    log.info("电子盘/虚拟赛事扫描 — match_outcomes")
    log.info("=" * 66)
    log.info(f"总场次: {len(df)}, 有比分: {len(scored)}")
    log.info(f"识别为虚拟: {int(df['is_virtual'].sum())} 场 "
             f"({df['is_virtual'].mean():.2%})")

    vir = scored[scored["is_virtual"]]
    real = scored[~scored["is_virtual"]]

    log.info("\n-- 虚拟赛事明细 --")
    if len(vir):
        g = vir.groupby("league").agg(n=("tot", "size"), avg_goals=("tot", "mean"),
                                      zero_rate=("tot", lambda s: (s == 0).mean()))
        for lg, r in g.sort_values("n", ascending=False).iterrows():
            log.info(f"  {lg:<32s} n={int(r['n']):>4d} 均进球={r['avg_goals']:.2f} "
                     f"零封={r['zero_rate']:.1%}")
    else:
        log.info("  (无)")

    log.info("\n-- 清洗前后对比 --")
    log.info(f"  {'':16s}{'n':>7s}{'均进球':>9s}{'零封率':>9s}")
    log.info(f"  {'污染库(全部)':<16s}{len(scored):>7d}{scored['tot'].mean():>9.3f}"
             f"{(scored['tot'] == 0).mean():>9.2%}")
    log.info(f"  {'真实比赛':<16s}{len(real):>7d}{real['tot'].mean():>9.3f}"
             f"{(real['tot'] == 0).mean():>9.2%}")
    log.info(f"  {'虚拟比赛':<16s}{len(vir):>7d}"
             f"{(vir['tot'].mean() if len(vir) else float('nan')):>9.3f}"
             f"{((vir['tot'] == 0).mean() if len(vir) else float('nan')):>9.2%}")
    log.info("  (真实足球基准: 均进球 2.5~2.8, 零封率 7~9%)")

    # 按日对比
    real["date"] = pd.to_datetime(real["kickoff"], errors="coerce")
    scored["date"] = pd.to_datetime(scored["kickoff"], errors="coerce")
    log.info("\n-- 按日: 清洗前 vs 清洗后 均进球 --")
    a = scored.dropna(subset=["date"]).groupby(scored["date"].dt.date)["tot"].agg(["size", "mean"])
    b = real.dropna(subset=["date"]).groupby(real["date"].dt.date)["tot"].agg(["size", "mean"])
    log.info(f"  {'日期':<12s}{'前n':>6s}{'前均':>8s}{'后n':>6s}{'后均':>8s}{'差':>8s}")
    for d in a.index:
        pn, pm = int(a.loc[d, "size"]), float(a.loc[d, "mean"])
        if d in b.index:
            qn, qm = int(b.loc[d, "size"]), float(b.loc[d, "mean"])
        else:
            qn, qm = 0, float("nan")
        log.info(f"  {str(d):<12s}{pn:>6d}{pm:>8.2f}{qn:>6d}{qm:>8.2f}{pm - qm:>+8.2f}")

    anomalies = detect_statistical_anomalies(scored)
    log.info("\n-- R4 统计异常(未被规则捕获, 需人工复核) --")
    if anomalies:
        for a_ in anomalies:
            log.info(f"  [!] {a_['league']:<30s} n={a_['n']:>4d} "
                     f"均进球={a_['avg_goals']:.2f} 零封={a_['zero_rate']:.1%}")
    else:
        log.info("  (无)")

    applied = False
    if apply_flag:
        try:
            cur = conn.cursor()
            cols = [r[1] for r in cur.execute("PRAGMA table_info(match_outcomes)").fetchall()]
            if "is_virtual" not in cols:
                cur.execute("ALTER TABLE match_outcomes ADD COLUMN is_virtual INTEGER DEFAULT 0")
                log.info("\n已新增列 match_outcomes.is_virtual")
            cur.execute("UPDATE match_outcomes SET is_virtual = 0")
            vir_leagues = sorted(set(vir["league"].dropna().unique().tolist()))
            for lg in vir_leagues:
                cur.execute("UPDATE match_outcomes SET is_virtual = 1 WHERE league = ?", (lg,))
            conn.commit()
            n_flag = cur.execute("SELECT COUNT(*) FROM match_outcomes WHERE is_virtual = 1").fetchone()[0]
            log.info(f"已标记 {n_flag} 场为 is_virtual=1 (未删除, 仅隔离)")
            applied = True
        except Exception as e:
            log.error(f"写库失败: {e}")
    conn.close()

    report = {
        "total": int(len(df)),
        "scored": int(len(scored)),
        "virtual_n": int(df["is_virtual"].sum()),
        "virtual_leagues": (
            vir.groupby("league")["tot"].agg(["size", "mean"]).reset_index()
            .rename(columns={"size": "n", "mean": "avg_goals"}).to_dict("records")
            if len(vir) else []
        ),
        "before": {"n": int(len(scored)), "avg_goals": round(float(scored["tot"].mean()), 3),
                   "zero_rate": round(float((scored["tot"] == 0).mean()), 4)},
        "after": {"n": int(len(real)), "avg_goals": round(float(real["tot"].mean()), 3),
                  "zero_rate": round(float((real["tot"] == 0).mean()), 4)},
        "statistical_anomalies": anomalies,
        "db_flag_applied": applied,
    }
    path = os.path.join(OUT_DIR, "virtual_match_scan.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    log.info(f"\n报告已存: {path}")
    return report


# ---------------------------------------------------------------- 回归自检
# 2026-08-05 事故: 裸 "FIFA" 关键词误杀 328 场真实世界杯。
# 任何人改动规则后必须让这组断言通过。
SELFTEST_VIRTUAL = [
    "瓦尔哈拉杯 2026 (8分钟)",
    "瓦尔基里杯 2026 (8分钟)",
    "EAFC25 PANDA",
    "eSoccer Battle (8分钟)",
    "FIFA 23 电竞联赛",
    "VS-Liga",
]
SELFTEST_REAL = [
    "FIFA World Cup 2026",
    "FIFA World Cup 2014",
    "FIFA World Cup 2018",
    "FIFA World Cup 2022",
    "FIFA Club World Cup",
    "拉尔库迪亚国际足球锦标赛U20 (2x40分钟)",
    "英格兰超级联赛",
    "丹麦杯",
    "卢旺达卡加梅杯",
    "奥地利杯",
]


def selftest() -> bool:
    ok = True
    for lg in SELFTEST_VIRTUAL:
        if not is_virtual(lg):
            log.error(f"[SELFTEST FAIL] 应判虚拟却判成真实: {lg}")
            ok = False
    for lg in SELFTEST_REAL:
        if is_virtual(lg):
            log.error(f"[SELFTEST FAIL] 应判真实却判成虚拟: {lg}")
            ok = False
    log.info(f"[SELFTEST] {'PASS' if ok else 'FAIL'} "
             f"({len(SELFTEST_VIRTUAL)} 虚拟 / {len(SELFTEST_REAL)} 真实)")
    return ok


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="在 match_outcomes 写 is_virtual 标记列")
    ap.add_argument("--selftest-only", action="store_true", help="只跑规则自检")
    args = ap.parse_args()
    passed = selftest()
    if args.selftest_only:
        sys.exit(0 if passed else 1)
    if not passed and args.apply:
        log.error("自检未通过, 拒绝写库。")
        sys.exit(1)
    scan(apply_flag=args.apply)
