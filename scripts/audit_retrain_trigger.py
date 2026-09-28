#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T36 retrain_gate 触发链路只读审计（承接 T32，把「评估」升级为「链路审计」）。

只读盘点四件事：
  1) retrain_gate 的触发条件源码与阈值常量位置（new_verdicts>=100 / drift>0.03）
  2) 该建议目前被谁消费（monitor 写 reports/monitor_status.json -> 谁读？）
  3) suggest 历史翻转时序（reports/monitor_history.jsonl）
  4) 若采纳重训，candles_ensemble 样本仅 181<2500，重训前后账本是否可比

铁律：纯只读。绝不触发重训 / 不碰模型文件 / 不碰生产调度 / 零 events.db 写入；
events.db 与 verification.db 一律 mode=ro 打开。
输出：reports/retrain_trigger_audit.{json,md}
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(ROOT, "reports")
MONITOR = os.path.join(ROOT, "scripts", "autonomous_monitor.py")
HISTORY = os.path.join(REPORTS, "monitor_history.jsonl")
MARKER = os.path.join(REPORTS, "last_candles_train_marker.json")
EVENTS_DB = os.path.join(ROOT, "data", "events.db")
LEDGER_DB = os.path.join(ROOT, "verification.db")
OUT_JSON = os.path.join(REPORTS, "retrain_trigger_audit.json")
OUT_MD = os.path.join(REPORTS, "retrain_trigger_audit.md")

CANDLES_SRC = "candles_ensemble"
G1_SAMPLE_GATE = 2500

SKIP_DIRS = {".git", "node_modules", "__pycache__", "archive", ".venv", "venv",
             "dist", "build", ".pytest_cache", "models", "logs", ".workbuddy"}
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".mp4",
            ".wav", ".mp3", ".zip", ".gz", ".xlsx", ".csv", ".parquet", ".joblib",
            ".pkl", ".so", ".dll", ".exe", ".pdf", ".map"}
SCAN_EXT = {".py", ".ts", ".tsx", ".js", ".jsx", ".md", ".json", ".jsonl", ".html",
            ".sh", ".yml"}
MAX_FILE_BYTES = 2_000_000

NEEDLE_GATE = "retrain_gate"
NEEDLE_STATUS = "monitor_status"
NEEDLE_MARKER = "last_candles_train_marker"


# --------------------------------------------------------------------------
# 1) 触发条件与阈值常量解析（纯文本，不 import 生产模块）
# --------------------------------------------------------------------------
def _def_block(src, name):
    """抽出 `def name(...)` 起的整个函数体文本；找不到返回 ''。"""
    m = re.search(r"^def\s+%s\s*\(" % re.escape(name), src, re.M)
    if not m:
        return ""
    # 行级切分：从 def 行首开始，遇到下一个顶格非空行即止（避免吞掉后续 def，
    # 否则 calibration_check 里的 drift 会被误判成门控内引用）
    lines = src.splitlines(keepends=True)
    start = m.start()
    head = ""
    acc = ""
    started = False
    for ln in lines:
        if not started:
            if src[start:start + len(ln)] == ln:
                started = True
                head = ln
            continue
        if ln.strip() and not ln[:1].isspace():
            break
        acc += ln
    return head + acc


def extract_gate_spec(src):
    """解析 autonomous_monitor.py 里 retrain_gate / 漂移阈值的真实实现。"""
    rg = _def_block(src, "retrain_gate")
    lt = _def_block(src, "_last_train_ts")
    tbl = re.search(r"FROM\s+([A-Za-z_][A-Za-z0-9_]*)", rg)
    thr = re.search(r"n_new\s*>=\s*(\d+)", rg)
    drift_thr = re.search(r"delta\s*>\s*([0-9]*\.?[0-9]+)", src)
    drift_ref = ("ll_delta" in rg) or ("drift" in rg)
    monitor_lines = src.splitlines()
    return {
        "gate_function": "retrain_gate()",
        "gate_line": next((i + 1 for i, ln in enumerate(monitor_lines)
                           if re.search(r"def\s+retrain_gate\s*\(", ln)), None),
        "gauge_table": tbl.group(1) if tbl else None,
        "threshold_new_verdicts": int(thr.group(1)) if thr else None,
        "threshold_syntax": ("n_new >= %s" % thr.group(1)) if thr else None,
        "marker_key": "ts" if "'ts'" in lt else None,
        "drift_threshold": float(drift_thr.group(1)) if drift_thr else None,
        "drift_wired_into_gate": drift_ref,
        "gauge_sql": " ".join(rg.split())[:180] if rg else "",
    }


