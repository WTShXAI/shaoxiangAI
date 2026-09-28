#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T38 「suggest 持续 True」被当新告警引用的污染面只读盘点（承接 T36 F5）。

背景：T36 实证 `retrain_gate.suggest` 自 2026-09-24 起连续 True（慢性状态，
marker 无写方 + 计数与账本解耦），远超阈值后单调增长、永不回落。但它已经被
多处以「当前」「本轮」这类现在时措辞复述：
  - `reports/monitor_status.json` 是**活文件**：值永远 true 且 mtime 一直刷新，
    读者无从判断它说了多久；
  - `reports/*.md|json` 把「当前 `retrain_gate.suggest=True`」写进推理前提
    （甚至写成"前提：开展一次重训"），却不带日期/持续时长；
  - `.workbuddy/memory/**` 用「本轮不重训」把它绑进单轮叙述，读起来像刚发生。

本脚本**只读**盘点这些引用，按「是否标注持续时长/日期」分类，并标出
「现在时 + 命令式（引导动作）」的高危引用。零生产 I/O：不重训、不碰调度、
零 events.db 写入；reports/ 只写本脚本自己的两份输出。

用法：
    python scripts/audit_suggest_badge.py            # 全量盘点
    python scripts/audit_suggest_badge.py --include-docs   # 额外扫 docs/
输出：reports/suggest_badge_audit.{json,md}
"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(ROOT, "reports")
MEMDIR = os.path.join(ROOT, ".workbuddy", "memory")
OUT_JSON = os.path.join(REPORTS, "suggest_badge_audit.json")
OUT_MD = os.path.join(REPORTS, "suggest_badge_audit.md")

LIVE_FILES = {"monitor_status.json", "monitor_history.jsonl"}   # 持续再写 = 活值
NEEDLE = "suggest"
# 「重训」上下文锚：只把与 retrain_gate/重训 相关的 suggest 计入，避免误抓无关措辞
CONTEXT_ANCHORS = ("retrain", "重训", "drift_alert", "ll_delta", "new_verdicts")
# 二阶污染链：T17 用 suggest 判定 INVALIDATED_BY_RETRAIN，89 份报告因此被标失效
DERIVED_NEEDLE = "INVALIDATED_BY_RETRAIN"

MAX_FILE_BYTES = 4_000_000
SCAN_EXT = {".md", ".json", ".jsonl", ".txt"}
SKIP_DIR_NAMES = {".git", "node_modules", "__pycache__", "archive", ".venv", "venv",
                  "dist", "build", ".pytest_cache", "models", "logs", "frontend",
                  "deliverables", "reports"}   # reports 单独精确扫（见 _candidate_files）

RE_DATE_ISO = re.compile(r"\d{4}-\d{2}-\d{2}")
RE_DATE_MD = re.compile(r"(?<!\d)\d{2}-\d{2}(?!\d)")
RE_DATE_CN = re.compile(r"\d{1,2}月\d{1,2}日")
RE_TS = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
RE_DURATION = re.compile(
    r"(持续|慢性|已\s*持续|自\s*\d{4}-\d{2}-\d{2}\s*起|自\s*09-|\d+\s*天|\d+\s*周期|"
    r"\d+\s*轮(?!次)|N\s*天|连续\s*[0-9]+)")
RE_PRESCRIPTIVE = re.compile(r"(应重训|须重训|先修|前提[:：]|建议重训|开展一次重训|"
                             r"补跑|入账|不补跑|NEVER_RATE_ZERO)")
RE_PRESENT_TENSE = re.compile(r"(当前|现\s|本轮|此刻|目前|最新|now)")


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _rel(fp, root=None):
    """统一正斜杠；root 默认当前 ROOT（测试可指向 tmp 目录，避免跨盘 relpath 报错）。"""
    try:
        return os.path.relpath(fp, root or ROOT).replace("\\", "/")
    except ValueError:      # 跨盘（测试 tmp 在 C:，仓库在 D:）时降级为文件名
        return os.path.basename(fp)


def _is_self(path_rel: str) -> bool:
    """审计脚本自身（及测试）引用属自指，不计入污染面（与 T33/T36 同一坑）。"""
    return ("audit_suggest_badge" in path_rel) or ("test_audit_suggest_badge" in path_rel)


def _read_lines(fp):
    try:
        if os.path.getsize(fp) > MAX_FILE_BYTES:
            return None
        with open(fp, encoding="utf-8", errors="ignore") as f:
            return f.read().splitlines()
    except OSError:
        return None


def _layer(path_rel: str) -> str:
    p = path_rel.replace("\\", "/")
    if p.startswith(".workbuddy/memory/"):
        return "agent_memory"
    if p.startswith("reports/"):
        return "report"
    if p.startswith("docs/"):
        return "doc"
    return "other"


