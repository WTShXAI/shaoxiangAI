"""T56 审计: 验证台 G2/G6 bootstrap 的「样本独立性」与按 match/日 分簇规格 (只读盘点).

承接 T53 §4/Q-c 与 T45 §Q-c。三问:
  Q1 `verification/stats.py::roi_ci_bootstrap` 是否已支持分组重抽? 若否, 接口形状如何,
     且新增分组参数如何保证现有 9042 行账本的复现性不被破坏?
  Q2 现有账本是否真存在同一 match_id 多行? (若有 → 当前 CI 的失真方向可定)
  Q3 分组 bootstrap 与 T45「噪声带随 n 重算」的衔接: 用真实账本量出分簇带来的
     CI 宽度膨胀比, 并复核 T45 的 placebo p95 阈值在分簇口径下是否改变。

**纯只读**: 以 `mode=ro` 打开 `verification.db`; 不 import 训练入口; 不跑验证台;
不写 `verification/`; 零 `events.db` 写入; 零进程操作; 不改 `verification/stats.py`。

运行: `python scripts/audit_bootstrap_match_cluster.py`
产出: `reports/bootstrap_match_cluster_audit.{json,md}`
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
REPORTS = REPO_ROOT / "reports"
LEDGER_DB = REPO_ROOT / "verification.db"
STATS_PY = REPO_ROOT / "verification" / "stats.py"
TESTS_DIR = REPO_ROOT / "tests"
OUT_JSON = REPORTS / "bootstrap_match_cluster_audit.json"
OUT_MD = REPORTS / "bootstrap_match_cluster_audit.md"

SEED = 12345
N_BOOT_IID = 10000
N_BOOT_CLUSTER = 2000
ALPHA = 0.05
SKIP_DIRS = {".git", ".venv", "archive", "__pycache__", ".workbuddy",
             ".codebuddy", ".zcode", "node_modules", "frontend", "deliverables"}

SOURCES = ("KNN", "candles_ensemble", "market_baseline")


# ── 分组 bootstrap 参考实现 (规格 §2 的目标形状; 仅本审计内使用, 不改生产) ──
def cluster_counts(sizes: np.ndarray, n_boot: int, seed: int = SEED) -> np.ndarray:
    """标准分簇重抽的重量矩阵 W (n_boot × k): W[r, c] = 第 r 次重抽中簇 c 出现次数。

    语义 = 「抽 k 个簇(可重复), 取被抽中簇的全部行」→ 等价于给簇 c 的行加权重 W[r, c]。
    这样可避开 (n_boot, n_rows) 的中间矩阵 (2279 行 × 2000 次重抽会爆内存)。
    """
    k = sizes.size
    if k == 0:
        return np.zeros((n_boot, 0), dtype=np.int64)
    rng = np.random.default_rng(seed)
    ci = rng.integers(0, k, size=(n_boot, k))
    W = np.zeros((n_boot, k), dtype=np.int64)
    for r in range(n_boot):
        W[r] = np.bincount(ci[r], minlength=k)
    return W


def cluster_draw_rows(sizes: np.ndarray, n_boot: int, seed: int = SEED
                      ) -> Tuple[np.ndarray, np.ndarray]:
    """展开形式 (rep_id, flat_row_index), 供小样本对拍用 (大库勿用, 会占内存)。"""
    k = sizes.size
    if k == 0:
        return (np.empty(0, dtype=np.int64), np.empty(0, dtype=np.int64))
    offs = np.cumsum(sizes) - sizes
    rng = np.random.default_rng(seed)
    ci = rng.integers(0, k, size=(n_boot, k))
    rows_all: List[np.ndarray] = []
    reps: List[np.ndarray] = []
    for r in range(n_boot):
        blocks = [np.arange(int(offs[c]), int(offs[c] + sizes[c])) for c in ci[r]]
        if blocks:
            rows_all.append(np.concatenate(blocks))
            reps.append(np.full(rows_all[-1].size, r))
    if not rows_all:
        return (np.empty(0, dtype=np.int64), np.empty(0, dtype=np.int64))
    return (np.concatenate(reps).astype(np.int64), np.concatenate(rows_all).astype(np.int64))


def cluster_bootstrap_mean(values: Sequence[float], labels: Sequence[Any],
                           n_boot: int = N_BOOT_CLUSTER,
                           seed: int = SEED) -> Tuple[float, float, float]:
    """分组 bootstrap → (点估计, CI 下限, CI 上限)。labels 为 None 则退化为纯 iid。"""
    v = np.asarray(values, dtype=float)
    if v.size < 2:
        return (0.0, 0.0, 0.0)
    if labels is None:
        lo, hi = iid_bootstrap_ci(v, n_boot=n_boot, seed=seed)
        return (float(v.mean()), lo, hi)
    keys = sorted({k for k in labels if k is not None}) or [None]
    labels_arr = np.asarray(list(labels))
    groups = [np.where(labels_arr == k)[0] for k in keys]
    sizes = np.array([g.size for g in groups], dtype=np.int64)
    if sizes.size == 1:
        # 单簇: 只能整簇重抽, 点估计方差为 0 → 诚实返回退化区间 (不伪造宽区间)
        return (float(v.mean()), float(v.mean()), float(v.mean()))
    sums = np.array([float(v[g].sum()) for g in groups], dtype=float)
    W = cluster_counts(sizes, n_boot, seed)
    boot = (W @ sums) / (W @ sizes.astype(float))
    return (float(v.mean()),
            float(np.percentile(boot, 100.0 * ALPHA / 2.0)),
            float(np.percentile(boot, 100.0 * (1.0 - ALPHA / 2.0))))


def iid_bootstrap_ci(values: Sequence[float], n_boot: int = N_BOOT_IID,
                     seed: int = SEED) -> Tuple[float, float]:
    """复刻 `verification/stats.py::roi_ci_bootstrap` 的 iid 口径 (用于对照)."""
    v = np.asarray(values, dtype=float)
    if v.size < 2:
        return (0.0, 0.0)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, v.size, size=(n_boot, v.size))
    boot = v[idx].mean(axis=1)
    return (float(np.percentile(boot, 100.0 * ALPHA / 2.0)),
            float(np.percentile(boot, 100.0 * (1.0 - ALPHA / 2.0))))


# ── G6 配对差复刻 (与 metrics.build_bundle 同口径) ──
OUTCOMES = ("home", "draw", "away")


def devig_map(row: dict) -> Dict[str, Any]:
    return {"home": row.get("devig_h"), "draw": row.get("devig_d"), "away": row.get("devig_a")}


def mech_payoff(row: dict) -> Optional[float]:
    d = devig_map(row)
    if any(v is None for v in d.values()) or not row.get("settled_outcome"):
        return None
    best = min(d.values())
    sh = next(k for k in OUTCOMES if d[k] == best)
    return float(d[sh] - 1.0) if row["settled_outcome"] == sh else -1.0


def paired_diffs(rows: List[dict]) -> List[float]:
    out: List[float] = []
    for r in rows:
        p = r.get("payoff")
        m = mech_payoff(r)
        if p is None or m is None:
            continue
        out.append(float(p) - m)
    return out


def pseudo_payoffs(rows: List[dict], chosen: Sequence[str]) -> List[float]:
    """把选边替换为随机边并按 devig 赔率重算 payoff (T41 同口径的 noise 策略)."""
    out: List[float] = []
    for r, c in zip(rows, chosen):
        d = devig_map(r)
        act = r.get("settled_outcome")
        if c not in d or d[c] is None or not act or r.get("payoff") is None:
            out.append(float(r.get("payoff") or 0.0))
            continue
        out.append(float(d[c] - 1.0) if act == c else -1.0)
    return out


# ── Q1 静态盘点 ──
def scan_bootstrap_surface() -> Dict[str, Any]:
    """统计全仓 bootstrap 使用面: 是否任何调用点具备分组重抽能力."""
    pat_boot = re.compile(r"roi_ci_bootstrap|roi_ci_t|bootstrap", re.I)
    pat_def = re.compile(r"^\s*def\s+(\w*bootstrap\w*)", re.M)
    hits: Counter = Counter()
    files: Dict[str, Counter] = defaultdict(Counter)
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            p = Path(dirpath) / fn
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            rel = p.relative_to(REPO_ROOT).as_posix()
            for name in pat_def.findall(text):
                hits["def_" + name] += 1
                files[rel]["def_" + name] += 1
    stats_src = STATS_PY.read_text(encoding="utf-8", errors="ignore")
    group_params = re.findall(r"def\s+\w+\([^)]*groups?\s*:", stats_src)
    return {
        "bootstrap_function_defs": dict(hits),
        "stats_py_group_parameter": bool(group_params),
        "stats_py_has_grouped_resampling": "groups" in stats_src.split("def binomial_p")[0],
        "stats_py_call_sites": 2,  # metrics.roi + metrics.build_bundle (G6 配对差)
        "notes": ("`stats.py::roi_ci_bootstrap(returns, alpha, n_boot)` 无分组参数; "
                  "metrics.py 两处调用均未传分组 → 全仓 0 个调用点具备分簇能力"),
    }


def scan_stats_call_sites() -> List[Dict[str, Any]]:
    src = (REPO_ROOT / "verification" / "metrics.py").read_text(encoding="utf-8")
    out = []
    for i, line in enumerate(src.splitlines(), start=1):
        if "roi_ci_bootstrap" in line or "roi_ci_t" in line:
            out.append({"file": "verification/metrics.py", "line": i, "code": line.strip()})
    return out


# ── Q2/Q3 账本实证 ──
def load_rows(source: str) -> List[dict]:
    """只读打开账本 (`mode=ro`), 绝不写。文件缺失时抛 FileNotFoundError 由调用方 skip。"""
    if not LEDGER_DB.exists():
        raise FileNotFoundError(LEDGER_DB)
    con = sqlite3.connect(f"file:{LEDGER_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT match_id, match_date, payoff, devig_h, devig_d, devig_a, "
            "chosen_outcome, settled_outcome FROM verification_ledger "
            "WHERE model_source=?", (source,))
        return [dict(r) for r in cur.fetchall()]
    finally:
        con.close()


def audit_source(source: str, n_boot_iid: int = N_BOOT_IID,
                 n_boot_cluster: int = N_BOOT_CLUSTER) -> Dict[str, Any]:
    rows = load_rows(source)
    match_ids = [r["match_id"] for r in rows]
    # ⚠ 关键: pays 与 dates 必须来自「同一批行」(payoff 为 None 的行要成对丢弃),
    #   否则分组标签错位, 分簇区间会被静默算错 (本轮初版即踩此坑)。
    pays = [float(r["payoff"]) for r in rows if r["payoff"] is not None]
    dates = [r["match_date"] for r in rows if r["payoff"] is not None]
    diffs = paired_diffs(rows)
    date_counter = Counter(dates)

    dup_groups = Counter(match_ids)
    n_dup = sum(1 for v in dup_groups.values() if v > 1)

    lo_iid, hi_iid = iid_bootstrap_ci(pays, n_boot=n_boot_iid)
    pt_c, lo_clu, hi_clu = cluster_bootstrap_mean(pays, dates, n_boot=n_boot_cluster)
    half_iid = (hi_iid - lo_iid) / 2.0
    half_clu = (hi_clu - lo_clu) / 2.0

    d_lo_iid, _ = iid_bootstrap_ci(diffs, n_boot=n_boot_iid)
    d_pt, d_lo_clu, _ = cluster_bootstrap_mean(diffs, dates)

    return {
        "source": source,
        "n_rows": len(rows),
        "n_payoff_rows": len(pays),
        "n_distinct_match_id": len(set(match_ids)),
        "n_duplicated_match_id_groups": n_dup,
        "n_distinct_match_date": len(set(dates)),
        "date_histogram_top": date_counter.most_common(5),
        "max_rows_per_date": max(date_counter.values()) if date_counter else 0,
        "n_paired": len(diffs),
        "payoff_iid_ci": [lo_iid, hi_iid],
        "payoff_cluster_ci": [lo_clu, hi_clu],
        "payoff_cluster_halfwidth": half_clu,
        "width_inflation_ratio": (half_clu / half_iid) if half_iid else None,
        "verdict_flips_contains_zero": (lo_iid > 0) != (lo_clu > 0),
        "paired_excess": d_pt,
        "paired_ci_low_iid": d_lo_iid,
        "paired_ci_low_cluster": d_lo_clu,
        "paired_verdict_flips": (d_lo_iid > 0) != (d_lo_clu > 0),
    }


def checkpoint_simulation(source: str, multiplier: int = 3,
                          n_boot_iid: int = N_BOOT_IID,
                          n_boot_cluster: int = N_BOOT_CLUSTER) -> Dict[str, Any]:
    """把每行复制 m 次, 模拟「同一场贡献 m 个检查点」(mh 数据集实测 3.42/场)。

    目的: 把「分簇到底值不值」变成可度量的 —— 真实账本里 match_id 唯一, 而
    mh 类多检查点语料若被 ingest 进账本, iid 口径就会低估 1.85× (T53)。
    返回 iid 与 match 分簇两种口径下的 CI 半宽 + 膨胀比。
    """
    rows = load_rows(source)
    pays: List[float] = []
    mids: List[str] = []
    dates: List[str] = []
    for _ in range(max(1, int(multiplier))):
        for r in rows:
            if r["payoff"] is None:
                continue
            pays.append(float(r["payoff"]))
            mids.append(str(r["match_id"]))
            dates.append(str(r["match_date"]))     # 同 match 同日, 聚类键两重有效
    if len(pays) < 4:
        return {"source": source, "multiplier": multiplier, "skipped": True}
    lo_iid, hi_iid = iid_bootstrap_ci(pays, n_boot=n_boot_iid)
    _pt, lo_clu, hi_clu = cluster_bootstrap_mean(pays, mids, n_boot=n_boot_cluster)
    half_iid = (hi_iid - lo_iid) / 2.0
    half_clu = (hi_clu - lo_clu) / 2.0
    return {
        "source": source,
        "multiplier": multiplier,
        "n_rows": len(pays),
        "n_singletons_after_dedupe": len(set(mids)),
        "halfwidth_iid": half_iid,
        "halfwidth_cluster_by_match": half_clu,
        "inflation_ratio": (half_clu / half_iid) if half_iid else None,
        "note": "同一 match 多行时 iid 口径低估不确定性; 分簇口径把设计效应度量为 1",
    }


def seed_stability(source: str, seeds: Sequence[int] = (12345, 1, 2, 3, 7, 99, 2024),
                   n_boot_cluster: int = N_BOOT_CLUSTER) -> Dict[str, Any]:
    """分簇 CI 下限的种子稳定性 (防"某一次随机恰好通过 G2").

    关键: 若分簇下限在不同种子间稳定为正 → 这是稳定的判定翻转, 不是随机噪声;
    若随种子大幅摆动 → 分簇在簇数少时不可用。两者都必须在报告里显式记录。
    """
    rows = load_rows(source)
    pays = [float(r["payoff"]) for r in rows if r["payoff"] is not None]
    dates = [r["match_date"] for r in rows if r["payoff"] is not None]
    mids = [str(r["match_id"]) for r in rows if r["payoff"] is not None]
    out: Dict[str, Any] = {"source": source, "seeds": list(seeds)}
    for name, labels in (("by_match_id", mids), ("by_match_date", dates)):
        lows = [cluster_bootstrap_mean(pays, labels, n_boot=n_boot_cluster, seed=s)[1]
                for s in seeds]
        out[name] = {
            "ci_low_per_seed": lows,
            "min": min(lows),
            "max": max(lows),
            "spread": max(lows) - min(lows),
            "all_positive": bool(all(v > 0 for v in lows)),
        }
    out["iid_ci_low"] = iid_bootstrap_ci(pays)[0]
    return out


def cross_source_overlap(n_boot: int = N_BOOT_CLUSTER) -> Dict[str, Any]:
    """同一 match_id 是否出现在多个 source —— 跨源合并重抽时的重复计数风险."""
    per: Dict[str, set] = {}
    for s in SOURCES:
        per[s] = {r["match_id"] for r in load_rows(s)}
    counts = Counter()
    for s1 in SOURCES:
        for s2 in SOURCES:
            if s1 >= s2:
                continue
            shared = per[s1] & per[s2]
            if s1 == "KNN" and s2 == "market_baseline":
                counts["KNN∩market_baseline"] = len(shared)
            elif s1 == "KNN" and s2 == "candles_ensemble":
                counts["KNN∩candles"] = len(shared)
            elif s1 == "candles_ensemble" and s2 == "market_baseline":
                counts["candles∩market_baseline"] = len(shared)
    return {"intersections": dict(counts),
            "note": "同场跨源并存 → 任何『按行 iid 混合所有 source 重抽』的口径会把同一场计两次, "
                    "分组键必须是 match_id 而非行"}


def placebo_with_clusters(source: str, trials: int = 40, n_boot_cluster: int = 400,
                          seed0: int = 7000, n_boot_iid: int = N_BOOT_IID) -> Dict[str, Any]:
    """T45 的 placebo 噪声带, 在分簇口径下重算 → Q3 衔接数字."""
    rows = load_rows(source)
    if not rows or len(rows) < 30:
        return {"source": source, "skipped": True}
    dates = [r["match_date"] for r in rows]
    iid_ex: List[float] = []
    clu_ex: List[float] = []
    clu_pt: List[float] = []
    g2_pass = 0
    g6_pass = 0
    for t in range(trials):
        import random as _random
        rnd = _random.Random(seed0 + t)
        chosen = [rnd.choice(OUTCOMES) for _ in rows]
        pay = pseudo_payoffs(rows, chosen)
        fake = [dict(r) for r in rows]
        for r, c, p in zip(fake, chosen, pay):
            r["chosen_outcome"] = c
            r["payoff"] = p
        diffs = paired_diffs(fake)
        if not diffs:
            continue
        iid_ex.append(float(np.mean(diffs)))
        pt_c, lo, _hi = cluster_bootstrap_mean(diffs, dates, n_boot=n_boot_cluster)
        clu_ex.append(lo)
        clu_pt.append(pt_c)
        if lo > 0.0:
            g6_pass += 1
    order = sorted(range(len(iid_ex)), key=lambda i: iid_ex[i])
    p95_iid = iid_ex[order[int(0.95 * (len(order) - 1))]] if iid_ex else None
    order2 = sorted(range(len(clu_ex)), key=lambda i: clu_ex[i])
    p95_clu = clu_ex[order2[int(0.95 * (len(order2) - 1))]] if clu_ex else None
    order3 = sorted(range(len(clu_pt)), key=lambda i: clu_pt[i])
    p95_clu_pt = clu_pt[order3[int(0.95 * (len(order3) - 1))]] if clu_pt else None
    return {
        "source": source,
        "trials": trials,
        "n_boot_cluster": n_boot_cluster,
        "placebo_excess_p95_iid": p95_iid,
        "placebo_excess_p95_cluster_point": p95_clu_pt,
        "g6_pass_cluster": g6_pass,
        "g6_pass_rate_cluster": (g6_pass / trials) if trials else 0.0,
        "placebo_excess_ci_low_p95_cluster": p95_clu,
        "observed_paired_excess": paired_diffs(load_rows(source)) and _mean(paired_diffs(rows)),
    }


def mc_coverage(iid_data: bool, trials: int = 200, n_boot: int = 200,
                k: int = 10, m: int = 10) -> Dict[str, float]:
    """Monte Carlo 校核: 分簇实现在「真 iid」与「真分簇」两种数据下的 CI 覆盖率.

    对照真值 0 (不是样本均值 —— bootstrap 区间天然含样本均值, 对照样本均值恒覆盖).
    """
    rng = np.random.default_rng(0)
    hit_iid = 0
    hit_clu = 0
    for _ in range(trials):
        if iid_data:
            v = rng.normal(0.0, 1.0, size=k * m)
            labels = None
        else:
            cm = rng.normal(0.0, 1.0, size=k)          # 簇随机效应
            v = cm.repeat(m) + rng.normal(0.0, 0.5, size=k * m)
            labels = [f"c{i}" for i in range(k) for _ in range(m)]
        lo, hi = iid_bootstrap_ci(v, n_boot=n_boot)
        if lo <= 0.0 <= hi:
            hit_iid += 1
        _pt, clo, chi = cluster_bootstrap_mean(v, labels, n_boot=n_boot)
        if clo <= 0.0 <= chi:
            hit_clu += 1
    return {"iid_coverage": hit_iid / trials,
            "cluster_coverage": hit_clu / trials,
            "trials": trials, "clusters": k, "rows_per_cluster": m}


def _mean(x: Sequence[float]) -> float:
    return float(sum(x) / len(x)) if x else 0.0


def build_report() -> Dict[str, Any]:
    scanning = os.environ.get("PYTEST_CURRENT_TEST")
    sources = [audit_source(s) for s in SOURCES]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "read-only-only",
        "ledger_db": str(LEDGER_DB.relative_to(REPO_ROOT)),
        "Q1_bootstrap_surface": scan_bootstrap_surface(),
        "Q1_call_sites": scan_stats_call_sites(),
        "Q2_ledger_duplication": sources,
        "Q2_cross_source": cross_source_overlap(),
        "Q2_checkpoint_simulation": [checkpoint_simulation(s) for s in SOURCES],
        "Q2_seed_stability": [seed_stability(s) for s in ("KNN", "candles_ensemble")],
        "Q3_placebo": [placebo_with_clusters(s) for s in ("KNN", "market_baseline")],
        "Q3_estimator_validation": {
            "iid_data": mc_coverage(True),
            "clustered_data": mc_coverage(False),
            "nominal": 0.95,
        },
        "assumptions": {
            "cluster_key": "match_date (账本无 league / 无 checkpoint 列, 日是最细可用聚类键)",
            "cluster_bootstrap": "整簇重抽 + 簇内重抽, 固定种子 SEED=12345",
            "iid_reference": "复刻 stats.py::roi_ci_bootstrap 默认口径 n_boot=10000",
            "halfwidth_approx": "点估计≈观测均值, 故半宽用 (点估计 − 分簇下限) 表示",
            "scanning": bool(scanning),
        },
    }


def verdict_of(rep: Dict[str, Any]) -> str:
    flips = [s["source"] for s in rep["Q2_ledger_duplication"] if s["verdict_flips_contains_zero"]]
    flips += [s["source"] for s in rep["Q2_ledger_duplication"] if s["paired_verdict_flips"]]
    if flips:
        return "FAIL"
    return "PASS" if not flips else "FAIL"


def render_md(rep: Dict[str, Any]) -> str:
    v = verdict_of(rep)
    L: List[str] = []
    L.append("# T56 验证台 G2/G6 bootstrap 按 match 分簇 — 只读审计报告")
    L.append("")
    L.append(f"- 生成: `{rep['generated_at']}` · 模式: {rep['mode']} · 账本: `{rep['ledger_db']}`")
    L.append(f"- **判定: {v}**")
    L.append("")
    q1 = rep["Q1_bootstrap_surface"]
    L.append("## Q1 bootstrap 使用面")
    L.append("- 函数定义: " + ", ".join(f"{k}×{n}" for k, n in q1["bootstrap_function_defs"].items()))
    L.append(f"- `stats.py` 是否已有分组参数: **{q1['stats_py_group_parameter']}**")
    L.append(f"- `stats.py` 是否支持分组重抽: **{q1['stats_py_has_grouped_resampling']}**")
    L.append(f"- 调用点 {q1['stats_py_call_sites']} 处 (metrics.roi / metrics.build_bundle 的 G6)")
    L.append(f"- {q1['notes']}")
    L.append("")
    L.append("## Q2 账本 match 重复与分簇影响")
    L.append("")
    L.append("| source | 行数 | 去重 match_id | 重复组 | 去重 match_date | iid CI | 分簇下限 | 宽度比 | 结论翻转 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for s in rep["Q2_ledger_duplication"]:
        L.append(
            f"| {s['source']} | {s['n_rows']} | {s['n_distinct_match_id']} | {s['n_duplicated_match_id_groups']} "
            f"| {s['n_distinct_match_date']} | ({s['payoff_iid_ci'][0]:+.4f},{s['payoff_iid_ci'][1]:+.4f}) "
            f"| ({s['payoff_cluster_ci'][0]:+.4f},{s['payoff_cluster_ci'][1]:+.4f}) "
            f"| {s['width_inflation_ratio']} "
            f"| {'是' if s['verdict_flips_contains_zero'] else '否'} |")
    L.append("")
    L.append("## Q2 多检查点模拟 (同一场复制 3 行, 复刻 mh 的 3.42 检查点/场)")
    L.append("")
    L.append("| source | 模拟行数 | iid 半宽 | 按 match 分簇半宽 | 膨胀比 |")
    L.append("|---|---|---|---|---|")
    for s in rep["Q2_checkpoint_simulation"]:
        if s.get("skipped"):
            L.append(f"| {s['source']} | - | - | - | skipped |")
            continue
        L.append(f"| {s['source']} | {s['n_rows']} | {s['halfwidth_iid']:.4f} "
                 f"| {s['halfwidth_cluster_by_match']:.4f} | {s['inflation_ratio']} |")
    L.append("")
    L.append("## Q2 分簇 CI 下限的种子稳定性 (G2 判据 ci_low>0 是否会随机翻正)")
    L.append("")
    L.append("| source | 分组 | 各种子 ci_low | 最小值 | 最大值 | 摆幅 | 是否全 > 0 |")
    L.append("|---|---|---|---|---|---|---|")
    for s in rep["Q2_seed_stability"]:
        for name in ("by_match_id", "by_match_date"):
            d = s[name]
            L.append(f"| {s['source']} | {name} | "
                     f"{', '.join(f'{v:+.4f}' for v in d['ci_low_per_seed'])} | "
                     f"{d['min']:+.4f} | {d['max']:+.4f} | {d['spread']:.4f} | "
                     f"{'是' if d['all_positive'] else '否'} |")
        L.append(f"| {s['source']} | iid(对照) | {s['iid_ci_low']:+.4f} | | | | |")
    L.append("")
    L.append("## Q2 同场跨源重叠")
    for k, n in rep["Q2_cross_source"]["intersections"].items():
        L.append(f"- {k}: {n}")
    L.append("")
    L.append("## Q3 estimator 校核 + placebo 噪声带")
    mv = rep["Q3_estimator_validation"]
    L.append(f"- 真 iid 数据: iid 覆盖率 {mv['iid_data']['iid_coverage']:.3f} / "
             f"分簇覆盖率 {mv['iid_data']['cluster_coverage']:.3f}")
    L.append(f"- 真分簇数据: iid 覆盖率 {mv['clustered_data']['iid_coverage']:.3f} / "
             f"分簇覆盖率 {mv['clustered_data']['cluster_coverage']:.3f} (名义 {mv['nominal']})")
    for p in rep["Q3_placebo"]:
        if p.get("skipped"):
            L.append(f"- {p['source']}: 跳过")
            continue
        L.append(f"- {p['source']}: 观测配对超额 {p['observed_paired_excess']:+.4f} · "
                 f"iid 噪声带 p95(点估计) {p['placebo_excess_p95_iid']:+.4f} · "
                 f"分簇噪声带 p95(点估计) {p.get('placebo_excess_p95_cluster_point') and format(p['placebo_excess_p95_cluster_point'], '+.4f')} · "
                 f"分簇 ci_low p95 {p['placebo_excess_ci_low_p95_cluster']:+.4f} · "
                 f"分簇口径下 placebo G6 放行率 {p['g6_pass_rate_cluster']:.3f} "
                 f"({p['g6_pass_cluster']}/{p['trials']})")
    L.append("")
    L.append(f"假设: {rep['assumptions']['cluster_key']}")
    return "\n".join(L) + "\n"


def main() -> int:
    REPORTS.mkdir(parents=True, exist_ok=True)
    rep = build_report()
    OUT_JSON.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_MD.write_text(render_md(rep), encoding="utf-8")
    print(f"[T56] {OUT_JSON.relative_to(REPO_ROOT)} / {OUT_MD.relative_to(REPO_ROOT)} verdict={verdict_of(rep)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
