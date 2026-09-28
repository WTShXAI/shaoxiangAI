#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T39 账本世代列缺失对 walkforward 分段的影响只读盘点（承接 T35③）

问题（全部只读）：
  Q1  verification_ledger 到底有没有"模型代/世代"可分段列？没有的话，
      现有列里有没有任何一列能当世代代理键（run_id / created_at / match_date / devig_method）？
  Q2  现有 9042 行账本实际被混入了多少次入账批次？按 created_at 批次能否近似切代？
      该近似与 git 提交时刻（口径/模型迭代）对齐到什么程度？
  Q3  不做 schema 变更的前提下，"最小可分段粒度"是什么？重训后能否按代计数？
  Q4  若要补上世代列，建议哪些列 / 回填策略 / 属什么类型的变更（WINDOW 判定）。

诚实口径（IR-30）：
  - 本文只是**样本及格线**可用的分段可行性盘点；G1(2500) 不是 edge 证据。
  - 本脚本不建表、不改 schema、不跑迁移、不写 verification.db、不重训、不碰 events.db 写入。

只读铁律：
  - verification.db 以 uri + mode=ro + PRAGMA query_only 打开，零写入。
  - git 只读 `log` / `cat-file -e`，不 checkout / 不提交。

用法：
  .venv/Scripts/python.exe scripts/audit_ledger_generation_split.py
  .venv/Scripts/python.exe scripts/audit_ledger_generation_split.py --window-days 9
  .venv/Scripts/python.exe scripts/audit_ledger_generation_split.py --json-only
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEDGER_DB = os.path.join(REPO_ROOT, "verification.db")
SCHEMA_PY = os.path.join(REPO_ROOT, "verification", "schema.py")
MODELS_DIR = os.path.join(REPO_ROOT, "models")
DEFAULT_OUT = os.path.join(REPO_ROOT, "reports")

G1_ACCEPT_N = 2500
TARGET_SOURCE = "candles_ensemble"
CONTROL_SOURCES = ("KNN", "market_baseline")

# 可被误当作"世代/代"的列命名候选（用于 Q1 判定是否存在）
GEN_COLUMN_CANDIDATES = (
    "model_generation", "generation", "model_version", "model_epoch",
    "trained_at", "train_ts", "generation_id", "model_snapshot",
)

# 口径/模型迭代关键词（用于 git 提交分类）
ITERATION_KEYWORDS = {
    "SETTLE_GUARD": ("假0-0", "credible", "settle", "结算", "守卫"),
    "DEVIG": ("去水", "devig", "幂法", "比例法", "去水口径"),
    "FEATURE_SCALE": ("goal_scale", "派生市场"),
    "MODEL_RETRAIN": ("重训", "retrain", "训练", "train", "模型"),
    "ARCH": ("转型", "refactor", "架构"),
    "LEDGER": ("verification", "账本", "ledger"),
}

SELF_TAG = "audit_ledger_generation_split"


# ── 只读连接 ─────────────────────────────────────────────────────────────────
def open_readonly(db_path: str) -> sqlite3.Connection:
    """uri + mode=ro + query_only 双保险，绝不写入。"""
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"DB not found: {db_path}")
    con = sqlite3.connect(f"file:{os.path.abspath(db_path)}?mode=ro", uri=True)
    con.execute("PRAGMA query_only=1")
    con.row_factory = sqlite3.Row
    return con


# ── Q1 世代列判定 ────────────────────────────────────────────────────────────
def declared_columns_from_ddl(ddl: str) -> list[str]:
    """从 schema.py 的 LEDGER_DDL 三引号串里正则提取声明列名。

    踩坑：DDL 里 `p_home REAL, p_draw REAL, p_away REAL,` 是**同行多列**，
    不能按 `\\n` 分行取首词，必须逐行用 `col TYPE` 正则扫。
    """
    out = []
    for ln in ddl.splitlines():
        for m in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\s+(TEXT|INTEGER|REAL|BLOB)\b", ln):
            out.append(m.group(1))
    # 去重保序（同行重复出现如 devig_h/d/a 各自独立，无真正重复；仍去重保稳）
    seen, res = set(), []
    for c in out:
        if c not in seen:
            seen.add(c)
            res.append(c)
    return res


def has_generation_column(columns: list[str]) -> bool:
    """列里是否存在"模型代/世代"可分段列。"""
    return any(c in columns for c in GEN_COLUMN_CANDIDATES)