def _source_kind(path_rel: str) -> str:
    p = path_rel.replace("\\", "/")
    if os.path.basename(p) in LIVE_FILES:
        return "LIVE_FILE"
    if _layer(p) == "agent_memory":
        return "MEMORY_NARRATIVE"
    return "FROZEN_REPORT"


# ---------------------------------------------------------------------------
# 1) 引用行抽取
# ---------------------------------------------------------------------------
def _relevant(line: str) -> bool:
    low = line.lower()
    if NEEDLE not in low:
        return False
    return any(a.lower() in low for a in CONTEXT_ANCHORS)


def _classify_ref(line: str) -> dict:
    return {
        "has_date": bool(RE_DATE_ISO.search(line) or RE_DATE_MD.search(line)
                         or RE_DATE_CN.search(line) or RE_TS.search(line)),
        "has_duration": bool(RE_DURATION.search(line)),
        "present_tense": bool(RE_PRESENT_TENSE.search(line)),
        "prescriptive": bool(RE_PRESCRIPTIVE.search(line)),
    }


def _risk(cls: dict) -> str:
    """无时长标注 + 现在时 + 引导动作 = 最易被读成刚发生事件的引用。"""
    if cls["prescriptive"] and not cls["has_duration"]:
        return "HIGH"
    if cls["present_tense"] and not (cls["has_duration"] or cls["has_date"]):
        return "MEDIUM"
    if not (cls["has_duration"] or cls["has_date"]):
        return "LOW"
    return "OK"


def _candidate_files(include_docs: bool, root: str = None):
    root = root or ROOT
    reports = REPORTS if os.path.isdir(REPORTS) else os.path.join(root, "reports")
    out = []
    # reports/ 精确扫（SKIP_DIR_NAMES 含 reports，这里显式加回）
    if os.path.isdir(reports):
        for name in sorted(os.listdir(reports)):
            fp = os.path.join(reports, name)
            if os.path.isfile(fp) and os.path.splitext(name)[1].lower() in SCAN_EXT:
                out.append(fp)
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES]
        for fn in files:
            if os.path.splitext(fn)[1].lower() not in SCAN_EXT:
                continue
            fp = os.path.join(base, fn)
            rel = _rel(fp, root)
            if _is_self(rel):
                continue
            if include_docs and rel.startswith("docs/"):
                out.append(fp)
                continue
            if rel.startswith(".workbuddy/memory/"):
                out.append(fp)
    return out


def scan_refs(include_docs: bool = False, root: str = None):
    root = root or ROOT
    refs, derived = [], []
    for fp in _candidate_files(include_docs, root):
        rel = _rel(fp)
        lines = _read_lines(fp)
        if lines is None:
            continue
        for i, ln in enumerate(lines, 1):
            if _relevant(ln):
                cls = _classify_ref(ln)
                refs.append(dict(path=rel, line=i, text=ln.strip()[:220], layer=_layer(rel),
                                 source_kind=_source_kind(rel), **cls, risk=_risk(cls)))
            elif DERIVED_NEEDLE in ln and RE_DATE_ISO.search(ln) is None:
                derived.append(dict(path=rel, line=i, text=ln.strip()[:220],
                                    has_date=bool(RE_DATE_ISO.search(ln))))
    seen, uniq = set(), []
    for r in refs:
        k = (r["path"], r["line"])
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    return uniq, derived


# ---------------------------------------------------------------------------
# 2) 真值：suggest 连续段起点（只读 monitor_history.jsonl）
# ---------------------------------------------------------------------------
def suggest_truth(history_path=None):
    history_path = history_path or os.path.join(REPORTS, "monitor_history.jsonl")
    out = {"history_file": _rel(history_path), "n_cycles": 0, "flips": 0,
           "streak_start": None, "first_true": None, "last_cycle_at": None,
           "last_suggest": None, "error": None}
    recs = []
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
                recs.append({"at": d.get("cycle_at"),
                             "suggest": bool(rg.get("suggest")),
                             "nv": rg.get("new_verdicts_since_train")})
    except OSError as e:
        out["error"] = str(e)
        return out
    if not recs:
        out["error"] = "no_cycles"
        return out
    out["n_cycles"] = len(recs)
    out["flips"] = sum(1 for a, b in zip(recs, recs[1:]) if a["suggest"] != b["suggest"])
    trues = [r for r in recs if r["suggest"]]
    out["first_true"] = trues[0]["at"] if trues else None
    out["last_cycle_at"] = recs[-1]["at"]
    out["last_suggest"] = recs[-1]["suggest"]
    if trues:
        last_false = max((i for i, r in enumerate(recs) if not r["suggest"]), default=None)
        start = (last_false + 1) if last_false is not None else 0
        if start < len(recs):
            out["streak_start"] = recs[start]["at"]
    return out


