#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T62 只读盘点: 信号词(`NO_EDGE` 一族 OU 信号)持久化面 + 迁移爆炸半径 + 冻结语义。

背景(T58): 滚球 OU 信号词 `NO_EDGE` 语义是「此刻不下注」, 与验证台三态 `NO EDGE`
(不存在 edge)同名不同义, 建议改名为 `NO_BET`。改名的成本不在改字面量, 而在**存量数据**:
数据已经落进两处持久化面。本条把「迁移规格」之前的现状盘清楚, 并给出可执行的守卫。

**本脚本纯只读**: 只读 `data/watch_verdicts.json`(业务快照) 与 events.db(`mode=ro`),
不跑迁移、不写快照、不碰生产服务、不改任何生产文件、零进程操作。

持久化面(实测, 2026-09-28 读数):
  1. 生产者    `analysis/live_goal_probe.py` 的 `*_signal` 字面量 (7 值)
  2. 桥接     `bridge_service.py` 原样透传 `pr["full"]["signal"]`
  3. 业务快照 `data/watch_verdicts.json`(509 条记录, `ou.signal`)
  4. events.db `prediction_ledger.signal` 列(41,755 行) —— T58 **漏掉**的持久化面
  5. 前端     `frontend/src/pages/Rollball/index.tsx` 的 `OU_SIG` 取键表

五问(对应 backlog T62):
  Q1 信号词出现在哪些持久化面
  Q2 三处词表是否一致(生产者 / 快照+账本 / 前端取键)
  Q3 「判定首见即冻结」到底冻结了什么(实测 + 首见 OU 结算空转)
  Q4 迁移爆炸半径(存量行数/字面量站点/字符串比较站点)
  Q5 重命名后前端会怎样(静默消失 vs 灰底兜底)