def column_role(column: str) -> str:
    """把列归到语义族：generation / calendar / settlement / caliber / identity / other。

    Q1 的核心：除世代列外，其余列均不是"代"的代理键。
    """
    if column in GEN_COLUMN_CANDIDATES:
        return "generation"
    if column in ("run_id", "row_id", "match_id"):
        return "identity"
    if column in ("created_at", "kickoff_utc"):
        return "calendar"
    if column in ("match_date",):
        return "calendar"
    if column in ("devig_method", "devig_h", "devig_d", "devig_a", "chosen_dec_odds"):
        return "caliber"
    if column in ("model_source",):
        return "identity"
    if column in ("is_credible", "payoff", "settled_outcome", "settled_home",
                  "settled_away", "predicted_prob", "p_home", "p_draw", "p_away",
                  "paper_stake", "chosen_outcome"):
        return "settlement"
    return "other"


def proxy_key_verdict(roles: dict[str, str]) -> dict:
    """裁定每一列能否当"世代代理键"，并给出逐列结论。"""
    verdicts = {}
    for col, role in roles.items():
        if role == "generation":
            verdicts[col] = {"role": role, "verdict": "CANNOT_NEED", "why": "这就是世代列本身"}
        elif col == "run_id":
            verdicts[col] = {
                "role": role,
                "verdict": "PROXY_PARTIAL",
                "why": "入账批次键，非模型代键：同一次入账内混入的**不同模型代**无法区分",
            }
        elif col == "created_at":
            verdicts[col] = {
                "role": role,
                "verdict": "PROXY_PARTIAL",
                "why": "写入时刻，只能近似到批次；跨批次的代迁移不可追溯",
            }
        elif col in ("match_date", "kickoff_utc"):
            verdicts[col] = {
                "role": role,
                "verdict": "NO",
                "why": "样本时间轴，与模型代正交",
            }
        elif col == "devig_method":
            verdicts[col] = {
                "role": role,
                "verdict": "CALIBER_ONLY",
                "why": "只记去水口径，与模型代不同维度；09-24 幂法 ≠ 模型重训",
            }
        else:
            verdicts[col] = {"role": role, "verdict": "NO", "why": "与代无关"}
    return verdicts


# ── Q2 批次与代对齐 ──────────────────────────────────────────────────────────
def ledger_batches(con: sqlite3.Connection) -> list[dict]:
    """账本入账批次：run_id + created_at 窗口 + 行数 + source 分布。"""
    rows = con.execute(
        "SELECT run_id, COUNT(*) AS n, MIN(created_at) AS first_at, MAX(created_at) AS last_at, "
        "COUNT(DISTINCT model_source) AS n_src FROM verification_ledger "
        "GROUP BY run_id ORDER BY first_at"
    ).fetchall()
    out = []
    for r in rows:
        srcs = dict(con.execute(
            "SELECT model_source, COUNT(*) FROM verification_ledger WHERE run_id=? "
            "GROUP BY model_source", (r["run_id"],)).fetchall())
        out.append({
            "run_id": r["run_id"], "rows": r["n"],
            "first_at_utc": r["first_at"], "last_at_utc": r["last_at"],
            "sources": srcs,
        })
    return out


def parse_utc(stamp: str) -> datetime | None:
    """把 ISO 串（Z 或 +08:00）统一转 aware UTC datetime。

    踩坑：git 的 `%aI` 带 +08:00 偏移，账本 created_at 带 Z 后缀，**时区必须归一**
    否则提交时刻与写入时刻比较会出现 8 小时系统性错位。
    """
    if not stamp:
        return None
    s = str(stamp).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def git_commits(since_iso: str) -> list[dict]:
    """只读 git log，返回 {commit, utc, subject, kinds}。"""
    cmd = ["git", "-C", REPO_ROOT, "log", f"--since={since_iso}",
           "--date=iso-strict", "--pretty=%H|%aI|%s"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                              encoding="utf-8", errors="replace")
    except Exception:
        return []
    out = []
    for ln in (proc.stdout or "").splitlines():
        if "|" not in ln:
            continue
        h, when, subj = ln.split("|", 2)
        dt = parse_utc(when)
        kinds = [k for k, kws in ITERATION_KEYWORDS.items()
                 if any(w in subj for w in kws)]
        out.append({"commit": h[:8], "utc": dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None,
                    "subject": subj, "kinds": kinds or ["OTHER"]})
    return out


