"""
哨响AI 可信回测工具 (proper_backtest.py)
==========================================
修复了 backtest_all_models.py 的两大致命 bug:
  1. 必须用 EnsembleTrainer.load_pipeline() 加载(带 scaler)
  2. 必须 scaler.transform(X) 后再推理
  3. 从 match_features 表取预计算特征,按模型 feature_names 对齐列序

用法:
    python scripts/proper_backtest.py                    # 默认 1000 场
    python scripts/proper_backtest.py --n 2000           # 指定样本数
    python scripts/proper_backtest.py --since 2023-01-01 # 指定起始日期
    python scripts/proper_backtest.py --threshold        # 应用 optimal_thresholds 阈值
    python scripts/proper_backtest.py --scan-threshold   # 阈值网格搜索
    python scripts/proper_backtest.py --scan-jepa 0.08,0.12,0.15,0.20  # JEPA权重扫描

设计:所有对比在同一 holdout 上,保证可比。
"""
from __future__ import annotations

import os
import sys
import argparse
import sqlite3
import numpy as np
from typing import List, Dict, Tuple

# ── 路径修复 (复刻 serve.py: backend优先解决core/database冲突) ──
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
for p in (BACKEND_DIR, PROJECT_ROOT,
          os.path.join(PROJECT_ROOT, "predictors", "components")):
    if p not in sys.path:
        sys.path.insert(0, p)
os.chdir(PROJECT_ROOT)
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")

# 注册 draw_expert 顶层模块 (joblib pickle 依赖)
from predictors.components import draw_expert  # noqa: E402
sys.modules["draw_expert"] = draw_expert


def load_model():
    """用生产标准方式加载模型 (带 scaler + feature_names + meta_learner)"""
    from predictors.components.ensemble_trainer import EnsembleTrainer
    path = os.path.join(PROJECT_ROOT, "saved_models", "football_v4.1_production.joblib")
    return EnsembleTrainer.load_pipeline(path)


def load_dataset(n: int, since: str) -> Tuple[np.ndarray, np.ndarray, List[str], List[Dict], List[dict]]:
    """从 match_features 表加载特征 + 标签。返回 (X_raw, y, leagues, raw_list, meta)"""
    trainer = load_model()
    feat_names = trainer.feature_names  # 72 维, 顺序固定
    defaults = trainer.config.get("data", {}).get("default_values", {})

    db = os.path.join(PROJECT_ROOT, "data", "football_data.db")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    mf_cols = [r[1] for r in conn.execute("PRAGMA table_info(match_features)").fetchall()]
    sel = ", ".join("mf." + c for c in mf_cols)
    rows = conn.execute(f"""
        SELECT m.match_id, m.match_date, m.home_team_name, m.away_team_name,
               m.home_score, m.away_score, m.final_result, m.league_name, {sel}
        FROM matches m JOIN match_features mf ON m.match_id = mf.match_id
        WHERE m.home_score IS NOT NULL AND m.away_score IS NOT NULL
          AND m.final_result IS NOT NULL AND m.match_date >= ?
        ORDER BY m.match_date LIMIT ?
    """, (since, n)).fetchall()
    conn.close()

    n_actual = len(rows)
    X = np.zeros((n_actual, len(feat_names)))
    leagues, raw_list, meta = [], [], []

    def _f(v, name):
        try:
            return float(defaults.get(name, 0.0)) if v is None else float(v)
        except (TypeError, ValueError):
            return float(defaults.get(name, 0.0))

    for i, r in enumerate(rows):
        rd = dict(r)
        d = {}
        for j, k in enumerate(feat_names):
            val = _f(rd.get(k), k)
            X[i, j] = val
            d[k] = val
        raw_list.append(d)
        leagues.append(rd.get("league_name") or "")
        label = {"H": 0, "D": 1, "A": 2}[rd["final_result"]]
        meta.append({
            "match_id": rd["match_id"], "date": rd["match_date"],
            "home": rd["home_team_name"], "away": rd["away_team_name"],
            "league": rd.get("league_name"),
        })
    y = np.zeros(n_actual, dtype=int)
    for i, r in enumerate(rows):
        y[i] = {"H": 0, "D": 1, "A": 2}[r["final_result"]]
    return trainer, X, y, leagues, raw_list, meta


