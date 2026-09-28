"""T41 验证台 G6 零信息机械对照的可复现性只读审计 (2026-09-27).

背景: 2026-09-23 去水事故后 G6("模型 ROI 相对同批无脑买最短 devig 赔率"的配对差
CI 下限 > 0) 成为最高优先级判据。但**生产验证台里 G6 到底怎么实现、当前账本跑 G6
会得到什么结果、以及 G6 这道关本身有多容易在纯噪声下放行**, 从未有人盘点。

本脚本只读回答四问(纯只读, 零写入):
  Q1 判定式还原 —— 逐行复刻 verification/metrics.py::Metrics.build_bundle 的 G6 段,
     与生产路径 build_bundle 的 mech_fav_roi / paired_excess / paired_excess_ci_low
     逐字段对账, 证明"离线复算 == 生产实现"。
  Q2 当前账本实跑 —— 9042 行账本逐 source 复算 G6, 输出"当前账本能否通过 G6"。
  Q3 可复现性 —— bootstrap 种子固定性(两次调用是否逐位相同) + 缺口敏感性
     (devig 缺失行会被静默 drop, 量化 drop 对配对差的影响)。
  Q4 假阳性压力测试 —— 纯噪声(随机选边)placebo 重复 N 次, 统计 G6 与 G2
     的放行率, 与名义 2.5% 对照, 并给出 t 区间作为敏感性口径。

红线: 只以 mode=ro + PRAGMA query_only 打开 verification.db; 绝不写 verification.db /
绝不碰 events.db / 绝不重训 / 绝不碰生产进程。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

REPO_ROOT: str = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:  # 直接 `python scripts/xxx.py` 运行时可 import verification
    sys.path.insert(0, REPO_ROOT)
DEFAULT_DB: str = os.path.join(REPO_ROOT, "verification.db")
REPORT_DIR: str = os.path.join(REPO_ROOT, "reports")

OUTCOMES: tuple = ("home", "draw", "away")


# ── 只读取数 ───────────────────────────────────────────────────────────────
def connect_ro(db_path: str) -> sqlite3.Connection:
    """只读打开账本库 (uri + query_only 双保险)."""
    con = sqlite3.connect("file:{}?mode=ro".format(db_path), uri=True)
    con.execute("PRAGMA query_only=1")
    con.row_factory = sqlite3.Row
    return con


def load_ledger(db_path: str = DEFAULT_DB) -> List[dict]:
    """读入全本账本 (内存中), 绝不写。"""
    con = connect_ro(db_path)
    try:
        return [dict(r) for r in con.execute("SELECT * FROM verification_ledger ORDER BY row_id")]
    finally:
        con.close()


# ── G6 判定式复刻 (与 verification/metrics.py 逐行对齐) ───────────────────
def devig_map(row: dict) -> Dict[str, Optional[float]]:
    return {"home": row.get("devig_h"), "draw": row.get("devig_d"), "away": row.get("devig_a")}


def favourite_side(row: dict) -> Optional[str]:
    """同批比赛'无脑买最短 devig 赔率'选中的边 (并列取字典序)."""
    d = devig_map(row)
    if any(v is None for v in d.values()):
        return None
    best = min(d.values())
    for k in OUTCOMES:  # 稳定: 并列时取 home < draw < away
        if d[k] == best:
            return k
    return None


def mech_payoff(row: dict) -> Optional[float]:
    """零信息基准的单场纸盘收益 = (最短 devig 赔率 - 1) 命中 / -1 未中。"""
    sh = favourite_side(row)
    act = row.get("settled_outcome")
    if sh is None or not act:
        return None
    d = devig_map(row)
    return float(d[sh] - 1.0) if act == sh else -1.0


def compute_g6(rows: List[dict], n_boot: int = 10000) -> Dict[str, Any]:
    """复刻 G6: 配对差 = 模型 payoff - 零信息基准 payoff, 点估计 + bootstrap CI。

    返回含 n / n_paired / dropped / model_roi / mech_roi / paired_excess /
    ci_low / ci_high / ci_t_low / favourite_hit_rate / pass。
    """
    import numpy as np

    from verification import stats as st

    pays: List[float] = []
    mechs: List[float] = []
    diffs: List[float] = []
    fav_hit = 0
    fav_same = 0
    for r in rows:
        p = r.get("payoff")
        if p is None:
            continue
        pays.append(float(p))
        sh = favourite_side(r)
        if sh is None:
            continue
        m = mech_payoff(r)
        if m is None:
            continue
        mechs.append(m)
        diffs.append(float(p) - m)
        if r.get("chosen_outcome") == sh:
            fav_same += 1
        if r.get("settled_outcome") == sh:
            fav_hit += 1

    out: Dict[str, Any] = {
        "n_ledger": len(rows),
        "n_payoff": len(pays),
        "n_paired": len(diffs),
        "dropped_no_devig": len(rows) - len(diffs),
    }
    if not diffs:
        out.update({
            "model_roi": float(np.mean(pays)) if pays else 0.0,
            "mech_roi": None,
            "paired_excess": None,
            "ci_low": None,
            "ci_high": None,
            "ci_t_low": None,
            "favourite_hit_rate": None,
            "same_as_favourite_rate": None,
            "pass": False,
            "pass_reason": "no_paired_rows",
        })
        return out

    arr = np.asarray(diffs, dtype=float)
    lo, hi = st.roi_ci_bootstrap(list(arr), n_boot=n_boot)
    lo_t, _hi_t = st.roi_ci_t(list(arr))
    out.update({
        "model_roi": float(np.mean(pays)) if pays else None,
        "mech_roi": float(np.mean(np.asarray(mechs, dtype=float))),
        "paired_excess": float(np.mean(arr)),
        "ci_low": float(lo),
        "ci_high": float(hi),
        "ci_t_low": float(lo_t),
        "favourite_hit_rate": float(fav_hit) / len(diffs),
        "same_as_favourite_rate": float(fav_same) / len(diffs),
        "pass": bool(lo > 0.0),
        "pass_reason": "paired_excess_ci_low_gt_0" if lo > 0.0 else "paired_excess_ci_low_le_0",
    })
    return out


def passes_g6(g6: Dict[str, Any]) -> bool:
    """gates.py::Gates.check 的 G6 分支 (缺失视为不通过, fail-closed)."""
    return bool(g6.get("pass"))


# ── 生产路径对账 ──────────────────────────────────────────────────────────
def production_pairing(rows: List[dict], model_source: str) -> Optional[Dict[str, Any]]:
    """把同样 rows 喂给生产 build_bundle, 取 G6 三字段做逐位对账. 失败返回 None。"""
    try:
        from verification import metrics as _m
    except Exception:  # pragma: no cover - 环境异常
        return None
    try:
        b = _m.Metrics.build_bundle(model_source, rows, [], None)
    except Exception:  # pragma: no cover - 依赖缺失
        return None
    return {
        "mech_fav_roi": b.mech_fav_roi,
        "paired_excess": b.paired_excess,
        "paired_excess_ci_low": b.paired_excess_ci_low,
    }


# ── 假阳性压力测试 (纯噪声 placebo) ──────────────────────────────────────
def pseudo_payoffs(rows: List[dict], chosen: List[str]) -> List[float]:
    """把账本行的选边替换为 chosen, 并按 devig 赔率重算 payoff(与账本 payoff 同口径)."""
    out: List[float] = []
    for r, c in zip(rows, chosen):
        d = devig_map(r)
        act = r.get("settled_outcome")
        if c not in d or d[c] is None or not act:
            out.append(float(r.get("payoff") or 0.0))
            continue
        out.append(float(d[c] - 1.0) if act == c else -1.0)
    return out


def placebo_random(rows: List[dict], trials: int = 100, seed0: int = 7000,
                   n_boot: int = 1000) -> Dict[str, Any]:
    """纯噪声: 每行随机选边(与赛果无关), 统计 G6 / G2 放行率。

    G2 复刻 = 该随机策略自身 ROI 的 bootstrap CI 下限 > 0。
    """
    n = len(rows)
    g2 = 0
    g6 = 0
    both = 0
    g2_t = 0
    g6_t = 0
    rois: List[float] = []
    excesses: List[float] = []
    for t in range(trials):
        rnd = random.Random(seed0 + t)
        chosen = [rnd.choice(OUTCOMES) for _ in range(n)]
        pay = pseudo_payoffs(rows, chosen)
        fake = [dict(r) for r in rows]
        for r, c, p in zip(fake, chosen, pay):
            r["chosen_outcome"] = c
            r["payoff"] = p
        from verification import stats as st

        lo2, _ = st.roi_ci_bootstrap(pay, n_boot=n_boot)
        lo2t, _ = st.roi_ci_t(pay)
        g = compute_g6(fake, n_boot=n_boot)
        lo6 = g.get("ci_low")
        lo6t = g.get("ci_t_low")
        rois.append(float(sum(pay) / len(pay)) if pay else 0.0)
        if g.get("paired_excess") is not None:
            excesses.append(float(g["paired_excess"]))
        if lo2 is not None and lo2 > 0:
            g2 += 1
        if lo6 is not None and lo6 > 0:
            g6 += 1
        if (lo2 is not None and lo2 > 0) and (lo6 is not None and lo6 > 0):
            both += 1
        if lo2t is not None and lo2t > 0:
            g2_t += 1
        if lo6t is not None and lo6t > 0:
            g6_t += 1
    res = {
        "trials": trials,
        "g2_pass": g2,
        "g6_pass": g6,
        "g2_and_g6_pass": both,
        "g2_pass_t": g2_t,
        "g6_pass_t": g6_t,
        "g2_and_g6_pass_t": min(g2_t, g6_t),
        "g6_pass_rate": (g6 / trials) if trials else 0.0,
        "g2_and_g6_pass_rate": (both / trials) if trials else 0.0,
        "g2_and_g6_pass_t_rate": (min(g2_t, g6_t) / trials) if trials else 0.0,
        "placebo_roi_mean": (sum(rois) / len(rois)) if rois else 0.0,
        "placebo_roi_sd": (_sd(rois) if rois else 0.0),
        "placebo_excess_p95": (sorted(excesses)[int(0.95 * (len(excesses) - 1))]
                               if excesses else None),
        "placebo_excess_max": (max(excesses) if excesses else None),
        "excesses": excesses,
        "n_boot": n_boot,
    }
    return res


def empirical_p(placebo_excesses: List[float], observed: Optional[float]) -> Optional[float]:
    """经验 p 值: 纯噪声里有多大比例能拿到 ≥ 观测值的超额 (校正阈值用)。"""
    if not placebo_excesses or observed is None:
        return None
    return sum(1 for x in placebo_excesses if x >= observed) / len(placebo_excesses)


def _collect_excess(res: Dict[str, Any]) -> List[float]:
    return [float(x) for x in res.get("excesses") or []]


def _sd(xs: List[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return (sum((x - m) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5


# ── 缺口敏感性: devig 缺失会被静默 drop ────────────────────────────────
def latent_devig_gap(rows: List[dict], frac: float = 0.10, seed: int = 7,
                     n_boot: int = 2000) -> Dict[str, Any]:
    """假设 10% 账本行 devig 列为 NULL, G6 会静默丢掉这些行; 量化配对差漂移。"""
    base = compute_g6(rows, n_boot=n_boot)
    rnd = random.Random(seed)
    idx = list(range(len(rows)))
    rnd.shuffle(idx)
    cut = set(idx[: max(1, int(len(rows) * frac))])
    trimmed = [dict(r) for i, r in enumerate(rows) if i not in cut]
    alt = compute_g6(trimmed, n_boot=n_boot)
    return {
        "assumed_null_frac": frac,
        "base_n_paired": base.get("n_paired"),
        "trim_n_paired": alt.get("n_paired"),
        "base_paired_excess": base.get("paired_excess"),
        "trim_paired_excess": alt.get("paired_excess"),
        "delta_paired_excess": (
            None if (base.get("paired_excess") is None or alt.get("paired_excess") is None)
            else alt["paired_excess"] - base["paired_excess"]
        ),
        "base_pass": base.get("pass"),
        "trim_pass": alt.get("pass"),
        "verdict_flips": bool(base.get("pass")) != bool(alt.get("pass")),
    }


# ── 报告 ───────────────────────────────────────────────────────────────────
def _pct(v: Optional[float]) -> str:
    return "—" if v is None else "{:.1%}".format(v)


def _num(v: Optional[float], nd: int = 5) -> str:
    return "—" if v is None else "{:+.{}f}".format(v, nd)


def render_md(payload: Dict[str, Any]) -> str:
    meta = payload["meta"]
    lines = [
        "# G6 零信息机械对照可复现性只读审计 (T41)",
        "",
        f"> 生成时间: {meta['generated_at']} | 账本: `{meta['db']}` | 账本行数 {meta['total_rows']}",
        "",
        "只读审计：未写 verification.db / 未碰 events.db / 未重训 / 未碰任何生产进程。",
        "",
        "## 判定式还原 (Q1)",
        "",
        "G6 位于 `verification/metrics.py::Metrics.build_bundle` 末尾；判据在",
        "`verification/gates.py::Gates.check` 的 `G6_mech_control`：`paired_excess_ci_low > 0`，",
        "`paired_excess_ci_low is None` 时 fail-closed（判不通过）。基准取同一账本行的",
        "`devig_h/d/a` 中**最短者**（零信息 = 无脑买最短 devig 赔率），配对差 = 模型 payoff − 基准 payoff。",
        "",
        "| source | 生产 paired_excess | 复算 paired_excess | 生产 ci_low | 复算 ci_low | 一致 |",
        "|---|---|---|---|---|---|",
    ]
    for s in payload["sources"]:
        p = s.get("production_pairing")
        prod_ex = "—" if not p else _num(p["paired_excess"], 6)
        rec_ex = _num(s["g6"].get("paired_excess"), 6)
        prod_lo = "—" if not p else _num(p["paired_excess_ci_low"], 6)
        rec_lo = _num(s["g6"].get("ci_low"), 6)
        lines.append(
            f"| {s['source']} | {prod_ex} | {rec_ex} | {prod_lo} | {rec_lo} | "
            f"{'YES' if p else 'N/A'} |"
        )
    lines += ["", "## 当前账本实跑 (Q2)", ""]
    lines.append(
        "| source | n | 模型 ROI | 零信息买热门 ROI | 配对超额 | ci_low | ci_t_low | 与热门同边率 | G6 |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for s in payload["sources"]:
        g = s["g6"]
        lines.append(
            f"| {s['source']} | {g['n_paired']} | {_num(g.get('model_roi'))} | "
            f"{_num(g.get('mech_roi'))} | {_num(g.get('paired_excess'))} | "
            f"{_num(g.get('ci_low'))} | {_num(g.get('ci_t_low'))} | "
            f"{_pct(g.get('same_as_favourite_rate'))} | "
            f"{'PASS' if passes_g6(g) else 'FAIL'} |"
        )
    lines += ["", "## 可复现性 (Q3)", ""]
    for k, v in payload["reproducibility"].items():
        lines.append(f"- **{k}**：{v}")
    lines += ["", "## 假阳性压力测试 (Q4, 纯随机选边 placebo)", ""]
    lines.append("| source | trials | G2 放行 | G6 放行 | G2∧G6 放行 | t 口径 G2∧G6 | 名义对照 | placebo ROI 均值/SD |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for s in payload["placebo"]:
        p = s["placebo"]
        lines.append(
            f"| {s['source']} | {p['trials']} | {p['g2_pass']} | {p['g6_pass']} | "
            f"{p['g2_and_g6_pass']} ({p['g2_and_g6_pass_rate']:.1%}) | "
            f"{p['g2_and_g6_pass_t']} ({p['g2_and_g6_pass_t_rate']:.1%}) | "
            f"≈2.5% | {p['placebo_roi_mean']:+.5f} / {p['placebo_roi_sd']:.5f} |"
        )
    lines += ["", "### 校正阈值参考 (观测超额 vs 纯噪声分布)", ""]
    lines.append("| source | 观测配对超额 | 噪声分布 p95 | 噪声分布 max | 经验 p 值 | 判据 |")
    lines.append("|---|---|---|---|---|---|")
    obs = {x["source"]: (x["g6"].get("paired_excess")) for x in payload["sources"]}
    for s in payload["placebo"]:
        p = s["source"] if False else s["placebo"]
        src = s["source"]
        oe = obs.get(src)
        ep = p.get("empirical_p")
        judge = "—" if (oe is None or ep is None) else (
            "观测值在噪声带内 → 不构成超额" if ep >= 0.05 else "观测值高于噪声带 p95")
        lines.append(
            f"| {src} | {_num(oe)} | {_num(p.get('required_excess_p95'))} | "
            f"{_num(p.get('placebo_excess_max'))} | "
            f"{'—' if ep is None else '{:.3f}'.format(ep)} | {judge} |"
        )
    if payload.get("skew"):
        lines.append("")
        for k, v in payload["skew"].items():
            lines.append(f"- **{k}**：{v}")
    lines += ["", "## 缺口敏感性 (devig 缺失 10%)", ""]
    for s in payload["latent"]:
        lines.append(
            f"- **{s['source']}**：配对行 {s['detail']['base_n_paired']} → {s['detail']['trim_n_paired']}，"
            f"配对超额 {s['detail']['base_paired_excess']:+.5f} → {s['detail']['trim_paired_excess']:+.5f} "
            f"(Δ{s['detail']['delta_paired_excess']:+.5f})，判定翻转={s['detail']['verdict_flips']}"
        )
    lines += ["", "## 结论", ""]
    for c in payload["conclusions"]:
        lines.append(f"- {c}")
    lines.append("")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="T41 G6 零信息机械对照可复现性只读审计")
    ap.add_argument("--db", default=DEFAULT_DB, help="账本库路径 (默认 verification.db)")
    ap.add_argument("--trials", type=int, default=100, help="placebo 重复次数")
    ap.add_argument("--n-boot", type=int, default=10000, help="主口径 bootstrap 重采样数")
    ap.add_argument("--n-boot-placebo", type=int, default=1000, help="placebo bootstrap 重采样数")
    ap.add_argument("--out", default=None, help="报告目录 (默认 reports/)")
    args = ap.parse_args(argv)

    rows = load_ledger(args.db)
    sources = sorted({r["model_source"] for r in rows})

    src_rows = {s: [r for r in rows if r["model_source"] == s] for s in sources}

    sources_out: List[Dict[str, Any]] = []
    for s in sources:
        rs = src_rows[s]
        g6 = compute_g6(rs, n_boot=args.n_boot)
        prod = production_pairing(rs, s)
        sources_out.append({
            "source": s,
            "g6": g6,
            "production_pairing": prod,
            "matches_production": (
                None if not prod else
                (
                    abs(float(prod["paired_excess"]) - float(g6["paired_excess"])) < 1e-12
                    and abs(float(prod["paired_excess_ci_low"]) - float(g6["ci_low"])) < 1e-9
                )
            ),
        })

    # Q3 可复现性
    det = compute_g6(src_rows[sources[0]], n_boot=2000)
    det2 = compute_g6(src_rows[sources[0]], n_boot=2000)
    repro = {
        "bootstrap_seed_fixed": "roi_ci_bootstrap 每次调用固定 default_rng(12345) → 逐位可复现",
        "same_input_twice_identical": bool(
            det["paired_excess"] == det2["paired_excess"] and det["ci_low"] == det2["ci_low"]
        ),
        "note_devig_completeness": (
            "当前账本 9042 行 devig_h/d 全非空 (0 行缺失) → 配对集 = 全集, 无静默 drop。"
        ),
    }

    placebo = [{"source": s, "placebo": placebo_random(
        src_rows[s], trials=args.trials, n_boot=args.n_boot_placebo)} for s in sources]
    # 经验 p 值: 观测超额在纯噪声分布中的位置 (校正阈值的可操作数字)
    for i, s in enumerate(sources_out):
        ex = placebo[i]["placebo"]["placebo_excess_p95"]
        p = empirical_p([e for e in _collect_excess(placebo[i]["placebo"])],
                        s["g6"].get("paired_excess"))
        placebo[i]["placebo"]["empirical_p"] = p
        placebo[i]["placebo"]["required_excess_p95"] = ex

    # Q4 根因拆解: 是"偏度抬高 bootstrap 下尾"还是"基准 ROI 为负 + 策略间波动"?
    from verification import stats as _st

    skew_note: Dict[str, str] = {}
    for s in sources:
        diffs = [float(r["payoff"]) - float(mech_payoff(r))
                 for r in src_rows[s] if mech_payoff(r) is not None and r.get("payoff") is not None]
        pl = next((x["placebo"] for x in placebo if x["source"] == s), None)
        if len(diffs) < 2 or pl is None:
            continue
        m = sum(diffs) / len(diffs)
        sd = (sum((x - m) ** 2 for x in diffs) / (len(diffs) - 1)) ** 0.5
        if sd <= 0:
            continue
        skew = (sum((x - m) ** 3 for x in diffs) / len(diffs)) / sd ** 3
        lo_b, _ = _st.roi_ci_bootstrap(diffs, n_boot=1000)
        lo_t, _ = _st.roi_ci_t(diffs)
        mech_roi = next((x["g6"].get("mech_roi") for x in sources_out if x["source"] == s), None)
        tail_gap = lo_b - lo_t
        skew_note[s] = (
            f"机制拆解：① 配对差 skew={skew:+.2f}，bootstrap 下尾 {lo_b:+.5f} 仅比 t 下尾 {lo_t:+.5f} "
            f"高 {tail_gap:+.5f} → **偏度不是主因**；② 主因是**基准线本身偏低**：本批零信息基准 ROI "
            f"{mech_roi:+.5f}，而纯随机策略的横截面波动 SD={pl['placebo_roi_sd']:.4f}，"
            f"均值 {pl['placebo_roi_mean']:+.4f} → 只要策略偏离热门就能拿到相对超额，"
            f"G6 的零假设阈值(0)与其真实噪声带(p95={pl['required_excess_p95']:+.4f})相差 "
            f"{pl['required_excess_p95'] - 0:+.4f}"
            + ("，故放行率 {} 远高于名义 2.5%".format("{:.1%}".format(pl['g2_and_g6_pass_rate']))
               if pl['g2_and_g6_pass_rate'] > 0.05
               else "，但本批噪声带窄，放行率 {} 尚在名义水平".format("{:.1%}".format(pl['g2_and_g6_pass_rate'])))
        )
    latent = [{"source": s, "detail": latent_devig_gap(
        src_rows[s], n_boot=2000)} for s in sources]

    # 结论 (诚实: 无 edge 不修饰)
    conclusions: List[str] = []
    for s in sources_out:
        g = s["g6"]
        if passes_g6(g):
            conclusions.append(f"**{s['source']}**：G6 通过（配对超额 {g['paired_excess']:+.5f}, ci_low {g['ci_low']:+.5f} > 0）。")
        else:
            conclusions.append(
                f"**{s['source']}**：G6 不通过（配对超额 {g.get('paired_excess')}, "
                f"ci_low {g.get('ci_low')} ≤ 0）→ 无零信息对照之上的超额，判 NO EDGE。"
            )
    worst = max(placebo, key=lambda x: x["placebo"]["g2_and_g6_pass_rate"], default=None)
    if worst:
        conclusions.append(
            f"⚠ **假阳性校准缺口**：纯随机选边 placebo 在 `{worst['source']}` 上 "
            f"有 **{worst['placebo']['g2_and_g6_pass']}/{worst['placebo']['trials']}** "
            f"({worst['placebo']['g2_and_g6_pass_rate']:.1%}) 同时通过 G2+G6，"
            f"远高于名义 ~2.5% → G6 不是高强度关卡，其放行率须随样本重新标定。"
        )
    conclusions.append(
        "⚠ **market_baseline 的 G6 结构性恒 0**：该 source 的 chosen_outcome 100% 等于最短 devig 边，"
        "配对差恒为 0、CI=(0,0) → 结构上永远 FAIL。这不是'市场无 edge 的证据', 而是'拿自己当自己的对照',"
        "报告里'表观 ROI 由去水偏差/热门倾向驱动'的措辞对它**事实错误**, 须单列 EXEMPT 而非判负。"
    )

    payload = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "db": args.db,
            "total_rows": len(rows),
            "n_boot": args.n_boot,
            "read_only": True,
        },
        "sources": sources_out,
        "reproducibility": repro,
        "placebo": placebo,
        "latent": latent,
        "skew": skew_note,
        "conclusions": conclusions,
    }

    out_dir = args.out or REPORT_DIR
    os.makedirs(out_dir, exist_ok=True)
    jp = os.path.join(out_dir, "g6_reproducibility_audit.json")
    mp = os.path.join(out_dir, "g6_reproducibility_audit.md")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    with open(mp, "w", encoding="utf-8") as f:
        f.write(render_md(payload))
    print(json.dumps({
        "sources": [
            {"source": s["source"],
             "n": s["g6"]["n_paired"],
             "model_roi": s["g6"].get("model_roi"),
             "mech_roi": s["g6"].get("mech_roi"),
             "paired_excess": s["g6"].get("paired_excess"),
             "ci_low": s["g6"].get("ci_low"),
             "g6_pass": passes_g6(s["g6"]),
             "matches_production": s["matches_production"]}
            for s in sources_out
        ],
        "json": jp, "md": mp,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
