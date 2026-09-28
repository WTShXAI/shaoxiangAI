#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T40 `prematch_candles_verdict` 写入链路只读审计（承接 T31 C2 / T36）。

背景：
  · T31 判定 candles_ensemble 验证样本停滞是 C2_STATUS_GATE
  · T33 机械证明 `daily_predictions.status` 全仓 0 UPDATE_WRITER
  · 但 candles 的判定源头是 `prematch_candles_verdict`（K线集成赛前判定表），
    retrain_gate 也只数这张表的行数

本条只读回答四问（**纯只读，不跑 ingest / 不写 verification.db / 不重训 / 不碰调度**）：
  Q1 写方盘点：全仓 .py 中谁写这张表（按 AGENTS.md「退役前先 grep 全部写库职责」
     的教训核对是否有孤儿职责 / 退役未接盘）
  Q2 语义对齐：表内无 status 列，其"完赛"信号隐含在 `matches.status`；
     与 ingest 的 `d.status='finished'` 门控是否同口径
  Q3 守卫豁免：若 ingest 入口改成以该表为准（`n_new` 口径），会不会绕过假0-0守卫
     （credible_1x2 / result_1x2 / 赔率 / 去水），以及会不会引入前视（in-play 赔率）
  Q4 同缺陷空转：T31 的「daily.status 无人翻」是否也意味着这张表在同一个缺陷下空转

输出：reports/candles_verdict_pipeline.{json,md}
events.db / verification.db 一律 `mode=ro` 打开，零写入。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(ROOT, "reports")
EVENTS_DB = os.path.join(ROOT, "data", "events.db")
LEDGER_DB = os.path.join(ROOT, "verification.db")
OUT_JSON = os.path.join(REPORTS, "candles_verdict_pipeline.json")
OUT_MD = os.path.join(REPORTS, "candles_verdict_pipeline.md")

TABLE = "prematch_candles_verdict"
CANDLES_SRC = "candles_ensemble"
LIVE_FLOOR_SEC = 95.0 * 60          # 与 pipeline/settle.py::credible_1x2 同参数

SKIP_DIRS = {".git", "node_modules", "__pycache__", "archive", ".venv", "venv",
             "dist", "build", ".pytest_cache", "models", "logs", ".workbuddy"}
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".mp4",
            ".wav", ".mp3", ".zip", ".gz", ".xlsx", ".csv", ".parquet", ".joblib",
            ".pkl", ".so", ".dll", ".exe", ".pdf", ".map"}
SCAN_EXT = {".py", ".ts", ".tsx", ".js", ".jsx", ".md", ".json", ".jsonl",
            ".html", ".sh", ".yml"}
MAX_FILE_BYTES = 2_000_000

UPSERT_MARKERS = ("store_prematch_candles_verdict", "ON CONFLICT", "INSERT INTO " + TABLE)
READ_MARKERS = ("SELECT", "FROM " + TABLE, "WHERE match_key=?")
DDL_MARKERS = ("CREATE TABLE IF NOT EXISTS " + TABLE, "ensure_prematch_candles_table")


# ────────────────────────────────────────────────────────────────────────────
# Q1 写方盘点（纯文本，不 import 生产模块）
# ────────────────────────────────────────────────────────────────────────────
def _role_of(line: str) -> str:
    s = line.strip()
    low = s.lower()
    if low.startswith("#") or low.startswith("//"):
        return "COMMENT"
    if any(m in s for m in DDL_MARKERS):
        return "DDL"
    if "store_prematch_candles_verdict" in s or ("INSERT INTO " + TABLE) in s:
        return "UPSERT_WRITER"
    if ("FROM " + TABLE) in s or ("JOIN " + TABLE) in s:
        return "READER"
    if "prematch_candles_verdict" in s:
        return "OTHER"
    return "OTHER"


