"""
diagnostic_probe.py — 真 OOS「读片能力」考卷仪器 (深度方向 SSoT)

定位 (用户 09-02 方向: 杠杆是「提高自身(读片/诊断能力)」而非「积累临床经验」):
  - 任何一条「读片方法」(从赔率报告 → 方向概率) 都必须拿真 OOS 考卷证明,
    不能自认进步 (防 in-sample 幻觉, 正是 evolution.py 五道关的意义)。
  - 本模块是「考卷 + 阅卷器」: 你只管实现 ReadingMethod.predict(report),
    仪器负责在「干净时间切分 (kickoff >= 切点)」上公正阅卷:
        方向准确率 vs 市场基线 / Brier / LogLoss / ROI + bootstrap 95% CI。

诚实边界 (IR-04 / IR-20 / IR-30):
  - 真相源 = match_outcomes.result (赛果 SSoT, 已剔虚拟/截断)。
  - 严格层 (默认开): LEFT JOIN matches ON mid, 排除 score_missing=1 的假 0-0
    (IR-04 第二层口径, 防 obscure 联赛采集截断污染渗入)。
  - 时间切分: kickoff 可解析 且 >= cutoff (默认 2023-01-01) 才计入 OOS;
    kickoff 为空/不可解析 → 无法判断样本内外, 一律剔除 (不混入)。
  - 不读 matches.score_* 直接结算; 只用 match_outcomes 已治理字段。
  - 赔率报告 = 初盘 1X2 (op_1x2_h/d/a), 即「检查报告」本身; 不含任何未来信息。

ReadingMethod 接口 (你后续要测的「深度假说」都实现它):
    class MyMethod:
        name = "my_method"
        def predict(self, report: OddsReport) -> dict:
            # 返回 {home, draw, away} 概率 (不必归一, 仪器会归一)
            return {"home": ..., "draw": ..., "away": ...}

内置基线 (便于自检仪器 + 对照):
  - MarketFavorite : 押隐含概率最高方 (= 市场基线, ROI 应≈ -抽水, 证明"读同一份报告赢不了")
  - Uniform        : 押 1/3 各 (退化基线, 方向准确率≈平局率, ROI 必为负)
  - ReverseFavorite: 押隐含概率最低方 (应劣于市场, 反向 sanity check)

用法:
  from pipeline.diagnostic_probe import DiagnosticProbe, MarketFavorite, Uniform
  rep = DiagnosticProbe().run(MarketFavorite(), cutoff="2023-01-01")
  print(rep.summary())

CLI:
  python pipeline/diagnostic_probe.py --method market|uniform|reverse \
      [--cutoff 2023-01-01] [--limit N] [--no-strict] [--boot 2000] [--seed 42]
"""
from __future__ import annotations

import argparse
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Protocol, runtime_checkable

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(ROOT, "data", "events.db")
DEFAULT_CUTOFF = "2023-01-01"
SIDES = ("home", "draw", "away")


# ═════════════════════════════════════════════════════════════════════
# 赔率报告 (检查报告)
# ═════════════════════════════════════════════════════════════════════
@dataclass
class OddsReport:
    """一份「检查报告」: 某场初盘 1X2 (及可选 AH/OU) 价格。

    predict() 只消费这份报告, 不接触任何未来信息 —— 这是「读片」的诚实边界。
    """

    match_key: str
    home_odds: float
    draw_odds: float
    away_odds: float
    # 可选扩展字段 (未来深度假说可用, v1 基线只用 1X2)
    ah_line: Optional[float] = None
    ah_home_odds: Optional[float] = None
    ah_away_odds: Optional[float] = None
    ou_line: Optional[float] = None
    ou_over_odds: Optional[float] = None
    ou_under_odds: Optional[float] = None
    kickoff: Optional[str] = None
    league: Optional[str] = None
    raw: dict = field(default_factory=dict)

    @property
    def implied(self) -> Dict[str, float]:
        """脱水隐含概率 (归一化 1/odds, 去除抽水)。sum=1。"""
        inv = {s: 1.0 / getattr(self, f"{s}_odds") for s in SIDES}
        tot = sum(inv.values())
        return {s: inv[s] / tot for s in SIDES}

    @property
    def overround(self) -> float:
        """抽水 (overround)。>1 表示庄家优势。"""
        return sum(1.0 / getattr(self, f"{s}_odds") for s in SIDES)

    @property
    def favorite(self) -> str:
        return max(self.implied, key=self.implied.get)