def predict(trainer, X_raw, leagues, raw_list) -> np.ndarray:
    """正确推理: scaler.transform → ensemble_predict_proba"""
    Xs = trainer.scaler.transform(X_raw)
    proba = trainer.ensemble_predict_proba(Xs, league_names=leagues, raw_features_list=raw_list)
    return np.asarray(proba, dtype=np.float64)


def metrics(y, proba, thresholds=None) -> Dict:
    """计算完整指标。thresholds=(th_h,th_d,th_a) 时应用偏置再argmax"""
    from sklearn.metrics import accuracy_score, f1_score, log_loss, brier_score_loss
    if thresholds is not None:
        adj = proba.copy()
        adj[:, 0] += thresholds[0]
        adj[:, 1] += thresholds[1]
        adj[:, 2] += thresholds[2]
        pred = adj.argmax(1)
    else:
        pred = proba.argmax(1)
    f1 = f1_score(y, pred, average=None, labels=[0, 1, 2])
    pred_dist = {["H", "D", "A"][k]: int((pred == k).sum()) for k in range(3)}
    true_dist = {["H", "D", "A"][k]: int((y == k).sum()) for k in range(3)}
    return {
        "accuracy": float(accuracy_score(y, pred)),
        "macro_f1": float(f1_score(y, pred, average="macro", labels=[0, 1, 2])),
        "f1_home": float(f1[0]), "f1_draw": float(f1[1]), "f1_away": float(f1[2]),
        "log_loss": float(log_loss(y, proba, labels=[0, 1, 2])),
        "brier_draw": float(brier_score_loss((y == 1).astype(int), proba[:, 1])),
        "pred_dist": pred_dist, "true_dist": true_dist,
    }


def odds_baseline(y, raw_list) -> Dict:
    """赔率隐含概率基线 (argmax of odds_implied, 反向: 最小赔率=最可能)"""
    proba = np.zeros((len(y), 3))
    for i, r in enumerate(raw_list):
        oh, od, oa = r.get("odds_imp_h"), r.get("odds_imp_d"), r.get("odds_imp_a")
        if oh and od and oa and oh > 0 and od > 0 and oa > 0:
            proba[i] = [oh, od, oa]
        else:
            proba[i] = [1/3, 1/3, 1/3]
    return metrics(y, proba)


def fmt(m: Dict) -> str:
    return (f"Acc {m['accuracy']:.4f} | MacroF1 {m['macro_f1']:.4f} | "
            f"H {m['f1_home']:.4f} D {m['f1_draw']:.4f} A {m['f1_away']:.4f} | "
            f"LogLoss {m['log_loss']:.4f} BrierD {m['brier_draw']:.4f}")