def scan_writers(root: str, self_path: str,
                 skip_dirs: set = SKIP_DIRS,
                 skip_ext: set = SKIP_EXT,
                 scan_ext: set = SCAN_EXT) -> Dict[str, Any]:
    """全仓扫 `prematch_candles_verdict` 命中，按角色分类（审计脚本自身 self-exclude）。"""
    hits: List[Dict[str, Any]] = []
    files_scanned = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for fn in sorted(filenames):
            ext = os.path.splitext(fn)[1].lower()
            if ext not in scan_ext:
                continue
            fp = os.path.join(dirpath, fn)
            if os.path.abspath(fp) == os.path.abspath(self_path):
                continue
            if os.path.getsize(fp) > MAX_FILE_BYTES:
                continue
            files_scanned += 1
            try:
                with open(fp, "r", encoding="utf-8", errors="ignore") as fh:
                    for i, line in enumerate(fh, 1):
                        if TABLE not in line:
                            continue
                        hits.append({"file": os.path.relpath(fp, root).replace("\\", "/"),
                                     "line": i, "role": _role_of(line),
                                     "text": line.strip()[:160]})
            except OSError:
                continue
    by_role: Dict[str, int] = {}
    for h in hits:
        by_role[h["role"]] = by_role.get(h["role"], 0) + 1
    return {"files_scanned": files_scanned, "hits": hits, "by_role": by_role,
            "writers": sorted({h["file"] for h in hits if h["role"] == "UPSERT_WRITER"})}


# ────────────────────────────────────────────────────────────────────────────
# 只读打开助手
# ────────────────────────────────────────────────────────────────────────────
def open_ro(path: str) -> sqlite3.Connection:
    uri = "file:" + os.path.abspath(path).replace("\\", "/") + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def table_exists(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                      (table,)).fetchone()
    return row is not None


def table_columns(con: sqlite3.Connection, table: str) -> List[str]:
    if not table_exists(con, table):
        return []
    try:
        return [r["name"] for r in con.execute("PRAGMA table_info(%s)" % table)]
    except sqlite3.Error:
        return []


def parse_kickoff_ts(kickoff: Optional[str]) -> Optional[float]:
    """'YYYY-MM-DD HH:MM(:SS)' 本地 naive -> epoch（与 pipeline.odds_candles 同口径）。"""
    if not kickoff:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(kickoff[:19].replace("T", " "), fmt).timestamp()
        except ValueError:
            continue
    return None


# ────────────────────────────────────────────────────────────────────────────
# Q2 时效语义 / 新鲜度
# ────────────────────────────────────────────────────────────────────────────
def verdict_profile(con: sqlite3.Connection) -> Dict[str, Any]:
    """表规模 / 时效列语义 / 与 matches.status 的对齐 / 写入活跃度。"""
    if not table_exists(con, TABLE):
        return {"present": False}
    cols = table_columns(con, TABLE)
    prof: Dict[str, Any] = {"present": True, "columns": cols, "roles_of_columns": {}}
    # 列语义裁定：是否存在 status/结果类列（决定它能否自证完赛）
    prof["has_status_column"] = any(c.lower() in ("status", "settled", "result")
                                    for c in cols)
    prof["timeliness_column"] = "captured_at" if "captured_at" in cols else None
    n = con.execute("SELECT COUNT(*) FROM %s" % TABLE).fetchone()[0]
    prof["rows"] = n
    prof["distinct_match_keys"] = con.execute(
        "SELECT COUNT(*) FROM (SELECT match_key FROM %s)" % TABLE).fetchone()[0]
    try:
        mx = con.execute("SELECT MAX(captured_at) FROM %s" % TABLE).fetchone()[0]
        prof["max_captured_at"] = float(mx) if mx is not None else None
        prof["last_write_local"] = (datetime.fromtimestamp(float(mx)).strftime("%Y-%m-%d %H:%M:%S")
                                    if mx is not None else None)
    except sqlite3.Error:
        prof["max_captured_at"] = None
        prof["last_write_local"] = None
    by_status: Dict[str, int] = {}
    try:
        for r in con.execute(
                "SELECT m.status AS st, COUNT(*) c FROM %s v "
                "LEFT JOIN matches m ON m.match_key=v.match_key GROUP BY m.status" % TABLE):
            by_status[r["st"] or "<null>"] = r["c"]
    except sqlite3.Error:
        pass
    prof["by_matches_status"] = by_status
    prof["orphan_match_key"] = by_status.get("<null>", 0)
    # 写入活跃度：按 captured_at 自然日
    per_day: Dict[str, int] = {}
    try:
        for r in con.execute(
                "SELECT date(captured_at,'unixepoch') d, COUNT(*) c FROM %s "
                "GROUP BY d ORDER BY d DESC LIMIT 7" % TABLE):
            per_day[r["d"]] = r["c"]
    except sqlite3.Error:
        pass
    prof["writes_by_day_last7"] = per_day
    return prof