# ═════════════════════════════════════════════════════════════════════
# 读片方法接口
# ═════════════════════════════════════════════════════════════════════
@runtime_checkable
class ReadingMethod(Protocol):
    name: str

    def predict(self, report: OddsReport) -> Dict[str, float]:
        """返回 {home, draw, away} 概率 (不必归一)。"""
        ...


# ── 内置基线 ──────────────────────────────────────────────────────────
class MarketFavorite:
    """押隐含概率最高方 = 市场基线。ROI 应≈ -抽水 (证明读同一份报告赢不了)。"""

    name = "market_favorite"

    def predict(self, report: OddsReport) -> Dict[str, float]:
        imp = report.implied
        out = {s: 0.0 for s in SIDES}
        out[report.favorite] = 1.0
        return out


class Uniform:
    """退化基线: 押 1/3 各。方向准确率≈平局率, ROI 必为负。"""

    name = "uniform"

    def predict(self, report: OddsReport) -> Dict[str, float]:
        return {s: 1 / 3 for s in SIDES}


class ReverseFavorite:
    """押隐含概率最低方。应劣于市场 (反向 sanity check)。"""

    name = "reverse_favorite"

    def predict(self, report: OddsReport) -> Dict[str, float]:
        imp = report.implied
        weak = min(SIDES, key=lambda s: imp[s])
        out = {s: 0.0 for s in SIDES}
        out[weak] = 1.0
        return out


# ═════════════════════════════════════════════════════════════════════
# 阅卷器
# ═════════════════════════════════════════════════════════════════════
def _parse_kickoff(k: Optional[str]) -> Optional[datetime]:
    if not k:
        return None
    k = str(k).strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(k, fmt)
        except ValueError:
            continue
    return None


def _norm(probs: Dict[str, float]) -> np.ndarray:
    arr = np.array([float(probs.get(s, 0.0)) for s in SIDES], dtype=float)
    s = arr.sum()
    if s <= 0:
        return np.ones(3) / 3.0
    return arr / s


def _result_to_idx(result: Optional[str]) -> Optional[int]:
    return {"home": 0, "draw": 1, "away": 2}.get((result or "").strip().lower())


def _bootstrap_roi(odds_arr, stakes_side, wins, n_boot, rng) -> Dict[str, float]:
    """对 ROI 做 bootstrap 95% CI。"""
    n = len(wins)
    if n == 0:
        return {"roi": float("nan"), "ci_low": float("nan"), "ci_high": float("nan")}
    rois = np.empty(n_boot)
    for b in range(n_boot):
        idx = rng.integers(0, n, n)
        net = np.where(wins[idx], odds_arr[idx] - 1.0, -1.0) * stakes_side[idx]
        rois[b] = net.sum() / (stakes_side[idx].sum() + 1e-12)
    return {
        "roi": float(rois.mean()),
        "ci_low": float(np.percentile(rois, 2.5)),
        "ci_high": float(np.percentile(rois, 97.5)),
    }