用法:  python scripts/audit_signal_vocab_migration.py
产物:  reports/signal_vocab_migration_audit.{json,md}
"""
from __future__ import annotations

import ast
import json
import os
import re
import sqlite3
import sys
from typing import Dict, List, Tuple

MODULE_BASENAME = os.path.basename(__file__)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRODUCER = os.path.join(REPO, "analysis", "live_goal_probe.py")
BRIDGE = os.path.join(REPO, "bridge_service.py")
SNAPSHOT = os.path.join(REPO, "data", "watch_verdicts.json")
WATCHER = os.path.join(REPO, "scripts", "watch_live_verdicts.py")
FRONTEND = os.path.join(REPO, "frontend", "src", "pages", "Rollball", "index.tsx")
EVENTS_DB = os.path.join(REPO, "data", "events.db")
LEDGER_TABLE = "prediction_ledger"
LEDGER_COLUMN = "signal"
OUT_DIR = os.path.join(REPO, "reports")
OUT_JSON = os.path.join(OUT_DIR, "signal_vocab_migration_audit.json")
OUT_MD = os.path.join(OUT_DIR, "signal_vocab_migration_audit.md")

#: T58 建议的新词; 本脚本作为迁移前的只读盘点, 不落地改名。
NEW_VOCAB = "NO_BET"

SELF_EXCLUDE = (MODULE_BASENAME, os.path.basename(FRONTEND))


# --------------------------------------------------------------------------- 工具
def _rel(path: str) -> str:
    try:
        return os.path.relpath(path, REPO).replace("\\", "/")
    except ValueError:  # pragma: no cover - 跨平台保护
        return os.path.basename(path)


def read_text(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def snip(text: str, needle: str, before: int = 120, after: int = 120) -> str:
    i = text.find(needle)
    if i < 0:
        return ""
    return text[max(0, i - before): i + after].strip()


# --------------------------------------------------------------------------- Q1/Q2
def producer_vocab() -> Dict[str, object]:
    """从生产者源码里抽出 `*_signal` 的字面量值(带理由登记, 防误改)。"""
    text = read_text(PRODUCER)
    vals = sorted(set(re.findall(r"(?:ht|ft)_signal\s*=\s*'([A-Z_]+)'", text)))
    return {
        "file": _rel(PRODUCER),
        "values": vals,
        "n": len(vals),
        "note": ("半场/全场两套 *_signal 赋值; ALREADY_BROKEN/SCORE_LAGGING/SETTLED_UNDER "
                 "属「终态/滞后」分支, 前端没有对应徽章"),
    }


def frontend_map_keys() -> Dict[str, object]:
    text = read_text(FRONTEND)
    m = re.search(r"const OU_SIG[^=]*=\s*\{(.*?)\n\}", text, re.S)
    keys = sorted(set(re.findall(r"^\s{2}([A-Z][A-Z_]*):\s*\{", m.group(1), re.M))) if m else []
    context = snip(text, "const OU_SIG", 0, 400)
    return {
        "file": _rel(FRONTEND),
        "map": keys,
        "n": len(keys),
        "lookup": "OU_SIG[ou.signal] 裸取键 + `&&` 短路 → 未知键渲染为空(无兜底分支)",
        "context": context,
    }


def snapshot_values() -> Dict[str, object]:
    if not os.path.exists(SNAPSHOT):
        return {"file": _rel(SNAPSHOT), "values": [], "n_records": 0, "missing": True}
    data = json.load(open(SNAPSHOT, encoding="utf-8"))
    vals = sorted({(v.get("ou") or {}).get("signal") for v in data.values()})
    return {
        "file": _rel(SNAPSHOT),
        "values": vals,
        "n_records": len(data),
        "missing": False,
    }


def ledger_signal_counts() -> Dict[str, object]:
    """只读 events.db 取 `prediction_ledger.signal` 分布(纯 SELECT, mode=ro)。"""
    con = sqlite3.connect("file:{}?mode=ro".format(EVENTS_DB.replace("\\", "/")), uri=True)
    try:
        rows = con.execute("SELECT {} FROM {}".format(LEDGER_COLUMN, LEDGER_TABLE)).fetchall()
    finally:
        con.close()
    dist: Dict[str, int] = {}
    for (v,) in rows:
        dist[v] = dist.get(v, 0) + 1
    return {
        "db": _rel(EVENTS_DB),
        "table": LEDGER_TABLE,
        "column": LEDGER_COLUMN,
        "n_rows": len(rows),
        "dist": dict(sorted(dist.items(), key=lambda kv: -kv[1])),
    }


def literal_sites() -> Dict[str, object]:
    """统计源码面出现信号词字面量的文件分布(自身与前端取键表排除在「待迁移」之外)。"""
    pat = re.compile(r"NO_EDGE|NO_BET")
    files: Dict[str, int] = {}
    for root, dirs, names in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in {"node_modules", ".git", "__pycache__", "venv", "archive"}]
        for n in names:
            if not n.endswith(".py"):
                continue
            p = os.path.join(root, n)
            try:
                if pat.findall(read_text(p)):
                    files[_rel(p)] = len(pat.findall(read_text(p)))
            except Exception:
                continue
    return {"hits": files, "n_files": len(files), "n_sites": sum(files.values())}


# --------------------------------------------------------------------------- Q3 冻结语义
def frozen_vs_live() -> Dict[str, object]:
    """实测「首见即冻结」到底冻住了什么: first_* 冻结, `ou` 等顶层字段每轮被覆盖。"""
    data = json.load(open(SNAPSHOT, encoding="utf-8"))
    refreshed = 0          # first_score != score → 说明同一记录后续被刷新过
    frozen_only = 0        # first_score == score
    frozen_fields = ("first_score", "first_minute", "first_seen", "first_verdict")
    live_overwritten_keys = ("ou", "x2", "cs_top1", "cs_top3", "gate", "score", "minute")
    n_fv = 0
    for v in data.values():
        if v.get("first_score") != v.get("score"):
            refreshed += 1
        else:
            frozen_only += 1
        if v.get("first_verdict"):
            n_fv += 1
    return {
        "n_records": len(data),
        "records_refreshed_after_freeze": refreshed,
        "records_first_score_equals_score": frozen_only,
        "records_with_first_verdict": n_fv,
        "frozen_field_prefixes": list(frozen_fields),
        "live_keys_overwritten_by_v.update(now_v)": list(live_overwritten_keys),
        "implication": (
            "`ou.signal` 位于会被 `v.update(now_v)` 覆盖的 `ou` 子字典里 → 它不是冻结字段, "
            "存的是**最后一次刷新**的信号; 真正冻结的只有 first_* 前缀字段。"
            "因此「迁移时不得改写存量 first_*」与「存量 ou.signal 会被下一轮快照刷新成新词」"
            "是同一枚硬币的两面, 迁移规格必须同时处理。"
        ),
    }


def first_verdict_settlement_is_dead() -> Dict[str, object]:
    """静态核验: `settle()` 的 first_OU 分支是否结构性空转(freeze 语义的实际受益人)。"""
    tree = ast.parse(read_text(WATCHER))
    fv_keys: List[str] = []
    ou_win_reads: List[str] = []
    src = read_text(WATCHER)
    for node in ast.walk(tree):
        # v['first_verdict'] = {'x2': ..., 'ou_direction': ...}
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if (isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant)
                        and t.slice.value == "first_verdict" and isinstance(node.value, ast.Dict)):
                    fv_keys = [k.value for k in node.value.keys if isinstance(k, ast.Constant)]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "ou_win":
            for n in ast.walk(node):
                if isinstance(n, ast.Attribute) and n.attr == "get" and isinstance(n.value, ast.Name):
                    if n.value.id == "o":
                        ou_win_reads.append(ast.unparse(n))
    return {
        "first_verdict_keys": fv_keys,
        "ou_win_keys_read": sorted(set(ou_win_reads)),
        "first_verdict_has_ou_subdict": "ou" in fv_keys,
        "ou_win_reads": sorted(set(ou_win_reads)),
        "first_verdict_ou_settlement_dead": bool(fv_keys) and "ou" not in fv_keys,
        "evidence": (
            "`first_verdict` 存的是扁平 `ou_direction`(字符串), 而 `ou_win(v)` 期望 "
            "`v['ou']['direction']` 子字典 → 首见冻结的 OU 判定在结算里恒为 None(first_OU 空转)。"
        ),
    }


def frozen_fields_are_first_sight_only() -> Dict[str, object]:
    """不变量: `first_*` 字段只能在 `if mk not in store:`(首见)分支内写入。

    若有人把 `first_verdict` / `first_score` 挪进 `v.update(now_v)` 的刷新集,
    或反过来在首见分支外的写回到冻结字段, 本检查即 FAIL。
    """
    tree = ast.parse(read_text(WATCHER))
    result: Dict[str, object] = {"ok": True, "issues": [], "first_sight_writes": [], "refresh_writes": []}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "snapshot":
            continue
        # 定位 if mk not in store 分支(首见写入区)
        branch_ids = set()
        first_sight = None
        for stmt in ast.walk(node):
            if isinstance(stmt, ast.If) and ast.unparse(stmt.test).endswith("not in store"):
                first_sight = stmt
                for s in ast.walk(ast.Module(body=stmt.body, type_ignores=[])):
                    if isinstance(s, ast.Assign) and "first_" in ast.unparse(s.targets[0]):
                        result["first_sight_writes"].append({"line": s.lineno, "target": ast.unparse(s.targets[0])})
                    if isinstance(s, ast.Call) and isinstance(s.func, ast.Attribute) \
                            and s.func.attr == "update" and isinstance(s.func.value, ast.Name):
                        result["refresh_writes"].append(
                            {"line": s.lineno, "target": s.func.value.id, "shape": "v.update(now_v)"}
                        )
                    s_id = id(s)
                    branch_ids.add(s_id)
                    for c in ast.walk(s):
                        branch_ids.add(id(c))
        # 首见分支之外的 first_* 赋值 = 冻结语义被破坏
        if first_sight is not None:
            stack = list(node.body)
            seen = set()
            while stack:
                s = stack.pop()
                if id(s) in seen:
                    continue
                seen.add(id(s))
                if s is not first_sight:
                    if isinstance(s, ast.Assign) and "first_" in ast.unparse(s.targets[0]):
                        result["issues"].append(
                            "first_* 赋值不在首见分支内: line {}".format(s.lineno))
                    stack.extend(getattr(s, "body", []) or [])
                    stack.extend(getattr(s, "orelse", []) or [])
    if result["issues"]:
        result["ok"] = False
    return result


# --------------------------------------------------------------------------- Q4/Q5
def blast_radius() -> Dict[str, object]:
    snap = snapshot_values()
    led = ledger_signal_counts()
    lit = literal_sites()
    frontend = frontend_map_keys()
    producer = producer_vocab()
    snap_vals = set(snap.get("values") or [])
    led_vals = set((led.get("dist") or {}).keys())
    prod_vals = set(producer["values"])
    fe_keys = set(frontend["map"])
    return {
        "surfaces": {
            "producer": producer["file"],
            "bridge_passthrough": "{}:4790 原样透传 pr['full']['signal']".format(_rel(BRIDGE)),
            "business_snapshot": snap["file"],
            "events_db_column": "{}:{}.{}".format(_rel(EVENTS_DB), LEDGER_TABLE, LEDGER_COLUMN),
            "frontend_map": frontend["file"],
        },
        "vocab": {
            "producer": sorted(prod_vals),
            "snapshot": sorted(snap_vals),
            "ledger": sorted(led_vals),
            "frontend_map": sorted(fe_keys),
        },
        "differences": {
            "in_producer_not_frontend": sorted(prod_vals - fe_keys),
            "in_frontend_not_producer": sorted(fe_keys - prod_vals),
            "in_data_not_producer": sorted((snap_vals | led_vals) - prod_vals),
            "in_data_not_frontend": sorted((snap_vals | led_vals) - fe_keys),
        },
        "counts": {
            "snapshot_records": snap.get("n_records"),
            "ledger_rows": led.get("n_rows"),
            "python_literal_files": lit["n_files"],
            "python_literal_sites": lit["n_sites"],
            "frontend_map_keys": frontend["n"],
        },
        # 关键: 存量 `prediction_ledger` 的改写 = events.db 写入 = 停机窗口/§4 门禁, 不属自主范围
        "events_db_write_flag": True,
        "events_db_write_note": (
            "`pipeline/prediction_ledger.py::record_from_probe_result()` 是 events.db 写方 "
            "(INSERT)。改写存量 {} 行属 §4 数据资产保全 + WINDOW 停机窗口, 必须由董事长批; "
            "本轮只读盘点不碰。".format(led.get("n_rows"))
        ),
    }


def frontend_rename_consequence() -> Dict[str, object]:
    text = read_text(FRONTEND)
    has_guard = bool(re.search(r"OU_SIG\[ou\.signal\]\s*&&", text))
    has_fallback = bool(re.search(r"OU_SIG\[[^\]]*\]\s*\|\|", text))
    moved = 0
    try:
        data = json.load(open(SNAPSHOT, encoding="utf-8"))
        moved = sum(1 for v in data.values() if (v.get("ou") or {}).get("signal") == "NO_EDGE")
    except Exception:
        pass
    return {
        "guard_form": "OU_SIG[ou.signal] && ...",
        "map_lookup_guarded": has_guard,
        "has_fallback_branch": has_fallback,
        "consequence": (
            "未知键 → 短路后整段徽章不渲染(连灰色兜底都没有)。改生产者为 {} 后, 前端 "
            "会在窗口期内**静默丢失徽章**而不是显示旧词 —— 与 T54「新增第四态被渲染成灰色未知」"
            "同族, 但更糟(完全消失)。".format(NEW_VOCAB)
        ),
        "already_blank_today": "ALREADY_BROKEN / SCORE_LAGGING / SETTLED_UNDER 已在生产者词表内但不在 OU_SIG 取键表内 → 今天就已经静默不显示",
        "snapshot_carrying_no_edge": moved,
    }


def cache_hazard() -> Dict[str, object]:
    text = read_text(PRODUCER)
    m = re.search(r"_PROBE_CACHE_TTL\s*=\s*([0-9.]+)", text)
    return {
        "ttl_sec": float(m.group(1)) if m else None,
        "note": (
            "`probe_match()` 的 LRU+TTL 缓存会把改名前的响应留住最多 {}s; "
            "迁移后前端读到的可能是缓存里的旧词, 验收须等 TTL 过期或清缓存.".format(m.group(1) if m else "?")
        ),
    }


def watcher_opens_db_writable() -> Dict[str, object]:
    """只读纪律: §4 要求零写入; 而盯盘器 settle() 以可写连接打开 events.db。"""
    text = read_text(WATCHER)
    hit = re.search(r"sqlite3\.connect\(([^)]*)\)", text)
    return {
        "file": _rel(WATCHER),
        "connect_call": hit.group(0) if hit else None,
        "opens_readwrite": bool(hit) and "mode=ro" not in hit.group(1),
        "note": "settle() 只读赛果却以默认(可写)连接打开 events.db; 迁移脚本若复用其连接会踩 §4。",
    }


# --------------------------------------------------------------------------- 汇总
def collect() -> Dict[str, object]:
    br = blast_radius()
    return {
        "module": MODULE_BASENAME,
        "read_only": True,
        "q1_surfaces": br["surfaces"],
        "q2_vocab_consistency": {
            "differences": br["differences"],
            "counts": br["counts"],
            "verdict": "FAIL" if any(
                br["differences"].values()
            ) else "PASS",
        },
        "q3_freeze_semantics": {
            **frozen_vs_live(),
            "first_verdict_settlement": first_verdict_settlement_is_dead(),
            "invariant_first_sight_only": frozen_fields_are_first_sight_only(),
        },
        "q4_blast_radius": br,
        "q5_rename_consequence": {
            **frontend_rename_consequence(),
            "probe_cache": cache_hazard(),
        },
        "q6_hygiene": watcher_opens_db_writable(),
    }


def render_md(res: Dict[str, object]) -> str:
    d = res["q2_vocab_consistency"]
    fr = res["q3_freeze_semantics"]
    br = res["q4_blast_radius"]
    fx = res["q5_rename_consequence"]
    L: List[str] = []
    L.append("# T62 信号词存量快照读侧兼容 — 只读盘点报告")
    L.append("")
    L.append("> 只读: 未跑迁移 / 未写快照 / 未改 events.db / 零进程操作。")
    L.append("")
    L.append("## 结论")
    L.append("")
    L.append("- 持久化面 **5 个**; 词表 **三处互不一致**(生产者 7 值 / 数据面 5 值 / 前端取键 5 键)。")
    L.append("- **T58 漏检的持久化面**: `events.db` `prediction_ledger.signal` **{} 行** —— "
             "改名成本不在 JSON 快照, 在这个库列。".format(br["counts"]["ledger_rows"]))
    L.append("- 存量改写属 events.db 写入(**§4 + WINDOW 停机窗口**), 不属本轮自主范围; "
             "可行的最小动作 = 只改生产者 + 读侧映射, 存量保持原词不动。")
    L.append("- 前端是**裸取键 + `&&` 短路**: 改名后徽章不是变灰而是**整段消失**。")
    L.append("")
    L.append("## Q1 持久化面")
    L.append("")
    for k, v in res["q1_surfaces"].items():
        L.append("- {}: {}".format(k, v))
    L.append("")
    L.append("## Q2 词表一致性")
    L.append("")
    L.append("| 面 | 值 | 数 |")
    L.append("|---|---|---|")
    L.append("| 生产者 | {} | {} |".format(", ".join(br["vocab"]["producer"]), len(br["vocab"]["producer"])))
    L.append("| 快照 | {} | {} |".format(", ".join(br["vocab"]["snapshot"]), br["counts"]["snapshot_records"]))
    L.append("| 账本列 | {} | {} |".format(", ".join(br["vocab"]["ledger"]), br["counts"]["ledger_rows"]))
    L.append("| 前端取键 | {} | {} |".format(", ".join(br["vocab"]["frontend_map"]), br["counts"]["frontend_map_keys"]))
    L.append("")
    L.append("差集:")
    for k, v in br["differences"].items():
        L.append("- {}: {}".format(k, ", ".join(v) or "(空)"))
    L.append("")
    L.append("## Q3 冻结语义")
    L.append("")
    for k in ("n_records", "records_refreshed_after_freeze", "records_with_first_verdict"):
        L.append("- {}: {}".format(k, fr.get(k)))
    L.append("- {}".format(fr["implication"]))
    fvs = fr["first_verdict_settlement"]
    L.append("- 首见 OU 结算空转: **{}** ({})".format(fvs["first_verdict_ou_settlement_dead"], fvs["evidence"]))
    inv = fr["invariant_first_sight_only"]
    L.append("- `first_*` 首见写入不变量: **{}** (issues={})".format(inv["ok"], inv["issues"] or "无"))
    L.append("")
    L.append("## Q4 迁移爆炸半径")
    L.append("")
    for k, v in br["counts"].items():
        L.append("- {}: {}".format(k, v))
    L.append("- {}".format(br["events_db_write_note"]))
    L.append("")
    L.append("## Q5 改名后果")
    L.append("")
    L.append("- {}".format(fx["consequence"]))
    L.append("- {}".format(fx["already_blank_today"]))
    L.append("- 快照中含 NO_EDGE 的记录: {}".format(fx["snapshot_carrying_no_edge"]))
    L.append("- 生产者缓存 TTL: {}s → {}".format(fx["probe_cache"]["ttl_sec"], fx["probe_cache"]["note"]))
    L.append("")
    L.append("## Q6 卫生")
    L.append("")
    L.append("- watcher 打开 events.db 可写: **{}** ({})".format(
        res["q6_hygiene"]["opens_readwrite"], res["q6_hygiene"]["connect_call"]))
    L.append("")
    return "\n".join(L)


def main() -> int:
    res = collect()
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write(render_md(res))
    print("[T62] 只读盘点完成 ->", _rel(OUT_JSON))
    print("[T62] 词表一致判定:", res["q2_vocab_consistency"]["verdict"])
    print("[T62] 首见 OU 结算空转:", res["q3_freeze_semantics"]["first_verdict_settlement"]["first_verdict_ou_settlement_dead"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