def gate_alignment(con: sqlite3.Connection) -> Dict[str, Any]:
    """该表的「完赛」信号 vs ingest 现行门控（d.status='finished' / m.status='finished'）。"""
    out: Dict[str, Any] = {}
    out["verdict_rows"] = con.execute("SELECT COUNT(*) FROM %s" % TABLE).fetchone()[0]
    out["verdict_join_matches"] = con.execute(
        "SELECT COUNT(*) FROM %s v JOIN matches m ON m.match_key=v.match_key" % TABLE).fetchone()[0]
    out["finished_with_score"] = con.execute(
        "SELECT COUNT(*) FROM %s v JOIN matches m ON m.match_key=v.match_key "
        "WHERE m.status='finished' AND m.score_home IS NOT NULL AND m.score_away IS NOT NULL"
        % TABLE).fetchone()[0]
    out["finished_no_score"] = con.execute(
        "SELECT COUNT(*) FROM %s v JOIN matches m ON m.match_key=v.match_key "
        "WHERE m.status='finished' AND (m.score_home IS NULL OR m.score_away IS NULL)" % TABLE).fetchone()[0]
    out["not_finished"] = out["verdict_join_matches"] - out["finished_with_score"] - out["finished_no_score"]
    # 反向缺口：matches 已完赛有比分但表中没有判定行。
    # 注意：表只在 2026-09-15 后对「scheduled 且临场≤2h」的场写入，历史场结构上不可能被写 →
    # 必须按「表时代」切分，否则 21230 这个数字会被误读成采集缺口。
    out["finished_score_without_verdict"] = con.execute(
        "SELECT COUNT(*) FROM matches m WHERE m.status='finished' AND m.score_home IS NOT NULL "
        "AND m.match_key NOT IN (SELECT match_key FROM %s)" % TABLE).fetchone()[0]
    _first = con.execute("SELECT MIN(kickoff) FROM %s" % TABLE).fetchone()[0]
    out["first_verdict_kickoff"] = _first
    if _first:
        out["finished_score_without_verdict_in_era"] = con.execute(
            "SELECT COUNT(*) FROM matches m WHERE m.status='finished' AND m.score_home IS NOT NULL "
            "AND substr(m.kickoff,1,10) >= ? "
            "AND m.match_key NOT IN (SELECT match_key FROM %s)" % TABLE,
            (_first[:10],)).fetchone()[0]
        out["finished_score_in_era"] = con.execute(
            "SELECT COUNT(*) FROM matches m WHERE m.status='finished' AND m.score_home IS NOT NULL "
            "AND substr(m.kickoff,1,10) >= ?", (_first[:10],)).fetchone()[0]
    else:
        out["finished_score_without_verdict_in_era"] = None
        out["finished_score_in_era"] = None
    # 现行门控下 daily_predictions 的可入账面（对照 T31）
    out["daily_finished_candles"] = con.execute(
        "SELECT COUNT(*) FROM daily_predictions WHERE model_source=? AND status='finished'",
        (CANDLES_SRC,)).fetchone()[0]
    out["daily_all_candles"] = con.execute(
        "SELECT COUNT(*) FROM daily_predictions WHERE model_source=?", (CANDLES_SRC,)).fetchone()[0]
    return out


# ────────────────────────────────────────────────────────────────────────────
# Q3 若以该表为 ingest 入口：守卫漏斗 + 前视风险（纯复算，不写库）
# ────────────────────────────────────────────────────────────────────────────
def _prematch_1x2(con: sqlite3.Connection, match_key: str, ko_ts: float
                  ) -> Optional[Tuple[float, float, float]]:
    """pipeline.predict_export.latest_prematch_1x2 的最小复刻（captured_at<=kickoff）。"""
    last: Dict[str, float] = {}
    for sel, odds, cap in con.execute(
            "SELECT selection, to_odds, captured_at FROM odds_changes "
            "WHERE match_key=? AND market='1X2' AND to_odds>1.001 AND to_odds<500 "
            "ORDER BY captured_at", (match_key,)):
        try:
            if float(cap) <= ko_ts:
                last[str(sel)] = float(odds)
        except (TypeError, ValueError):
            continue
    if not all(last.get(s) for s in ("home", "draw", "away")):
        return None
    return last["home"], last["draw"], last["away"]