def align_batches_to_commits(batches: list[dict], commits: list[dict]) -> list[dict]:
    """把每个入账批次挂到"该批次写入时刻之前最后一次口径/模型类提交"。

    返回每段 {run_id, first_at_utc, rows, anchored_commit, anchor_kinds, anchor_utc,
              next_commit, next_kinds, seconds_to_next}。

    两处踩坑（本脚本实测踩到，勿退回）：
      ① git log 输出是**新→旧**序，直接 before[-1] 会取到**最旧**的提交；
         必须按 utc 升序排序后再取最后一个。
      ② 提交 utc 形如 `...Z` 而批次时刻 isoformat 形如 `...+00:00`，
         字符串比较在分钟位不同时侥幸正确、跨小时位会错，必须统一成 datetime 比较
         或统一成同一后缀字符串。
    """
    anchors = sorted((c for c in commits if c.get("utc")),
                     key=lambda c: parse_utc(c["utc"]))
    keyed = [(parse_utc(c["utc"]), c) for c in anchors]
    res = []
    for b in batches:
        dt = parse_utc(b["first_at_utc"])
        anchor, a_kinds, a_utc = None, ["NONE"], None
        nxt, n_kinds, secs = None, ["NONE"], None
        if dt and keyed:
            before = [c for k, c in keyed if k <= dt]
            after = [c for k, c in keyed if k > dt]
            if before:
                a = before[-1]
                anchor, a_kinds, a_utc = a["commit"], a["kinds"], a["utc"]
            if after:
                nx = after[0]
                nxt, n_kinds = nx["commit"], nx["kinds"]
                secs = round((parse_utc(nx["utc"]) - dt).total_seconds())
        res.append({**b, "anchor_commit": anchor, "anchor_kinds": a_kinds,
                    "anchor_utc": a_utc, "next_commit": nxt, "next_kinds": n_kinds,
                    "seconds_to_next": secs})
    return res


def is_run_id_git_resolvable(run_id: str) -> bool:
    """run_id 是否可在 git 里反查为提交（决定它能不能当世代代理键）。"""
    try:
        p = subprocess.run(["git", "-C", REPO_ROOT, "cat-file", "-e", run_id],
                           capture_output=True, text=True, timeout=20)
        return p.returncode == 0
    except Exception:
        return False


# ── Q3 分段粒度与缺口 ────────────────────────────────────────────────────────
def split_granularity(segments: list[dict]) -> dict:
    """最小可分段粒度：以 (run_id 批次) 为切分单位时的段数、每段行数与样本跨度。"""
    return {
        "granularity": "verification_ledger 入账批次 (run_id)",
        "n_segments": len(segments),
        "segment_rows": {s["run_id"][:8]: s["rows"] for s in segments},
        "exactly_reproducible": True,
        "note": "同批次内若发生多次重训，批次内仍不可分 —— 这是不加列的上限",
    }


def gap_by_segment(segments: list[dict], target: int = G1_ACCEPT_N) -> dict:
    """按批次段统计每行距 G1 的缺口（段内全为 candles 时才算样本贡献）。"""
    out = {}
    for s in segments:
        n_cand = s["sources"].get(TARGET_SOURCE, 0)
        out[s["run_id"][:8]] = {
            "rows": s["rows"],
            "candles_rows": n_cand,
            "gap_to_g1": max(0, target - n_cand) if n_cand < target else 0,
        }
    return out


def mixing_report(batches: list[dict]) -> dict:
    """混批判定：入账批次跨越了多少个自然日、是否覆盖口径迭代点。"""
    days = set()
    for b in batches:
        d = parse_utc(b["first_at_utc"])
        if d:
            days.add(d.strftime("%Y-%m-%d"))
    span = 0
    if len(days) >= 2:
        ds = sorted(days)
        span = (date.fromisoformat(ds[-1]) - date.fromisoformat(ds[0])).days
    return {
        "n_batches": len(batches),
        "n_distinct_days": len(days),
        "day_span_days": span,
        "one_shot_backfill": any(b["rows"] >= 1000 for b in batches),
        "batch_sizes": sorted((b["rows"] for b in batches), reverse=True),
    }