# --------------------------------------------------------------------------
# 2) 消费方盘点（bounded walk，跳过大目录与二进制，绝不写任何文件）
# --------------------------------------------------------------------------
def _rel(fp, root):
    """统一正斜杠，保证 Windows 下 layer 前缀判定与测试断言一致。"""
    return os.path.relpath(fp, root).replace("\\", "/")


def _self_excluded(path_rel: str) -> bool:
    """本审计脚本自身的引用属自指（与 T33 同一坑），不计入消费方/写方。"""
    return "audit_retrain_trigger" in path_rel


def _classify(path_rel, line):
    p = path_rel.replace("\\", "/")
    low = line.lower()
    if p.startswith("frontend/") or p.startswith("deliverables/dashboard"):
        layer = "frontend"
    elif p.startswith(("scripts/", "tests/")):
        layer = "automation_or_test"
    elif p.endswith(".py") and (
            p.startswith(("core/", "pipeline/", "gq/", "data_collector/", "config/"))
            or os.path.basename(p).startswith("bridge_service")):
        layer = "backend"
    elif "/.workbuddy/memory/" in p:
        layer = "agent_memory"
    elif p.startswith("docs/") or p.startswith("deliverables/") or p.endswith(".md"):
        layer = "doc"
    else:
        layer = "other"
    if "json.dump" in low or "f.write" in low:
        access = "producer"
    elif "open(" in low or "json.load" in low or ".get(" in low:
        access = "consumer"
    else:
        access = "unknown"
    return {"layer": layer, "access": access}


def scan_needle(needle, root=ROOT):
    """在仓库内 grep needle（只读），返回 [{path,line,text,layer,access}]。"""
    hits = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() not in SCAN_EXT:
                continue
            fp = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(fp) > MAX_FILE_BYTES:
                    continue
                with open(fp, encoding="utf-8", errors="ignore") as f:
                    for i, ln in enumerate(f, 1):
                        if needle in ln:
                            rel = _rel(fp, root)
                            if _self_excluded(rel):
                                continue
                            hits.append(dict({"path": rel, "line": i,
                                              "text": ln.strip()[:180]},
                                             **_classify(rel, ln)))
            except OSError:
                continue
    return hits


def summarize_consumers(hits):
    by_layer, by_access = {}, {}
    for h in hits:
        by_layer[h["layer"]] = by_layer.get(h["layer"], 0) + 1
        by_access[h["access"]] = by_access.get(h["access"], 0) + 1
    runtime = [h for h in hits if h["layer"] in ("backend", "frontend")]
    seen, uniq = set(), []
    for h in hits:
        k = (h["path"], h["line"])
        if k not in seen:
            seen.add(k)
            uniq.append(h)
    return {"by_layer": by_layer, "by_access": by_access, "total_hits": len(uniq),
            "runtime_layer_hits": len(runtime), "runtime_consumers": runtime}


def find_marker_writers(root=ROOT):
    """marker 无写方 => 重训后 count 不会自愈复位。"""
    readers, writers = [], []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            fp = os.path.join(dirpath, fn)
            if os.path.getsize(fp) > MAX_FILE_BYTES:
                continue
            try:
                with open(fp, encoding="utf-8", errors="ignore") as f:
                    for i, ln in enumerate(f, 1):
                        if NEEDLE_MARKER in ln:
                            rel = _rel(fp, root)
                            if _self_excluded(rel):
                                continue
                            rec = {"path": rel, "line": i, "text": ln.strip()[:180]}
                            if re.search(r"'w'|json\.dump", ln):
                                writers.append(rec)
                            else:
                                readers.append(rec)
            except OSError:
                continue
    return {"marker": NEEDLE_MARKER, "reader_or_defs": readers, "writers": writers,
            "has_writer": bool(writers)}