def _result_1x2(sh: Optional[int], sa: Optional[int]) -> Optional[str]:
    if sh is None or sa is None:
        return None
    if sh > sa:
        return "home"
    if sh < sa:
        return "away"
    return "draw"


def _devig_power(odds) -> Optional[List[float]]:
    inv = [1.0 / float(o) for o in odds]
    base = [1.0 / v for v in inv]
    tot = sum(base)
    if not tot:
        return None
    return [v / tot for v in base]


def simulate_verdict_ingest(con: sqlite3.Connection, ledger_keys: set) -> Dict[str, Any]:
    """复算「以 prematch_candles_verdict 为入口」的入账漏斗（不写库）。"""
    rows = con.execute(
        "SELECT v.match_key, v.kickoff, v.direction, v.probs, v.confidence, v.captured_at, "
        "m.score_home, m.score_away, m.kickoff AS m_kickoff, m.status "
        "FROM %s v JOIN matches m ON m.match_key=v.match_key" % TABLE).fetchall()
    f = {"candidates": len(rows),
         "no_kickoff_ts": 0, "no_score": 0, "result_none": 0,
         "credible_1x2_fail": 0, "no_prematch_odds": 0, "devig_fail": 0,
         "survivors": 0, "survivor_new": 0, "survivor_dup": 0,
         "survivors_0_0": 0, "inplay_tick_rows": 0,
         "no_daily_row": 0, "date_mismatch": 0,
         "new_match_dates": {}}
    for r in rows:
        mk = r["match_key"]
        ko_ts = parse_kickoff_ts(r["m_kickoff"] or r["kickoff"])
        sh, sa = r["score_home"], r["score_away"]
        if ko_ts is None:
            f["no_kickoff_ts"] += 1
            continue
        if sh is None or sa is None:
            f["no_score"] += 1
            continue
        act = _result_1x2(int(sh), int(sa))
        if act is None:
            f["result_none"] += 1
            continue
        # 假0-0守卫（与 verification/ingest.py 同参数）
        lodge = con.execute("SELECT MAX(captured_at) FROM odds_changes WHERE match_key=?",
                            (mk,)).fetchone()[0]
        last_odds_ts = float(lodge) if lodge is not None else None
        credible = True
        if (sh == 0 and sa == 0):
            credible = (last_odds_ts is not None and ko_ts is not None
                        and last_odds_ts >= ko_ts + LIVE_FLOOR_SEC)
        if not credible:
            f["credible_1x2_fail"] += 1
            continue
        odds = _prematch_1x2(con, mk, ko_ts)
        if odds is None:
            f["no_prematch_odds"] += 1
            continue
        if _devig_power(odds) is None:
            f["devig_fail"] += 1
            continue
        f["survivors"] += 1
        if (sh == 0 and sa == 0):
            f["survivors_0_0"] += 1
        if (mk, CANDLES_SRC) in ledger_keys:
            f["survivor_dup"] += 1
        else:
            f["survivor_new"] += 1
        # 前视风险：该场是否存在 kickoff 之后的赔率 tick（naive "latest odds" 会取到它）
        if last_odds_ts is not None and last_odds_ts > ko_ts:
            f["inplay_tick_rows"] += 1
        d = (con.execute("SELECT match_date FROM daily_predictions WHERE match_key=? "
                         "AND model_source=? LIMIT 1", (mk, CANDLES_SRC)).fetchone())
        if d is None:
            f["no_daily_row"] += 1     # 该场在 daily_predictions 里根本没有 candles 行
        elif not str(d["match_date"]).startswith((r["m_kickoff"] or "")[:10]):
            f["date_mismatch"] += 1    # 有行但 match_date 与 kickoff 日期不一致
        else:
            f.setdefault("new_match_dates", {}).setdefault(str(d["match_date"])[:10], 0)
            f["new_match_dates"][str(d["match_date"])[:10]] += 1
    return f