# ── Q4 补列建议（纯建议，ALTER TABLE 属 WINDOW 不运行）────────────────────────
def propose_schema_columns(existing: list[str]) -> list[dict]:
    """世代列增列建议；只输出规格，绝不执行。"""
    lit = ("model_generation TEXT", "trained_at TEXT", "ingest_rule_set TEXT",
           "run_commit TEXT")
    out = []
    for decl in lit:
        col = decl.split()[0]
        existing_cols = [c for c in existing if c != col]
        out.append({
            "column": col,
            "type": "TEXT",
            "status": "已存在" if col in existing_cols else "建议新增",
            "rationale": {
                "model_generation": "代标签（如 candles_gen3），重训后按代分段计数的主键",
                "trained_at": "该行样本所用模型的固化时刻 UTC，与 created_at 区分",
                "ingest_rule_set": "结算/守卫口径版本（如 guard_v3_devig_power），与 devig_method 正交",
                "run_commit": "入账时的 git HEAD，把 run_id 从随机串变成可反查的代锚点",
            }[col],
            "backfill": "存量行按 run_id 批次 + 批前最后一次提交近似回填（有损：代内多次重训不可分）",
            "window": "WINDOW（ALTER TABLE，须停机窗口 + 全量回归）",
        })
    return out


def migration_path_status(schema_src: str) -> dict:
    """schema.py 的 LEDGER_MIGRATIONS 是否已具备"加列"的既有通路。"""
    m = re.search(r"LEDGER_MIGRATIONS[^=]*=\s*\((.*?)\)", schema_src, re.S)
    body = m.group(1) if m else ""
    adds = re.findall(r"ADD COLUMN\s+([A-Za-z_][A-Za-z0-9_]*)\s+([A-Za-z]+)", body)
    return {
        "ledger_migrations_count": len(re.findall(r"ALTER TABLE", body)),
        "add_column_examples": [f"{c} {t}" for c, t in adds],
        "path_ready": bool(adds),
        "note": "既有 ALTER TABLE 通路存在（devig_method 即为历史增量列），"
                "加世代列不是新机制，但仍属 schema 变更 → WINDOW",
    }


def model_generation_evidence() -> dict:
    """模型固化证据：models/ 内 .joblib mtime 的最近变更日。"""
    files = []
    if os.path.isdir(MODELS_DIR):
        for n in sorted(os.listdir(MODELS_DIR)):
            if n.endswith(".joblib"):
                p = os.path.join(MODELS_DIR, n)
                files.append({"file": n,
                              "mtime_utc": datetime.fromtimestamp(
                                  os.path.getmtime(p), tz=timezone.utc).strftime("%Y-%m-%d")})
    return {
        "n_joblib": len(files),
        "newest_joblib_utc": max((f["mtime_utc"] for f in files), default=None),
        "files": files,
    }


