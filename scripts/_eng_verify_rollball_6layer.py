#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T08 滚球时间窗口 6 层 bug 回归闭环护栏 (engineering regression guard).

只读 / additive: 本脚本**不修改** analysis/live_goal_probe.py 任何已修逻辑, 仅对各层
修复点做独立硬断言, 把"已修对"钉死为"不回归", 防止未来改动踩回旧坑。

背景: 事故⑥ 滚球时间窗口 6 层根因已于 2026-08-21 在 analysis/live_goal_probe.py 根治
(主理人逐层读码确认)。T08 不是重写, 是回归闭环。

6 层与对应断言:
  L1/L2  λ 口径未拆分(赛前×rem_ratio vs 滚球 λ-G) → latest_ou_snapshot_phase 正确区分
          prematch/live, 且调用方把 lambda_source 透传 remaining_break_prob。
  L3     feed minute 占位污染(上半场恒45/下半场恒90) → resolve_true_minute 用 kickoff 墙钟
          解析真实分钟, 拒绝把 45/90 当分钟; 补时 feed>90 直接采信。
  L4     旧 `minute<90` 误筛下半场 → resolve_true_minute 把下半场真实分钟(46-90)正确归为
          phase='second', 不被踢掉。
  L5     _implied_total_from_pairs 方向反(旧: 全 P<0.5 却算 T=1.00) → 外推修正后 T<0.5 且≈0.20。
  L6     原查 'OU_2.5' 而库里是 'OU_2.50'(两位小数) → LW_OU_MARKETS 含 OU_2.50, 命中不落空。

运行(在 D:/Architecture 下, 用带 numpy 的 python):
  python scripts/_eng_verify_rollball_6layer.py
