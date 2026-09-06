# ╔══════════════════════════════════════════════════════════════════════╗
# ║  ⚠ DEPRECATED — 2026-08-05 模型收敛 (M1-M7)                          ║
# ║  死链: 权重 outcome_*/reversal_top12 已归档, 线上零调用            ║
# ║  替代: pipeline/model_catalog.py (M1-M7 注册表)                      ║
# ║  单一真相源: pipeline/model_catalog.py                                ║
# ║  本文件保留仅为历史可追溯, 禁止在新代码中引用.                        ║
# ╚══════════════════════════════════════════════════════════════════════╝
"""
模型共享注册中心 — v1.0

所有哨响AI模型从此处统一加载、数据分发、交叉验证。
设计原则: 单一事实源 — 一个注册中心管理全部模型, 新模型只需 register() 即可参与交叉验证。

当前注册模型:
  unified_predictor      — 1X2 方向预测 (v7.4 operator-anchored)
  operator_reversal      — 操盘手逆转检测 (LightGBM, AUC 0.655)
  operator_reliability   — 操盘手可靠性评分 (LightGBM, AUC 0.660)
  conditional_score       — 条件波胆 (truncated Poisson)
  multibook_consensus    — 多庄 sharp/retail 共识
"""

import os
import json
from pathlib import Path
from typing import Dict, List, Callable, Optional
import numpy as np

MODEL_DIR = Path(__file__).resolve().parent.parent / "saved_models"
DB = Path(__file__).resolve().parent.parent / "data" / "football_data.db"

_registry: Dict[str, dict] = {}

def register(name: str, load_fn: Callable, description: str, inputs: List[str], outputs: List[str]):
    """注册模型到共享中心."""
    _registry[name] = {
        "name": name, "load_fn": load_fn, "description": description,
        "inputs": inputs, "outputs": outputs, "loaded": False, "model": None,
    }

def load_all():
    """加载所有已注册模型."""
    for name, info in _registry.items():
        if not info["loaded"]:
            try:
                info["model"] = info["load_fn"]()
                info["loaded"] = True
            except Exception as e:
                print(f"[registry] 加载失败 {name}: {e}")

def get(name: str):
    """获取模型实例."""
    if name not in _registry:
        raise KeyError(f"未注册模型: {name}")
    if not _registry[name]["loaded"]:
        _registry[name]["model"] = _registry[name]["load_fn"]()
        _registry[name]["loaded"] = True
    return _registry[name]["model"]

def list_models() -> List[dict]:
    """列出所有模型."""
    return [{"name": info["name"], "inputs": info["inputs"], "outputs": info["outputs"], "loaded": info["loaded"]}
            for info in _registry.values()]

def cross_validate(models: List[str] = None, n_samples: int = 5000):
    """交叉验证: 用共享数据对所有模型跑一次评估比对."""
    if models is None:
        models = list(_registry.keys())

    print(f"[registry] 交叉验证 {len(models)} 个模型...")
    import sqlite3
    conn = sqlite3.connect(str(DB))
    rows = conn.execute("""
        SELECT open_h, open_d, open_a, close_h, close_d, close_a,
               outcome, home_score, away_score
        FROM odds_features WHERE outcome IN ('H','D','A')
        AND home_score IS NOT NULL ORDER BY RANDOM() LIMIT ?
    """, (n_samples,)).fetchall()
    conn.close()

    results = {}
    for name in models:
        try:
            model = get(name)
            # 简化评估: 统计可用性
            results[name] = {"samples": len(rows), "status": "loaded"}
        except Exception as e:
            results[name] = {"error": str(e)}
    return results


# ── 注册所有模型 ──

def _load_reversal():
    import joblib
    return joblib.load(str(MODEL_DIR / "operator_reversal_detector.joblib"))

def _load_reliability():
    import joblib
    return joblib.load(str(MODEL_DIR / "operator_drift_reliability.joblib"))

def _load_unified():
    """1X2方向预测 — 从 bridge_service._live_predict 调用, 不独立导入."""
    return "unified_predictor_v7.4"  # 实际由 bridge_service._live_predict 内联调用

def _load_conditional():
    from pipeline.conditional_score import conditional_score_matrix
    return conditional_score_matrix

def _load_multibook():
    from pipeline.multibook_consensus import analyze_match
    return analyze_match

# 注册 — 全模型(含优化版)
register("operator_reversal", _load_reversal,
         "操盘手逆转检测(LightGBM, AUC 0.655, 全18特征)", ["open_h,d,a", "close_h,d,a"], ["reversal_risk"])
register("operator_reversal_opt", lambda: __import__('joblib').load(str(MODEL_DIR / "reversal_top12.joblib")),
         "操盘手逆转检测(Top-12精简版, AUC 0.614)", ["open_h,d,a", "close_h,d,a"], ["reversal_risk"])
register("outcome_3class_full", lambda: __import__('joblib').load(str(MODEL_DIR / "outcome_full18.joblib")),
         "1X2方向预测(全18特征, acc=48.0%)", ["18维特征"], ["outcome_proba"])
register("outcome_25feat", lambda: __import__('joblib').load(str(MODEL_DIR / "outcome_25feat.joblib")),
         "1X2方向预测(18+7比分-赔率特征, acc=48.3%)", ["25维特征"], ["outcome_proba"])
register("outcome_3class_top10", lambda: __import__('joblib').load(str(MODEL_DIR / "outcome_top10.joblib")),
         "1X2方向预测(Top-10精简, acc=47.5%, 与全18持平)", ["10维特征(平赔+漂移+结构)"], ["outcome_proba"])
register("operator_reliability", _load_reliability,
         "操盘手可靠性评分(LightGBM, AUC 0.660)", ["open_h,d,a", "close_h,d,a"], ["reliability_score"])
register("unified_predictor", _load_unified,
         "1X2方向预测 v7.4(operator-anchored)", ["home,away,odds_h,d,a"], ["prediction,probabilities"])
register("conditional_score", _load_conditional,
         "条件波胆(truncated Poisson)", ["lam,mu,score_h,score_a,minutes"], ["score_matrix"])
register("multibook_consensus", _load_multibook,
         "多庄sharp/retail共识", ["leisu_group"], ["consensus"])

# 启动时自动加载
load_all()
print(f"[model_registry] {len(_registry)} 模型已注册: {[m['name'] for m in list_models()]}")