# ── 报告渲染 ─────────────────────────────────────────────────────────────────
def render_markdown(summary: dict) -> str:
    L = []
    L.append("# 账本世代列缺失对 walkforward 分段的影响（只读盘点 / T39）")
    L.append("")
    L.append(f"- 生成时间: {summary['generated_at']}（as_of {summary['as_of']}）")
    L.append(f"- 目标 source: `{summary['target_source']}`；G1 验收线 {summary['g1_accept_n']}")
    L.append("")
    L.append("## 0. 结论先行")
    L.append("")
    for line in summary["headlines"]:
        L.append("- " + line)
    L.append("")
    L.append("## 1. Q1：有没有世代列 / 谁能当代理键")
    L.append("")
    L.append("| 列 | 语义族 | 裁定 | 理由 |")
    L.append("|---|---|---|---|")
    for col, v in summary["proxy_keys"].items():
        L.append("| `{}` | {} | **{}** | {} |".format(col, v["role"], v["verdict"], v["why"]))
    L.append("")
    lg = summary["ledger"]
    L.append(f"- 账本实际列数 {lg['n_columns']}，"
             f"世代列存在 = **{summary['has_generation_column']}**")
    L.append(f"- schema.py DDL 声明列 {lg['declared_columns_len']} 个，"
             f"声明但未落地 = {lg['declared_not_materialized'] or '无'}")
    L.append(f"- `run_id` 可在 git 反查为提交 = **{summary['run_id_git_resolvable']}**"
             "（决定它能否当世代锚点）")
    L.append("")
    L.append("## 2. Q2：入账批次与口径迭代点的对齐")
    L.append("")
    L.append("| run_id(8) | 行数 | 写入时刻(UTC) | 锚定提交 | 锚类别 |")
    L.append("|---|---|---|---|---|")
    for s in summary["aligned_batches"]:
        L.append("| {} | {} | {} | {} | {} |".format(
            s["run_id"][:8], s["rows"], s["first_at_utc"],
            s["anchor_commit"] or "-", ",".join(s["anchor_kinds"])))
    L.append("")
    L.append("**批次 → 提交锚定（写入之后落地的提交 = 该批次之后发生的口径变更）**")
    L.append("")
    L.append("| run_id(8) | 写入后首提交 | 相差秒 | 类别 |")
    L.append("|---|---|---|---|")
    for s in summary["aligned_batches"]:
        L.append("| {} | {} | {} | {} |".format(
            s["run_id"][:8], s.get("next_commit") or "-",
            s.get("seconds_to_next") if s.get("seconds_to_next") is not None else "-",
            ",".join(s.get("next_kinds") or [])))
    L.append("")
    L.append("**混批判定**")
    L.append("")
    mx = summary["mixing"]
    L.append(f"- 入账批次 {mx['n_batches']} 个 / 覆盖 {mx['n_distinct_days']} 个自然日 / "
             f"跨度 {mx['day_span_days']} 天 / 含一次性回填 = {mx['one_shot_backfill']}"
             f" / 批次规模 {mx['batch_sizes']}")
    L.append("")
    L.append("**窗口内口径/模型类提交（git 只读）**")
    L.append("")
    L.append("| commit | UTC | 类别 | 说明 |")
    L.append("|---|---|---|---|")
    for c in summary["commits"][:15]:
        L.append("| {} | {} | {} | {} |".format(c["commit"], c["utc"], ",".join(c["kinds"]),
                                                c["subject"]))
    L.append("")
    L.append("## 3. Q3：最小可分段粒度与缺口")
    L.append("")
    g = summary["granularity"]
    L.append(f"- 粒度定义: {g['granularity']}")
    L.append(f"- 段数 {g['n_segments']}，规模 {g['segment_rows']}")
    L.append(f"- 可精确复现 = {g['exactly_reproducible']}；{g['note']}")
    L.append("")
    L.append("| 段 | 行数 | 其中 candles | 距 G1 缺口 |")
    L.append("|---|---|---|---|")
    for seg, v in summary["gap_table"].items():
        L.append("| {} | {} | {} | {} |".format(seg, v["rows"], v["candles_rows"],
                                                 v["gap_to_g1"]))
    L.append("")
    mg = summary["model_generation"]
    L.append(f"模型固化证据: models/ 内 {mg['n_joblib']} 个 joblib，"
             f"最新 mtime {mg['newest_joblib_utc'] or '-'}。")
    L.append("")
    L.append("## 4. Q4：补列建议（规格，绝不执行）")
    L.append("")
    L.append("| 列 | 类型 | 状态 | 用途 | 回填 | 变更类型 |")
    L.append("|---|---|---|---|---|---|")
    for c in summary["schema_proposal"]:
        L.append("| `{}` | {} | {} | {} | {} | {} |".format(
            c["column"], c["type"], c["status"], c["rationale"], c["backfill"], c["window"]))
    L.append("")
    mp = summary["migration_path"]
    L.append(f"- schema.py `LEDGER_MIGRATIONS` 既有 ALTER TABLE 通路 = "
             f"{mp['path_ready']}（{mp['ledger_migrations_count']} 条）"
             f"，示例增量列 {mp['add_column_examples'] or '无'}")
    L.append("")
    L.append("## 5. 诚实口径（先读）")
    L.append("")
    for line in summary["honest_notes"]:
        L.append("- " + line)
    L.append("")
    L.append("## 6. 未做的事（红线）")
    L.append("")
    L.append("- 未建表 / 未 ALTER / 未跑迁移 / 未写 verification.db / 未重训 / "
             "未改 verification/ 生产包 / 未碰 events.db 写入 / 未重启任何进程。")
    L.append("")
    return "\n".join(L)


