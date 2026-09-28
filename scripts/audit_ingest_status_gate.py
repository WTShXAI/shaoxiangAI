"""audit_ingest_status_gate.py — ingest 门控口径只读对账（承接 T31 的 C2 结论）

背景（T31 判定）：verification/ingest.py::ingest_daily_predictions 用
``d.status='finished'``（= daily_predictions.status）做入口门控，而
``ingest_knn`` 用 ``m.status='finished'``（= matches.status）。两者口径不一致，
且 daily_predictions.status 是 predict_export 落库时**对 matches.status 的一次性快照**，
前向链路里没有任何写方会把它翻成 finished → 前向产生的行**永远进不了验证台**。

本脚本只做只读对账，回答三个问题：
  Q1 三个入口的口径差异清单是什么（daily / knn / 其它）？
  Q2 daily_predictions.status 究竟由哪些写方维护？是否存在"会翻 finished"的写方？
  Q3 若把 daily 入口改成 matches 侧口径，会一次性多进多少行？
     其中多少会被 credible_1x2 假0-0守卫 / 赔率守卫剔除（避免"开门即污染"）？

只读铁律（不可破）：
  - events.db 以 uri + mode=ro + query_only 打开，零写入（不 VACUUM / 不 ALTER）。
  - verification.db 只读打开；本脚本绝不调用 verification.ingest_all / 不写账本。
  - 不跑 ingest、不改 ingest 源码、不跑 schema 变更、不杀进程、不碰生产服务。

用法：
  .venv/Scripts/python.exe scripts/audit_ingest_status_gate.py
  .venv/Scripts/python.exe scripts/audit_ingest_status_gate.py --json-only
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVENTS_DB = os.path.join(REPO_ROOT, "data", "events.db")
LEDGER_DB = os.path.join(REPO_ROOT, "verification.db")
INGEST_SRC = os.path.join(REPO_ROOT, "verification", "ingest.py")
DEFAULT_OUT = os.path.join(REPO_ROOT, "reports")

# 受口径影响的 source（与 ingest_daily_predictions 内 IN (...) 保持一致）
TARGET_SOURCES = ("candles_ensemble", "market_baseline")

# ── 拒绝原因（与 ingest.py 内 loop 的 continue 分支一一对应）──────────────
R_BAD_PAYLOAD = "BAD_PAYLOAD"      # payload 非 JSON
R_NO_RESULT = "NO_RESULT"          # result_1x2 返回 None（比分缺失）
R_NOT_CREDIBLE = "NOT_CREDIBLE"    # credible_1x2 假0-0守卫剔除
R_NO_ODDS = "NO_ODDS"              # payload 无 market_implied.odds_1x2 (len!=3)
R_DEVIG_FAIL = "DEVIG_FAIL"        # devig_power 返回 None（非法赔率）
R_OK = "OK"

REASON_ORDER = (R_BAD_PAYLOAD, R_NO_RESULT, R_NOT_CREDIBLE, R_NO_ODDS, R_DEVIG_FAIL, R_OK)


# ── 只读连接 ─────────────────────────────────────────────────────────────
def open_readonly(db_path: str) -> sqlite3.Connection:
    """uri + mode=ro + query_only 双保险，绝不写入。"""
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"DB not found: {db_path}")
    uri = f"file:{os.path.abspath(db_path)}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=30)
    con.execute("PRAGMA query_only=1")
    return con


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Q1 口径解析（纯文本/AST，可在无库环境单测）─────────────────────────────
STATUS_RE = re.compile(r"([A-Za-z_][\w.]*\.status)\s*=\s*'([^']+)'")


def extract_status_predicates(where_sql: str) -> dict:
    """从一段 WHERE SQL 里抽 status 判据。无则 {}。"""
    out = {}
    for tbl, val in STATUS_RE.findall(where_sql or ""):
        out[tbl] = val
    return out


def parse_ingest_gates(ingest_path: str = INGEST_SRC) -> dict:
    """解析 verification/ingest.py 三个入口的 status 口径。

    返回 {daily: {...}, knn: {...}, sources: [...]}。纯文本解析，不做任何导入。
    """
    if not os.path.exists(ingest_path):
        return {"daily": {}, "knn": {}, "sources": list(TARGET_SOURCES)}
    with open(ingest_path, "r", encoding="utf-8") as fh:
        src = fh.read()

    gates = {}
    # ingest_daily_predictions: WHERE 位于 "FROM daily_predictions d" 之后
    m_daily = re.search(
        r"FROM\s+daily_predictions\s+d(.*?)(?:ORDER BY|\)|\Z)", src, re.S | re.I)
    if m_daily:
        gates["daily"] = extract_status_predicates(m_daily.group(1))
    # ingest_knn: WHERE 位于 "FROM prematch_conclusion p" 之后
    m_knn = re.search(
        r"FROM\s+prematch_conclusion\s+p(.*?)(?:ORDER BY|\)|\Z)", src, re.S | re.I)
    if m_knn:
        gates["knn"] = extract_status_predicates(m_knn.group(1))

    m_src = re.search(r"model_source\s+IN\s*\(([^)]*)\)", src)
    sources = tuple(x.strip().strip("'\"") for x in m_src.group(1).split(",")) \
        if m_src else tuple(TARGET_SOURCES)
    return {"daily": gates.get("daily", {}), "knn": gates.get("knn", {}), "sources": list(sources)}


def same_status_gate(gates: dict) -> bool:
    """两入口是否用**同一张表**的 status 做门控（口径一致）。

    注意：值相同（都='finished'）不等于口径一致 —— daily.status 是 matches.status 的
    一次性快照，前向链路里无人翻转，两者并不同源。
    """
    d, k = gates.get("daily", {}), gates.get("knn", {})
    keys = {t for t in list(d) + list(k)}
    if len(keys) != 1:
        return False
    return bool(d) and bool(k) and list(d.values())[0] == list(k.values())[0]


# ── Q2 写方盘点（纯 grep，不导入不执行）───────────────────────────────────
UPDATE_RE = re.compile(r"UPDATE\s+(?:[\w.]*\.)?daily_predictions", re.I)
INSERT_RE = re.compile(r"INSERT\s+INTO\s+(?:[\w.]*\.)?daily_predictions", re.I)


def classify_writer_line(line: str, window: str = "") -> str:
    """按行内容把 daily_predictions 的接触方分类。

    UPDATE_WRITER 会改写 status（唯一能翻 finished 的形态）
    INSERT_WRITER  写入时 status 取 matches.status 快照（前向链路：非 finished）
    READER         SELECT
    PASSTHROUGH    INSERT 但 status 列来自 excluded.status（快照，非翻）
    OTHER          其它（注释/docstring/字符串）
    """
    s = line.strip()
    if UPDATE_RE.search(s):
        return "UPDATE_WRITER"
    if INSERT_RE.search(s):
        # 写 status 但值来自 excluded.* → 快照语义（把 matches.status 原样抄进 daily），
        # 不是"把 daily 翻成 finished"
        if re.search(r"status\s*=\s*excluded\.", window or s) or "excluded." in (window or s):
            return "PASSTHROUGH"
        return "INSERT_WRITER"
    if re.search(r"FROM\s+(?:[\w.]*\.)?daily_predictions", s, re.I):
        return "READER"
    if re.search(r"daily_predictions", s):
        return "OTHER"
    return ""


def find_status_writers(repo_root: str = REPO_ROOT) -> dict:
    """全仓 .py 扫描 daily_predictions 接触方，标记是否存在"会翻 finished"的写方。"""
    hits = []
    # 自指排除：本脚本与其单测会提到 "UPDATE daily_predictions" 字面量，
    # 若不排除会把自己的断言当成"写方"（与 T30-C 的同类自指假阳性同源）。
    self_exclude = ("audit_ingest_status_gate.py", "test_audit_ingest_status_gate.py")
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames
                       if d not in (".git", "__pycache__", "node_modules", "venv", ".venv")]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            if any(fn.endswith(ex) for ex in self_exclude):
                continue
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    lines = fh.read().splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for i, line in enumerate(lines, 1):
                if "daily_predictions" not in line:
                    continue
                # INSERT 的 status 值常与列名在不同行（列清单 / VALUES 各占一行），
                # 故取后续 6 行作为语句窗口判断是否为 excluded.* 快照写入。
                window = "\n".join(lines[i:i + 6])
                hits.append({
                    "file": os.path.relpath(path, repo_root).replace("\\", "/"),
                    "line": i,
                    "kind": classify_writer_line(line, window),
                    "text": line.strip()[:160],
                })
    by_kind = defaultdict(int)
    for h in hits:
        by_kind[h["kind"]] += 1
    return {
        "total_hits": len(hits),
        "by_kind": dict(by_kind),
        "hits": hits,
        "status_flippers_exist": by_kind.get("UPDATE_WRITER", 0) > 0,
    }


# ── Q3 口径差额与守卫剔除预估（只读 SELECT）──────────────────────────────
def collect_candidates(con: sqlite3.Connection) -> list[dict]:
    """取门控 B（matches 侧口径）全集候选行（只读）。"""
    marks = ",".join("?" * len(TARGET_SOURCES))
    sql = f"""
        SELECT d.match_key, d.model_source, d.payload, d.match_date, d.kickoff,
               m.status AS m_status, m.score_home, m.score_away
        FROM daily_predictions d
        JOIN matches m ON m.match_key = d.match_key
        WHERE m.status='finished' AND m.score_home IS NOT NULL
          AND d.model_source IN ({marks})
    """
    con.row_factory = sqlite3.Row
    return [dict(r) for r in con.execute(sql, TARGET_SOURCES).fetchall()]


def _blank_reasons() -> dict:
    return dict.fromkeys(REASON_ORDER, 0)


def last_odds_map(con: sqlite3.Connection, keys: list[str], chunk: int = 900) -> dict:
    """一次性取各 match_key 的 MAX(captured_at)，避免 N+1 查询。只读。"""
    out: dict[str, float] = {}
    for i in range(0, len(keys), chunk):
        part = keys[i:i + chunk]
        marks = ",".join("?" * len(part))
        sql = (f"SELECT match_key, MAX(captured_at) FROM odds_changes "
               f"WHERE match_key IN ({marks}) GROUP BY match_key")
        for mk, mx in con.execute(sql, part).fetchall():
            out[mk] = float(mx) if mx is not None else None
    return out


def evaluate_guard(row: dict, lodge: dict, kickoff_ts_fn) -> tuple[str, str]:
    """复刻 ingest_daily_predictions 的 loop 守卫，返回 (reason, chosen_outcome)。

    与生产代码逐分支对齐：payload 解析 → result_1x2 → credible_1x2 → odds → devig。
    """
    try:
        p = json.loads(row.get("payload") or "")
    except Exception:
        return R_BAD_PAYLOAD, ""
    if not isinstance(p, dict):
        return R_BAD_PAYLOAD, ""
    fsh, fsa = row.get("score_home"), row.get("score_away")
    act = _result_1x2(fsh, fsa)
    if act is None:
        return R_NO_RESULT, ""
    ko_ts = kickoff_ts_fn(row.get("kickoff") or "")
    if not _credible(fsh, fsa, lodge.get(row["match_key"]), ko_ts):
        return R_NOT_CREDIBLE, ""
    odds = (p.get("market_implied") or {}).get("odds_1x2")
    if not odds or len(odds) != 3:
        return R_NO_ODDS, ""
    probs = _devig_power([float(odds[0]), float(odds[1]), float(odds[2])])
    if probs is None:
        return R_DEVIG_FAIL, ""
    prob_map = {"home": float(p["p_home"]), "draw": float(p["p_draw"]), "away": float(p["p_away"])}
    chosen = max(prob_map, key=prob_map.get)
    return R_OK, chosen


def _result_1x2(sh, sa):
    if sh is None or sa is None:
        return None
    if sh > sa:
        return "home"
    if sh == sa:
        return "draw"
    return "away"


def _credible(sh, sa, lodge_ts, ko_ts) -> bool:
    """复刻 pipeline.settle.credible_1x2（本脚本不导入生产模块以保持零副作用）。"""
    if sh is None or sa is None:
        return False
    if sh != 0 or sa != 0:
        return True
    if lodge_ts is None or ko_ts is None:
        return False
    return float(lodge_ts) >= float(ko_ts) + 95.0 * 60


def _devig_power(odds):
    """幂法去水的最小复刻（与 pipeline.odds_math.devig_power 同族）；非法返回 None。"""
    try:
        vals = [float(x) for x in odds]
    except (TypeError, ValueError):
        return None
    if any(v <= 1.0 or v >= 50.0 for v in vals):
        return None
    import math
    inv = [1.0 / v for v in vals]
    tot = sum(inv)
    if tot <= 0:
        return None
    p = [i / tot for i in inv]
    # 幂法: p_i = (inv_i/tot)^k 归一 (k>1 抑制热门); 与 SSoT 同族
    ks = (1.0, 1.1, 1.2, 1.3)
    best, best_err = None, 1e9
    for k in ks:
        raw = [(i / tot) ** k for i in inv]
        s = sum(raw)
        pp = [r / s for r in raw]
        err = sum(abs(a - b) for a, b in zip(pp, p))
        if err < best_err:
            best_err, best = err, pp
    return best


# ── 主流程 ───────────────────────────────────────────────────────────────
def parse_kickoff_ts_local(s: str):
    """parse_kickoff_ts 的最小复刻（'YYYY-MM-DD HH:MM:SS' → epoch）。"""
    m = re.match(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})", s or "")
    if not m:
        return None
    try:
        from datetime import datetime as _dt
        return _dt(*[int(x) for x in m.group(1).split("-")],
                   *[int(x) for x in m.group(2).split(":")]).timestamp()
    except Exception:
        return None


def count_gates(con: sqlite3.Connection, sources: tuple) -> dict:
    """两个门控的 SQL 层行数（入口判据，未含 loop 内守卫）。只读 COUNT。"""
    marks = ",".join("?" * len(sources))
    a = con.execute(
        f"""SELECT COUNT(*) FROM daily_predictions d JOIN matches m ON m.match_key=d.match_key
            WHERE d.status='finished' AND m.score_home IS NOT NULL
              AND d.model_source IN ({marks})""", sources).fetchone()[0]
    b = con.execute(
        f"""SELECT COUNT(*) FROM daily_predictions d JOIN matches m ON m.match_key=d.match_key
            WHERE m.status='finished' AND m.score_home IS NOT NULL
              AND d.model_source IN ({marks})""", sources).fetchone()[0]
    return {"gate_A_sql": a, "gate_B_sql": b, "delta_sql": b - a}


def run_audit(events_db: str = EVENTS_DB, ledger_db: str = LEDGER_DB) -> dict:
    gates = parse_ingest_gates()
    writers = find_status_writers()
    sources = tuple(gates.get("sources") or TARGET_SOURCES)

    result = {
        "generated_at_utc": _utc_now(),
        "gates": gates,
        "gate_consistent": same_status_gate(gates),
        "writers": {k: v for k, v in writers.items() if k != "hits"},
        "writer_hit_top": writers["hits"][:20],
    }
    if not os.path.exists(events_db):
        result["error"] = "events.db not found; skip DB part"
        return result

    ev = open_readonly(events_db)
    try:
        gate_counts = count_gates(ev, sources)
        cands = collect_candidates(ev)
        keys = [r["match_key"] for r in cands]
        dp_status = _dp_status_map(ev, keys)          # 门控 A 判据（只读）
        lodge = last_odds_map(ev, keys)               # 假0-0守卫输入（只读）

        ledger_keys: set[str] = set()
        if os.path.exists(ledger_db):
            lg = open_readonly(ledger_db)
            try:
                ledger_keys = {r[0] for r in lg.execute(
                    "SELECT DISTINCT match_id FROM verification_ledger").fetchall()}
            finally:
                lg.close()

        statsA = defaultdict(int)   # 门控 A（d.status='finished'）下的守卫结局分布
        statsB = defaultdict(int)   # 门控 B（m.status='finished'）下的守卫结局分布
        delta_reasons = defaultdict(int)   # 只统计"改口径才会新增"的行
        delta_survivors: list[str] = []
        delta_dates: set[str] = set()

        for row in cands:
            reason, _chosen = evaluate_guard(row, lodge, parse_kickoff_ts_local)
            in_gate_a = dp_status.get(row["match_key"]) == "finished"
            if in_gate_a:
                statsA[reason] += 1
            statsB[reason] += 1
            if not in_gate_a:
                delta_reasons[reason] += 1
                if reason == R_OK:
                    delta_survivors.append(row["match_key"])
                    delta_dates.add(row.get("match_date") or "")

        new_rows = set(delta_survivors) - ledger_keys
        result["candidates_B"] = len(cands)
        result["gate_sql_counts"] = gate_counts
        result["gate_A"] = dict(statsA)
        result["gate_B"] = dict(statsB)
        result["delta_survivors"] = len(delta_survivors)
        result["delta_by_reason"] = dict(delta_reasons)
        result["delta_keys_sample"] = delta_survivors[:50]
        result["already_in_ledger"] = len(set(delta_survivors) & ledger_keys)
        result["net_new_rows"] = len(new_rows)
        dn = sorted(d for d in delta_dates if d)
        result["delta_date_span"] = {"min": dn[0] if dn else "", "max": dn[-1] if dn else "",
                                     "distinct_dates": len(dn)}
    finally:
        ev.close()
    return result


def _dp_status_map(con, keys: list[str]) -> dict:
    """只读取 daily_predictions.status（门控 A 判据）。"""
    out = {}
    for i in range(0, len(keys), 900):
        part = keys[i:i + 900]
        marks = ",".join("?" * len(part))
        for mk, st in con.execute(
                f"SELECT match_key, status FROM daily_predictions WHERE match_key IN ({marks})",
                part).fetchall():
            out[mk] = st
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="ingest 门控口径只读对账 (T33)")
    ap.add_argument("--json-only", action="store_true", help="只打印 JSON 摘要")
    args = ap.parse_args()

    result = run_audit()
    os.makedirs(DEFAULT_OUT, exist_ok=True)
    jpath = os.path.join(DEFAULT_OUT, "ingest_status_gate_audit.json")
    with open(jpath, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    if not args.json_only:
        mpath = os.path.join(DEFAULT_OUT, "ingest_status_gate_audit.md")
        with open(mpath, "w", encoding="utf-8") as fh:
            fh.write(render_markdown(result))
        print(f"[T33] wrote {jpath}")
        print(f"[T33] wrote {mpath}")
    print(json.dumps({k: v for k, v in result.items() if k != "writer_hit_top"},
                     ensure_ascii=False, indent=2)[:4000])
    return 0


def render_markdown(r: dict) -> str:
    g = r.get("gates", {})
    w = r.get("writers", {})
    lines = [
        "# T33 ingest 门控口径只读对账",
        "",
        f"生成时间(UTC): {r.get('generated_at_utc','')}",
        "",
        "## 1. 口径差异清单",
        "",
        f"- `ingest_daily_predictions` status 判据: `{g.get('daily', {})}`",
        f"- `ingest_knn` status 判据: `{g.get('knn', {})}`",
        f"- 两入口口径一致: **{r.get('gate_consistent')}**",
        f"- 受影响 source: `{g.get('sources', [])}`",
        "",
        "## 2. daily_predictions.status 写方盘点",
        "",
        f"- 全仓 .py 中 `daily_predictions` 接触行: **{w.get('total_hits', 0)}**",
        f"- 分类: `{w.get('by_kind', {})}`",
        f"- 存在会翻 finished 的 UPDATE 写方: **{w.get('status_flippers_exist')}**",
        "",
        "## 3. 改口径前后行数差（门控 B 候选集）",
        "",
        f"- 门控 B 候选总数: **{r.get('candidates_B', 0)}**",
        f"- 门控 A(GATE_A)  survivor 预估: **{r.get('gate_A', {}).get('survivors', 0)}**",
        f"- 门控 B(GATE_B)  survivor 预估: **{r.get('gate_B', {}).get('survivors', 0)}**",
        f"- 净增 survivor: **{r.get('delta_survivors', 0)}**",
        f"- 其中已被账本收录: {r.get('already_in_ledger', 0)} / 净新增: **{r.get('net_new_rows', 0)}**",
        "",
        "## 4. 净增部分的守卫剔除预估（防止开门即污染）",
        "",
        "| 剔除原因 | 行数 |",
        "|---|---|",
    ]
    for k, v in (r.get("delta_by_reason", {}) or {}).items():
        lines.append(f"| {k} | {v} |")
    lines += [
        "",
        "> 只读对账：未跑 ingest、未写 verification.db、未改 verification/ingest.py。",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
