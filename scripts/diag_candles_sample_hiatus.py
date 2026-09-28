"""diag_candles_sample_hiatus.py — candles_ensemble 验证样本停滞诊断（只读）

背景：验证台 G1 样本验收线 2500，candles_ensemble 长期停在 n≈181，
而同账本的 KNN(2779) / market_baseline(6082) 均在增长。本脚本只读判定
"样本不涨"的根因属于哪一类：

  C1_NOT_WIRED      模型未接入验证台（账本里根本没有该 source 的行）
  C2_STATUS_GATE    写盘被 status='finished' 门控卡死（daily_predictions.status 无人翻）
  C3_EXCLUSION      确有 finished 行，但被可信/赔率守卫（假0-0 / 无 odds）剔除
  C4_SLOW_REAL      真实累积慢（无门控问题，只是增量不足）

只读铁律：
- events.db 以 uri + mode=ro + query_only 打开，零写入（不 VACUUM / 不 ALTER）。
- verification.db 同样只读打开，本脚本绝不调用 verification.ingest_new / 不写账本。
- 不跑 schema 变更、不杀进程、不碰生产服务。

用法：
  .venv/Scripts/python.exe scripts/diag_candles_sample_hiatus.py
  .venv/Scripts/python.exe scripts/diag_candles_sample_hiatus.py --json-only
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVENTS_DB = os.path.join(REPO_ROOT, "data", "events.db")
LEDGER_DB = os.path.join(REPO_ROOT, "verification.db")
DEFAULT_OUT = os.path.join(REPO_ROOT, "reports")

G1_ACCEPT_N = 2500
TARGET_SOURCE = "candles_ensemble"
CONTROL_SOURCES = ("market_baseline", "KNN")

C1_NOT_WIRED = "C1_NOT_WIRED"
C2_STATUS_GATE = "C2_STATUS_GATE"
C3_EXCLUSION = "C3_EXCLUSION"
C4_SLOW_REAL = "C4_SLOW_REAL"


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


# ── 取数（均为只读 SELECT）────────────────────────────────────────────────
def fetch_ledger(con: sqlite3.Connection) -> list[dict]:
    """账本按 model_source 汇总（不取 payload）。"""
    cur = con.execute(
        """
        SELECT model_source,
               COUNT(*) AS n,
               MIN(match_date) AS first_date,
               MAX(match_date) AS last_date,
               MAX(substr(created_at,1,16)) AS last_created
        FROM verification_ledger
        GROUP BY model_source
        """
    )
    return [dict(r) for r in cur.fetchall()]


def fetch_ledger_keys(con: sqlite3.Connection, source: str) -> set[str]:
    """账本内该 source 已有 match_id 集合（用于算"已入账 vs 候选"差集）。"""
    cur = con.execute(
        "SELECT match_id FROM verification_ledger WHERE model_source=?", (source,)
    )
    return {r[0] for r in cur.fetchall()}


def fetch_daily(con: sqlite3.Connection) -> list[dict]:
    """daily_predictions 轻量列（payload 按需单取，避免全表解析）。"""
    cur = con.execute(
        """
        SELECT match_key, model_source, status, match_date, kickoff
        FROM daily_predictions
        """
    )
    return [dict(r) for r in cur.fetchall()]


def fetch_match_status(con: sqlite3.Connection, keys: list[str]) -> dict[str, dict]:
    """批量取 matches 的 status/比分（只读）。"""
    out: dict[str, dict] = {}
    if not keys:
        return out
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        marks = ",".join("?" * len(chunk))
        cur = con.execute(
            f"SELECT match_key, status, score_home, score_away "
            f"FROM matches WHERE match_key IN ({marks})",
            chunk,
        )
        for r in cur.fetchall():
            out[r["match_key"]] = {
                "status": r["status"],
                "score_home": r["score_home"],
                "score_away": r["score_away"],
            }
    return out


def fetch_payloads(con: sqlite3.Connection, keys: list[str]) -> dict[str, dict]:
    """仅对指定 match_key 取 payload（只读，避免全表解析 11k 行）。"""
    out: dict[str, dict] = {}
    if not keys:
        return out
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        marks = ",".join("?" * len(chunk))
        cur = con.execute(
            f"SELECT match_key, payload FROM daily_predictions "
            f"WHERE match_key IN ({marks})",
            chunk,
        )
        for r in cur.fetchall():
            try:
                out[r["match_key"]] = json.loads(r["payload"] or "{}")
            except Exception:
                out[r["match_key"]] = {}
    return out


# ── 纯分析函数（可单测，零 DB）───────────────────────────────────────────
def group_by(rows: list[dict], key: str) -> dict:
    d: dict = defaultdict(int)
    for r in rows:
        d[r.get(key)] += 1
    return dict(d)


def ledger_span(ledger_rows: list[dict], source: str) -> dict | None:
    """取某 source 的账本规模与时间跨度。"""
    hit = [r for r in ledger_rows if r["model_source"] == source]
    if not hit:
        return None
    r = hit[0]
    return {
        "n": r["n"],
        "first_date": r["first_date"],
        "last_date": r["last_date"],
        "last_created": r["last_created"],
        "g1_target": G1_ACCEPT_N,
        "gap_to_g1": max(0, G1_ACCEPT_N - r["n"]),
    }


def daily_profile(daily_rows: list[dict], source: str) -> dict:
    """daily_predictions 中该 source 的 status / match_date 分布。"""
    rows = [r for r in daily_rows if r["model_source"] == source]
    by_status = group_by(rows, "status")
    dates = sorted({(r["match_date"] or "")[:10] for r in rows if r["match_date"]})
    return {
        "total": len(rows),
        "by_status": by_status,
        "finished": by_status.get("finished", 0),
        "not_finished": len(rows) - by_status.get("finished", 0),
        "first_date": dates[0] if dates else None,
        "last_date": dates[-1] if dates else None,
    }


def classify_stall(
    target: str,
    ledger_rows: list[dict],
    daily_rows: list[dict],
    match_rows: dict[str, dict],
    payloads: dict[str, dict] | None = None,
    ledger_keys: set[str] | None = None,
) -> dict:
    """判定停滞根因（纯函数，便于单测）。

    逻辑：
      1) 账本中该 source 一个都没有 → C1_NOT_WIRED
      2) 该 source 有 daily 行但 finished=0，且大量行的 match(status)=finished
         （即"比赛已完赛但 daily_predictions.status 没被翻成 finished"）→ C2_STATUS_GATE
      3) 有 finished 行、账本却没有对应 match_id 且被守卫剔除（0-0 假分/无 odds）→ C3_EXCLUSION
      4) 其余 → C4_SLOW_REAL
    """
    payloads = payloads or {}
    led = ledger_span(ledger_rows, target)
    prof = daily_profile(daily_rows, target)

    if led is None:
        return {
            "verdict": C1_NOT_WIRED,
            "ledger": None,
            "daily": prof,
            "unflipped_candidates": 0,
            "ledger_covered_candidates": 0,
            "excluded_0_0": 0,
            "excluded_no_odds": 0,
            "detail": "账本内不存在该 model_source，模型未接入验证台 ingest 路径",
        }

    ledger_keys = set(ledger_keys or ())
    finished_dates = sorted(
        {(r["match_date"] or "")[:10] for r in daily_rows
         if r["model_source"] == target and r["status"] == "finished" and r["match_date"]}
    )
    last_finished_date = finished_dates[-1] if finished_dates else None

    unflipped = 0          # 比赛已完赛，但 daily.status 未翻 finished
    unflipped_ok = 0
    unflipped_no_odds = 0
    unflipped_zero_scored = 0
    finished_candidates = 0  # daily.status='finished' 且账本没有的行
    finished_zero_scored = 0
    finished_no_odds = 0
    finished_ok = 0

    for r in daily_rows:
        if r["model_source"] != target:
            continue
        mk = r["match_key"]
        if mk in ledger_keys:
            continue
        m = match_rows.get(mk) or {}
        mstatus = (m.get("status") or "")
        fsh, fsa = m.get("score_home"), m.get("score_away")
        p = payloads.get(mk) or {}
        has_odds = bool((p.get("market_implied") or {}).get("odds_1x2"))
        zero_scored = (
            mstatus == "finished"
            and fsh is not None and fsa is not None
            and float(fsh) == 0 and float(fsa) == 0
        )

        if r["status"] == "finished":
            finished_candidates += 1
            if zero_scored:
                finished_zero_scored += 1
            elif not has_odds:
                finished_no_odds += 1
            else:
                finished_ok += 1
        elif mstatus == "finished" and fsh is not None:
            unflipped += 1
            if zero_scored:
                unflipped_zero_scored += 1
            elif not has_odds:
                unflipped_no_odds += 1
            else:
                unflipped_ok += 1

    # 主判据：daily 侧已完赛但 status 未翻 → 门控卡死
    # 触发条件：存在"可入账候选"(unflipped_ok>0) 且 daily 侧 finished 行不再增长
    # （最后一次 finished 日期不晚于账本最后一次日期）
    frozen_finished = (
        last_finished_date is not None
        and (led["last_date"] is None or last_finished_date <= led["last_date"])
    )
    if unflipped_ok > 0 and frozen_finished:
        verdict = C2_STATUS_GATE
        detail = (
            f"daily_predictions.status 已停止翻转：最后一次 finished 行停留在 "
            f"{last_finished_date}（不晚于账本最后日期 {led['last_date']}），"
            f"而 {unflipped_ok} 行比赛在 matches 侧已完赛、payload 赔率齐全，"
            f"却因 daily.status 仍为 live/scheduled 被 ingest 的 `d.status='finished'` 门控拦住"
        )
    elif finished_candidates > 0 and (finished_zero_scored + finished_no_odds) >= finished_candidates:
        verdict = C3_EXCLUSION
        detail = (
            f"{finished_candidates} 条 finished 行全部被守卫剔除"
            f"(假0-0 {finished_zero_scored} / 无赛前1X2赔率 {finished_no_odds})"
        )
    else:
        verdict = C4_SLOW_REAL
        detail = "无门控阻塞，属真实累积速度问题"

    return {
        "verdict": verdict,
        "ledger": led,
        "daily": prof,
        "unflipped_candidates": unflipped,
        "unflipped_zero_scored": unflipped_zero_scored,
        "unflipped_no_odds": unflipped_no_odds,
        "unflipped_ok": unflipped_ok,
        "finished_candidates": finished_candidates,
        "finished_zero_scored": finished_zero_scored,
        "finished_no_odds": finished_no_odds,
        "finished_ok": finished_ok,
        "last_finished_date": last_finished_date,
        "frozen_finished": frozen_finished,
        "detail": detail,
    }


def build_summary(
    ledger_rows: list[dict],
    daily_rows: list[dict],
    target: str,
    result: dict,
    controls: dict[str, dict],
) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "note": "只读诊断 candles_ensemble 验证样本停滞；不写 verification.db / 不跑 ingest",
        "g1_accept_n": G1_ACCEPT_N,
        "target": target,
        "control_sources": controls,
        "analysis": result,
    }


# ── 报告输出 ───────────────────────────────────────────────────────────────
def render_markdown(summary: dict) -> str:
    a = summary["analysis"]
    t = summary["target"]
    L = a.get("ledger") or {}
    d = a["daily"]
    lines = [
        f"# {t} 验证样本停滞诊断（只读）",
        "",
        f"- 生成时间: {summary['generated_at']}",
        f"- 判定: **{a['verdict']}** — {a['detail']}",
        "",
        "## 1. 账本现状",
        "",
        f"- 账本样本 n={L.get('n', 0)} / G1 验收线 {summary['g1_accept_n']}（缺 {L.get('gap_to_g1', '-')}）",
        f"- 时间跨度 {L.get('first_date')} -> {L.get('last_date')}"
        + (f"（最后一次写账本 {L.get('last_created')}）" if L.get("last_created") else ""),
        "",
        "## 2. daily_predictions 供给面",
        "",
        f"- 该 source 共 {d['total']} 行；status 分布 {d['by_status']}",
        f"- status='finished' {d['finished']} 行 / 未完赛 {d['not_finished']} 行",
        f"- match_date 跨度 {d['first_date']} -> {d['last_date']}",
        "",
        "## 3. 停滞成因证据",
        "",
        f"- 比赛已完赛但 daily.status 未翻 finished：{a['unflipped_candidates']} 行"
        f"（其中 0-0 假分 {a['unflipped_zero_scored']} / 无赛前1X2赔率 {a['unflipped_no_odds']} / 可入账 {a['unflipped_ok']}）",
        f"- daily.status='finished' 且账本缺失：{a['finished_candidates']} 行"
        f"（0-0 {a['finished_zero_scored']} / 无赔率 {a['finished_no_odds']} / 可入账 {a['finished_ok']}）",
        "",
        "## 4. 对照组（同账本其他 source）",
        "",
    ]
    for src, info in summary["control_sources"].items():
        lines.append(
            f"- {src}: n={info.get('n')} span {info.get('first_date')}->{info.get('last_date')}"
        )
    lines.append("")
    lines.append("## 5. 结论与恢复建议")
    lines.append("")
    v = a["verdict"]
    if v == C2_STATUS_GATE:
        lines += [
            "根因属 **门控口径缺陷**：`verification/ingest.py::ingest_daily_predictions` "
            "以 `d.status='finished'` 为入口，但生产链路上 daily_predictions.status "
            "由 backfill 写入，实时链路不再翻该字段（matches.status 才由采集器翻）。",
            "对照 KNN ingest 走 `matches.status='finished'`，账本因此持续上涨到最新日期。",
            "恢复建议（须走 walkforward 门禁，本脚本不执行）：",
            "  1) 入口判据改为以 `matches.status='finished' AND score_home IS NOT NULL` 为准，"
            "不再依赖 daily_predictions.status；",
            "  2) 补跑一次增量 ingest，直接跳到 G1 门槛外（一次性补 2000+ 场）将丢失 walkforward 时序，"
            "须先确认补入区间是否跨模型迭代——若跨窗口，G1 计数须按 walkforward 分段判定，不可直接并账。",
        ]
    elif v == C3_EXCLUSION:
        lines.append("根因属 **守卫剔除**，恢复建议：复核 credible_1x2 口径与赔率缺失率，勿放宽守卫。")
    elif v == C1_NOT_WIRED:
        lines.append("根因属 **未接入**，恢复建议：把该 model_source 接入 ingest 路径并补跑增量。")
    else:
        lines.append("根因属 **真实累积慢**，无门禁缺陷；建议维持现有累积节奏并观察。")
    lines.append("")
    lines.append("> 红线：本脚本全程只读，未写 events.db / verification.db，未触发任何训练或重训。")
    return "\n".join(lines)


def write_outputs(summary: dict, out_dir: str) -> tuple[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    jp = os.path.join(out_dir, "candles_sample_hiatus.json")
    mp = os.path.join(out_dir, "candles_sample_hiatus.md")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    with open(mp, "w", encoding="utf-8") as f:
        f.write(render_markdown(summary))
    return jp, mp


# ── main ───────────────────────────────────────────────────────────────────
def run(events_db: str, ledger_db: str) -> dict:
    con_e = open_readonly(events_db)
    con_l = open_readonly(ledger_db)
    try:
        ledger_rows = fetch_ledger(con_l)
        ledger_keys = fetch_ledger_keys(con_l, TARGET_SOURCE)
        daily_rows = fetch_daily(con_e)
    finally:
        con_e.close()
        con_l.close()

    controls = {s: ledger_span(ledger_rows, s) or {} for s in CONTROL_SOURCES}

    result = classify_stall(TARGET_SOURCE, ledger_rows, daily_rows, {})
    # 第二轮：对"未入账候选"取 matches 状态 + payload，得到精确证据面
    cand = [r for r in daily_rows
            if r["model_source"] == TARGET_SOURCE and r["match_key"] not in ledger_keys]
    con_e = open_readonly(events_db)
    try:
        match_rows = fetch_match_status(con_e, [r["match_key"] for r in cand][:4000])
        keys = [mk for mk, m in match_rows.items() if (m.get("status") or "") == "finished"]
        payloads = fetch_payloads(con_e, keys[:2000])
    finally:
        con_e.close()
    result = classify_stall(
        TARGET_SOURCE, ledger_rows, daily_rows, match_rows, payloads, ledger_keys
    )

    return build_summary(ledger_rows, daily_rows, TARGET_SOURCE, result, controls)


def main():
    ap = argparse.ArgumentParser(description="candles_ensemble 验证样本停滞诊断（只读）")
    ap.add_argument("--events-db", default=EVENTS_DB)
    ap.add_argument("--ledger-db", default=LEDGER_DB)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--json-only", action="store_true")
    args = ap.parse_args()

    summary = run(args.events_db, args.ledger_db)
    a = summary["analysis"]
    L = a.get("ledger") or {}

    if args.json_only:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return summary

    print(f"[ledger] {L.get('n', 0)} rows  span {L.get('first_date')} -> {L.get('last_date')}")
    print(f"[daily ] status={a['daily']['by_status']}  finished={a['daily']['finished']}")
    print(f"[verdict] {a['verdict']}: {a['detail']}")
    for src, info in summary["control_sources"].items():
        print(f"[ctrl  ] {src:18s} n={info.get('n')} {info.get('first_date')}->{info.get('last_date')}")
    jp, mp = write_outputs(summary, args.out)
    print(f"[out] {jp}")
    print(f"[out] {mp}")
    return summary


if __name__ == "__main__":
    main()