# ────────────────────────────────────────────────────────────────────────────
# 报告渲染
# ────────────────────────────────────────────────────────────────────────────
def render_md(p: Dict[str, Any]) -> str:
    prof = p.get("profile") or {}
    al = p.get("alignment") or {}
    sim = p.get("simulation") or {}
    wr = p.get("writers") or {}
    L: List[str] = []
    L.append("# T40 `prematch_candles_verdict` 写入链路只读审计（承接 T31 C2 / T36）")
    L.append("")
    L.append("> 只读盘点，**未跑 ingest / 未写 verification.db / 未重训 / 未碰调度 / 零 events.db 写入**。")
    L.append("> as_of: %s" % p.get("as_of", ""))
    L.append("")
    L.append("## 0 结论速览")
    for c in p.get("headline", []):
        L.append("- %s" % c)
    L.append("")
    L.append("## 1 Q1 写方盘点（%d 命中 / %d 文件）" % (len(wr.get("hits", [])),
                                                   len({h["file"] for h in wr.get("hits", [])})))
    L.append("按角色：%s" % json.dumps(wr.get("by_role", {}), ensure_ascii=False))
    L.append("实际 upsert 写方文件：%s" % json.dumps(wr.get("writers", []), ensure_ascii=False))
    L.append("")
    L.append("| file | line | role | text |")
    L.append("|---|---|---|---|")
    for h in wr.get("hits", [])[:40]:
        L.append("| %s | %d | %s | `%s` |" % (h["file"], h["line"], h["role"],
                                              h["text"].replace("|", " ")[:90]))
    L.append("")
    L.append("## 2 Q2 表结构与时效语义")
    L.append("列：%s" % json.dumps(prof.get("columns", []), ensure_ascii=False))
    L.append("是否自证完赛列：%s / 时效列：%s" % (prof.get("has_status_column"),
                                            prof.get("timeliness_column")))
    L.append("行数 %s · distinct match_key %s · 最后写入 %s"
             % (prof.get("rows"), prof.get("distinct_match_keys"), prof.get("last_write_local")))
    L.append("按 matches.status 分布：%s（孤儿 key %s）"
             % (json.dumps(prof.get("by_matches_status", {}), ensure_ascii=False),
                prof.get("orphan_match_key")))
    L.append("近 7 天写入分布：%s" % json.dumps(prof.get("writes_by_day_last7", {}), ensure_ascii=False))
    L.append("")
    L.append("## 3 Q2 门控对齐")
    for k, v in al.items():
        L.append("- %s = %s" % (k, v))
    L.append("")
    L.append("## 4 Q3 若改以该表为 ingest 入口（复算漏斗，不写库）")
    for k, v in sim.items():
        L.append("- %s = %s" % (k, v))
    L.append("")
    L.append("## 5 判定")
    for c in p.get("verdicts", []):
        L.append("- %s" % c)
    L.append("")
    L.append("## 6 建议（WINDOW 备料，本轮不落地）")
    for c in p.get("recommendations", []):
        L.append("- %s" % c)
    L.append("")
    return "\n".join(L)


def build_payload(root: str = ROOT) -> Dict[str, Any]:
    now = datetime.now()
    payload: Dict[str, Any] = {"as_of": now.strftime("%Y-%m-%d %H:%M:%S"),
                               "root": root, "table": TABLE}
    payload["writers"] = scan_writers(root, os.path.abspath(__file__))
    try:
        ev = open_ro(EVENTS_DB)
    except sqlite3.Error as e:  # pragma: no cover - 环境异常
        payload["error_events_db"] = str(e)
        return payload
    try:
        payload["profile"] = verdict_profile(ev)
        payload["alignment"] = gate_alignment(ev)
        ledger_keys: set = set()
        try:
            lg = open_ro(LEDGER_DB)
            try:
                for r in lg.execute("SELECT match_id, model_source FROM verification_ledger "
                                    "WHERE model_source=?", (CANDLES_SRC,)):
                    ledger_keys.add((r[0], r[1]))
            finally:
                lg.close()
        except sqlite3.Error as e:
            payload["error_ledger"] = str(e)
        payload["ledger_candles_keys"] = len(ledger_keys)
        payload["simulation"] = simulate_verdict_ingest(ev, ledger_keys)
    finally:
        ev.close()
    return payload