# --------------------------------------------------------------------------
# 3) suggest 翻转时序
# --------------------------------------------------------------------------
def suggest_timeline(history_path=HISTORY):
    records, flips, prev = [], [], None
    try:
        with open(history_path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    d = json.loads(ln)
                except Exception:
                    continue
                rg = d.get("retrain_gate") or {}
                rec = {"cycle_at": d.get("cycle_at"),
                       "suggest": bool(rg.get("suggest")),
                       "new_verdicts": rg.get("new_verdicts_since_train"),
                       "ll_delta": ((d.get("calibration") or {}).get("drift") or {}).get(
                           "ll_delta_vs_market")}
                records.append(rec)
                if prev is not None and rec["suggest"] != prev:
                    flips.append({"from": prev, "to": rec["suggest"],
                                  "cycle_at": rec["cycle_at"],
                                  "new_verdicts_at_flip": rec["new_verdicts"]})
                prev = rec["suggest"]
    except OSError:
        pass
    true_streak = [r for r in records if r["suggest"]]
    # 当前这段 True 连续段的起点：最后一个 False 之后的那个周期（避免把 marker 落盘前的
    # 短暂 True 误当成"持续告警起点"）
    streak_start = None
    if true_streak:
        # 连续段起点 = 最后一个 False 之后那条 True（若全部 True 则从首条算起）
        last_false_idx = max((i for i, r in enumerate(records) if not r["suggest"]),
                             default=None)
        start_idx = (last_false_idx + 1) if last_false_idx is not None else 0
        if 0 <= start_idx < len(records):
            streak_start = records[start_idx]["cycle_at"]
    return {"n_cycles": len(records), "flips": flips,
            "first_true": true_streak[0]["cycle_at"] if true_streak else None,
            "current_streak_start": streak_start,
            "last": records[-1] if records else None}


# --------------------------------------------------------------------------
# 4) 只读量化：门控计数 vs 账本真实入账
# --------------------------------------------------------------------------
def _ro(db_path):
    return sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\", "/"), uri=True)


def read_gate_counters(marker_path=MARKER):
    out = {"marker_ts": None, "marker_note": None, "marker_mtime": None, "error": None}
    try:
        with open(marker_path, encoding="utf-8") as f:
            mk = json.load(f)
        out["marker_ts"] = mk.get("ts")
        out["marker_note"] = mk.get("note")
        out["marker_mtime"] = datetime.fromtimestamp(
            os.path.getmtime(marker_path)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception as e:  # marker 不可读不致命
        out["error"] = "marker_unreadable: %s" % e
        return out
    try:
        con = _ro(EVENTS_DB)
        ts = out["marker_ts"]
        out["verdict_total"] = con.execute(
            "SELECT COUNT(*) FROM prematch_candles_verdict").fetchone()[0]
        out["verdicts_since_train"] = con.execute(
            "SELECT COUNT(*) FROM prematch_candles_verdict WHERE captured_at > ?",
            (ts,)).fetchone()[0]
        con.close()
    except Exception as e:
        out["error"] = "%s | events_db: %s" % (out.get("error"), e)
    return out


def read_ledger_counters(src=CANDLES_SRC):
    out = {"model_source": src, "error": None}
    try:
        con = _ro(LEDGER_DB)
        out["ledger_rows"], out["ledger_max_match_date"], out["ledger_max_created_at"] = con.execute(
            "SELECT COUNT(*), MAX(match_date), MAX(created_at) FROM verification_ledger "
            "WHERE model_source=?", (src,)).fetchone()
        md = out.get("ledger_max_match_date") or ""
        if md:
            out["new_rows_after_last_match_date"] = con.execute(
                "SELECT COUNT(*) FROM verification_ledger WHERE model_source=? AND match_date > ?",
                (src, md)).fetchone()[0]
        con.close()
    except Exception as e:
        out["error"] = "ledger: %s" % e
        return out
    out["gap_to_g1"] = max(0, G1_SAMPLE_GATE - int(out.get("ledger_rows") or 0))
    return out


def decoupling_metrics(counters, ledger):
    """门控计数（生产判定行数）与账本入账（有效样本）是否同源可比。"""
    verdicts = int(counters.get("verdicts_since_train") or 0)
    new_ledger = int(ledger.get("new_rows_after_last_match_date") or 0)
    return {
        "verdicts_since_train": verdicts,
        "new_ledger_rows_after_last_sample": new_ledger,
        "decoupled": verdicts > 0 and new_ledger == 0,
        "note": "门控数的是 prematch_candles_verdict 生产判定行数，不是进账本的有效样本；"
                "两者同时为 0 / 同步增长才算口径同源。",
        "ledger_rows": int(ledger.get("ledger_rows") or 0),
        "gap_to_g1": int(ledger.get("gap_to_g1") or 0),
    }


# --------------------------------------------------------------------------
# 5) 结论与渲染
# --------------------------------------------------------------------------
def build_findings(spec, consumers, marker_info, timeline, deci, ledger):
    f = []
    if not spec.get("drift_wired_into_gate"):
        f.append({"id": "F1", "severity": "high",
                  "title": "漂移与门控未联动：ll_delta>0.03 只置 drift_alert，不进 suggest",
                  "detail": "autonomous_monitor.py:%s 的 retrain_gate() 只数 new_verdicts>=%s；"
                            "漂移阈值 %s 仅在 calibration_check() 里产出 drift_alert。"
                            "两个被当作「重训触发」的条件在代码层面互相独立，互不喂养。"
                            % (spec.get("gate_line"), spec.get("threshold_new_verdicts"),
                               spec.get("drift_threshold"))})
    if not marker_info.get("has_writer"):
        f.append({"id": "F2", "severity": "high",
                  "title": "marker 无写方：重训后 count 不会复位，suggest 永久 True",
                  "detail": "全仓 .py 中 %s 只有 %d 处定义/读取、%d 处写；即使真重训，"
                            "门控也不会自愈，须人工回写 ts。历史翻转也印证这点："
                            "唯一一次由 True 回落(False)发生在 marker 落盘那 39 秒内。"
                            % (NEEDLE_MARKER, len(marker_info.get("reader_or_defs") or []),
                               len(marker_info.get("writers") or []))})
    if deci.get("decoupled"):
        f.append({"id": "F3", "severity": "critical",
                  "title": "计数与账本解耦：重训解决不了样本缺口",
                  "detail": "门控用 %s 条新判定怂恿重训，但 candles_ensemble 账本在最后样本"
                            "(%s)之后新入账 %s 行，缺口仍 %s（G1=2500）。此时重训只是把同一批 "
                            "in-sample 重新拟合，账本不动 → G1 依旧 FAILED。"
                            % (deci.get("verdicts_since_train"),
                               ledger.get("ledger_max_match_date"),
                               deci.get("new_ledger_rows_after_last_sample"),
                               deci.get("gap_to_g1"))})
    if consumers.get("runtime_layer_hits", 0) == 0:
        f.append({"id": "F4", "severity": "medium",
                  "title": "门控无运行时消费者：纯提示，不接自动化/看板/告警",
                  "detail": "retrain_gate / monitor_status 的命中只落在 scripts/、tests/、"
                            "docs/ 与 .workbuddy/memory；backend 与 frontend 命中 0，"
                            "monitor 自身仅打一条 WARN 日志。无人执行 = 无副作用，也无闭环。"})
    streak = timeline.get("current_streak_start") or timeline.get("first_true")
    if streak:
        f.append({"id": "F5", "severity": "low",
                  "title": "suggest 自 %s 起连续 True（慢性状态，非突发告警）" % streak,
                  "detail": "历史 %s 个周期仅翻转 %s 次；越过阈值 %s 后单调增长、永不回落"
                            "（marker 无自动回写）。看板/报告引用它时应标注「已持续 N 天」，"
                            "否则读者会误当成刚发生的事件。"
                            % (timeline.get("n_cycles"), len(timeline.get("flips") or []),
                               spec.get("threshold_new_verdicts"))})
    return f


def build_report(spec, consumers, marker_info, timeline, counters, ledger, deci):
    return {
        "task": "T36 retrain_gate 触发链路只读审计",
        "as_of": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "redlines": ["只读：未触发重训 / 未碰模型文件 / 未碰生产调度 / 零 events.db 写入"],
        "gate_spec": spec,
        "consumers": consumers,
        "train_marker": marker_info,
        "timeline": timeline,
        "counters": counters,
        "ledger": ledger,
        "decoupling": deci,
        "findings": build_findings(spec, consumers, marker_info, timeline, deci, ledger),
        "recommendation": (
            "维持 suggest=True、本轮不重训：重训前须先修 ingest 门控（T31 C2 / T33 已机械证明"
            " daily_predictions.status 无人翻 finished），否则重训只是把同批样本重新拟合，"
            "G1(2500) 不会前进。要让门控恢复可信需同时改三处（均属生产代码，走 walkforward "
            "门禁 + 全量 pytest，本轮不落地）：① finalize 训练结束时自动回写 marker ts；"
            "② 门控计数改为按 ledger 新增行数而非 verdict 行数；"
            "③ 把 drift ll_delta 真正接进 suggest，或显式声明二者无关并在文档中统一口径。"),
    }


def render_md(rep):
    L = ["# T36 retrain_gate 触发链路只读审计", "",
         "- 时间：%s ｜ 红线：%s" % (rep["as_of"], "；".join(rep["redlines"])), ""]
    g = rep["gate_spec"]
    L += ["## 1 触发条件与阈值常量", "", "| 项 | 值 |", "|---|---|",
          "| 门控函数 | `scripts/autonomous_monitor.py:%s retrain_gate()` |"
          % g.get("gate_line"),
          "| 计数表 | `%s` |" % g.get("gauge_table"),
          "| 阈值 | `%s` |" % g.get("threshold_syntax"),
          "| drift 阈值 | `%s`（仅置 drift_alert） |" % g.get("drift_threshold"),
          "| drift 是否接进 suggest | **%s** |"
          % ("是" if g.get("drift_wired_into_gate") else "否"),
          "| 计数 SQL | `%s` |" % g.get("gauge_sql"), ""]
    c = rep["consumers"]
    L += ["## 2 消费方（谁读这个建议）", "",
          "- 去重命中 %s ｜ 按层 %s ｜ 按存取 %s" % (c["total_hits"], c["by_layer"], c["by_access"]),
          "- **运行时层命中（backend+frontend）= %s**" % c["runtime_layer_hits"], ""]
    for h in c["runtime_consumers"][:20]:
        L.append("  - %s:%s — %s" % (h["path"], h["line"], h["text"][:120]))
    L += ["", "## 3 marker（`%s`）写方" % rep["train_marker"]["marker"], "",
          "- 存在写方：**%s**（%d 处定义/读取 / %d 处写）"
          % (rep["train_marker"]["has_writer"],
             len(rep["train_marker"]["reader_or_defs"]),
             len(rep["train_marker"]["writers"])), ""]
    t = rep["timeline"]
    L += ["## 4 suggest 翻转时序", "", "- 周期数 %s ｜ 首次 True %s ｜ 末次快照 %s"
          % (t["n_cycles"], t["first_true"], (t.get("last") or {}).get("cycle_at")), ""]
    for fl in t.get("flips") or []:
        L.append("  - %s — suggest %s→%s（new_verdicts=%s）"
                 % (fl["cycle_at"], "True" if fl["from"] else "False",
                    "True" if fl["to"] else "False", fl["new_verdicts_at_flip"]))
    d, cc, lg = rep["decoupling"], rep["counters"], rep["ledger"]
    L += ["", "## 5 门控计数 vs 账本入账", "", "| 指标 | 值 |", "|---|---|",
          "| marker ts / 文件 mtime | %s / %s |" % (cc.get("marker_ts"), cc.get("marker_mtime")),
          "| verdict 总数 / 自上次重训 | %s / %s |" % (cc.get("verdict_total"),
                                                       cc.get("verdicts_since_train")),
          "| 账本 candles 行数 | %s |" % lg.get("ledger_rows"),
          "| 账本最后 match_date | %s |" % lg.get("ledger_max_match_date"),
          "| **最后样本之后新入账行** | **%s** |" % d.get("new_ledger_rows_after_last_sample"),
          "| G1(2500) 缺口 | %s |" % d.get("gap_to_g1"),
          "| 是否解耦 | **%s** |" % ("是" if d.get("decoupled") else "否"), ""]
    L.append("")
    L.append("## 6 发现")
    for f in rep["findings"]:
        L += ["### %s [%s] %s" % (f["id"], f["severity"], f["title"]), "", f["detail"], ""]
    L += ["## 7 建议", rep["recommendation"], ""]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-db", action="store_true", help="跳过 events.db / ledger 只读查询")
    args = ap.parse_args()

    monitor_src = ""
    try:
        with open(MONITOR, encoding="utf-8") as f:
            monitor_src = f.read()
    except OSError:
        pass
    spec = extract_gate_spec(monitor_src) if monitor_src else {}

    hits, seen = [], set()
    for needle in (NEEDLE_GATE, NEEDLE_STATUS):
        for h in scan_needle(needle):
            k = (h["path"], h["line"])
            if k not in seen:
                seen.add(k)
                hits.append(h)
    consumers = summarize_consumers(hits)

    marker_info = find_marker_writers()
    timeline = suggest_timeline()
    counters = read_gate_counters() if not args.no_db else {
        "marker_ts": None, "verdict_total": None, "verdicts_since_train": None}
    ledger = read_ledger_counters() if not args.no_db else {"model_source": CANDLES_SRC}
    deci = decoupling_metrics(counters, ledger)

    rep = build_report(spec, consumers, marker_info, timeline, counters, ledger, deci)
    rep["consuming_files"] = sorted({h["path"] for h in hits})
    os.makedirs(REPORTS, exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)
    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write(render_md(rep))
    print("[OK] %s" % OUT_JSON)
    print("[OK] %s" % OUT_MD)
    print("findings=%d  severity={%s}" % (len(rep["findings"]),
                                          ",".join(sorted({x["severity"]
                                                           for x in rep["findings"]}))))


if __name__ == "__main__":
    main()