def _parse_cycle_at(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    return None


def duration_days(truth, as_of=None):
    as_of = as_of or datetime.now()
    start_raw = truth.get("streak_start") or truth.get("first_true")
    a = _parse_cycle_at(start_raw)
    if a is None:
        return None
    return round((as_of - a).total_seconds() / 86400.0, 2)


def read_live_status(status_path=None):
    status_path = status_path or os.path.join(REPORTS, "monitor_status.json")
    out = {"file": _rel(status_path), "exists": os.path.exists(status_path),
           "mtime": None, "retrain_gate": None, "error": None}
    if not out["exists"]:
        return out
    out["mtime"] = datetime.fromtimestamp(os.path.getmtime(status_path)).strftime(
        "%Y-%m-%d %H:%M:%S")
    try:
        with open(status_path, encoding="utf-8") as f:
            d = json.load(f)
        out["retrain_gate"] = d.get("retrain_gate")
    except Exception as e:
        out["error"] = str(e)
    return out


# ---------------------------------------------------------------------------
# 3) 报告与渲染
# ---------------------------------------------------------------------------
def summarize(refs):
    by_risk, by_layer, by_kind = {}, {}, {}
    for r in refs:
        by_risk[r["risk"]] = by_risk.get(r["risk"], 0) + 1
        by_layer[r["layer"]] = by_layer.get(r["layer"], 0) + 1
        by_kind[r["source_kind"]] = by_kind.get(r["source_kind"], 0) + 1
    high = [r for r in refs if r["risk"] == "HIGH"]
    return {"total": len(refs), "by_risk": by_risk, "by_layer": by_layer,
            "by_source_kind": by_kind, "high_risk": high,
            "high_risk_files": sorted({r["path"] for r in high})}


def build_findings(refs, summ, truth, dur, live):
    f = []
    n_high = summ["by_risk"].get("HIGH", 0)
    f.append({"id": "F1", "severity": "high" if n_high else "medium",
              "title": "%d 处引用以「当前/本轮」现在时复述一个已持续 %.1f 天的慢性状态"
                       % (n_high, dur if dur is not None else -1),
              "detail": "report/memory 中共 %s 处 retrain_gate.suggest 引用，其中 HIGH %d / "
                        "MEDIUM %d / LOW %d。连续段起点 %s，as_of 已持续 %s 天，历史 %s 周期"
                        "仅翻转 %s 次（marker 无写方 → 永不回落）。不带「已持续 N 天/起于 X」"
                        "的现在时引用会把慢性状态读成刚发生的事件。"
                        % (summ["total"], n_high, summ["by_risk"].get("MEDIUM", 0),
                           summ["by_risk"].get("LOW", 0),
                           truth.get("streak_start") or truth.get("first_true"), dur,
                           truth.get("n_cycles"), truth.get("flips"))})
    live_refs = [r for r in refs if r["source_kind"] == "LIVE_FILE"]
    if live_refs:
        f.append({"id": "F2", "severity": "medium",
                  "title": "活文件 %s 里的 suggest 会被反复复述，且永不自然过期"
                           % ", ".join(sorted({r["path"] for r in live_refs})),
                  "detail": "monitor_status.json 每次周期重写（当前 mtime %s），值为 "
                            "`retrain_gate.suggest=%s`。它既不带「自 X 起」，文件 mtime 又随"
                            "周期刷新，看不出陈旧 → 下游任何引用都无法判断时效。"
                            % (live.get("mtime"),
                               (live.get("retrain_gate") or {}).get("suggest"))})
    presc = [r for r in refs if r["prescriptive"] and r["risk"] == "HIGH"]
    if presc:
        f.append({"id": "F3", "severity": "high",
                  "title": "%d 处把 suggest=True 写成「前提：开展一次重训」类的动作前置"
                           % len(presc),
                  "detail": "这些引用不仅是描述，还把慢性 flag 当成动作触发条件："
                            + " ; ".join("%s:%s" % (r["path"], r["line"]) for r in presc[:6])
                             + "。而 T36 已证重训不会推进 G1（账本最后样本后新入账 0 行）→ "
                              "以它为前置的推演结论（如模型世代分段）同样失效。"})
    f.append({"id": "F4", "severity": "low",
              "title": "二阶污染链：INVALIDATED_BY_RETRAIN 判定式让 suggest 间接作废 89 份报告",
              "detail": "T17 的失效分类把 `suggest` 与 `drift_alert` 并列为真值判定式，"
                        "导致 89 份模型评估报告被标 INVALIDATED_BY_RETRAIN；这个分类又被"
                        " T24/T29 反复引用。suggest 若永不回落，这 89 份也不因时间自动复活。"
                        "本轮已把引用行单独抽出（见 derived_refs），仅盘点不修改判定式。"})
    return f


def render_md(rep):
    L = ["# T38 「suggest 持续 True」引用污染面只读盘点", "",
         "- 时间：%s ｜ 红线：只读、不重训、不碰调度、零 events.db 写入" % rep["as_of"], ""]
    t, s, lv = rep["truth"], rep["summary"], rep["live_status"]
    L += ["## 1 真值（慢性状态的起点与时长）", "", "| 项 | 值 |", "|---|---|",
          "| 连续段起点 | %s |" % (t.get("streak_start") or t.get("first_true")),
          "| as_of 已持续 | **%s 天** |" % rep["duration_days"],
          "| 历史周期数 / 翻转次数 | %s / %s |" % (t.get("n_cycles"), t.get("flips")),
          "| 末次周期 | %s |" % t.get("last_cycle_at"),
          "| 活文件 suggest | %s |" % ((lv.get("retrain_gate") or {}).get("suggest")),
          "| 活文件 mtime | %s |" % lv.get("mtime"), ""]
    L += ["## 2 引用统计", "",
          "- 总引用 %s ｜ 按风险 %s ｜ 按层 %s ｜ 按来源 %s"
          % (s["total"], s["by_risk"], s["by_layer"], s["by_source_kind"]), ""]
    if s["high_risk_files"]:
        L.append("### 2.1 HIGH 高危引用文件")
        for r in [x for x in rep["refs"] if x["risk"] == "HIGH"]:
            L.append("- `%s:%s` [%s/%s] — %s"
                     % (r["path"], r["line"], r["source_kind"],
                        "现在时" if r["present_tense"] else "陈述", r["text"][:150]))
        L.append("")
    L += ["## 3 发现"]
    for f in rep["findings"]:
        L += ["### %s [%s] %s" % (f["id"], f["severity"], f["title"]), "", f["detail"], ""]
    L += ["## 4 建议（仅建议，本轮不改任何生产文件）", rep["recommendation"], ""]
    L += ["## 5 全量引用清单"] if rep["refs"] else ["## 5 全量引用清单", ""]
    for r in rep["refs"][:80]:
        L.append("- `%s:%s` [%s|%s|date=%s|dur=%s|presc=%s] — %s"
                 % (r["path"], r["line"], r["risk"], r["source_kind"], r["has_date"],
                    r["has_duration"], r["prescriptive"], r["text"][:120]))
    return "\n".join(L) + "\n"


def build_report(refs, derived, truth, live, include_docs):
    summ = summarize(refs)
    rep = {
        "task": "T38 retrain_gate.suggest 引用污染面只读盘点",
        "as_of": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "scope": {"include_docs": include_docs,
                  "scanned": ["reports/*.{md,json,jsonl}", ".workbuddy/memory/**.md"]},
        "redlines": ["只读：不重训 / 不碰调度 / 不改引用处 / 零 events.db 写入"],
        "truth": truth,
        "live_status": live,
        "duration_days": duration_days(truth),
        "summary": {k: v for k, v in summ.items() if k != "high_risk"},
        "refs": refs,
        "derived_refs": derived,
    }
    rep["findings"] = build_findings(refs, summ, truth, rep["duration_days"], live)
    rep["recommendation"] = (
        "① 在 monitor 输出里给 retrain_gate 加 `since` + `days_true` 字段（生产代码改动，"
        "走 walkforward + 全量 pytest，本轮不落地），让任何下游引用都能自证时效；"
        "② reports/ 与 memory 里引用该 flag 时统一写成「suggest=True（自 2026-09-24 起持续 "
        "N 天，marker 无写方，永不自动回落）」，禁止裸写「当前 suggest=True」；"
        "③ T17 的 INVALIDATED_BY_RETRAIN 判定式应从「suggest 或 drift_alert」收回「suggest」"
        "（T37 R3 已提出），否则 89 份报告不会因时间自动复活；"
        "④ 在 DISCIPLINE.md §6 补一条「慢性状态不得当突发告警引用」的措辞纪律。")
    return rep


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--include-docs", action="store_true",
                    help="额外扫 docs/（默认只扫 reports/ 与 .workbuddy/memory/）")
    args = ap.parse_args()
    refs, derived = scan_refs(args.include_docs)
    rep = build_report(refs, derived, suggest_truth(), read_live_status(), args.include_docs)
    os.makedirs(REPORTS, exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)
    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write(render_md(rep))
    s = rep["summary"]
    print("[OK] %s" % OUT_JSON)
    print("[OK] %s" % OUT_MD)
    print("refs=%d risk=%s high_files=%d duration=%s days"
          % (s["total"], s["by_risk"], len(s["high_risk_files"]), rep["duration_days"]))


if __name__ == "__main__":
    main()