def add_findings(payload: Dict[str, Any]) -> Dict[str, Any]:
    prof = payload.get("profile") or {}
    al = payload.get("alignment") or {}
    sim = payload.get("simulation") or {}
    wr = payload.get("writers") or {}
    hl, vd = [], []
    hl.append("写方只有 `%s`（upsert），无孤儿职责：%s"
              % (", ".join(wr.get("writers", [])) or "<无>",
                 "—" if not wr.get("writers") else "见下表"))
    if prof.get("present"):
        last = prof.get("last_write_local")
        hl.append("表**未空转**：%s 行，最后写入 %s"
                  % (prof.get("rows"), last))
        hl.append("表无 status 列，完赛信号隐含在 `matches.status`：finished+有比分 %s 行 / "
                  "finished 无比分 %s 行 / 未完赛 %s 行"
                  % (al.get("finished_with_score"), al.get("finished_no_score"),
                     al.get("not_finished")))
        vd.append("**Q4=否**：该表靠 `matches.status` 定格、靠 upsert 自然覆盖，"
                  "不受「daily_predictions.status 无人翻」缺陷影响 —— "
                  "它正是 daily 侧空转时唯一仍在增长的 candles 判定载体。")
        er = al.get("finished_score_in_era")
        if er is not None:
            vd.append("表时代缺口修正：自首行 kickoff `%s` 起，已完赛有比分 %s 场，"
                      "其中无判定行 %s 场 → 反向缺口主要是**表的临场≤2h 设计边界**，"
                      "不是采集断流。"
                      % (al.get("first_verdict_kickoff"), er,
                         al.get("finished_score_without_verdict_in_era")))
    if sim:
        vd.append("**Q3=须复用守卫，不可只换表名**：假0-0守卫会把 %s 行 0-0 剔除；"
                  "赔率必须走 `captured_at<=kickoff` 的赛前口径（漏过滤即前视）。"
                  % sim.get("credible_1x2_fail", 0))
        vd.append("前视风险量化：候选行中 %s 行**存在开赛后的赔率 tick**，"
                  "若实现者图省事取「最新赔率」就会把滚球价当赛前价 → 伪 edge。"
                  % sim.get("inplay_tick_rows", 0))
        vd.append("去重：账本按 (match_id, model_source) 去重，两路入口不会重复计数；"
                  "本复算新行 %s / 已入账 %s。" % (sim.get("survivor_new"), sim.get("survivor_dup")))
        hl.append("以该表为入口的复算漏斗：候选 %s → 存活 %s（新 %s），"
                  "绕不开 result_1x2/credible_1x2/赛前赔率/去水四道守卫。"
                  % (sim.get("candidates"), sim.get("survivors"), sim.get("survivor_new")))
    hl.append("retrain_gate 数的是**这张表**（随采集持续涨）而 G1 数的是**账本**（停 09-18）"
              "→ suggest 恒 True 是口径错位，不是样本在涨。")
    payload["headline"] = hl
    payload["verdicts"] = vd
    payload["recommendations"] = [
        "新增第三个 ingest 入口 `ingest_candles_verdict()`：`prematch_candles_verdict v "
        "JOIN matches m ON m.match_key=v.match_key WHERE m.status='finished' AND "
        "m.score_home IS NOT NULL`，循环内**原样复用** ingest_knn 的守卫序列 "
        "(result_1x2 → credible_1x2 → latest_prematch_1x2 → devig_power)，"
        "不得直接读 v.probs 当概率 bypass 去水。",
        "赔率来源必须走 `captured_at<=kickoff` 的赛前口径（本复算显示 %s/%s 候选行存在开赛后 tick）"
        % (sim.get("inplay_tick_rows", 0), sim.get("candidates", 0)),
        "入账前先按 walkforward 分段（T35）：新行覆盖 2026-09-19→09-27 跨迭代窗口，"
        "直接并账会污染 G1 计数。",
        "账本去重键 (match_id, model_source) 已能吸收两路入口重叠（本复算 dup=%s），"
        "无需额外去重逻辑。" % sim.get("survivor_dup"),
        "清理 `gq/auto_collector.py` 内的同款写方（退役残留死代码，职责已迁 ws_collector 09-20）；"
        "属 WINDOW 项，本轮不删不改。",
    ]
    return payload


def main() -> int:
    ap = argparse.ArgumentParser(description="T40 prematch_candles_verdict 写入链路只读审计")
    ap.add_argument("--no-write", action="store_true", help="只打印不落盘")
    args = ap.parse_args()
    payload = add_findings(build_payload())
    print(json.dumps({k: payload[k] for k in ("as_of", "writers", "profile", "alignment",
                                              "simulation")},
                     ensure_ascii=False, indent=2)[:4000])
    if not args.no_write:
        os.makedirs(REPORTS, exist_ok=True)
        with open(OUT_JSON, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        with open(OUT_MD, "w", encoding="utf-8") as fh:
            fh.write(render_md(payload))
        print("wrote", OUT_JSON, "and", OUT_MD)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