@dataclass
class ProbeReport:
    method_name: str
    cutoff: str
    n_total: int          # 剔除虚拟/假0-0/缺odds后, 进入阅卷的样本
    n_oos: int            # kickoff>=cutoff 的真 OOS 样本 (主报告口径)
    accuracy: float
    market_accuracy: float
    accuracy_ci: List[float]      # [low, high] 95%
    brier: float
    logloss: float
    roi: Dict[str, float]         # flat-argmax 下注 ROI + CI
    roi_edge: Optional[Dict[str, float]] = None  # edge门控下注 (edge_min>0)
    edge_min: float = 0.0
    notes: List[str] = field(default_factory=list)

    def summary(self) -> str:
        L = []
        L.append("=" * 70)
        L.append(f"真 OOS 读片考卷 — 方法: {self.method_name}")
        L.append("=" * 70)
        L.append(f"切点(含)      : {self.cutoff}  (kickoff < 切点 一律不计入 OOS)")
        L.append(f"阅卷样本       : {self.n_total}  (已剔虚拟/假0-0/缺odds)")
        L.append(f"真 OOS 样本    : {self.n_oos}")
        L.append("-" * 70)
        L.append(f"方向准确率     : {self.accuracy:6.2%}   vs 市场基线 {self.market_accuracy:6.2%}")
        L.append(f"  准确率 95%CI : [{self.accuracy_ci[0]:6.2%}, {self.accuracy_ci[1]:6.2%}]")
        if self.accuracy > self.market_accuracy + 0.005:
            L.append("  ✅ 读片优于市场基线 (方向)")
        elif self.accuracy < self.market_accuracy - 0.005:
            L.append("  ⚠️ 读片劣于市场基线 (方向)")
        else:
            L.append("  ➖ 与市持平 (无方向增量)")
        L.append(f"Brier (越低越好): {self.brier:.4f}")
        L.append(f"LogLoss(越低越好): {self.logloss:.4f}")
        L.append("-" * 70)
        r = self.roi
        lo, hi = r.get("ci_low", float("nan")), r.get("ci_high", float("nan"))
        L.append(f"平注(argmax) ROI: {r['roi']:+7.2%}  CI[{lo:+6.2%}, {hi:+6.2%}]")
        if lo > 0:
            L.append("  ✅ +EV 确立 (CI 不跨零)")
        elif hi < 0:
            L.append("  ❌ 显著负期望 (不如不押)")
        else:
            L.append("  ➖ +EV 未确立 (CI 跨零)")
        if self.roi_edge is not None:
            re = self.roi_edge
            lo2, hi2 = re.get("ci_low", float("nan")), re.get("ci_high", float("nan"))
            L.append(f"edge门(>={self.edge_min:.3f}) ROI: {re['roi']:+7.2%}  CI[{lo2:+6.2%}, {hi2:+6.2%}]")
        if self.notes:
            L.append("-" * 70)
            for n in self.notes:
                L.append("  · " + n)
        return "\n".join(L)


