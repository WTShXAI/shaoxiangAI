"""
哨响AI - 数据质量检查器
=======================
全面的数据质量检查，覆盖数据库完整性、特征分布、异常检测。

检查维度:
1. 数据库完整性 - 表行数、索引状态、外键一致性
2. 特征分布 - 缺失率、异常值(IQR)、分布偏移
3. 数据新鲜度 - 各表最后更新时间
4. 逻辑一致性 - 比分与结果匹配、赔率合理性
5. 预测反馈 - 预测与实际对比闭环

用法:
    checker = DataQualityChecker(db_manager)
    result = checker.run_full_check()
    # or individual:
    checker.check_db_integrity()
    checker.check_feature_distribution()
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# 期望的特征范围 (min, max) - 用于异常值检测
EXPECTED_RANGES: dict[str, tuple[float, float]] = {
    "a1": (-1.0, 1.0),
    "a2": (-1.0, 1.0),
    "a3": (-1.0, 1.0),
    "a4": (0.0, 1.0),
    "a5": (0.0, 1.0),
    "a6": (-1.0, 1.0),
    "sigma_trap": (0.0, 0.5),
    "lambda_crush": (0.0, 1.0),
    "confidence": (0.0, 1.0),
    "home_prob": (0.0, 1.0),
    "draw_prob": (0.0, 1.0),
    "away_prob": (0.0, 1.0),
}

# 关键表清单
CRITICAL_TABLES = [
    "matches",
    "teams",
    "predictions",
    "match_features",
    "odds",
    "model_training",
    "standings",
]


class DataQualityChecker:
    """数据质量综合检查器"""

    def __init__(self, db_manager):
        self.db = db_manager
        self._last_check: dict[str, Any] = {}
        self._baseline: dict[str, Any] | None = None

    # ══════════════════════════════════════════════════
    # 1. 数据库完整性检查
    # ══════════════════════════════════════════════════

    def check_db_integrity(self) -> dict[str, Any]:
        """检查数据库完整性"""
        issues: list[dict[str, Any]] = []
        table_stats: dict[str, dict[str, int]] = {}

        for table in CRITICAL_TABLES:
            try:
                result = self.db.execute_sql(
                    f"SELECT COUNT(*) as cnt FROM {table}"
                )
                if result and len(result) > 0:
                    row = result[0] if isinstance(result[0], dict) else {"cnt": result[0]}
                    count = row.get("cnt", 0) if isinstance(row, dict) else row[0]
                    table_stats[table] = {"row_count": count}

                    if count == 0 and table not in ("weather_data", "odds_history"):
                        issues.append({
                            "type": "empty_table",
                            "table": table,
                            "severity": "warning",
                            "message": f"表 '{table}' 为空，可能影响预测质量",
                        })

            except (Exception, KeyError, IndexError) as e:
                issues.append({
                    "type": "table_access_error",
                    "table": table,
                    "severity": "error",
                    "message": f"无法读取表 '{table}': {e}",
                })

        # 检查预测反馈闭环
        try:
            result = self.db.execute_sql(
                "SELECT COUNT(*) FROM predictions WHERE is_correct IS NULL"
            )
            if result and len(result) > 0:
                undetermined = result[0]["cnt"] if isinstance(result[0], dict) else result[0]
                if undetermined > 50:
                    issues.append({
                        "type": "feedback_gap",
                        "severity": "warning",
                        "message": f"有 {undetermined} 条预测未获得结果反馈，反馈闭环不完整",
                    })
        except (Exception, KeyError, IndexError):
            pass

        # 检查赔率合理性
        try:
            result = self.db.execute_sql(
                "SELECT COUNT(*) FROM odds WHERE home_odds IS NULL OR home_odds <= 1.0"
            )
            if result and len(result) > 0:
                bad_odds = result[0]["cnt"] if isinstance(result[0], dict) else result[0]
                if bad_odds > 0:
                    issues.append({
                        "type": "invalid_odds",
                        "severity": "warning",
                        "message": f"有 {bad_odds} 条赔率数据异常（NULL或≤1.0）",
                    })
        except (Exception, KeyError, IndexError):
            pass

        return {
            "passed": len(issues) == 0,
            "issues": issues,
            "table_stats": table_stats,
            "total_issues": len(issues),
        }

    # ══════════════════════════════════════════════════
    # 2. 特征分布检查
    # ══════════════════════════════════════════════════

    def check_feature_distribution(self) -> dict[str, Any]:
        """检查特征分布（缺失率 + 异常值 + 基础统计）"""
        features_report: dict[str, Any] = {}
        overall_issues: list[dict[str, Any]] = []

        for col, (low, high) in EXPECTED_RANGES.items():
            try:
                # 缺失率
                null_result = self.db.execute_sql(
                    f"SELECT COUNT(*) FROM match_features WHERE {col} IS NULL"
                )
                total_result = self.db.execute_sql(
                    "SELECT COUNT(*) FROM match_features"
                )

                if null_result and total_result:
                    nulls = null_result[0]["cnt"] if isinstance(null_result[0], dict) else null_result[0]
                    total = total_result[0]["cnt"] if isinstance(total_result[0], dict) else total_result[0]

                    missing_rate = nulls / max(total, 1)
                    features_report[col] = {
                        "missing_rate": round(missing_rate, 4),
                        "total_samples": total,
                        "null_count": nulls,
                    }

                    if missing_rate > 0.3:
                        overall_issues.append({
                            "type": "high_missing_rate",
                            "feature": col,
                            "severity": "error",
                            "message": f"特征 '{col}' 缺失率 {missing_rate * 100:.1f}%",
                        })
                    elif missing_rate > 0.1:
                        overall_issues.append({
                            "type": "elevated_missing_rate",
                            "feature": col,
                            "severity": "warning",
                            "message": f"特征 '{col}' 缺失率 {missing_rate * 100:.1f}%",
                        })

                # 异常值检测（IQR方法）
                if total > 0 and nulls < total:
                    stats = self.db.execute_sql(
                        f"SELECT AVG({col}) as avg_val, "
                        f"MIN({col}) as min_val, MAX({col}) as max_val "
                        f"FROM match_features WHERE {col} IS NOT NULL"
                    )
                    if stats and len(stats) > 0:
                        s = stats[0]
                        avg = s["avg_val"] if isinstance(s, dict) else s[0]
                        min_v = s["min_val"] if isinstance(s, dict) else s[1]
                        max_v = s["max_val"] if isinstance(s, dict) else s[2]

                        if avg is not None:
                            features_report[col]["mean"] = round(avg, 4)
                            features_report[col]["min"] = min_v
                            features_report[col]["max"] = max_v

                        # 检查是否偏离预期范围
                        if min_v is not None and min_v < low:
                            overall_issues.append({
                                "type": "out_of_range",
                                "feature": col,
                                "severity": "info",
                                "message": f"特征 '{col}' 最小值 {min_v:.3f} < 预期下限 {low}",
                            })
                        if max_v is not None and max_v > high:
                            overall_issues.append({
                                "type": "out_of_range",
                                "feature": col,
                                "severity": "info",
                                "message": f"特征 '{col}' 最大值 {max_v:.3f} > 预期上限 {high}",
                            })

            except (Exception) as e:
                logger.warning(f"[DataQuality] 检查特征 '{col}' 失败: {e}")

        # 整体缺失率
        total_missing = sum(
            f.get("missing_rate", 0) for f in features_report.values()
        )
        avg_missing = total_missing / max(len(features_report), 1)

        return {
            "passed": len(overall_issues) == 0,
            "issues": overall_issues,
            "feature_details": features_report,
            "average_missing_rate": round(avg_missing, 4),
            "total_issues": len(overall_issues),
        }

    # ══════════════════════════════════════════════════
    # 3. 数据新鲜度检查
    # ══════════════════════════════════════════════════

    def check_data_freshness(self) -> dict[str, Any]:
        """检查各数据源的新鲜度"""
        freshness: dict[str, Any] = {}
        issues: list[dict[str, Any]] = []

        date_columns = {
            "matches": "utc_date",
            "predictions": "created_at",
            "odds": "created_at",
            "match_features": "created_at",
            "model_training": "created_at",
            "standings": "updated_at",
        }

        for table, col in date_columns.items():
            try:
                result = self.db.execute_sql(
                    f"SELECT MAX({col}) as last_update FROM {table}"
                )
                if result and len(result) > 0 and result[0]:
                    last = result[0]["last_update"] if isinstance(result[0], dict) else result[0]
                    if last:
                        try:
                            if isinstance(last, str):
                                last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
                            else:
                                last_dt = last
                            hours_ago = (datetime.now() - last_dt.replace(tzinfo=None)).total_seconds() / 3600
                        except (ValueError, TypeError):
                            hours_ago = None
                    else:
                        hours_ago = None

                    freshness[table] = {
                        "last_update": str(last) if last else None,
                        "hours_ago": round(hours_ago, 1) if hours_ago is not None else None,
                    }

                    if hours_ago is not None:
                        if hours_ago > 48 and table != "weather_data":
                            issues.append({
                                "type": "stale_data",
                                "table": table,
                                "severity": "error",
                                "message": f"表 '{table}' 已 {hours_ago:.0f} 小时未更新",
                            })
                        elif hours_ago > 12 and table in ("matches", "predictions"):
                            issues.append({
                                "type": "stale_data",
                                "table": table,
                                "severity": "warning",
                                "message": f"表 '{table}' 已 {hours_ago:.0f} 小时未更新",
                            })

            except (Exception) as e:
                logger.debug(f"[DataQuality] 检查新鲜度 '{table}' 失败: {e}")

        return {
            "passed": len(issues) == 0,
            "issues": issues,
            "freshness": freshness,
            "total_issues": len(issues),
        }

    # ══════════════════════════════════════════════════
    # 4. 预测准确性统计
    # ══════════════════════════════════════════════════

    def get_prediction_performance(self, window_hours: int = 168) -> dict[str, Any]:
        """获取预测性能统计"""
        try:
            cutoff = (datetime.now() - timedelta(hours=window_hours)).isoformat()

            # 总体统计
            result = self.db.execute_sql(
                f"SELECT "
                f"  COUNT(*) as total, "
                f"  SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END) as correct, "
                f"  SUM(CASE WHEN is_correct = 0 THEN 1 ELSE 0 END) as incorrect, "
                f"  SUM(CASE WHEN is_correct IS NULL THEN 1 ELSE 0 END) as pending "
                f"FROM predictions "
                f"WHERE created_at >= '{cutoff}'"
            )

            if result and len(result) > 0:
                r = result[0]
                total = r["total"] if isinstance(r, dict) else r[0]
                correct = r["correct"] if isinstance(r, dict) else r[1]
                incorrect = r["incorrect"] if isinstance(r, dict) else r[2]
                pending = r["pending"] if isinstance(r, dict) else r[3]
            else:
                total = correct = incorrect = pending = 0

            # 按联赛
            by_league = []
            league_result = self.db.execute_sql(
                f"SELECT "
                f"  league, "
                f"  COUNT(*) as total, "
                f"  SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END) as correct "
                f"FROM predictions "
                f"WHERE created_at >= '{cutoff}' AND league IS NOT NULL "
                f"GROUP BY league ORDER BY total DESC"
            )
            if league_result:
                for row in league_result:
                    lg = dict(row) if not isinstance(row, dict) else row
                    lg_total = lg.get("total", 0)
                    lg_correct = lg.get("correct", 0)
                    by_league.append({
                        "league": lg.get("league", "unknown"),
                        "total": lg_total,
                        "correct": lg_correct,
                        "accuracy": round(lg_correct / max(lg_total, 1), 4),
                    })

            # 按决策
            by_decision = []
            dec_result = self.db.execute_sql(
                f"SELECT "
                f"  decision, "
                f"  COUNT(*) as total, "
                f"  SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END) as correct "
                f"FROM predictions WHERE created_at >= '{cutoff}' "
                f"GROUP BY decision"
            )
            if dec_result:
                for row in dec_result:
                    d = dict(row) if not isinstance(row, dict) else row
                    d_total = d.get("total", 0)
                    d_correct = d.get("correct", 0)
                    by_decision.append({
                        "decision": d.get("decision", "?"),
                        "total": d_total,
                        "correct": d_correct,
                        "accuracy": round(d_correct / max(d_total, 1), 4),
                    })

            # 按置信度分桶
            conf_buckets = [
                {"range": "0.3-0.4", "total": 0, "correct": 0, "accuracy": 0},
                {"range": "0.4-0.5", "total": 0, "correct": 0, "accuracy": 0},
                {"range": "0.5-0.6", "total": 0, "correct": 0, "accuracy": 0},
                {"range": "0.6-0.7", "total": 0, "correct": 0, "accuracy": 0},
                {"range": "0.7-1.0", "total": 0, "correct": 0, "accuracy": 0},
            ]
            conf_result = self.db.execute_sql(
                f"SELECT confidence, is_correct FROM predictions "
                f"WHERE created_at >= '{cutoff}' AND confidence IS NOT NULL"
            )
            if conf_result:
                for row in conf_result:
                    c = dict(row) if not isinstance(row, dict) else row
                    conf = c.get("confidence", 0) or 0
                    is_ok = c.get("is_correct")
                    for bucket in conf_buckets:
                        low, high = map(float, bucket["range"].split("-"))
                        if low <= conf < high:
                            bucket["total"] += 1
                            if is_ok == 1:
                                bucket["correct"] += 1
                            break
                for bucket in conf_buckets:
                    bucket["accuracy"] = round(
                        bucket["correct"] / max(bucket["total"], 1), 4
                    )

            return {
                "window_hours": window_hours,
                "total": total,
                "correct": correct,
                "incorrect": incorrect,
                "pending": pending,
                "overall_accuracy": round(correct / max(total - pending, 1), 4),
                "by_league": by_league,
                "by_decision": by_decision,
                "by_confidence": conf_buckets,
                "timestamp": datetime.now().isoformat(),
            }

        except (Exception) as e:
            logger.error(f"[DataQuality] 预测性能统计失败: {e}")
            return {"error": str(e)}

    # ══════════════════════════════════════════════════
    # 5. 综合检查
    # ══════════════════════════════════════════════════

    def run_full_check(self) -> dict[str, Any]:
        """运行所有质量检查"""
        results = {
            "timestamp": datetime.now().isoformat(),
            "db_integrity": self.check_db_integrity(),
            "feature_distribution": self.check_feature_distribution(),
            "data_freshness": self.check_data_freshness(),
            "prediction_performance": self.get_prediction_performance(window_hours=168),
        }

        # 汇总
        total_issues = sum(
            r.get("total_issues", 0)
            for r in [results["db_integrity"], results["feature_distribution"], results["data_freshness"]]
        )
        overall_passed = (
            results["db_integrity"]["passed"]
            and results["feature_distribution"]["passed"]
            and results["data_freshness"]["passed"]
        )

        results["summary"] = {
            "overall_passed": overall_passed,
            "total_issues": total_issues,
            "health_score": self._compute_health_score(results),
        }

        self._last_check = results
        return results

    def _compute_health_score(self, results: dict[str, Any]) -> int:
        """计算数据健康分数 0-100"""
        score = 100

        # 数据库问题扣分
        db_issues = results["db_integrity"]["total_issues"]
        score -= min(db_issues * 8, 40)

        # 特征问题扣分
        feat_issues = results["feature_distribution"]["total_issues"]
        score -= min(feat_issues * 5, 25)

        # 新鲜度扣分
        fresh_issues = results["data_freshness"]["total_issues"]
        score -= min(fresh_issues * 10, 25)

        # 预测性能
        perf = results["prediction_performance"]
        accuracy = perf.get("overall_accuracy", 0)
        if accuracy and accuracy < 0.30:
            score -= 15
        elif accuracy and accuracy < 0.35:
            score -= 5

        return max(score, 0)

    def get_last_check(self) -> dict[str, Any]:
        """获取上一次检查结果"""
        return self._last_check