退出码: 0=全部 PASS, 1=有 FAIL。
"""
import os
import sys
import time
import sqlite3

# ── 路径: 让 `analysis` 包可导入 (本脚本位于 scripts/) ──
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from analysis.live_goal_probe import (  # noqa: E402
    resolve_true_minute,
    latest_ou_snapshot_phase,
    _implied_total_from_pairs,
    _lightweight_signal,
    _dewatered_over_prob,
    _parse_kickoff,
    LW_OU_MARKETS,
)

# 与 live_goal_probe 内部一致的中场休息时长(仅用于构造测试输入)
HALFTIME_BREAK_MIN = 15.0

# ───────────────────────── 结果收集 ─────────────────────────
_RESULTS = []


def _record(layer, name, ok, detail):
    _RESULTS.append((layer, name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {layer:>6}  {name}: {detail}")


# ───────────────────────── 测试输入构造 ─────────────────────────
def _kickoff_ts(kickoff_str):
    """复用 live_goal_probe._parse_kickoff 的解析, 返回 Unix 时间戳。"""
    return _parse_kickoff(kickoff_str)


def _now_for_playing(kickoff_str, playing_minute, half="second"):
    """构造 now_ts。

    playing_minute = 真实比赛进行分钟(不含中场)。
    - 上半场: 墙钟 elapsed = playing_minute
    - 下半场: 墙钟 elapsed = playing_minute + 15 (中场 15′)
    """
    kots = _kickoff_ts(kickoff_str)
    elapsed_wall = playing_minute if half == "first" else playing_minute + HALFTIME_BREAK_MIN
    return kots + elapsed_wall * 60.0


def _make_synthetic_db(match_key, markets):
    """建内存库, 写 matches + odds_snapshots。markets: dict market -> (over, under)。"""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE matches (match_key TEXT, kickoff TEXT)")
    con.execute(
        "CREATE TABLE odds_snapshots ("
        "match_key TEXT, market TEXT, selection TEXT, odds REAL, captured_at REAL)"
    )
    con.execute("INSERT INTO matches VALUES (?,?)", (match_key, "2026-08-21 12:00:00"))
    cap = 1_700_000_000.0
    for mkt, (ov, un) in markets.items():
        con.execute(
            "INSERT INTO odds_snapshots VALUES (?,?,?,?,?)",
            (match_key, mkt, "over", ov, cap),
        )
        con.execute(
            "INSERT INTO odds_snapshots VALUES (?,?,?,?,?)",
            (match_key, mkt, "under", un, cap),
        )
    con.commit()
    return con


# ───────────────────────── L3: feed 占位污染拒绝 ─────────────────────────
def test_L3_feed_pollution_rejected():
    """feed minute 占位污染(上半场恒45/下半场恒90)不得作为真实分钟。"""
    kickoff = "2026-08-21 12:00:00"

    # 真实进行 15′(上半场), feed 报 45(占位) → 真实分钟应≈15, 而非 45
    r1 = resolve_true_minute(
        kickoff, feed_minute=45, now_ts=_now_for_playing(kickoff, 15, "first")
    )
    ok1 = r1["minute"] == 15 and r1["phase"] == "first" and r1["minute"] != 45
    _record("L3", "上半场 feed=45 真实15′ → minute=15(非45)", ok1,
            f"minute={r1['minute']} phase={r1['phase']}")

    # 真实进行 60′(下半场), feed 报 90(占位) → 真实分钟应≈60, 而非 90
    r2 = resolve_true_minute(
        kickoff, feed_minute=90, now_ts=_now_for_playing(kickoff, 60, "second")
    )
    ok2 = r2["minute"] == 60 and r2["phase"] == "second" and r2["minute"] != 90
    _record("L3", "下半场 feed=90 真实60′ → minute=60(非90)", ok2,
            f"minute={r2['minute']} phase={r2['phase']}")

    # 补时 feed>90 直接采信(真实递增值)
    r3 = resolve_true_minute(
        kickoff, feed_minute=95, now_ts=_now_for_playing(kickoff, 80, "second")
    )
    ok3 = r3["minute"] == 95 and r3["phase"] == "et" and r3["source"] == "feed_extra"
    _record("L3", "补时 feed=95 → 直接采信 minute=95/phase=et", ok3,
            f"minute={r3['minute']} phase={r3['phase']} source={r3['source']}")

    return ok1 and ok2 and ok3


# ───────────────────────── L4: 下半场不被误筛 ─────────────────────────
def test_L4_second_half_not_screened():
    """下半场(真实分钟 46-89) 用 resolve_true_minute 解析后, phase 必须='second',
    且真实分钟落 46-90, 不会被旧 `minute<90` 式误筛踢掉。"""
    kickoff = "2026-08-21 12:00:00"
    all_ok = True
    for m in (46, 50, 60, 70, 80, 89):
        r = resolve_true_minute(
            kickoff, feed_minute=90, now_ts=_now_for_playing(kickoff, m, "second")
        )
        ok = r["phase"] == "second" and 46 <= r["minute"] <= 90
        all_ok = all_ok and ok
        if not ok:
            _record("L4", f"playing={m}′ → second/分钟区间", ok,
                    f"minute={r['minute']} phase={r['phase']}")
    _record("L4", "下半场 46-89′ 全部解析为 second 且 46<=minute<=90", all_ok,
            "playing∈{46,50,60,70,80,89}")
    return all_ok


# ───────────────────────── L5: 隐含总球方向 ─────────────────────────
def test_L5_implied_total_direction():
    """已知 pairs (塔什干棉农 vs 铁尔米兹 35.6′ 0-0): P(over@0.5)=0.389, P(over@0.75)=0.296
    (明确看小, 所有线 P<0.5) → 隐含总球 T 应 <0.5 且 ≈0.20 (旧逻辑错算 T=1.00)。"""
    # 构造产生目标去水 P 的 (line, over, under); 赔率须均 >1.01(否则被去水函数判非法丢弃)
    pairs = [
        (0.5, 2.0, 1.27332),   # → P(over)=0.38899
        (0.75, 3.33, 1.40),    # → P(over)=0.29600 (under=1.40>1.01 合法)
    ]
    t = _implied_total_from_pairs(pairs)
    ok = t is not None and t < 0.5 and abs(t - 0.20) < 0.02
    _record("L5", "全 P<0.5 → T<0.5 且 ≈0.20(非旧错值 1.00)", ok, f"T={t}")
    return ok


# ───────────────────────── L6: OU 市场命名(两位小数) ─────────────────────────
def test_L6_ou_market_naming():
    """LW_OU_MARKETS 含两位小数 'OU_2.50'; _lightweight_signal 必须命中
    (而非查 'OU_2.5' 落空 → NO_EDGE 假信号)。"""
    con = _make_synthetic_db("M2", {"OU_2.50": (1.30, 5.0)})
    res = _lightweight_signal(con, "M2")
    # _lightweight_signal 命中后 half/full.signal 为 STRONG_BREAK(prob≈0.794, 方向 OVER),
    # 而非 NO_EDGE(旧查 'OU_2.5' 会落空)。注: 该函数返回体不含 ou_line 键,
    # 故以 signal/prob 证明"确实命中了某条 OU 配对", 配合下方负向控制(OU_2.5→NO_EDGE)
    # 即可证明是 LW_OU_MARKETS 含 'OU_2.50' 让命中成立。
    hit_ok = (
        res.get("half", {}).get("signal") == "STRONG_BREAK"
        and abs(res["half"]["prob"] - 0.794) < 0.001
        and res.get("full", {}).get("signal") == "STRONG_BREAK"
    )
    _record("L6", "OU_2.50 快照被命中(STRONG_BREAK, prob≈0.794, 非NO_EDGE漏空)", hit_ok,
            f"signal={res.get('half')}")
    con.close()

    # 负向控制: 仅存在旧命名 'OU_2.5' → 必须落空(NO_EDGE), 印证修复必要性
    con2 = _make_synthetic_db("M3", {"OU_2.5": (1.30, 5.0)})
    res2 = _lightweight_signal(con2, "M3")
    ctrl_ok = res2.get("half", {}).get("signal") == "NO_EDGE"
    _record("L6", "负向控制: 仅 'OU_2.5'(旧名) → NO_EDGE(印证修复)", ctrl_ok,
            f"signal={res2.get('half')}")
    con2.close()
    return hit_ok and ctrl_ok


# ───────────────────────── L1/L2: λ 口径拆分决策点 ─────────────────────────
def _guard_caller_wires_lambda_source():
    """静态护栏: 确认调用方把 latest_ou_snapshot_phase 的结果透传给 remaining_break_prob
    (rem_ratio×λ 的 prematch 口径键控在 lambda_source 上, 防止被解耦)。"""
    src_path = os.path.join(_ROOT, "analysis", "live_goal_probe.py")
    with open(src_path, "r", encoding="utf-8") as f:
        src = f.read()
    n_wire = src.count("lambda_source=_lambda_src")
    ok = (
        "_lambda_src, _snap_off = latest_ou_snapshot_phase" in src
        and n_wire >= 2
    )
    _record("L1/L2", "调用方把 lambda_source 透传 remaining_break_prob(≥2处)", ok,
            f"lambda_source 透传次数={n_wire}")
    return ok


def test_L1L2_lambda_caliber_split():
    """latest_ou_snapshot_phase 必须正确区分 赛前(prematch)/滚球(live) 快照,
    这是 λ 口径拆分(赛前×rem_ratio, 滚球用 λ-G)的决策点。"""
    kickoff = "2026-08-21 12:00:00"
    kots = _kickoff_ts(kickoff)
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE matches (match_key TEXT, kickoff TEXT)")
    con.execute(
        "CREATE TABLE odds_snapshots ("
        "match_key TEXT, market TEXT, selection TEXT, odds REAL, captured_at REAL)"
    )
    con.execute("INSERT INTO matches VALUES (?,?)", ("M1", kickoff))

    def _add_snap(offset_sec):
        cap = kots + offset_sec
        con.execute(
            "INSERT INTO odds_snapshots VALUES (?,?,?,?,?)",
            ("M1", "OU_2.50", "over", 2.0, cap),
        )
        con.execute(
            "INSERT INTO odds_snapshots VALUES (?,?,?,?,?)",
            ("M1", "OU_2.50", "under", 1.8, cap),
        )

    # 赛前: 快照在开赛 30s 后(<60s) → prematch
    _add_snap(30)
    phase_pre, off_pre = latest_ou_snapshot_phase(con, "M1", kickoff=kickoff)
    ok_pre = phase_pre == "prematch" and off_pre is not None
    _record("L1", "开赛30s快照 → prematch(λ需×rem_ratio)", ok_pre,
            f"phase={phase_pre} offset={off_pre}")

    # 滚球: 再加一个开赛 120s 后的快照(MAX 取最新) → live
    _add_snap(120)
    phase_live, off_live = latest_ou_snapshot_phase(con, "M1", kickoff=kickoff)
    ok_live = phase_live == "live" and off_live is not None
    _record("L2", "开赛120s快照 → live(λ用 λ-G)", ok_live,
            f"phase={phase_live} offset={off_live}")
    con.close()

    return ok_pre and ok_live and _guard_caller_wires_lambda_source()


# ───────────────────────── SMOKE: 真实/合成 DB endpoint 级冒烟 ─────────────────────────
def test_smoke_real_or_synthetic():
    """真实/合成 DB endpoint 级冒烟: 调 _lightweight_signal, 确认无异常, 记录耗时。"""
    gq_path = os.environ.get("GQ_DB", "D:/Architecture/data/GQ.db")
    using_real = False
    con = None
    mk = "SMOKE"
    if os.path.exists(gq_path):
        try:
            con = sqlite3.connect(gq_path, timeout=5)
            con.execute("PRAGMA busy_timeout=3000")
            row = con.execute(
                "SELECT match_key FROM odds_snapshots "
                "WHERE market='OU_2.50' AND selection='over' AND odds>1.01 AND odds<1000 "
                "LIMIT 1"
            ).fetchone()
            if row:
                mk = row[0]
                using_real = True
        except Exception:
            if con is not None:
                try:
                    con.close()
                except Exception:
                    pass
            con = None
    if con is None:
        con = _make_synthetic_db("SMOKE", {"OU_2.50": (1.30, 5.0)})
        mk = "SMOKE"
        using_real = False

    t0 = time.time()
    try:
        res = _lightweight_signal(con, mk)
        ok = isinstance(res, dict) and "half" in res and "full" in res
        dt = time.time() - t0
        _record(
            "SMOKE",
            f"{'真实' if using_real else '合成'}DB _lightweight_signal 无异常",
            ok,
            f"dt={dt*1000:.1f}ms match_key={mk} signal={res.get('half', {}).get('signal')}",
        )
    except Exception as e:  # noqa: BLE001
        _record("SMOKE", "冒烟调用抛异常", False, f"{type(e).__name__}: {e}")
        ok = False
    finally:
        try:
            con.close()
        except Exception:
            pass
    # 基线 0.895s (8/21 修复后列表查询), 此处仅记录耗时, 不硬性卡阈值(环境差异大)
    return ok


# ───────────────────────── main ─────────────────────────
def main():
    print("=" * 72)
    print("T08 滚球时间窗口 6 层 bug 回归闭环护栏")
    print("(只读 / additive, 不修改 analysis/live_goal_probe.py 已修逻辑)")
    print("=" * 72)

    groups = [
        ("L3", test_L3_feed_pollution_rejected),
        ("L4", test_L4_second_half_not_screened),
        ("L5", test_L5_implied_total_direction),
        ("L6", test_L6_ou_market_naming),
        ("L1/L2", test_L1L2_lambda_caliber_split),
        ("SMOKE", test_smoke_real_or_synthetic),
    ]

    results = []
    for layer, fn in groups:
        try:
            ok = fn()
        except Exception as e:  # noqa: BLE001
            ok = False
            _record(layer, "执行抛异常", False, f"{type(e).__name__}: {e}")
        results.append((layer, ok))

    print("-" * 72)
    n_pass = sum(1 for _, ok in results if ok)
    n_total = len(results)
    print(f"汇总: {n_pass}/{n_total} 组 PASS")
    for layer, ok in results:
        print(f"  {layer:>6}: {'PASS' if ok else 'FAIL'}")
    print("-" * 72)
    if n_pass == n_total:
        print("ALL PASS  6 层修复已钉死为不回归。")
        return 0
    print("SOME FAIL  存在回归!")
    return 1


if __name__ == "__main__":
    sys.exit(main())
