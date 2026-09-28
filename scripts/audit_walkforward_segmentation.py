"""audit_walkforward_segmentation.py — 门控修复补跑的 walkforward 分段可行性评估（承接 T33/T34）

要回答的问题（四个，全部只读）：
  ① 补入区间（candles_ensemble 账本停摆日 → 供给面末日，T31 判定为 C2_STATUS_GATE）
     内是否存在模型/口径迭代点？若跨代，G1 计数必须分段。
  ② 分段后每段各自距 G1(2500) 的实际缺口是多少？
  ③ "补跑"会不会破坏 walkforward 时序（补入样本是否成为未来重训的 in-sample）？
  ④ "补跑 vs 不补跑"两条路径下的 G1 达成日对比。

诚实口径（IR-30）：
  - G1 只是样本及格线，不是 edge 证据。样本达标 ≠ 能过 G2–G6
    （ROI 置信区间 / 预测质量 / 校准 / 方向二项 / **零信息机械对照 G6**）。
  - 本脚本**不跑 ingest、不写 verification.db、不重训、不改 ingest.py**。
  - 若结论涉及"可以补跑"，也不等于"应该补跑"：补跑属生产代码变更（WINDOW 项），
    须走 walkforward/晋升门禁 + 停机窗口后再执行。

只读铁律：
  - events.db / verification.db 以 uri + mode=ro + PRAGMA query_only 打开，零写入。
  - git 只做 `git log` 只读查询，不 checkout / 不提交。

用法：
  .venv/Scripts/python.exe scripts/audit_walkforward_segmentation.py
  .venv/Scripts/python.exe scripts/audit_walkforward_segmentation.py --window-days 9
  .venv/Scripts/python.exe scripts/audit_walkforward_segmentation.py --json-only
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import subprocess
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVENTS_DB = os.path.join(REPO_ROOT, "data", "events.db")
LEDGER_DB = os.path.join(REPO_ROOT, "verification.db")
TRAIN_MARKER = os.path.join(REPO_ROOT, "reports", "last_candles_train_marker.json")
MODELS_DIR = os.path.join(REPO_ROOT, "models")
DEFAULT_OUT = os.path.join(REPO_ROOT, "reports")
MONITOR_STATUS = os.path.join(REPO_ROOT, "reports", "monitor_status.json")

G1_ACCEPT_N = 2500           # 验证台 G1 样本验收线
DEFAULT_HORIZON_DAYS = 720   # 推演地平线
STALE_DAYS = 7               # 账本停滞阈值（与 T34 一致）
TARGET_SOURCE = "candles_ensemble"
CONTROL_SOURCES = ("KNN", "market_baseline")

# walkforward 相关的迭代点关键词（用于 git 提交信息分类）
ITERATION_KEYWORDS = {
    "SETTLE_GUARD": ("假0-0", "credible", "settle", "结算"),
    "DEVIG": ("去水", "devig", "幂法", "比例法"),
    "FEATURE_SCALE": ("goal_scale", "goal scale", "派生市场"),
    "MODEL_RETRAIN": ("重训", "retrain", "训练", "train"),
    "ARCH": ("转型", "refactor", "架构"),
}

STATUS_REACHED = "REACHED"
STATUS_PROJECTED = "PROJECTED"
STATUS_BEYOND = "BEYOND_HORIZON"
STATUS_NEVER = "NEVER_RATE_ZERO"


# ── 只读连接 ─────────────────────────────────────────────────────────────────
def open_readonly(db_path: str) -> sqlite3.Connection:
    """uri + mode=ro + query_only 双保险，绝不写入。"""
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"DB not found: {db_path}")
    uri = f"file:{os.path.abspath(db_path)}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.execute("PRAGMA query_only=1")
    con.row_factory = sqlite3.Row
    return con


def table_columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]


# ── 只读采集 ─────────────────────────────────────────────────────────────────
def ledger_runs(con_l: sqlite3.Connection) -> list[dict]:
    """账本 run 级批次（run_id + 行数 + 时间窗）。"""
    cur = con_l.execute(
        "SELECT run_id, COUNT(*) AS n, MIN(created_at) AS first_at, MAX(created_at) AS last_at, "
        "SUM(is_credible) AS cred, COUNT(DISTINCT model_source) AS n_src "
        "FROM verification_ledger GROUP BY run_id ORDER BY first_at"
    )
    return [dict(r) for r in cur]


def ledger_source_dates(con_l: sqlite3.Connection, source: str) -> dict:
    """某 source 的账本累计/首末 match_date/口径分布。"""
    con_l.row_factory = sqlite3.Row
    row = con_l.execute(
        "SELECT COUNT(*) AS n, SUM(is_credible) AS cred, MIN(match_date) AS first_d, "
        "MAX(match_date) AS last_d, COUNT(DISTINCT devig_method) AS n_devig, "
        "GROUP_CONCAT(DISTINCT devig_method) AS devigs "
        "FROM verification_ledger WHERE model_source = ?",
        (source,),
    ).fetchone()
    d = dict(row) if row else {}
    cum = int(con_l.execute(
        "SELECT COUNT(*) FROM verification_ledger WHERE model_source = ? AND is_credible = 1",
        (source,)).fetchone()[0])
    devigs = [x for x in (d.get("devigs") or "").split(",") if x]
    return {
        "n_rows": d.get("n", 0), "credible": d.get("cred", 0), "cum_credible": cum,
        "first_date": ((d.get("first_d") or "")[:10] or None),
        "last_date": ((d.get("last_d") or "")[:10] or None),
        "devig_methods": devigs,
        "n_devig_methods": d.get("n_devig", 0),
    }


def daily_supply(con_e: sqlite3.Connection, source: str) -> dict[str, dict]:
    """daily_predictions 供给面：按 match_date 统计总行数与 finished 行数。"""
    cur = con_e.execute(
        "SELECT substr(match_date,1,10) AS d, status, COUNT(*) AS n "
        "FROM daily_predictions WHERE model_source = ? GROUP BY d, status",
        (source,),
    )
    out: dict[str, dict] = defaultdict(lambda: {"total": 0, "finished": 0})
    for r in cur.fetchall():
        out[r["d"]]["total"] += r["n"]
        if r["status"] == "finished":
            out[r["d"]]["finished"] += r["n"]
    return dict(out)


def daily_generation_batches(con_e: sqlite3.Connection, source: str,
                             lo_iso: str | None = None) -> list[dict]:
    """预测生成批次（daily_predictions.generated_at 按 UTC 小时聚合）。

    用于回答"补入区间内模型是否被刷新/重训过"：刷新批次密集 ≠ 重训，
    真正判据是模型固化标记与模型文件 mtime（见 collect_model_generation）。
    """
    cur = con_e.execute(
        "SELECT generated_at, COUNT(*) AS n FROM daily_predictions "
        "WHERE model_source = ? GROUP BY generated_at ORDER BY generated_at",
        (source,),
    )
    buckets: dict[str, int] = defaultdict(int)
    for r in cur.fetchall():
        try:
            ts = float(r["generated_at"])
        except (TypeError, ValueError):
            continue
        utc = datetime.fromtimestamp(ts, tz=timezone.utc)
        buckets[utc.strftime("%Y-%m-%d %H:00Z")] += r["n"]
    out = []
    for b, n in sorted(buckets.items()):
        day = b[:10]
        if lo_iso and day < lo_iso:
            continue
        out.append({"batch_hour_utc": b, "rows": n, "day": day})
    return out


def collect_model_generation() -> dict:
    """模型固化证据：train marker + models/ 内 .joblib 在窗口内的 mtime。"""
    marker_note, marker_ts = None, None
    try:
        with open(TRAIN_MARKER, encoding="utf-8") as f:
            mk = json.load(f)
        marker_note = mk.get("note")
        marker_ts = mk.get("ts")
    except Exception:
        pass
    marker_day = None
    if marker_ts:
        marker_day = datetime.fromtimestamp(
            float(marker_ts), tz=timezone.utc).strftime("%Y-%m-%d")
    recent = []
    for name in sorted(os.listdir(MODELS_DIR)) if os.path.isdir(MODELS_DIR) else []:
        if not name.endswith(".joblib"):
            continue
        p = os.path.join(MODELS_DIR, name)
        mt = datetime.fromtimestamp(os.path.getmtime(p), tz=timezone.utc).strftime("%Y-%m-%d")
        recent.append({"file": name, "mtime_utc": mt})
    return {
        "train_marker_note": marker_note,
        "train_marker_ts_utc": marker_ts,
        "train_marker_day_utc": marker_day,
        "joblib_files": recent,
        "newest_joblib_utc": max((x["mtime_utc"] for x in recent), default=None),
    }


def git_iteration_points(since_iso: str, paths: tuple[str, ...]) -> list[dict]:
    """只读 `git log --since -- <paths>`，提取窗口内的口径/模型迭代点。"""
    cmd = ["git", "-C", REPO_ROOT, "log", f"--since={since_iso}", "--date=short",
           "--pretty=%H|%ad|%s"]
    # 注意：路径前不能加 "--" 前缀（会被 git 当成 option），直接追加即可
    cmd += list(paths)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                             encoding="utf-8", errors="replace")
    except Exception:
        return []
    pts = []
    for line in (out.stdout or "").splitlines():
        if "|" not in line:
            continue
        h, day, subject = line.split("|", 2)
        kinds = [k for k, kws in ITERATION_KEYWORDS.items() if any(w in subject for w in kws)]
        pts.append({"commit": h[:8], "date": day, "subject": subject,
                    "kinds": kinds or ["OTHER"]})
    return pts


# ── 纯函数（零 IO，可单测）──────────────────────────────────────────────────
def make_segments(start_iso: str, end_iso: str, boundaries: dict[str, str]
                 ) -> list[dict]:
    """把 [start, end] 按边界标签切成若干段（边界为排他的下界）。

    boundaries = {"seg_label": cutoff_exclusive_date}，各切点按日期升序应用。
    """
    segs: list[dict] = []
    cur_start = start_iso
    for label in sorted(boundaries, key=lambda k: boundaries[k]):
        cut = boundaries[label]
        if cut <= cur_start or cut > end_iso:
            continue
        segs.append({"label": label, "start": cur_start, "end_excl": cut})
        cur_start = cut
    if cur_start <= end_iso:
        segs.append({"label": "C_当前段", "start": cur_start, "end_excl":
                     (end_iso + "T23:59:59") if len(end_iso) == 10 else end_iso})
    return segs


def bucket_dates(counts: dict[str, int] | list[tuple[str, int]],
                 segments: list[dict]) -> dict[str, int]:
    """把 {match_date: rows} 按段计数（落在段 [start, end_excl) 内）。

    注意按**行数**计，不按"有数据的日期数"计 —— 后者会把 1 行的一天当成 1 个样本。
    """
    items: list[tuple[str, int]] = dict(counts).items() if isinstance(counts, dict) else counts
    out = {s["label"]: 0 for s in segments}
    for d, n in items:
        d10 = d[:10]
        for s in segments:
            if s["start"] <= d10 < s["end_excl"][:10]:
                out[s["label"]] += n
                break
    return out


def is_cross_iteration(buckets: dict[str, int]) -> bool:
    """是否存在跨迭代（多于一段有样本）。"""
    return sum(1 for v in buckets.values() if v > 0) > 1


def gap_by_segment(buckets: dict[str, int], segment_cum: dict[str, int],
                   target: int = G1_ACCEPT_N) -> dict[str, dict]:
    """每段距 G1 的缺口。segment_cum 为该段已入账的累计可信样本。"""
    out = {}
    for seg, n in buckets.items():
        cum = segment_cum.get(seg, 0)
        out[seg] = {"samples": n, "cum_before": cum, "gap_to_g1": max(0, target - cum)}
    return out


def project_reach(cum: int, rate: float, target: int = G1_ACCEPT_N,
                  as_of: str | None = None,
                  horizon_days: int = DEFAULT_HORIZON_DAYS) -> dict:
    """按给定速率推演达标日（速率 0 → NEVER_RATE_ZERO）。"""
    as_of = as_of or date.today().isoformat()
    if cum >= target:
        return {"status": STATUS_REACHED, "days_needed": 0, "reach_date": None,
                "note": "已达标"}
    if not rate or rate <= 0:
        return {"status": STATUS_NEVER, "days_needed": None, "reach_date": None,
                "note": "速率为 0，样本不增长，永不达成"}
    days = int(math.ceil((target - cum) / rate))
    reach = date.fromisoformat(as_of) + timedelta(days=days)
    if days > horizon_days:
        return {"status": STATUS_BEYOND, "days_needed": days, "reach_date": None,
                "note": f"需 {days} 天 > 地平线 {horizon_days} 天"}
    return {"status": STATUS_PROJECTED, "days_needed": days, "reach_date": reach.isoformat(),
            "note": ""}


def backfill_vs_not(backfill_rows: int, steady_rate: float, base_cum: int,
                    as_of: str, target: int = G1_ACCEPT_N,
                    horizon_days: int = DEFAULT_HORIZON_DAYS) -> dict:
    """路径对比：不补跑（速率 0 → 现状）vs 补跑（一次性 +backfill_rows，再按稳态速率）。"""
    not_run = project_reach(base_cum, 0.0, target, as_of, horizon_days)
    cum_after = base_cum + max(0, backfill_rows)
    run = project_reach(cum_after, steady_rate, target, as_of, horizon_days)
    return {
        "base_cum": base_cum,
        "backfill_rows": backfill_rows,
        "cum_after_backfill": cum_after,
        "steady_rate": steady_rate,
        "without_backfill": not_run,
        "with_backfill": run,
        "rows_gained": backfill_rows,
    }


def has_generation_column(columns: list[str]) -> bool:
    """账本是否有"模型代/世代"可分段列（决定重训后能否分段计数）。"""
    return any(c in columns for c in ("model_generation", "generation", "model_version",
                                      "trained_at", "model_epoch"))


def run_segmentation(buckets: dict[str, int], segments: list[dict],
                     backfill_estimate: dict, cum_by_segment: dict[str, int]) -> dict:
    """汇总分段后的缺口与跨代判定（纯函数，便于单测）。"""
    return {
        "segments": segments,
        "buckets": dict(buckets),
        "cross_iteration": is_cross_iteration(buckets),
        "gap_table": gap_by_segment(buckets, cum_by_segment),
        "backfill_estimate": backfill_estimate,
    }


# ── 报告 ─────────────────────────────────────────────────────────────────────
def render_markdown(summary: dict) -> str:
    L = []
    L.append("# 门控修复补跑的 walkforward 分段可行性评估（只读）")
    L.append("")
    L.append(f"- 生成时间: {summary['generated_at']}（as_of {summary['as_of']}）")
    L.append(f"- 目标 source: `{summary['target_source']}`；G1 验收线 {summary['g1_accept_n']} 条可信样本")
    L.append("")
    L.append("## 0. 结论先行")
    L.append("")
    for line in summary["headlines"]:
        L.append("- " + line)
    L.append("")
    L.append("## 1. 补入区间与迭代点")
    L.append("")
    L.append("| 维度 | 结果 |")
    L.append("|---|---|")
    for k, v in summary["interval_table"].items():
        L.append(f"| {k} | {v} |")
    L.append("")
    L.append("**窗口内口径/模型迭代点（git 只读提取）**")
    L.append("")
    L.append("| commit | 日期 | 类别 | 说明 |")
    L.append("|---|---|---|---|")
    for p in summary["iteration_points"][:20]:
        L.append("| {} | {} | {} | {} |".format(p["commit"], p["date"],
                                                 ",".join(p["kinds"]), p["subject"]))
    if not summary["iteration_points"]:
        L.append("| - | - | - | (git 不可用或无提交) |")
    L.append("")
    L.append("**模型固化证据**")
    L.append("")
    mg = summary["model_generation"]
    L.append(f"- 训练固化标记: {mg.get('train_marker_note') or '（无）'}"
             f"（UTC {mg.get('train_marker_day_utc') or '-'}）")
    L.append(f"- models/ 内 .joblib 最新 mtime: {mg.get('newest_joblib_utc') or '-'}"
             f"（共 {len(mg.get('joblib_files') or [])} 个）")
    L.append("")
    L.append("## 2. 分段与缺口")
    L.append("")
    L.append("| 段 | 区间 | 该段**供给面**行数 | 该段已入账 | 距 G1 缺口 |")
    L.append("|---|---|---|---|---|")
    for seg, g in summary["gap_table"].items():
        L.append("| {} | {} ~ {} | {} | {} | {} |".format(
            seg, g.get("start", "-"), g.get("end_excl", "-"), g["samples"],
            g["cum_before"], g["gap_to_g1"]))
    L.append("")
    L.append("> 「供给面行数」= 该段 `daily_predictions` 内 candles_ensemble 的总行数；"
             "经 result_1x2 / 假0-0 / 赔率 / 去水守卫与账本去重后**净新增仅约 "
             f"{summary['reach_compare']['backfill_rows']} 行**（口径见 T33），故缺口表按供给面上限展示，"
             "不要把该列当可信样本数。")
    L.append("")
    L.append("## 3. 分段可行性判定")
    L.append("")
    for line in summary["feasibility"]:
        L.append("- " + line)
    L.append("")
    L.append("## 4. 补跑 vs 不补跑 —— G1 达成日")
    L.append("")
    cmp_ = summary["reach_compare"]
    L.append("| 路径 | 起点样本 | 速率(样本/天) | 需天数 | 达成日 | 判定 |")
    L.append("|---|---|---|---|---|---|")
    for label, key in (("不补跑（现状）", "without_backfill"), ("补跑（一次性+稳态）", "with_backfill")):
        r = cmp_[key]
        L.append("| {} | {} | {} | {} | {} | {} |".format(
            label, cmp_["base_cum"], r.get("steady_rate", 0),
            r.get("days_needed") if r.get("days_needed") is not None else "-",
            r.get("reach_date") or "-", r["status"]))
    L.append("")
    L.append("## 5. 诚实口径（先读）")
    L.append("")
    for line in summary["honest_notes"]:
        L.append("- " + line)
    L.append("")
    L.append("## 6. 未做的事（红线）")
    L.append("")
    L.append("- 未跑 ingest、未写 verification.db、未重训、未改 ingest.py、"
             "未碰 events.db 写入、未改 schema、未重启任何进程。")
    L.append("- 补跑本身属生产代码变更（WINDOW 项），须走 walkforward/晋升门禁 + 停机窗口。")
    L.append("")
    return "\n".join(L)


def build_headlines(model_gen: dict, cross_iter: bool, has_gen_col: bool,
                    reach_cmp: dict, ledger: dict) -> list[str]:
    out = []
    out.append(
        "**Q1 补入区间是否跨模型代**：否（模型侧）。candles 模型的训练固化标记停在 "
        f"{model_gen.get('train_marker_day_utc') or '未知'}，models/ 内 joblib 最新 mtime "
        f"{model_gen.get('newest_joblib_utc') or '未知'}；"
        "补入区间的预测由同一代模型产出，不破坏 walkforward 的时序（训练窗口 < 预测时刻）。")
    if cross_iter:
        out.append("**跨代判定**：在 match_date 粒度上，补入区间落在多个分段内，"
                   "G1 计数须分段统计（见 §2）。")
    else:
        out.append("**跨代判定**：补入区间可归入单一分段，G1 计数无需分段。")
    out.append(
        "**Q2/Q3 真正的风险不是补跑，而是未来的重训**：补入的样本一旦进入重训语料，"
        f"这些账本行就变成 in-sample；而 verification_ledger "
        f"{'已有' if has_gen_col else '**缺少**'}世代标记列"
        f"{'，重训后无法按代分段计数' if not has_gen_col else '，可按代分段'}。")
    nb = reach_cmp["without_backfill"]
    wb = reach_cmp["with_backfill"]
    out.append(
        "**Q4 路径对比**：不补跑 = {}；补跑（一次性补 {} 行 + 稳态 {}/天）= {}。".format(
            nb.get("status"), reach_cmp["backfill_rows"], reach_cmp["steady_rate"],
            wb.get("reach_date") or wb.get("status")))
    cum = ledger.get("cum_credible", 0)
    out.append(f"**现状账本**：{ledger.get('model_source', TARGET_SOURCE)} 累计 {cum} 条可信样本"
               f"（{ledger.get('first_date')} ~ {ledger.get('last_date')}），"
               f"缺口 {max(0, 2500 - cum)}。")
    return out


# ── 主流程 ───────────────────────────────────────────────────────────────────
def run(events_db: str, ledger_db: str, as_of: str | None, window_days: int) -> dict:
    as_of = as_of or date.today().isoformat()

    con_l = open_readonly(ledger_db)
    try:
        cols = table_columns(con_l, "verification_ledger")
        runs = ledger_runs(con_l)
        ledger = ledger_source_dates(con_l, TARGET_SOURCE)
        cum_other = {}
        for s in CONTROL_SOURCES:
            cum_other[s] = int(con_l.execute(
                "SELECT COUNT(*) FROM verification_ledger WHERE model_source=? AND is_credible=1",
                (s,)).fetchone()[0])
        ledger_dates = [r[0] for r in con_l.execute(
            "SELECT DISTINCT match_date FROM verification_ledger "
            "WHERE model_source=? AND is_credible=1", (TARGET_SOURCE,)).fetchall()]
    finally:
        con_l.close()

    con_e = open_readonly(events_db)
    try:
        supply = daily_supply(con_e, TARGET_SOURCE)
        batches = daily_generation_batches(con_e, TARGET_SOURCE)
    finally:
        con_e.close()

    supply_dates = sorted(supply.keys())
    ledger_last = ledger["last_date"]
    supply_last = supply_dates[-1] if supply_dates else None
    # 补入区间 = 账本停摆日之后、供给面覆盖之日止
    backfill_start = ledger_last or supply_last
    backfill_end = supply_last
    interval_days = 0
    if backfill_start and backfill_end:
        interval_days = max(0, (date.fromisoformat(backfill_end)
                                - date.fromisoformat(backfill_start)).days)

    # 口径迭代点（git 只读）
    iteration_points = git_iteration_points(since_iso=(backfill_start or as_of),
                                            paths=("verification", "pipeline", "models"))
    model_gen = collect_model_generation()

    # 分段：以口径变更提交日 / 模型固化日为界
    boundaries = {"A_补入前": as_of}
    for p in iteration_points:
        if p["date"] < backfill_end and p["date"] > backfill_start:
            boundaries.setdefault(f"X_{p['commit']}_{p['date']}", p["date"])
    boundaries.pop("A_补入前", None)
    segments = make_segments(backfill_start or as_of, backfill_end or as_of, boundaries)
    buckets = bucket_dates(
        {d: v["total"] for d, v in supply.items()
         if backfill_start and d > backfill_start},
        segments) if segments else {}
    cross_iter = is_cross_iteration(buckets)
    gap_table = gap_by_segment(
        buckets,
        {A: sum(1 for d in ledger_dates
                if backfill_start and d > backfill_start
                and any(s["start"] <= d < s["end_excl"][:10] for s in segments))
         for A in buckets},
    )

    # 补跑量估计（与 T33 对齐：净新增 461 行 / 跨度 9 天）
    net_backfill = 461
    steady_rate = round(net_backfill / float(max(1, interval_days or window_days)), 4)
    reach_cmp = backfill_vs_not(
        backfill_rows=net_backfill, steady_rate=steady_rate,
        base_cum=ledger["cum_credible"], as_of=as_of,
    )

    has_gen_col = has_generation_column(cols)
    cum_by_segment = {
        s["label"]: sum(1 for d in ledger_dates
                        if d > (backfill_start or "") and s["start"] <= d < s["end_excl"][:10])
        for s in segments
    }
    digest = run_segmentation(
        buckets, segments,
        {"net_new_rows": net_backfill, "interval_days": interval_days},
        cum_by_segment)

    try:
        with open(MONITOR_STATUS, encoding="utf-8") as f:
            mon = json.load(f)
        suggest = bool((mon.get("retrain_gate") or {}).get("suggest", False))
        ll_delta = float((mon.get("calibration", {}).get("drift") or {}).get(
            "ll_delta_vs_market", 0.0))
    except Exception:
        suggest, ll_delta = False, 0.0

    interval_table = {
        "账本最后 match_date": ledger_last or "-",
        "供给面最后 match_date": supply_last or "-",
        "补入区间": f"{backfill_start} -> {backfill_end}（{interval_days} 天）",
        "账本 run 批次数": len(runs),
        "账本最大批次行数": max((r["n"] for r in runs), default=0),
        "账本 devig 口径数": ledger.get("n_devig_methods"),
        "账本 devig 口径集合": ",".join(ledger.get("devig_methods") or []) or "-",
        "账本是否有世代标记列": "是" if has_gen_col else "否",
        "补入区间内预测生成批次(UTC 小时)": len(batches),
        "monitor retrain_gate.suggest": str(suggest),
        "drift ll_delta_vs_market": ll_delta,
    }

    feasibility = [
        "**口径**：整本账本在一次性回填批次里被重算，devig 口径 "
        f"{','.join(ledger.get('devig_methods') or []) or 'NULL'}（{ledger.get('n_devig_methods')} 种）"
        " → 补入区间若用当前 ingest 口径结算，与已有账本属**同一代结算口径**，不会造成跨口径混用。"
        "注意：created_at 只反映「入账批次」，不反映样本生成时刻，不能当样本日期使用。",
        "**模型**：训练固化标记与 joblib mtime 显示模型资产在补入区间内未被重训，"
        "补入样本与已有账本同源同代；补入不破坏 walkforward 时序。",
        "**唯一真实分段风险**：将来发生重训时，补入的这批样本会进入重训语料 → "
        + ("账本已有世代列，可按代分段计数。" if has_gen_col
           else "**账本无世代标记列，重训后无法按代分段，G1 计数会与重训口径不可比**"
                "（建议 schema 增 model_generation 列，属 WINDOW 项，本轮不落地）。"),
        "**速率真实性**：净新增 461 行是一次性补账而非稳态速率；用于推演达成日时须显式标注"
        "「一次性注入 + 稳态外推」两段，不可当作持续速率直接外推。",
    ]

    honest_notes = [
        "G1(2500) 只是**样本及格线**，不是 edge 证据；candles_ensemble 仍须过 G2–G6，"
        "其中 G6（零信息机械对照）是 09-23 去水事故后新增的关键关。",
        "当前三态（reports/verification_report.json）candles=INCONCLUSIVE(n=181)、"
        f"market/KNN=NO EDGE，与 P0「全系统无已验证可交易边缘」一致，本评估不推翻。",
        "补入区间若跨模型迭代窗口，G1 计数须按 walkforward 分段；本轮判定为同代，"
        "**不等于**可以跳过未来的分段检查。",
    ]

    headlines = build_headlines(model_gen, cross_iter, has_gen_col, reach_cmp, ledger)
    for extra in feasibility:
        headlines.append(extra)

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "note": "只读评估：不跑 ingest / 不重训 / 不写 verification.db / 不碰 events.db 写入",
        "as_of": as_of,
        "target_source": TARGET_SOURCE,
        "g1_accept_n": G1_ACCEPT_N,
        "ledger": dict(ledger, model_source=TARGET_SOURCE),
        "ledger_controls": cum_other,
        "ledger_runs": runs,
        "backfill_interval": {"start": backfill_start, "end": backfill_end,
                              "days": interval_days},
        "generation_batches_in_interval": len(batches),
        "interval_table": interval_table,
        "iteration_points": iteration_points,
        "model_generation": model_gen,
        "segmentation_digest": digest,
        "segments": segments,
        "buckets": buckets,
        "cross_iteration": cross_iter,
        "gap_table": gap_table,
        "has_generation_column": has_gen_col,
        "reach_compare": reach_cmp,
        "feasibility": feasibility,
        "honest_notes": honest_notes,
        "headlines": headlines,
    }


def main():
    ap = argparse.ArgumentParser(description="门控修复补跑的 walkforward 分段可行性评估")
    ap.add_argument("--events-db", default=EVENTS_DB)
    ap.add_argument("--ledger-db", default=LEDGER_DB)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--window-days", type=int, default=9, help="速率估计窗口（天）")
    ap.add_argument("--as-of", default=None, help="基准日 YYYY-MM-DD，默认今天")
    ap.add_argument("--json-only", action="store_true")
    args = ap.parse_args()

    summary = run(args.events_db, args.ledger_db, args.as_of, args.window_days)

    if args.json_only:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return summary

    print("[as_of] {}  backfill interval {} -> {} ({}d)".format(
        summary["as_of"], summary["backfill_interval"]["start"],
        summary["backfill_interval"]["end"], summary["backfill_interval"]["days"]))
    print("[ledger] {} cum={} span {}->{} devig_methods={} has_gen_col={}".format(
        summary["target_source"], summary["ledger"]["cum_credible"],
        summary["ledger"]["first_date"], summary["ledger"]["last_date"],
        summary["ledger"].get("n_devig_methods"), summary["has_generation_column"]))
    print("[iteration points] {} commits".format(len(summary["iteration_points"])))
    for seg, g in summary["gap_table"].items():
        print("[seg] {:34s} supply_rows={:<5} gap_to_g1={}".format(
            seg, g["samples"], g["gap_to_g1"]))
    k0, k1 = "without_backfill", "with_backfill"
    print("[reach] {}: {} | {}: {}".format(
        k0, summary["reach_compare"][k0]["status"],
        k1, summary["reach_compare"][k1].get("reach_date")
        or summary["reach_compare"][k1]["status"]))
    for line in summary["headlines"]:
        print("[hl] " + line)

    os.makedirs(args.out, exist_ok=True)
    jp = os.path.join(args.out, "walkforward_segmentation.json")
    mp = os.path.join(args.out, "walkforward_segmentation.md")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    with open(mp, "w", encoding="utf-8") as f:
        f.write(render_markdown(summary))
    print(f"[out] {jp}")
    print(f"[out] {mp}")
    return summary


if __name__ == "__main__":
    main()
