#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T53 mh 训练脚本绕过 gates.verdict 的只读盘点 + 静态守卫规格数据面（承接 T51 R6）。

只读事实清单：
  Q1 产出体盘点 —— 全仓 .py 的「自印三态判定」字面量，按 Tier 分类；重点是把
     scripts/mh_train_div.py / mh_train_walkforward.py / mh_train_walkforward_x.py
     三个「真绕过体」从 T51 的漏检里捞出来（T51 的正则要求字面量两侧带引号，
     而这三个文件的判定词在行尾是无引号的 ``NO EDGE (model >= market)``）。
  Q2 bundle 映射 —— MetricsBundle 每个字段对 mh 口径「现成 / 可算 / 缺」三档裁定。
  Q3 G1 计数口径 —— 复算 mh 数据集的 walk-forward 折切分，给出「按样本 n」与
     「按场次 m」两个计数，证明 G1 在二选一口径下必有一边失真。
  Q4 噪声底 —— 对市场基线做**按场次聚类** bootstrap，估计 ΔLL 的噪声尺度，
     与 mh 硬编码的 ±0.001 判定带对比。
  Q5 判定映射 —— mh 三分支标签 -> gates.verdict 的确定性映射（纯函数，可测）。

铁律：本脚本只读 data/mh_dataset_x.csv（纯 CSV，不连任何库）、不 import 任何训练
入口、不跑训练、不写 events.db、不改 verification/、不碰调度、零进程操作。

⚠ 本文件禁止写出 ``python -m verification ...`` 形状的命令行字面量 ——
   T47 的 ``SCHEDULED_CLI`` 正则按该形状识别调度方, 本文件出现一次就会把
   T47 的「零调度方」结论从 0 打成 1。同理, 判定词一律以「分类常量」形式出现。