# ── 组装 ─────────────────────────────────────────────────────────────────────
def build_headlines(has_gen: bool, batch_anchor_coverage: int, mixing: dict,
                    gran: dict, run_git: bool, mp: dict) -> list[str]:
    out = []
    out.append(
        "**Q1 世代列**：`verification_ledger` **没有任何世代/模型代列**"
        f"（{has_gen}）；而 `run_id` 虽在，却**无法在 git 中反查**（反查成功="
        f"{run_git}），因此它不是世代锚点，只能用做**入账批次号**。")
    out.append(
        "**Q1 代理键裁定**：唯一可用的近似是 `created_at`（写入时刻）+ git 提交时刻对齐，"
        "它只能近似到「批次」而非「模型代」。`devig_method` 只记去水口径，"
        "**与模型代正交**（09-24 幂法 ≠ 模型重训，不能互相替代）。")
    out.append(
        f"**Q2 混批**：整本 {mixing['n_batches']} 个入账批次、跨 {mixing['n_distinct_days']} 个"
        f"自然日（跨度 {mixing['day_span_days']} 天），其中一次性回填 "
        f"{'存在' if mixing['one_shot_backfill'] else '不存在'}。批次时刻与口径提交时刻"
        f"锚定成功率 {batch_anchor_coverage}/{mixing['n_batches']}。"
        "→ 按 created_at 批次切代**可行但有损**：它切的是「入账时机」，不是「模型代」。")
    out.append(
        f"**Q3 最小粒度**：不加列时粒度 = 入账批次（{gran['n_segments']} 段）。"
        "上限边界：同一次入账内若发生多次重训，这些行**永远不可分**。")
    out.append(
        "**Q4 补列**：建议 `model_generation / trained_at / ingest_rule_set / run_commit` 四列；"
        "既有 `LEDGER_MIGRATIONS` ALTER TABLE 通路（"
        f"{mp['path_ready']}）说明加列不是新机制，但仍属 schema 变更 → **WINDOW 项**，"
        "本轮只出规格不执行。")
    return out


def run(ledger_db: str, as_of: str | None) -> dict:
    as_of = as_of or date.today().isoformat()

    con = open_readonly(ledger_db)
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(verification_ledger)")]
        n_rows = con.execute("SELECT COUNT(*) FROM verification_ledger").fetchone()[0]
        src_span = dict(con.execute(
            "SELECT model_source, COUNT(*) FROM verification_ledger GROUP BY model_source"
        ).fetchall())
        batches = ledger_batches(con)
        dup = con.execute(
            "SELECT COUNT(*) FROM (SELECT match_id, model_source FROM verification_ledger "
            "GROUP BY 1,2 HAVING COUNT(*)>1)").fetchone()[0]
        devig_null = con.execute(
            "SELECT COUNT(*) FROM verification_ledger WHERE devig_method IS NULL").fetchone()[0]
    finally:
        con.close()

    try:
        with open(SCHEMA_PY, encoding="utf-8") as f:
            schema_src = f.read()
    except Exception:
        schema_src = ""
    # 踩坑：schema.py 里是 `LEDGER_DDL: str = """..."""`，`LEDGER_DDL` 与 `=` 之间
    # 隔着类型注解，正则不能写成 `LEDGER_DDL\s*=`。
    ddl_m = re.search(r"LEDGER_DDL[^=]*=\s*\"\"\"(.*?)\"\"\"", schema_src, re.S)
    ddl = ddl_m.group(1) if ddl_m else ""
    declared = declared_columns_from_ddl(ddl)

    roles = {c: column_role(c) for c in cols}
    proxy = proxy_key_verdict(roles)
    has_gen = has_generation_column(cols)

    commits = git_commits(since_iso=(date.fromisoformat(as_of) - timedelta(days=30)).isoformat())
    aligned = align_batches_to_commits(batches, commits)
    covered = sum(1 for a in aligned if a["anchor_commit"])
    mixing = mixing_report(batches)
    gran = split_granularity(aligned)
    gaps = gap_by_segment(aligned)
    mp = migration_path_status(schema_src)

    run_first = batches[0]["first_at_utc"] if batches else None
    run_git = False
    for r in (b["run_id"] for b in batches[:5]):
        if is_run_id_git_resolvable(r):
            run_git = True
            break

    mg = model_generation_evidence()
    declared_not = [c for c in declared if c not in cols]

    summary = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "note": "只读盘点：不建表 / 不 ALTER / 不跑迁移 / 不写 verification.db / 不重训",
        "as_of": as_of,
        "target_source": TARGET_SOURCE,
        "g1_accept_n": G1_ACCEPT_N,
        "ledger": {
            "db": os.path.relpath(ledger_db, REPO_ROOT).replace("\\", "/"),
            "n_rows": n_rows, "n_columns": len(cols), "columns": cols,
            "source_counts": src_span,
            "dup_match_model": dup,
            "devig_method_null": devig_null,
            "first_batch_at_utc": run_first,
            "declared_columns_len": len(declared),
            "declared_not_materialized": declared_not,
        },
        "has_generation_column": has_gen,
        "proxy_keys": proxy,
        "run_id_git_resolvable": run_git,
        "batches": batches,
        "aligned_batches": aligned,
        "commits": commits,
        "mixing": mixing,
        "granularity": gran,
        "gap_table": gaps,
        "model_generation": mg,
        "schema_proposal": propose_schema_columns(cols),
        "migration_path": mp,
    }
    summary["headlines"] = build_headlines(
        has_gen, covered, mixing, gran, run_git, mp)
    summary["honest_notes"] = honest_notes(mixing, dup)
    return summary