class DiagnosticProbe:
    """真 OOS 读片能力考卷仪器。"""

    def __init__(self, db_path: str = DEFAULT_DB):
        self.db_path = db_path

    # ── 数据装载 (诚实层) ────────────────────────────────────────────
    def _load(self, cutoff: str, strict_clean: bool):
        """返回 (reports, truths_idx)。严格遵守 IR-04/IR-30。

        注意: 不使用 LIMIT 前 N 行 —— 那会取到 ROWID 连续的偏置子集
        (实测前 300 行恰好是某联赛批量, favorite 命中率 100% 假象)。
        --limit 的随机抽样放到 run() 里用 rng 做, 保证可复现。
        """
        cutoff_dt = _parse_kickoff(cutoff)
        assert cutoff_dt is not None, f"无法解析 cutoff={cutoff}"
        con = sqlite3.connect(self.db_path)
        try:
            join = ""
            extra = ""
            if strict_clean:
                # LEFT JOIN matches ON mid, 排除 score_missing=1 假 0-0 (IR-04 第二层)
                join = "LEFT JOIN matches m ON mo.mid = m.mid"
                extra = "AND COALESCE(m.score_missing, 0) != 1"
            sql = f"""
                SELECT mo.mid, mo.home, mo.away, mo.league, mo.kickoff, mo.result,
                       mo.op_1x2_h, mo.op_1x2_d, mo.op_1x2_a,
                       mo.op_ah_line, mo.op_ah_home, mo.op_ah_away,
                       mo.op_ou_line, mo.op_ou_over, mo.op_ou_under
                FROM match_outcomes mo {join}
                WHERE mo.op_1x2_h IS NOT NULL AND mo.op_1x2_d IS NOT NULL
                  AND mo.op_1x2_a IS NOT NULL
                  AND mo.is_virtual != 1
                  AND mo.result IN ('home','draw','away')
                  {extra}
            """
            rows = con.execute(sql).fetchall()
        finally:
            con.close()

        reports: List[OddsReport] = []
        truths: List[int] = []
        dropped_no_kickoff = 0
        dropped_pre_cutoff = 0
        for r in rows:
            (mid, home, away, league, kickoff, result,
             h, d, a, ah_l, ah_h, ah_a, ou_l, ou_o, ou_u) = r
            kdt = _parse_kickoff(kickoff)
            if kdt is None:
                dropped_no_kickoff += 1
                continue
            if kdt < cutoff_dt:
                dropped_pre_cutoff += 1
                continue
            # 价格合法性守卫
            if not (h > 1.0 and d > 1.0 and a > 1.0):
                continue
            rep = OddsReport(
                match_key=str(mid), home_odds=float(h), draw_odds=float(d),
                away_odds=float(a),
                ah_line=(float(ah_l) if ah_l is not None else None),
                ah_home_odds=(float(ah_h) if ah_h is not None else None),
                ah_away_odds=(float(ah_a) if ah_a is not None else None),
                ou_line=(float(ou_l) if ou_l is not None else None),
                ou_over_odds=(float(ou_o) if ou_o is not None else None),
                ou_under_odds=(float(ou_u) if ou_u is not None else None),
                kickoff=kickoff, league=league,
                raw={"home": home, "away": away, "mid": mid},
            )
            reports.append(rep)
            truths.append(_result_to_idx(result))

        self._last_drop = {
            "no_kickoff": dropped_no_kickoff,
            "pre_cutoff": dropped_pre_cutoff,
        }
        return reports, truths

    # ── 阅卷 ─────────────────────────────────────────────────────────
    def run(self, method: ReadingMethod, cutoff: str = DEFAULT_CUTOFF,
            strict_clean: bool = True, limit: Optional[int] = None,
            edge_min: float = 0.0, n_boot: int = 2000, seed: int = 42) -> ProbeReport:
        rng = np.random.default_rng(seed)
        reports, truths = self._load(cutoff, strict_clean)
        n_total = len(reports)
        if n_total == 0:
            raise RuntimeError("无符合条件的 OOS 样本 (检查 cutoff / strict_clean / db)")
        # --limit 改为可复现随机抽样 (避免前 N 行偏置子集)
        if limit and limit < n_total:
            idx = rng.choice(n_total, size=int(limit), replace=False)
            reports = [reports[i] for i in idx]
            truths = [truths[i] for i in idx]
            n_total = len(reports)

        # 市场基线 (隐含概率最高方)
        mkt_preds = [MarketFavorite().predict(r) for r in reports]
        truth_arr = np.array(truths)

        # 方法预测
        method_preds = []
        for r in reports:
            try:
                p = method.predict(r)
                method_preds.append(_norm(p))
            except Exception as e:  # 方法炸了 → 记退化, 不静默吞
                method_preds.append(np.ones(3) / 3.0)
                self._last_drop.setdefault("method_errors", []).append(
                    f"{getattr(method,'name','?')}: {e}")

        method_preds = np.array(method_preds)
        mkt_preds = np.array([_norm(p) for p in mkt_preds])

        # 方向准确率
        method_argmax = method_preds.argmax(1)
        mkt_argmax = mkt_preds.argmax(1)
        acc = float((method_argmax == truth_arr).mean())
        mkt_acc = float((mkt_argmax == truth_arr).mean())

        # 准确率 bootstrap CI (预测与真相必须用同一套重采样索引, 不可各自独立抽样)
        acc_boot = np.empty(n_boot)
        for b in range(n_boot):
            idx = rng.integers(0, n_total, n_total)
            acc_boot[b] = float((method_preds[idx].argmax(1) == truth_arr[idx]).mean())
        acc_ci = [float(np.percentile(acc_boot, 2.5)),
                  float(np.percentile(acc_boot, 97.5))]

        # Brier / LogLoss
        onehot = np.zeros((n_total, 3)); onehot[np.arange(n_total), truth_arr] = 1
        brier = float(((method_preds - onehot) ** 2).sum(1).mean())
        eps = 1e-12
        logloss = float(-np.log(method_preds[np.arange(n_total), truth_arr] + eps).mean())

        # ROI: 平注 argmax 方, 结算价为该方初盘 odds
        stake_side = np.zeros(n_total)
        odds_at = np.zeros(n_total)
        wins = np.zeros(n_total, dtype=bool)
        for i in range(n_total):
            s = int(method_argmax[i])
            stake_side[i] = 1.0
            odds_at[i] = [reports[i].home_odds, reports[i].draw_odds,
                          reports[i].away_odds][s]
            wins[i] = (truth_arr[i] == s)
        roi_flat = _bootstrap_roi(odds_at, stake_side, wins, n_boot, rng)

        # ROI: edge 门控 (仅押 p_model - implied > edge_min 的方)
        roi_edge = None
        if edge_min > 0:
            imp = np.array([_norm({s: 1.0 / getattr(reports[i], f"{s}_odds")
                                   for s in SIDES}) for i in range(n_total)])
            edges = method_preds - imp
            stake_e = np.zeros(n_total)
            odds_e = np.zeros(n_total)
            win_e = np.zeros(n_total, dtype=bool)
            for i in range(n_total):
                am = int(edges[i].argmax())
                if edges[i][am] > edge_min:
                    stake_e[i] = 1.0
                    odds_e[i] = [reports[i].home_odds, reports[i].draw_odds,
                                 reports[i].away_odds][am]
                    win_e[i] = (truth_arr[i] == am)
            if stake_e.sum() > 0:
                roi_edge = _bootstrap_roi(odds_e, stake_e, win_e, n_boot, rng)

        notes = []
        d = getattr(self, "_last_drop", {})
        notes.append(f"剔除 kickoff 不可解析: {d.get('no_kickoff',0)} 场")
        notes.append(f"剔除 kickoff < 切点: {d.get('pre_cutoff',0)} 场")
        if d.get("method_errors"):
            notes.append(f"方法预测异常 {len(d['method_errors'])} 场 → 退化为 1/3")

        return ProbeReport(
            method_name=getattr(method, "name", str(method)),
            cutoff=cutoff, n_total=n_total, n_oos=n_total,
            accuracy=acc, market_accuracy=mkt_acc, accuracy_ci=acc_ci,
            brier=brier, logloss=logloss, roi=roi_flat, roi_edge=roi_edge,
            edge_min=edge_min, notes=notes,
        )


