"""model_g1_reach_plan.py — 验证台 G1(2500) 达成路径只读推演（承接 T31/T33）

做什么：
  只读 verification.db 账本 + events.db daily_predictions 供给面 + reports/ 三态报告，
  按不同情景（S0 现状外推 / S1 门控修复 / S2 极端乐观 / S3 分段对齐 walkforward）
  推演三个在测 model_source（candles_ensemble / market_baseline / KNN）
  各自达成 G1 样本验收线 2500 的日历日，并输出"假设依赖表"。

诚实口径（IR-30）：
  - G1 只是**样本及格线**，不是 edge 证据。样本量够 ≠ 能过 G2-G6
    （ROI 置信区间 / 预测质量 / 校准 / 方向二项 / 零信息机械对照）。
  - 现状三态（reports/verification_report.json）：candles INCONCLUSIVE(n=181) /
    market NO EDGE / KNN NO EDGE —— 与 P0「全系统无已验证可交易边缘」一致，本脚本不推翻。
  - 若补入区间跨模型迭代（monitor drift ll_delta 越 0.03 线、retrain_gate.suggest=true），
    则 G1 计数须按 walkforward 分段，重训前的样本不可计入重训后的达标数 → S3 情景。

只读铁律：
  - events.db / verification.db 以 uri + mode=ro + PRAGMA query_only 打开，零写入。
  - 不跑 verification ingest、不重训、不碰生产调度、不杀进程、不跑 schema 变更。

用法：
  .venv/Scripts/python.exe scripts/model_g1_reach_plan.py
  .venv/Scripts/python.exe scripts/model_g1_reach_plan.py --json-only
  .venv/Scripts/python.exe scripts/model_g1_reach_plan.py --window-days 14
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVENTS_DB = os.path.join(REPO_ROOT, "data", "events.db")
LEDGER_DB = os.path.join(REPO_ROOT, "verification.db")
VERIFICATION_REPORT = os.path.join(REPO_ROOT, "reports", "verification_report.json")
MONITOR_STATUS = os.path.join(REPO_ROOT, "reports", "monitor_status.json")
DEFAULT_OUT = os.path.join(REPO_ROOT, "reports")

G1_ACCEPT_N = 2500          # 验证台 G1 样本验收线
DEFAULT_HORIZON_DAYS = 720  # 推演地平线：超过即判"本代模型内不可能"
BULK_WRITE_SHARE = 0.8      # 窗口内单日增量占比 ≥ 此值即视作一次性批量写入、速率按 0
STALE_DAYS = 7              # 账本最后日期距推演基准日超过此天数 → 判"已停滞"，速率按 0
SOURCES = ("candles_ensemble", "market_baseline", "KNN")

S0_AS_IS = "S0_现状外推"
S1_GATE_FIXED = "S1_门控修复"
S2_OPTIMISTIC = "S2_极端乐观"
S3_SEGMENT = "ST_分段对齐walkforward"

STATUS_REACHED = "REACHED"
STATUS_PROJECTED = "PROJECTED"
STATUS_BEYOND = "BEYOND_HORIZON"
STATUS_NEVER = "NEVER_RATE_ZERO"


# ── 只读连接 ───────────────────────────────────────────────────────────────
def open_readonly(db_path: str) -> sqlite3.Connection:
    """uri + mode=ro + query_only 双保险，绝不写入。"""
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"DB not found: {db_path}")
    uri = f"file:{os.path.abspath(db_path)}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.execute("PRAGMA query_only=1")
    con.row_factory = sqlite3.Row
    return con


def daily_ledger_counts(con: sqlite3.Connection) -> dict[tuple[str, str], int]:
    """账本按 (model_source, match_date) 的可信样本计数（只读）。"""
    cur = con.execute(
        "SELECT model_source, match_date, COUNT(*) AS n "
        "FROM verification_ledger WHERE is_credible = 1 "
        "GROUP BY model_source, match_date"
    )
    return {(r["model_source"], (r["match_date"] or "")[:10]): r["n"] for r in cur}


def daily_supply_counts(con: sqlite3.Connection, source: str) -> dict[str, dict]:
    """daily_predictions 供给面：按 match_date 统计总行数与 finished 行数（只读）。"""
    cur = con.execute(
        "SELECT substr(match_date,1,10) AS d, status, COUNT(*) AS n "
        "FROM daily_predictions WHERE model_source = ? "
        "GROUP BY d, status",
        (source,),
    )
    out: dict[str, dict] = defaultdict(lambda: {"total": 0, "finished": 0})
    for r in cur.fetchall():
        d = r["d"]
        out[d]["total"] += r["n"]
        if r["status"] == "finished":
            out[d]["finished"] += r["n"]
    return dict(out)


# ── 纯推演函数（零 DB，可单测）─────────────────────────────────────────────
def build_series(counts: dict[tuple[str, str], int]) -> dict[str, list[dict]]:
    """把 (source, date) -> n 转成各 source 的累计序列（按日期升序）。"""
    per: dict[tuple[str, str], int] = defaultdict(int)
    for (src, d), n in counts.items():
        if d:
            per[(src, d)] += n
    series: dict[str, list[dict]] = defaultdict(list)
    for (src, d) in sorted(per.keys(), key=lambda k: (k[1], k[0])):
        prev = series[src][-1]["cum_n"] if series[src] else 0
        series[src].append({"date": d, "daily_n": per[(src, d)], "cum_n": prev + per[(src, d)]})
    return dict(series)


def crossing_date(series: list[dict], target: int) -> str | None:
    """累计首次 >= target 的日期。"""
    for row in series:
        if row["cum_n"] >= target:
            return row["date"]
    return None


def rate_over_window(series: list[dict], end_date: str, window_days: int) -> float:
    """窗口 (end_date-window_days, end_date] 内的日均入账速率（样本/天）。"""
    if window_days <= 0:
        return 0.0
    lo = date.fromisoformat(end_date) - timedelta(days=window_days)
    total = sum(
        r["daily_n"] for r in series
        if r["date"] > lo.isoformat() and r["date"] <= end_date
    )
    return round(total / float(window_days), 4)


def bulk_share_over_window(series: list[dict], end_date: str, window_days: int) -> float:
    """窗口内单日最大增量占比（用于识别"一次性批量写入"而非稳态累积）。"""
    lo = date.fromisoformat(end_date) - timedelta(days=window_days)
    tot = 0
    mx = 0
    for r in series:
        if r["date"] > lo.isoformat() and r["date"] <= end_date:
            tot += r["daily_n"]
            mx = max(mx, r["daily_n"])
    if tot <= 0:
        return 0.0
    return round(mx / float(tot), 4)


def project(
    cum: int,
    rate: float,
    target: int = G1_ACCEPT_N,
    as_of: str | None = None,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
) -> dict:
    """按给定速率推演达标日。速率为 0 → NEVER_RATE_ZERO；超出地平线 → BEYOND_HORIZON。"""
    as_of = as_of or date.today().isoformat()
    if cum >= target:
        return {"status": STATUS_REACHED, "cum": cum, "rate": rate, "days_needed": 0,
                "reach_date": None, "note": "已达标（具体达标日见 crossing_date）"}

    if rate is None or rate <= 0:
        return {"status": STATUS_NEVER, "cum": cum, "rate": rate, "days_needed": None,
                "reach_date": None, "note": "当前速率为 0，样本不增长，永不达成"}

    days_needed = int(math.ceil((target - cum) / rate))
    reach = date.fromisoformat(as_of) + timedelta(days=days_needed)
    if days_needed > horizon_days:
        return {"status": STATUS_BEYOND, "cum": cum, "rate": rate, "days_needed": days_needed,
                "reach_date": None,
                "note": f"需 {days_needed} 天 > 地平线 {horizon_days} 天"}
    return {"status": STATUS_PROJECTED, "cum": cum, "rate": rate, "days_needed": days_needed,
            "reach_date": reach.isoformat(), "note": ""}


def segment_aligned_reach(
    cum: int,
    rate: float,
    target: int = G1_ACCEPT_N,
    as_of: str | None = None,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
) -> dict:
    """S3：重训后重新起算（重训前样本属上一代模型，walkforward 下不可并账）。"""
    as_of = as_of or date.today().isoformat()
    return project(cum=0, rate=rate, target=target, as_of=as_of, horizon_days=horizon_days)


def assumption_row(scenario: str, source: str, rate_basis: str, holds_now: bool,
                   note: str) -> dict:
    """假设依赖表的一行（纯函数，便于单测）。"""
    return {
        "scenario": scenario,
        "source": source,
        "rate_basis": rate_basis,
        "holds_now": bool(holds_now),
        "valid_before_fix": not bool(holds_now),
        "note": note,
    }


def build_scenarios(series: dict[str, list[dict]], supply: dict[str, dict[str, dict]],
                    window_days: int, as_of: str) -> dict[str, list[dict]]:
    """为三个 source 生成 S0/S1/S2 三种情景的推演结果。

    情景定义：
      S0 现状外推  —— 用窗口内实测速率外推（门控未修 → 门控类 source 速率=0 → NEVER）
      S1 门控修复  —— 用"补账机会"速率 = (门控后仍未入账的候选数) / (距上次账本日期天数)
      S2 极端乐观  —— 用供给面原始行数速率（daily_predictions 当日新增，未经守卫折算）
    """
    out: dict[str, list[dict]] = {}
    for src in SOURCES:
        ser = series.get(src, [])
        cum = ser[-1]["cum_n"] if ser else 0
        last_date = ser[-1]["date"] if ser else None
        sup = supply.get(src) or {}
        backlog = 0
        days_since = 0
        if last_date:
            # 门控后仍未入账的候选（供给面总行 - 已翻 finished 的行）
            backlog = sum(v["total"] - v["finished"] for d, v in sup.items() if d > last_date)
            days_since = max(1, (date.fromisoformat(as_of) - date.fromisoformat(last_date)).days)
        rows: list[dict] = []

        # S0 —— 现状外推。两道防假阳性闸门：
        #   ① 停滞闸门：账本最后日期距基准日 > STALE_DAYS → 该 source 已停止入账，速率按 0
        #      （candles_ensemble 自 09-18 起零增长，窗口速率全来自一次性批量写入）
        #   ② 批量闸门：窗口内单日增量占比 ≥ 0.8 亦按 0 处理
        stale = (date.fromisoformat(as_of) - date.fromisoformat(last_date)).days if last_date else None
        r0 = rate_over_window(ser, last_date or as_of, window_days) if last_date else 0.0
        bulk = bulk_share_over_window(ser, last_date, window_days) if last_date else 0.0
        if last_date and (stale or 0) > STALE_DAYS:
            r0 = 0.0
            s0 = project(cum, r0, as_of=as_of)
            s0["note"] = (f"账本已 {stale} 天未增长（> {STALE_DAYS} 天停滞线），速率按 0 处理")
        elif last_date and bulk >= BULK_WRITE_SHARE:
            r0 = 0.0
            s0 = project(cum, r0, as_of=as_of)
            s0["note"] = f"窗口内 {bulk:.0%} 增量集中于单日批量写入，非稳态速率，按 0 处理"
        else:
            s0 = project(cum, r0, as_of=as_of)
            s0["note"] = ""
        s0.update({"r0": r0, "bulk_share": bulk, "stale_days": stale,
                   "last_ledger_date": last_date, "scenario": S0_AS_IS})
        rows.append(s0)

        # S1 门控修复：补账机会速率
        if last_date:
            r1 = round(backlog / float(days_since), 4)
            note1 = (
                f"补账候选 {backlog} 行 / {days_since} 天；含一次性 backlog 消化，"
                f"非稳态速率（T33 实测净新增 461 行跨 9 天 ≈ 51/天，含去重与假0-0剔除）"
            )
        else:
            backlog, days_since, r1 = 0, 0, 0.0
            note1 = "无账本基线，无法估计"
        s1 = project(cum, r1, as_of=as_of)
        s1.update({"backlog": backlog, "days_since": days_since, "scenario": S1_GATE_FIXED,
                   "rate_note": note1})
        rows.append(s1)

        # S2 极端乐观：供给面原始行数速率（供给窗口 = window_days）
        sup_total = sum(v["total"] for d, v in sup.items()
                        if d > (date.fromisoformat(as_of) - timedelta(days=window_days)).isoformat()
                        and d <= as_of)
        r2 = round(sup_total / float(window_days), 4) if window_days > 0 else 0.0
        s2 = project(cum, r2, as_of=as_of)
        s2.update({"supply_rows": sup_total, "scenario": S2_OPTIMISTIC,
                   "rate_note": f"供给面原始行数 {sup_total} / {window_days} 天，未扣除守卫剔除"})
        rows.append(s2)

        # ST 分段对齐：重训后重新起算，速率取 S0/S1 中较高者（两者皆 0 则 NEVER）
        fwd = max(r0, r1)
        basis = "max(现状速率, 补账速率)"
        s3 = project(0, fwd, as_of=as_of)
        s3.update({"scenario": S3_SEGMENT, "rate": fwd, "rate_basis_note": basis,
                   "rate_note": "重训后重新起算；重训前样本属上一代模型，walkforward 下不计入"})
        rows.append(s3)

        out[src] = rows
    return out


def build_assumptions(scenarios: dict[str, list[dict]], series: dict[str, list[dict]],
                      gate_blocked_srcs: list[str], drift_alert: bool, retrain_suggest: bool,
                      as_of: str) -> list[dict]:
    """假设依赖表：每条情景依赖什么前提，哪些"未经修复前不成立"。

    `gate_blocked_srcs` 逐 source 判定（KNN 走 matches.status 门控，不受 daily 门控影响）。
    """
    rows: list[dict] = []
    gate_blocked_set = set(gate_blocked_srcs or [])
    for src, rows_sc in scenarios.items():
        cum = series.get(src, [-1])[-1]["cum_n"] if series.get(src) else 0
        gate_blocked = src in gate_blocked_set
        s0 = next(r for r in rows_sc if r["scenario"] == S0_AS_IS)
        s1 = next(r for r in rows_sc if r["scenario"] == S1_GATE_FIXED)
        rows.append(assumption_row(
            S0_AS_IS, src, "窗口内实测日均入账速率", not gate_blocked,
            "门控缺陷（daily_predictions.status 无人翻 finished，T33 机械证明 UPDATE_WRITER=0）"
            "未修时，门控类 source 速率=0，S0 永远不达标" if gate_blocked
            else "速率来自账本实测外推，无额外前提",
        ))
        rows.append(assumption_row(
            S1_GATE_FIXED, src, "补账候选/天数", gate_blocked,
            "仅在门控口径改为以 matches.status 为准后才成立；"
            "且补入区间若跨模型迭代，须按 walkforward 分段计数（见 S3）",
        ))
        rows.append(assumption_row(
            S2_OPTIMISTIC, src, "供给面原始行数", False,
            "未扣除假0-0/无赔率守卫剔除，属上限估计，实际速率必然更低",
        ))
        rows.append(assumption_row(
            S3_SEGMENT, src, "稳态速率（重训后重新起算）", True,
            f"前提：开展一次重训（当前 monitor drift_alert={drift_alert}, "
            f"retrain_gate.suggest={retrain_suggest}）。重训前账本样本属上一代模型，"
            f"walkforward 下不可并入重训后的 G1 计数",
        ))
    return rows


def render_markdown(summary: dict) -> str:
    L = []
    L.append("# 验证台 G1(2500) 达成路径推演（只读）")
    L.append("")
    L.append(f"- 生成时间: {summary['generated_at']}（as_of {summary['as_of']}）")
    L.append(f"- G1 验收线: {summary['g1_accept_n']} 条可信样本；推演地平线 {summary['horizon_days']} 天")
    L.append("")
    L.append("## 0. 诚实口径（先读）")
    L.append("")
    L.append("- **G1 只是样本及格线，不是 edge 证据**。样本量达标 ≠ 能过 G2–G6")
    L.append("  （ROI 置信区间 / 预测质量 / 校准 / 方向二项 / **零信息机械对照**）。")
    L.append("- 若补入区间跨模型迭代（当前 `ll_delta_vs_market=%.5f`、`retrain_gate.suggest=%s`），"
             % (summary.get("drift_ll_delta", 0.0), summary.get("retrain_suggest", False)))
    L.append("  G1 计数须按 walkforward 分段，重训前样本不可计入重训后的达标数（见 S3）。")
    L.append("- 本脚本**不重训、不跑 ingest、不写 verification.db**，只做只读推演。")
    L.append("")
    L.append("## 1. 现状：账本与三态")
    L.append("")
    L.append("| model_source | 账本累计 | G1 缺口 | 账本时间窗 | verification_report 判定 |")
    L.append("|---|---|---|---|---|")
    for src, info in summary["current"].items():
        L.append("| {} | {} | {} | {} -> {} | {} |".format(
            src, info["cum_n"], info["gap_to_g1"], info["first_date"] or "-",
            info["last_date"] or "-", (info.get("verdict") or "-")))
    L.append("")
    L.append("## 2. 达成日推演（四种情景）")
    L.append("")
    L.append("| source | 情景 | 速率(样本/天) | 需天数 | 达成日 | 判定 |")
    L.append("|---|---|---|---|---|---|")
    for src, rows in summary["scenarios"].items():
        for r in rows:
            L.append("| {} | {} | {} | {} | {} | {} |".format(
                src, r["scenario"], r["rate"], r.get("days_needed", "-"),
                r.get("reach_date") or "-", r["status"]))
    L.append("")
    L.append("> 说明：S0=现状外推（门控未修，门控类 source 速率 0 → NEVER）；"
             "S1=门控修复后按补账速率；S2=供给面上限（未扣守卫剔除）；"
             "ST=重训后重新起算的分段口径。")
    L.append("")
    L.append("## 3. 假设依赖表")
    L.append("")
    L.append("| source | 情景 | 速率依据 | 当前是否成立 | 依赖说明 |")
    L.append("|---|---|---|---|---|")
    for a in summary["assumptions"]:
        mark = "成立" if a["holds_now"] else "不成立"
        L.append("| {} | {} | {} | {} | {} |".format(
            a["source"], a["scenario"], a["rate_basis"], mark, a["note"]))
    L.append("")
    L.append("## 4. 结论")
    L.append("")
    for line in summary["conclusions"]:
        L.append("- " + line)
    L.append("")
    L.append("## 5. 未做的事（红线）")
    L.append("")
    L.append("- 未跑 verification ingest、未写 verification.db、未重训、未改 ingest.py、"
             "未碰 events.db 写入、未重启任何进程。")
    L.append("- 门控修复本身属**生产代码变更**，须走 walkforward 门禁 + 停机窗口（WINDOW 项），"
             "本轮只备料不执行。")
    L.append("")
    return "\n".join(L)


def build_conclusions(scenarios: dict[str, list[dict]], current: dict,
                      assumptions: list[dict]) -> list[str]:
    out = []
    for src in SOURCES:
        rows = scenarios.get(src, [])
        s0 = next((r for r in rows if r["scenario"] == S0_AS_IS), {})
        s1 = next((r for r in rows if r["scenario"] == S1_GATE_FIXED), {})
        info = current.get(src, {})
        gap = info.get("gap_to_g1", "-")
        if s0.get("status") == STATUS_NEVER:
            out.append(
                f"**{src}**：现状速率 0，缺口 {gap} —— 不修门控则**永远到不了 G1**"
                f"（根因见 T33：全仓不存在把 daily_predictions.status 翻成 finished 的写方）。")
        elif s0.get("status") == STATUS_PROJECTED:
            out.append(f"**{src}**：按现状速率可在 {s0.get('reach_date')} 达成 G1（缺口 {gap}）。")
        elif info.get("cum_n", 0) >= G1_ACCEPT_N:
            out.append(f"**{src}**：账本已 {info.get('cum_n')} 条 ≥ 2500"
                       f"（{info.get('first_date')} -> {info.get('last_date')}），"
                       f"判定由三态报告给出，不再以样本量表述。")
        if s1.get("status") == STATUS_PROJECTED and info.get("cum_n", 0) < G1_ACCEPT_N:
            out.append(
                f"  - S1（假设门控修复 + 补账速率 {s1.get('rate')}/天）：约 "
                f"{s1.get('reach_date')} 达成，但该速率含一次性 backlog 消化，非稳态；"
                f"且须先解决跨迭代窗口的 walkforward 分段问题。")
    out.append("**样本量不是 edge**：即便全部到达 2500，candles_ensemble 仍须过 G2–G6，"
               "其中 G6（零信息机械对照）是 09-23 事故后新增的关键关；"
               "当前三态 candles=INCONCLUSIVE / market=NO EDGE / KNN=NO EDGE 与 P0 判定一致。")
    return out


def run(events_db: str, ledger_db: str, window_days: int, as_of: str | None) -> dict:
    as_of = as_of or date.today().isoformat()

    con_l = open_readonly(ledger_db)
    try:
        counts = daily_ledger_counts(con_l)
    finally:
        con_l.close()

    series = build_series(counts)

    con_e = open_readonly(events_db)
    try:
        supply_all = {s: daily_supply_counts(con_e, s) for s in SOURCES}
    finally:
        con_e.close()

    # 门控阻塞判定：某 source 账本最后日期早于供给面最后日期 → 被 status 门控卡住
    gate_blocked_srcs = []
    for s in SOURCES:
        ser = series.get(s, [])
        last_led = ser[-1]["date"] if ser else None
        last_sup = max(supply_all[s].keys()) if supply_all[s] else None
        if last_led and last_sup and last_sup > last_led:
            gate_blocked_srcs.append(s)
    gate_blocked = bool(gate_blocked_srcs)

    # monitor / 三态（缺失则降级，不崩）
    verdicts: dict[str, str] = {}
    drift_ll_delta, retrain_suggest = 0.0, False
    try:
        with open(VERIFICATION_REPORT, encoding="utf-8") as f:
            rep = json.load(f)
        for s, m in (rep.get("models") or {}).items():
            verdicts[s] = m.get("verdict", "UNKNOWN")
    except Exception:
        pass
    try:
        with open(MONITOR_STATUS, encoding="utf-8") as f:
            mon = json.load(f)
        drift_ll_delta = float((mon.get("calibration", {}).get("drift") or {}).get("ll_delta_vs_market", 0.0))
        retrain_suggest = bool((mon.get("retrain_gate") or {}).get("suggest", False))
    except Exception:
        pass

    current = {}
    for s in SOURCES:
        ser = series.get(s, [])
        cum = ser[-1]["cum_n"] if ser else 0
        current[s] = {
            "cum_n": cum,
            "gap_to_g1": max(0, G1_ACCEPT_N - cum),
            "g1_reached": cum >= G1_ACCEPT_N,
            "first_date": ser[0]["date"] if ser else None,
            "last_date": ser[-1]["date"] if ser else None,
            "crossing_date": crossing_date(ser, G1_ACCEPT_N),
            "verdict": verdicts.get(s, "UNKNOWN"),
        }

    scenarios = build_scenarios(series, supply_all, window_days, as_of)
    assumptions = build_assumptions(scenarios, series, gate_blocked_srcs,
                                    drift_ll_delta > 0.03, retrain_suggest, as_of)
    conclusions = build_conclusions(scenarios, current, assumptions)

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "note": "只读推演：不跑 ingest / 不重训 / 不写 verification.db / 不碰 events.db 写入",
        "as_of": as_of,
        "g1_accept_n": G1_ACCEPT_N,
        "horizon_days": DEFAULT_HORIZON_DAYS,
        "window_days": window_days,
        "gate_blocked_sources": gate_blocked_srcs,
        "drift_ll_delta": drift_ll_delta,
        "retrain_suggest": retrain_suggest,
        "current": current,
        "scenarios": scenarios,
        "assumptions": assumptions,
        "conclusions": conclusions,
    }


def main():
    ap = argparse.ArgumentParser(description="验证台 G1 达成路径只读推演")
    ap.add_argument("--events-db", default=EVENTS_DB)
    ap.add_argument("--ledger-db", default=LEDGER_DB)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--window-days", type=int, default=14, help="速率估计窗口（天）")
    ap.add_argument("--as-of", default=None, help="推演基准日 YYYY-MM-DD，默认今天")
    ap.add_argument("--json-only", action="store_true")
    args = ap.parse_args()

    summary = run(args.events_db, args.ledger_db, args.window_days, args.as_of)

    if args.json_only:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return summary

    print("[as_of] {}  window={}d".format(summary["as_of"], summary["window_days"]))
    print("[ledger] " + " | ".join(
        "{} n={} span {}->{} {}".format(
            s, summary["current"][s]["cum_n"], summary["current"][s]["first_date"] or "-",
            summary["current"][s]["last_date"] or "-", summary["current"][s]["verdict"])
        for s in SOURCES))
    for s in SOURCES:
        for r in summary["scenarios"].get(s, []):
            print("[{:22s}] {:24s} rate={:<8} days={:<5} reach={:<12} {}".format(
                s, r["scenario"], str(r.get("rate", "-")), str(r.get("days_needed", "-")),
                str(r.get("reach_date") or "-"), r["status"]))
    os.makedirs(args.out, exist_ok=True)
    jp = os.path.join(args.out, "model_g1_reach_plan.json")
    mp = os.path.join(args.out, "model_g1_reach_plan.md")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    with open(mp, "w", encoding="utf-8") as f:
        f.write(render_markdown(summary))
    print(f"[out] {jp}")
    print(f"[out] {mp}")
    return summary


if __name__ == "__main__":
    main()