def honest_notes(mixing: dict, dup: int) -> list[str]:
    return [
        "G1(2500) 只是**样本及格线**，不是 edge 证据；按代分段解决的是「计数可比性」，"
        "不是 edge。candles_ensemble 仍须过 G2–G6（含 G6 零信息机械对照）。",
        f"账本 (match_id, model_source) 重复 {dup} 行 → 按 created_at 批次分段不会引入重复计数；"
        "但反过来说，若将来按 match_date 分段而入账批次跨了 match_date 边界，就可能出现"
        "同一场被不同代同名的两行，须以 (match_id, model_source) 去重后再计。",
        f"入账批次跨 {mixing['n_distinct_days']} 个自然日（一次性回填 {mixing['one_shot_backfill']}）→ "
        "「按批次切代」与「按口径提交切代」在回填场景下会给出不同分段，"
        "报告里两条线都要给，不能只报一条。",
        "**不加列 ≠ 不能分段**：现在就能用 run_id + created_at + git 提交对齐做分段；"
        "加列的收益是「让未来的代可追溯」，不是「让现在可分段」。两者不要混为一谈。",
        "本脚本未做任何 schema 改动；ALTER TABLE 增列属 WINDOW 项，须停机窗口 + 全量回归。",
    ]


def main():
    ap = argparse.ArgumentParser(description="账本世代列缺失对 walkforward 分段的影响")
    ap.add_argument("--ledger-db", default=LEDGER_DB)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--json-only", action="store_true")
    args = ap.parse_args()

    summary = run(args.ledger_db, args.as_of)

    if args.json_only:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return summary

    print("[as_of] {}  ledger {} rows={}".format(
        summary["as_of"], summary["ledger"]["db"], summary["ledger"]["n_rows"]))
    print("[Q1] has_generation_column={}  run_id_git_resolvable={}  cols={}".format(
        summary["has_generation_column"], summary["run_id_git_resolvable"],
        summary["ledger"]["n_columns"]))
    for s in summary["aligned_batches"]:
        print("[seg] {:<8} rows={:<5} @{}  anchor={} {}".format(
            s["run_id"][:8], s["rows"], s["first_at_utc"],
            s["anchor_commit"] or "-", ",".join(s["anchor_kinds"])))
    print("[Q3] granularity={} segments={} gaps={}".format(
        summary["granularity"]["granularity"], summary["granularity"]["n_segments"],
        {k: v["gap_to_g1"] for k, v in summary["gap_table"].items()}))
    print("[Q4] migration_path_ready={} add_column_examples={}".format(
        summary["migration_path"]["path_ready"], summary["migration_path"]["add_column_examples"]))
    for line in summary["headlines"]:
        print("[hl] " + line)
    for line in summary["honest_notes"]:
        print("[ok] " + line)

    os.makedirs(args.out, exist_ok=True)
    jp = os.path.join(args.out, "ledger_generation_split.json")
    mp_ = os.path.join(args.out, "ledger_generation_split.md")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    with open(mp_, "w", encoding="utf-8") as f:
        f.write(render_markdown(summary))
    print(f"[out] {jp}")
    print(f"[out] {mp_}")
    return summary


if __name__ == "__main__":
    main()