# ═════════════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════════════
_REGISTRY = {
    "market": MarketFavorite,
    "uniform": Uniform,
    "reverse": ReverseFavorite,
}


def _cli():
    ap = argparse.ArgumentParser(description="真 OOS 读片能力考卷仪器")
    ap.add_argument("--method", choices=list(_REGISTRY.keys()),
                    default="market", help="内置基线方法")
    ap.add_argument("--cutoff", default=DEFAULT_CUTOFF,
                    help="OOS 时间切点 (含), 默认 2023-01-01")
    ap.add_argument("--limit", type=int, default=None, help="仅取前 N 行 (快速自检)")
    ap.add_argument("--no-strict", action="store_true",
                    help="关闭 score_missing 假0-0 排除层")
    ap.add_argument("--edge-min", type=float, default=0.0,
                    help="edge 门控下注阈值 (>0 时额外报告门控 ROI)")
    ap.add_argument("--boot", type=int, default=2000, help="bootstrap 次数")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--db", default=DEFAULT_DB)
    args = ap.parse_args()

    probe = DiagnosticProbe(db_path=args.db)
    rep = probe.run(
        _REGISTRY[args.method](), cutoff=args.cutoff,
        strict_clean=not args.no_strict, limit=args.limit,
        edge_min=args.edge_min, n_boot=args.boot, seed=args.seed,
    )
    print(rep.summary())


if __name__ == "__main__":
    _cli()