def main():
    ap = argparse.ArgumentParser(description="哨响AI 可信回测")
    ap.add_argument("--n", type=int, default=1000)
    ap.add_argument("--since", default="2024-01-01")
    ap.add_argument("--threshold", action="store_true", help="应用模型自带 optimal_thresholds")
    ap.add_argument("--scan-threshold", action="store_true", help="阈值网格搜索")
    ap.add_argument("--scan-threshold-d", action="store_true", help="只扫 D 阈值偏置")
    ap.add_argument("--jepa", type=float, default=0.0, help="模拟JEPA后融合权重(平局通道)")
    args = ap.parse_args()

    print("=" * 64)
    print(f"  哨响AI 可信回测 | 样本上限{args.n} | 起始{args.since}")
    print("=" * 64)

    trainer, X, y, leagues, raw_list, meta = load_dataset(args.n, args.since)
    print(f"  实际样本: {len(y)} 场")
    print(f"  模型: {trainer.model_version} | 特征{len(trainer.feature_names)}维 | scaler={type(trainer.scaler).__name__}")
    print()

    proba = predict(trainer, X, leagues, raw_list)

    # 可选: 模拟 JEPA 后融合 (对平局通道加 JEPA 风格的偏移)
    if args.jepa > 0:
        w = args.jepa
        # 简化模拟: JEPA 倾向高 P(D),这里用一个温和的 D 增强混合
        jepa_d = np.clip(proba[:, 1] * 1.3, 0, 1)  # JEPA 平局信号更强
        proba_blend = proba.copy()
        proba_blend[:, 1] = (1 - w) * proba[:, 1] + w * jepa_d
        proba_blend = proba_blend / proba_blend.sum(1, keepdims=True)
        print(f"[JEPA模拟] w={w} (注意: 离线模拟,与线上JEPA不完全等价)")
        m_blend = metrics(y, proba_blend)
        print(f"  JEPA融合后: {fmt(m_blend)}")
        print()

    # 基线: 裸 argmax
    m_base = metrics(y, proba)
    print(f"【模型基线】 {fmt(m_base)}")
    print(f"  真实分布: {m_base['true_dist']}")
    print(f"  预测分布: {m_base['pred_dist']}")

    # 赔率基线
    m_odds = odds_baseline(y, raw_list)
    print(f"【赔率基线】 {fmt(m_odds)}")
    print()

    # 应用模型自带阈值
    ot = trainer.__dict__.get("optimal_thresholds") or getattr(trainer, "optimal_thresholds", None)
    if args.threshold and ot:
        m_ot = metrics(y, proba, thresholds=ot)
        print(f"【+optimal_thresholds {tuple(round(t,3) for t in ot)}】 {fmt(m_ot)}")
        print()

    # 阈值网格搜索
    if args.scan_threshold or args.scan_threshold_d:
        print("── 阈值网格搜索 (找最优 D 偏置, 守住 H/A 不降超0.02) ──")
        base_f1 = [m_base["f1_home"], m_base["f1_draw"], m_base["f1_away"]]
        base_macro = m_base["macro_f1"]
        best = None
        if args.scan_threshold_d:
            grid = [(0.0, d, 0.0) for d in np.arange(0.00, 0.16, 0.01)]
        else:
            grid = []
            for d in np.arange(0.00, 0.16, 0.02):
                for h in np.arange(-0.04, 0.03, 0.02):
                    for a in np.arange(-0.04, 0.03, 0.02):
                        grid.append((round(h, 2), round(d, 2), round(a, 2)))
        print(f"  扫描 {len(grid)} 组...")
        for th in grid:
            m = metrics(y, proba, thresholds=th)
            # 守门规则: Macro-F1 提升 + D-F1 不降 + H/A 各降<0.02
            ok = (m["macro_f1"] > base_macro
                  and m["f1_draw"] >= m_base["f1_draw"] - 1e-9
                  and m["f1_home"] >= base_f1[0] - 0.02
                  and m["f1_away"] >= base_f1[2] - 0.02)
            if ok and (best is None or m["macro_f1"] > best["macro_f1"]):
                best = m
                best_th = th
        if best:
            print(f"  ✅ 最优阈值 {best_th}: {fmt(best)}")
            print(f"     vs 基线 MacroF1 {base_macro:.4f} → {best['macro_f1']:.4f} "
                  f"(+{best['macro_f1']-base_macro:.4f})")
            print(f"     D-F1: {m_base['f1_draw']:.4f} → {best['f1_draw']:.4f}")
        else:
            print(f"  ❌ 无满足守门规则的阈值组合 (基线已较优)")

    print()
    print("=" * 64)


if __name__ == "__main__":
    main()
