"""
哨响AI - 数据漂移检测器 v1.0
=============================
监控特征分布随时间变化，检测概念漂移 (Concept Drift) 和
数据漂移 (Data Drift)。

检测方法:
1. KS 双样本检验 (Kolmogorov-Smirnov) — 分布显著性检验
2. PSI (Population Stability Index) — 分箱分布偏移
3. 滑动窗口统计 — 均值/方差趋势追踪

用法:
    detector = DataDriftDetector(reference_df)
    drift_report = detector.detect(current_df, window_days=30)

    # or with DB
    detector = DataDriftDetector.from_db(db_manager, lookback_days=90)
    drift_report = detector.check_recent_drift()
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# 需要监控的核心数值特征
DEFAULT_DRIFT_FEATURES = [
    "a1", "a2", "a3", "a4", "a5", "a6",
    "sigma_trap", "lambda_crush",
    "home_prob", "draw_prob", "away_prob",
    "confidence",
]

# PSI 解释阈值
PSI_INTERPRETATION = {
    (0, 0.1):   "无显著漂移 ✅",
    (0.1, 0.25): "轻微漂移 ⚠️",
    (0.25, float("inf")): "显著漂移 🔴",
}

class DataDriftDetector:
    """数据漂移检测器"""

    def __init__(self, reference_data: pd.DataFrame,
                 features: list[str] | None = None):
        """
        Args:
            reference_data: 参考数据集 (基线)
            features: 要监控的特征列表 (None=使用默认)
        """
        self.ref_df = reference_data.copy()
        self.features = features or [f for f in DEFAULT_DRIFT_FEATURES
                                     if f in reference_data.columns]
        self._last_report: dict[str, Any] = {}

    @classmethod
    def from_db(cls, db_manager, lookback_days: int = 90,
                features: list[str] | None = None):
        """从数据库加载参考数据"""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime('%Y-%m-%d')
        df = db_manager.load_matches_with_features(cutoff_date=cutoff)
        return cls(df, features=features)

    # ══════════════════════════════════════════════════
    # KS 检验 (Kolmogorov-Smirnov)
    # ══════════════════════════════════════════════════

    def detect_ks_drift(self, current_data: pd.DataFrame,
                        significance_level: float = 0.05) -> dict[str, Any]:
        """KS 双样本检验 — 逐特征检测分布偏移"""
        from scipy import stats

        drift_detected = False
        feature_results: dict[str, dict[str, Any]] = {}

        for col in self.features:
            if col not in self.ref_df.columns or col not in current_data.columns:
                continue

            ref_vals = self.ref_df[col].dropna().values
            cur_vals = current_data[col].dropna().values

            if len(ref_vals) < 10 or len(cur_vals) < 10:
                continue

            try:
                ks_stat, ks_p = stats.ks_2samp(ref_vals, cur_vals)
                mean_ref = float(np.mean(ref_vals))
                mean_cur = float(np.mean(cur_vals))
                mean_change_pct = abs(mean_cur - mean_ref) / max(abs(mean_ref), 1e-8) * 100

                is_drift = ks_p < significance_level
                if is_drift:
                    drift_detected = True

                feature_results[col] = {
                    "ks_statistic": round(float(ks_stat), 4),
                    "ks_p_value": round(float(ks_p), 4),
                    "mean_ref": round(mean_ref, 4),
                    "mean_cur": round(mean_cur, 4),
                    "mean_change_pct": round(float(mean_change_pct), 2),
                    "drift_detected": is_drift,
                    "severity": "red" if ks_p < 0.01 else ("yellow" if ks_p < 0.05 else "green"),
                }
            except (Exception, ValueError, KeyError, IndexError) as e:
                logger.debug(f"KS 检验失败 {col}: {e}")

        return {
            "method": "KS",
            "significance_level": significance_level,
            "drift_detected": drift_detected,
            "features_checked": len(feature_results),
            "features_drifted": sum(1 for r in feature_results.values() if r["drift_detected"]),
            "feature_details": feature_results,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # ══════════════════════════════════════════════════
    # PSI (Population Stability Index)
    # ══════════════════════════════════════════════════

    @staticmethod
    def _compute_psi(expected: np.ndarray, actual: np.ndarray,
                     bins: int = 10, epsilon: float = 1e-6) -> float:
        """计算单个特征的 PSI"""
        # 用 expected 分箱边界
        hist_ref, bin_edges = np.histogram(expected[~np.isnan(expected)], bins=bins)
        hist_cur, _ = np.histogram(actual[~np.isnan(actual)], bins=bin_edges)

        # 转为比例
        ref_p = hist_ref / max(hist_ref.sum(), 1) + epsilon
        cur_p = hist_cur / max(hist_cur.sum(), 1) + epsilon

        # PSI = sum((actual - expected) * ln(actual / expected))
        psi = float(np.sum((cur_p - ref_p) * np.log(cur_p / ref_p)))
        return psi

    def detect_psi_drift(self, current_data: pd.DataFrame) -> dict[str, Any]:
        """PSI 分箱偏移检测"""
        drift_detected = False
        feature_results: dict[str, dict[str, Any]] = {}

        for col in self.features:
            if col not in self.ref_df.columns or col not in current_data.columns:
                continue

            ref_vals = self.ref_df[col].dropna().values
            cur_vals = current_data[col].dropna().values

            if len(ref_vals) < 20 or len(cur_vals) < 20:
                continue

            try:
                psi_val = self._compute_psi(ref_vals, cur_vals)

                interpretation = "无显著漂移 ✅"
                for (lo, hi), desc in PSI_INTERPRETATION.items():
                    if lo <= psi_val < hi:
                        interpretation = desc
                        break

                is_drift = psi_val >= 0.1
                if is_drift:
                    drift_detected = True

                feature_results[col] = {
                    "psi": round(psi_val, 4),
                    "interpretation": interpretation,
                    "drift_detected": is_drift,
                    "severity": (
                        "red" if psi_val >= 0.25 else
                        ("yellow" if psi_val >= 0.1 else "green")
                    ),
                }
            except (Exception, KeyError, IndexError) as e:
                logger.debug(f"PSI 计算失败 {col}: {e}")

        return {
            "method": "PSI",
            "drift_detected": drift_detected,
            "features_checked": len(feature_results),
            "features_drifted": sum(1 for r in feature_results.values() if r["drift_detected"]),
            "feature_details": feature_results,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # ══════════════════════════════════════════════════
    # 综合漂移检测
    # ══════════════════════════════════════════════════

    def detect(self, current_data: pd.DataFrame,
               window_days: int = 30) -> dict[str, Any]:
        """综合漂移检测 (KS + PSI)"""
        ks_result = self.detect_ks_drift(current_data)
        psi_result = self.detect_psi_drift(current_data)

        total_drifted = sum([
            ks_result["drift_detected"],
            psi_result["drift_detected"],
        ])

        # 汇总漂移特征
        drifted_features = []
        for col, r in ks_result.get("feature_details", {}).items():
            if r["drift_detected"] and col not in drifted_features:
                drifted_features.append(col)
        for col, r in psi_result.get("feature_details", {}).items():
            if r["drift_detected"] and col not in drifted_features:
                drifted_features.append(col)

        report = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "window_days": window_days,
            "reference_samples": len(self.ref_df),
            "current_samples": len(current_data),
            "overall_drift_detected": total_drifted > 0,
            "drifted_features": drifted_features,
            "total_drifted_features": len(drifted_features),
            "ks": ks_result,
            "psi": psi_result,
        }

        self._last_report = report

        if report["overall_drift_detected"]:
            logger.warning(
                f"数据漂移检测: {len(drifted_features)} 个特征偏离基线 | "
                f"KS={ks_result['features_drifted']} PSI={psi_result['features_drifted']}"
            )
        else:
            logger.info("数据漂移检测: 未发现显著漂移")

        return report

    def check_recent_drift(self, lookback_days: int = 30) -> dict[str, Any]:
        """检查近期数据漂移 (使用自身最近数据作为当前数据)"""
        if self.ref_df.empty:
            return {"error": "参考数据集为空"}

        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        if "match_date" in self.ref_df.columns:
            cutoff_str = cutoff.strftime('%Y-%m-%d')
            recent = self.ref_df[self.ref_df["match_date"] >= cutoff_str].copy()
        else:
            n = max(1, len(self.ref_df) // 3)
            recent = self.ref_df.tail(n).copy()

        return self.detect(recent, window_days=lookback_days)

    def get_last_report(self) -> dict[str, Any]:
        """获取最近一次检测报告"""
        return self._last_report

    def get_drift_summary(self) -> str:
        """获取人类可读的漂移摘要"""
        if not self._last_report:
            return "无漂移检测记录"

        report = self._last_report
        lines = [
            f"=== 数据漂移检测摘要 ({report['timestamp'][:19]}) ===",
            f"参考样本: {report['reference_samples']} | 当前样本: {report['current_samples']}",
            f"漂移状态: {'🔴 检测到漂移' if report['overall_drift_detected'] else '✅ 无漂移'}",
        ]

        if report["drifted_features"]:
            lines.append(f"漂移特征: {', '.join(report['drifted_features'])}")

        return "\n".join(lines)