"""
from __future__ import annotations

import json
import math
import os
import random
import re
import statistics
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

REPO_ROOT: str = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "scripts") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import verdict_guard_ssot as VG  # noqa: E402  (T57 判定词表/Tier SSoT)

ROOT: str = REPO_ROOT
SCRIPTS_DIR: str = os.path.join(ROOT, "scripts")
CONSTANTS_PY: str = os.path.join(ROOT, "verification", "constants.py")
GATES_PY: str = os.path.join(ROOT, "verification", "gates.py")
METRICS_PY: str = os.path.join(ROOT, "verification", "metrics.py")
DATASET: str = os.path.join(ROOT, "data", "mh_dataset_x.csv")
REPORTS_DIR: str = os.path.join(ROOT, "reports")
OUT_JSON: str = os.path.join(REPORTS_DIR, "mh_train_div_bypass_audit.json")
OUT_MD: str = os.path.join(REPORTS_DIR, "mh_train_div_bypass_audit.md")

_SELF: str = os.path.basename(__file__)
_SELF_EXCLUDE: Tuple[str, ...] = (_SELF, "test_" + _SELF, "audit_mh_train_div_bypass_spec.md")

# ── Q1 判定词表 ─────────────────────────────────────────────────────────
# T57 合流: 词表与 Tier 表的 SSoT 已上收到 ``verdict_guard_ssot.py``。
# 本模块**禁止**自带判定词正则 —— 与 T51 各写一份正是「同一个文件两个守卫两种 Tier」的病根。
RE_VERDICT_TOKEN = VG.RE_VERDICT_TOKEN
ALLOWED_VERDICTS: Tuple[str, ...] = VG.ALLOWED_VERDICTS

# 分层：白名单外的任何命中都算「未登记产出体」→ RED（防豁免清单静默长大）。
# T57: Tier 常量与登记册全部取自 ``verdict_guard_ssot`` SSoT, 本模块只保留旧名别名,
# 保证两个守卫对**同一个文件**给出同一个 Tier（此前 T51 会把 mh_train_walkforward.py
# 判成 UNEXPECTED 而本模块判成 EXPECTED_TIER —— 两份 Tier 表互相打脸）。
TIER_SINGLE_EXIT: str = VG.TIER_SINGLE_EXIT        # verification/ 包内, gates.verdict 唯一出口
TIER_RENDER: str = VG.TIER_RENDER                  # 只渲染不产判定
TIER_AUDIT_TOOL: str = VG.TIER_AUDIT_TOOL          # 审计/测试脚本（自身含词表字面量）
TIER_DOC_META: str = VG.TIER_DOC
TIER_NOISE: str = VG.TIER_NOISE                    # NO_EDGE 当盘口信号用
TIER_EXPECTED: str = VG.TIER_EXPECTED              # 已知绕过体，必须显式登记
TIER_UNEXPECTED: str = VG.TIER_UNEXPECTED          # 未登记 → RED

# 已知绕过体（T51 旧词表只捞到 1 个；T53 补出另外 2 个，T57 起与 T51 共用同一登记册）
EXPECTED_EMITTERS: Tuple[str, ...] = tuple(sorted(VG.EMITTER_REGISTRY))
# 已判定的假阳性（阈值字典键，非产出体）—— 与 T51 的 UNEXPECTED 清单共用同一登记册
KNOWN_FALSE_POSITIVES: Tuple[str, ...] = tuple(sorted(VG.FALSE_POSITIVE_REGISTRY))

NOISE_PREFIXES: Tuple[str, ...] = VG.NOISE_PREFIXES
AUDIT_MARKERS: Tuple[str, ...] = VG.DOC_META_MARKERS
# 「词汇撞车」登记表: 这些文件里的判定词**不是三态判定**（每条必须写理由, 空理由即不许登记）
NOISE_FILES: Dict[str, str] = dict(VG.NOISE_FILE_REGISTRY)
DOC_QUOTE_FILES: Dict[str, str] = dict(VG.DOC_QUOTE_REGISTRY)
SKIP_DIR_PREFIXES: Tuple[str, ...] = (
    "archive/", ".venv/", ".git/", ".workbuddy/", ".codebuddy/", ".zcode/",
    "frontend/node_modules/", "backend/node_modules/", "reports/", "deliverables/",
    "logs/", "models/", "data/", "odds_db/", "node_modules/",
)
MAX_SCAN_LINES: int = 3000

# ── Q3/Q4 复算参数（与 mh_train_*.py 的 cuts=[0.60,0.80,0.90] 对齐）──────
FOLD_CUTS: Tuple[float, ...] = (0.60, 0.80, 0.90)
CHECKPOINTS: Tuple[int, ...] = (0, 45, 60, 75, 85)
BOOTSTRAP_REPS: int = 600
BOOTSTRAP_SEED: int = 20260928
DELTA_BAND: float = 0.001          # mh 三个文件硬编码的判定带
G1_MIN_SAMPLE: int = 2500          # verification/constants.py 基线值

# ── Q2 bundle 映射口径 ──────────────────────────────────────────────────
# 取值: READY(现成) / COMPUTABLE(可算) / MISSING(缺)
BUNDLE_FIELDS: Tuple[str, ...] = (
    "n", "roi_point", "roi_ci_low", "roi_ci_high", "roi_method",
    "log_loss", "brier", "ece", "slope", "accuracy", "vs_market_ll",
    "direction_binomial_p", "mech_fav_roi", "paired_excess",
    "paired_excess_ci_low", "model_source",
)
MH_BUNDLE_MAP: Dict[str, str] = {
    "n": "COMPUTABLE",
    "roi_point": "MISSING",
    "roi_ci_low": "MISSING",
    "roi_ci_high": "MISSING",
    "roi_method": "MISSING",
    "log_loss": "COMPUTABLE",
    "brier": "COMPUTABLE",
    "ece": "COMPUTABLE",
    "slope": "COMPUTABLE",
    "accuracy": "COMPUTABLE",
    "vs_market_ll": "READY",
    "direction_binomial_p": "COMPUTABLE",
    "mech_fav_roi": "MISSING",
    "paired_excess": "MISSING",
    "paired_excess_ci_low": "MISSING",
    "model_source": "MISSING",
}

FINDING_RED: str = "RED"
FINDING_AMBER: str = "AMBER"


# ── 基础设施 ────────────────────────────────────────────────────────────
def self_exclude() -> Tuple[str, ...]:
    """本脚本 + 自身测试 + T57 共享 SSoT 模块（三者都含判定词字面量）。"""
    return _SELF_EXCLUDE + tuple(VG.GUARD_SELF_EXCLUDE)


def _rel(path: str) -> str:
    try:
        return os.path.relpath(path, ROOT).replace(os.sep, "/")
    except ValueError:
        return os.path.basename(path)


def read_text(path: str) -> Optional[str]:
    try:
        if os.path.getsize(path) > 64 * 1024 * 1024:
            return None
        with open(path, "rb") as fh:
            raw = fh.read()
        for enc in ("utf-8", "gbk", "latin-1"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return None
    except OSError:
        return None


def iter_python_files(root: str = ROOT) -> List[str]:
    out: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        for skip in SKIP_DIR_PREFIXES:
            if _rel(dirpath).startswith(skip.rstrip("/")):
                dirnames[:] = []
                break
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".pytest_tmp")]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(dirpath, fn)
            if os.path.basename(p) in self_exclude():
                continue
            try:
                if sum(1 for _ in open(p, "r", encoding="utf-8", errors="ignore")) > MAX_SCAN_LINES:
                    continue
            except OSError:
                continue
            out.append(p)
    return sorted(out, key=_rel)


# ── Q1 产出体盘点 ───────────────────────────────────────────────────────
def classify_file(rel: str) -> str:
    """把一个含判定词的文件分到某个 Tier（白名单外的都算 UNEXPECTED）。

    T57: 判定函数已上收到 ``verdict_guard_ssot.classify_verdict_file``。
    分叉前的真实风险: 旧实现把 mh_train_walkforward.py 判成 EXPECTED_TIER,
    而 T51 对**同一个文件**判 UNEXPECTED —— 两个守卫互相打脸, 放宽词表后
    T51 会永久红在已知 offenders 上, 于是「守卫」退化成背景噪声。
    """
    return VG.classify_verdict_file(rel)


def scan_verdict_emitters(root: str = ROOT) -> dict:
    """全仓 .py 判定词盘点（放宽版正则：同时抓带引号与行尾无引号的写法）。"""
    buckets: Dict[str, List[dict]] = {}
    for path in iter_python_files(root):
        rel = _rel(path)
        text = read_text(path)
        if text is None:
            continue
        tier = classify_file(rel)
        for i, line in enumerate(text.splitlines(), 1):
            for mt in RE_VERDICT_TOKEN.finditer(line):
                buckets.setdefault(tier, []).append(
                    {"file": rel, "line": i, "token": mt.group(0), "tier": tier}
                )
    out = {t: v for t, v in buckets.items()}
    out["expected"] = buckets.get(TIER_EXPECTED, [])
    out["unexpected"] = buckets.get(TIER_UNEXPECTED, [])
    out["unexpected_count"] = len(out["unexpected"])
    out["noise_count"] = len(buckets.get(TIER_NOISE, []))
    return out


def scan_expectation_registry() -> dict:
    """既有登记项是否还在（退役未登记 = AMBER，防止名单静默失效）。"""
    reg: List[dict] = []
    for rel in EXPECTED_EMITTERS:
        p = os.path.join(ROOT, rel)
        text = read_text(p) if os.path.exists(p) else None
        hits = 0
        if text:
            hits = sum(len(RE_VERDICT_TOKEN.findall(ln)) for ln in text.splitlines())
        reg.append({"file": rel, "exists": os.path.exists(p), "verdict_literal_hits": hits})
    return {"registry": reg, "all_present": all(r["exists"] for r in reg),
            "with_literals": sum(1 for r in reg if r["verdict_literal_hits"] > 0)}


# ── Q2 bundle 映射 ──────────────────────────────────────────────────────
def bundle_field_map() -> dict:
    missing = [f for f in BUNDLE_FIELDS if MH_BUNDLE_MAP.get(f) == "MISSING"]
    return {
        "fields": list(BUNDLE_FIELDS),
        "state_of": dict(MH_BUNDLE_MAP),
        "missing": missing,
        "missing_count": len(missing),
        "ready": [f for f in BUNDLE_FIELDS if MH_BUNDLE_MAP.get(f) == "READY"],
        "computable": [f for f in BUNDLE_FIELDS if MH_BUNDLE_MAP.get(f) == "COMPUTABLE"],
    }


def gates_verdict_for_mh(label: str, n_samples: Optional[int] = None,
                         n_matches: Optional[int] = None) -> dict:
    """mh 自印标签 -> gates.verdict 的确定性映射（不跑训练，纯语义）。

    依据 gates.verdict 结构：
      · 走 G1 先看计数口径（样本 vs 场次，二选一必失真，见 Q3）；
      · G3(=mh 的 ΔLL<0) 单独成立**不足以**产出判定，EDGE 还需 G2/G4/G5/G6 全绿；
      · mh 三个分支里只有「BEATS MARKET」这一支的结论会翻转，其余两支同判 NO EDGE。
    """
    lab = (label or "").strip()
    if lab.startswith("BEATS"):
        branch, mh_verdict = "BEATS MARKET", "MODEL 优于市场基线(仅 G3 成立)"
    elif lab == "TIE":
        branch, mh_verdict = "TIE", "ΔLL 在 ±0.001 带内"
    else:
        branch, mh_verdict = "NO EDGE", "ΔLL > 0.001 (模型劣于市场)"
    if lab.startswith("BEATS"):
        g3 = True
    elif lab == "TIE":
        g3 = False          # vs_market_ll < 0 不成立（≈0 不小于 0）
    else:
        g3 = False
    blocking = ["G2_roi_ci", "G4_calib", "G5_dir_binom", "G6_mech_control"] if g3 \
        else ["G3_quality"]
    return {
        "branch": branch,
        "mh_label": mh_verdict,
        "g3_quality": g3,
        # gates 侧永远不产 EDGE: mh 口径无 payoff / 无 G6 三元组, 而 G2+G6 缺一即 NO EDGE
        "gates_verdict": "NO EDGE",
        "edge_claimable": False,
        "blocking_gates": blocking,
        "reason": ("G3 单独成立不足以产出 EDGE；G2/G4/G5/G6 无数据 → 判 NO EDGE" if g3
                   else "G3(Gates 口径) 未通过 → 判 NO EDGE"),
        "conclusion_changed": lab.startswith("BEATS"),
        "conclusion_note": (
            "mh 的「BEATS MARKET」在 gates 下只会变成 NO EDGE"
            if lab.startswith("BEATS") else "mh 标签与 gates 判定一致，结论不变"
        ),
        "n_samples_g1": ("PASS" if (n_samples or 0) >= G1_MIN_SAMPLE else "FAIL"),
        "n_matches_g1": ("PASS" if (n_matches or 0) >= G1_MIN_SAMPLE else "FAIL"),
        "semantics_delta": (
            "mh 语义 = 单门禁 G3(ΔLL<0, 阈值 ±0.001, 无 CI); "
            "gates 语义 = 六门禁合取(G1∧G2∧G3∧G4∧G5∧G6), 且 EDGE 必须与 ROI/机械对照同时成立"
        ),
    }


# ── Q3 walk-forward 复算（纯 CSV，不连库）────────────────────────────────
def load_rows(path: str) -> Optional[List[dict]]:
    if not os.path.exists(path):
        return None
    text = read_text(path)
    if text is None:
        return None
    rows: List[dict] = []
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    header = lines[0].split(",")
    for ln in lines[1:]:
        parts = ln.split(",")
        if len(parts) < len(header):
            parts = parts + [""] * (len(header) - len(parts))
        r = dict(zip(header, parts[: len(header)]))
        try:
            r["_y"] = int(r.get("ridx", "-1"))
            r["_kt"] = float(r.get("kickoff_epoch", "0"))
            r["_cp"] = int(r.get("cp", "-1"))
        except ValueError:
            continue
        rows.append(r)
    return rows


def _neg_log(p: float) -> float:
    p = max(min(p, 0.999), 0.001)
    return -math.log(p)


def market_ll_of(row: dict) -> float:
    imp = [float(row.get("imp_home", 0)) or 0.0,
           float(row.get("imp_draw", 0)) or 0.0,
           float(row.get("imp_away", 0)) or 0.0]
    s = sum(imp)
    if s <= 0:
        return float("nan")
    norm = [x / s for x in imp]
    y = row["_y"]
    if not (0 <= y < 3):
        return float("nan")
    return _neg_log(norm[y])


def walkforward_g1_counts(rows: Sequence[dict]) -> dict:
    """复刻 mh_train_*.py 的按 match 时间序折切分，给出样本/场次两套 G1 计数。"""
    by: Dict[str, List[dict]] = {}
    for r in rows:
        by.setdefault(r.get("match_key", ""), []).append(r)
    mks = sorted(by, key=lambda m: min(r["_kt"] for r in by[m]))
    folds: List[dict] = []
    tot_s = tot_m = 0
    for c in FOLD_CUTS:
        k = int(len(mks) * c)
        tem = mks[k:]
        if not tem:
            continue
        te = [r for m in tem for r in by[m]]
        mo = 0.0
        for r in te:
            try:
                mo += market_ll_of(r)
            except ValueError:
                pass
        n_ok = sum(1 for r in te if not math.isnan(market_ll_of(r)))
        folds.append({"cut": c, "test_matches": len(tem), "test_samples": len(te),
                      "market_ll": (mo / n_ok) if n_ok else None})
        tot_s += len(te)
        tot_m += len(tem)
    deff = (tot_s / tot_m) if tot_m else 0.0
    return {
        "folds": folds,
        "total_test_samples": tot_s,
        "total_test_matches": tot_m,
        "checkpoints_per_match": round(deff, 3),
        "g1_by_sample": tot_s >= G1_MIN_SAMPLE,
        "g1_by_match": tot_m >= G1_MIN_SAMPLE,
        "design_effect": round(deff, 3),
        "naive_ci_understate_factor": round(math.sqrt(deff), 3) if deff > 0 else 0.0,
    }


def cluster_bootstrap_sd(values: Sequence[float], reps: int = BOOTSTRAP_REPS,
                         seed: int = BOOTSTRAP_SEED) -> float:
    """按「同一场比赛」整簇重抽的 bootstrap SD（防同一场多 checkpoint 被当成独立样本）。"""
    if len(values) < 2:
        return 0.0
    rng = random.Random(seed)
    m = len(values)
    draws: List[float] = []
    for _ in range(max(1, reps)):
        picks = [values[rng.randrange(m)] for _ in range(m)]
        draws.append(sum(picks) / m)
    return statistics.pstdev(draws)


def noise_floor(rows: Sequence[dict]) -> dict:
    """对市场基线 LL 做按场次聚类 bootstrap，估计 ΔLL 的噪声尺度下界。"""
    by: Dict[str, List[dict]] = {}
    for r in rows:
        by.setdefault(r.get("match_key", ""), []).append(r)
    mks = sorted(by, key=lambda m: min(r["_kt"] for r in by[m]))
    out: List[dict] = []
    for c in FOLD_CUTS:
        k = int(len(mks) * c)
        tem = mks[k:]
        if not tem:
            continue
        per_match = []
        for m in tem:
            lst = [market_ll_of(r) for r in by[m]]
            vals = [v for v in lst if not math.isnan(v)]
            if vals:
                per_match.append(sum(vals) / len(vals))
        if not per_match:
            continue
        sd = cluster_bootstrap_sd(per_match)
        out.append({
            "cut": c, "matches": len(per_match),
            "cluster_sd_market_ll": round(sd, 6),
            "band_over_sd": round(DELTA_BAND / sd, 6) if sd else None,
        })
    return {"per_fold": out, "band": DELTA_BAND,
            "worst_band_over_sd": min((f["band_over_sd"] for f in out if f["band_over_sd"]),
                                      default=None)}


DEVIG_SRC: str = os.path.join(SCRIPTS_DIR, "mh_build_dataset.py")


def devig_caliber_of(constants_src: Optional[str], dataset_src: Optional[str]) -> dict:
    """mh 数据集的去水口径裁定（比例法 vs 幂法）—— 决定 G6 机械基准可否直接沿用。

    ``fair_probs()`` = 比例法(1/odds 归一) = SSoT 里的 devig_n/devig3,
    不是幂法 → 用它反算赔率搭 G6 机械基准会复刻 09-23 事故。
    """
    text = (constants_src or "")
    has_prop = bool(re.search(r"inv\s*=\s*\[1\.0\s*/\s*o[^\]]*\]", text))
    has_power = "devig_power" in text or "power" in text.lower()
    return {
        "dataset": _rel(DEVIG_SRC),
        "proportional_method_found": has_prop,
        "power_method_found": has_power,
        "verdict": ("比例法(fair_probs)" if has_prop else "未识别"),
        "g6_ready": has_power,
        "note": (
            "比例法系统性高估热门 → 用它反算赔率搭 G6 机械基准会复刻 09-23 事故; "
            "但 vs_market_ll 与市场基线同源同口径, 差值是自洽的。"
        ),
    }


def constant_baseline() -> dict:
    txt = read_text(CONSTANTS_PY) or ""
    m = re.search(r"^MIN_SAMPLE:\s*int\s*=\s*(\d+)", txt, re.M)
    return {"min_sample": int(m.group(1)) if m else None, "expected": G1_MIN_SAMPLE,
            "matches": bool(m)}


# ── 结论装配 ────────────────────────────────────────────────────────────
def build_findings(emitters: dict, registry: dict, bundle: dict, g1: Optional[dict],
                   noise: Optional[dict], devig: dict, consts: dict,
                   live_ok: bool) -> List[dict]:
    f: List[dict] = []
    for h in emitters["unexpected"]:
        f.append({"id": "R1_UNREGISTERED_VERDICT_EMITTER", "sev": FINDING_RED,
                  "detail": f"{h['file']}:{h['line']} 含判定词 {h['token']!r} 但未登记"})
    if not emitters["unexpected"]:
        f.append({"id": "R1_UNREGISTERED_VERDICT_EMITTER", "sev": "OK",
                  "detail": "无未登记判定产出体"})
    missing = [r["file"] for r in registry["registry"] if not r["exists"]]
    if missing:
        f.append({"id": "R2_EMITTER_RETIRED_UNLOGGED", "sev": FINDING_AMBER,
                  "detail": "登记在册的绕过体缺失（退役未登记）: " + ", ".join(missing)})
    if bundle["missing_count"]:
        f.append({"id": "R3_BUNDLE_FIELDS_MISSING", "sev": FINDING_RED,
                  "detail": "复用 gates 必缺字段 %d 个: %s"
                            % (bundle["missing_count"], ", ".join(bundle["missing"]))})
    if devig["proportional_method_found"] and not devig["g6_ready"]:
        f.append({"id": "R4_G6_CALIBER_BLOCKED", "sev": FINDING_RED,
                  "detail": f"{devig['dataset']} 用{devig['verdict']}去水 → G6 机械基准须先换幂法重建"})
    if g1 and live_ok:
        f.append({"id": "R5_G1_UNIT_DILEMMA", "sev": FINDING_RED,
                  "detail": ("G1 计数二难: 按样本 n=%d %s, 按场次 m=%d %s; "
                             "design_effect=%.3f → 朴素 iid 置信区间低估 %.2f×"
                             % (g1["total_test_samples"],
                                "过线" if g1["g1_by_sample"] else "不过",
                                g1["total_test_matches"],
                                "过线" if g1["g1_by_match"] else "不过",
                                g1["design_effect"], g1["naive_ci_understate_factor"]))})
    if noise and noise.get("worst_band_over_sd") is not None:
        f.append({"id": "R6_DELTA_BAND_BELOW_NOISE", "sev": FINDING_RED,
                  "detail": ("mh 硬编码判定带 ±%.3f 仅为市场基线聚类噪声的 %.4f×σ → "
                             "标签翻转由噪声驱动, 非证据" % (noise["band"],
                                                            noise["worst_band_over_sd"]))})
    if consts.get("min_sample") != consts.get("expected"):
        f.append({"id": "R7_MIN_SAMPLE_DRIFT", "sev": FINDING_RED,
                  "detail": "MIN_SAMPLE 基线漂移: %s" % consts.get("min_sample")})
    return f


def verdict_of(findings: List[dict]) -> str:
    for x in findings:
        if x["sev"] == FINDING_RED:
            return "FAIL"
    return "PASS"


def run_audit() -> dict:
    emitters = scan_verdict_emitters()
    registry = scan_expectation_registry()
    bundle = bundle_field_map()
    consts = constant_baseline()
    devig = devig_caliber_of(read_text(DEVIG_SRC), None)

    rows = load_rows(DATASET)
    g1 = walkforward_g1_counts(rows) if rows else None
    noise = noise_floor(rows) if rows else None
    live_ok = rows is not None

    findings = build_findings(emitters, registry, bundle, g1, noise, devig, consts, live_ok)
    if not live_ok:
        findings.append({"id": "N_LIVE_NOT_RUN", "sev": FINDING_AMBER,
                         "detail": "数据集不可读 → Q3/Q4 活体复核未执行，静态结论不变"})

    branch = gates_verdict_for_mh("BEATS MARKET",
                                  (g1 or {}).get("total_test_samples"),
                                  (g1 or {}).get("total_test_matches"))
    all_branches = [gates_verdict_for_mh(x, (g1 or {}).get("total_test_samples"),
                                         (g1 or {}).get("total_test_matches"))
                    for x in ("BEATS MARKET", "TIE", "NO EDGE")]
    out = {
        "audit": "T53 mh_train_div.py 绕过 gates.verdict 只读盘点",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "red_lines": "只读 CSV; 不 import 训练入口; 不跑训练; 不写库; 不改 verification/; 零进程操作",
        "verdict": verdict_of(findings),
        "findings": findings,
        "Q1_emitters": {k: v for k, v in emitters.items() if isinstance(v, list)},
        "Q1_summary": {
            "unexpected_count": emitters["unexpected_count"],
            "noise_count": emitters["noise_count"],
            "expected_files": list(EXPECTED_EMITTERS),
            "t51_miss_reason": (
                "T51 正则要求字面量两侧带引号; mh_train_walkforward.py:118 / "
                "mh_train_walkforward_x.py:82 的判定词形如 'NO EDGE (model >= market)' "
                "(尾部无引号) → 双双漏检"
            ),
        },
        "Q1_registry": registry,
        "Q2_bundle": bundle,
        "Q3_g1_counts": g1,
        "Q4_noise_floor": noise,
        "Q5_branch_map": branch,
        "Q5_all_branches": all_branches,
        "devig_caliber": devig,
        "constants": consts,
        "live_ok": live_ok,
    }
    return out


def render_md(r: dict) -> str:
    L: List[str] = []
    L.append("# T53 mh 训练脚本绕过 gates.verdict 只读盘点")
    L.append("")
    L.append(f"- 判定: **{r['verdict']}**")
    L.append(f"- 生成: {r['generated_at']} (UTC)")
    L.append(f"- 红线: {r['red_lines']}")
    L.append("")
    L.append("## 结论清单")
    for x in r["findings"]:
        L.append(f"- `{x['id']}` [{x['sev']}] {x['detail']}")
    s = r["Q1_summary"]
    L.append("")
    L.append("## Q1 判定产出体分层")
    L.append(f"- 未登记产出体: **{s['unexpected_count']}**；已知噪声(盘口信号): {s['noise_count']}")
    L.append(f"- 在册绕过体: {', '.join(s['expected_files'])}")
    L.append(f"- T51 漏检原因: {s['t51_miss_reason']}")
    for h in r["Q1_emitters"].get(TIER_EXPECTED, []):
        L.append(f"  - {h['file']}:{h['line']} `{h['token']}`")
    L.append("")
    b = r["Q2_bundle"]
    L.append("## Q2 MetricsBundle 映射")
    L.append(f"- 现成(1:1): {', '.join(b['ready']) or '—'}")
    L.append(f"- 可算: {', '.join(b['computable']) or '—'}")
    L.append(f"- **缺({b['missing_count']})**: {', '.join(b['missing']) or '—'}")
    g = r["Q3_g1_counts"]
    if g:
        L.append("")
        L.append("## Q3 G1 计数口径二难")
        L.append(f"- 折切分 {[ (f['cut'], f['test_matches'], f['test_samples']) for f in g['folds'] ]}")
        L.append(f"- 合计: 样本 n={g['total_test_samples']} / 场次 m={g['total_test_matches']}"
                 f" / 每场检查点数={g['checkpoints_per_match']}")
        L.append(f"- G1 按样本={g['g1_by_sample']} · 按场次={g['g1_by_match']}"
                 f" · design_effect={g['design_effect']} · iid 低估 {g['naive_ci_understate_factor']}×")
    n = r["Q4_noise_floor"]
    if n:
        L.append("")
        L.append("## Q4 ΔLL 噪声底（市场基线按场次聚类 bootstrap）")
        for f in n["per_fold"]:
            ratio = f"±{n['band']} / SD = {f['band_over_sd']:.4f}" \
                if f.get("band_over_sd") is not None else "SD=0（不可判定）"
            L.append(f"- cut={f['cut']} matches={f['matches']} SD={f['cluster_sd_market_ll']:.4f}"
                     f" → {ratio}")
    L.append("")
    L.append("## Q5 判定映射（BEATS MARKET 分支）")
    br = r["Q5_branch_map"]
    L.append(f"- mh 标签「{br['mh_label']}」→ gates **{br['gates_verdict']}**（{br['reason']}）")
    L.append(f"- 缺门禁: {', '.join(br['blocking_gates'])}")
    d = r["devig_caliber"]
    L.append("")
    L.append("## 去水口径")
    L.append(f"- {d['dataset']}: {d['verdict']} · G6 可用={d['g6_ready']} · {d['note']}")
    return "\n".join(L) + "\n"


def main() -> int:
    res = run_audit()
    os.makedirs(REPORTS_DIR, exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=2)
    with open(OUT_MD, "w", encoding="utf-8") as fh:
        fh.write(render_md(res))
    print(f"[T53] verdict={res['verdict']} unexpected={res['Q1_summary']['unexpected_count']} "
          f"live_ok={res['live_ok']}")
    for x in res["findings"]:
        print(f"  [{x['sev']}] {x['id']}: {x['detail']}")
    print(f"[T53] -> {OUT_MD}")
    return 0 if res["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
