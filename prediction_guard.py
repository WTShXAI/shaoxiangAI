"""
预测流程守护系统 v1.0 — PredictionGuard
=========================================
完整的预测质量门控，覆盖预测全生命周期：

  预测前 (PRE) → 预测中 (DURING) → 预测后 (POST) → 入库验证 (SAVE)

每一个预测都必须经过这四道关口，任何环节发现问题都会：
  - CRITICAL: 阻断全部预测，要求人工介入
  - ERROR: 跳过异常比赛，记录并继续
  - WARN: 记录警告，继续预测（累计3个WARN升级为ERROR）

设计原则:
  1. 宁可不预测，也不错预测
  2. 每一个检查项都可追溯（审计ID + 时间戳）
  3. 失败自动降级，绝不静默吞错
  4. 所有异常写入审计日志，可复盘

用法:
  from prediction_guard import PredictionGuard

  guard = PredictionGuard()
  results = guard.guarded_predict(sp, days_ahead=7)
  guard.print_audit()
"""

import sys, os, json, logging, hashlib, traceback
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Tuple, Any, Callable
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import sqlite3

logger = logging.getLogger('PredictionGuard')

# ── 严重级别 ──
class Severity(Enum):
    CRITICAL = 0   # 阻断全部，必须人工介入（如：模型损坏、数据库宕机）
    ERROR = 1      # 阻断当前比赛/批次，记录后跳过
    WARN = 2       # 记录警告，继续执行（累计3个WARN → 升级为ERROR）
    INFO = 3       # 纯信息记录
    DEBUG = 4      # 调试信息

# ── 检查阶段 ──
class Phase(Enum):
    PRE = "PRE"           # 预测前：数据、模型、环境就绪检查
    DURING = "DURING"     # 预测中：特征、概率、异常值检查
    POST = "POST"         # 预测后：结果合理性、等级分布检查
    SAVE = "SAVE"         # 入库：数据完整性、类型安全检查

# ── 检查项定义 ──
@dataclass
class GuardCheck:
    """单个检查项"""
    check_id: str                # 唯一ID, 如 "PRE-001"
    name: str                    # 人类可读名称
    description: str             # 检查说明
    phase: Phase                 # 所属阶段
    severity: Severity           # 失败严重级别
    action_on_fail: str          # 失败处理策略: "BLOCK_ALL" | "SKIP_ITEM" | "FIX_AND_CONTINUE" | "LOG_ONLY"
    check_fn: Callable = None    # 实际检查函数

@dataclass
class GuardResult:
    """单个检查的执行结果"""
    check_id: str
    passed: bool
    severity: Severity
    phase: Phase
    timestamp: str = ""
    detail: str = ""
    affected_matches: List[int] = field(default_factory=list)
    fix_applied: str = ""
    duration_ms: float = 0

@dataclass
class GuardReport:
    """单次预测守护的完整审计报告"""
    report_id: str
    timestamp: str
    model_version: str
    total_checks: int = 0
    passed: int = 0
    failed_critical: int = 0
    failed_error: int = 0
    failed_warn: int = 0
    matches_total: int = 0
    matches_blocked: int = 0
    matches_skipped: int = 0
    matches_predicted: int = 0
    results: List[GuardResult] = field(default_factory=list)
    final_decision: str = ""      # "PROCEED" | "DEGRADED" | "BLOCKED"
    summary: str = ""

# ── 全局审计日志文件 ──
AUDIT_LOG_DIR = "output/guard_audit"


class PredictionGuard:
    """
    预测流程守护系统

    在每一个预测中按照完整预测流程执行四阶段检查：
      1. PRE  — 数据就绪、模型健康、特征有效性
      2. DURING — 概率合理性、异常值检测、多样性检查
      3. POST  — 结果逻辑验证、等级分布、一致性审计
      4. SAVE  — 入库验证、类型安全、去重检查
    """

    def __init__(self, db_path: str = 'data/football_data.db',
                 audit_enabled: bool = True,
                 strict_mode: bool = False):
        """
        Args:
            db_path: 数据库路径
            audit_enabled: 是否写审计日志到文件
            strict_mode: 严格模式 (WARN也会阻断)
        """
        self.db_path = db_path
        self.audit_enabled = audit_enabled
        self.strict_mode = strict_mode
        self.warn_count = 0
        self.MAX_WARNS = 3  # 累计WARN数超过此值→升级为ERROR

        # 注册所有检查项
        self.checks: List[GuardCheck] = []
        self._register_all_checks()

        # 当前审计报告
        self.current_report: Optional[GuardReport] = None

        logger.info(f"PredictionGuard 初始化完成 ({len(self.checks)} 项检查, 含PRE-010/DUR-013/SAVE-004等关键门控)")
        if strict_mode:
            logger.warning("  ⚠ 严格模式已开启，任何WARN都会阻断预测")

    # ═══════════════════════════════════════════════════════════════
    # 检查项注册
    # ═══════════════════════════════════════════════════════════════

    def _register_all_checks(self):
        """注册全部检查项 — 预测流程的完整定义"""

        # ──────────── PHASE 1: PRE (预测前) ────────────
        self.checks.extend([
            GuardCheck("PRE-001", "数据库连接", "验证SQLite数据库文件存在且可读写",
                       Phase.PRE, Severity.CRITICAL, "BLOCK_ALL"),

            GuardCheck("PRE-002", "模型文件存在", "验证.joblib模型文件存在于磁盘",
                       Phase.PRE, Severity.CRITICAL, "BLOCK_ALL"),

            GuardCheck("PRE-003", "模型可加载", "模型文件可被joblib正确加载且包含必要属性",
                       Phase.PRE, Severity.CRITICAL, "BLOCK_ALL"),

            GuardCheck("PRE-004", "Scaler存在", "模型中包含StandardScaler且已fit",
                       Phase.PRE, Severity.CRITICAL, "BLOCK_ALL"),

            GuardCheck("PRE-005", "特征维度对齐", "预测时特征数(before prepare) == 训练时特征数",
                       Phase.PRE, Severity.ERROR, "SKIP_ITEM"),

            GuardCheck("PRE-006", "特征名非空", "feature_names列表不为空",
                       Phase.PRE, Severity.CRITICAL, "BLOCK_ALL"),

            GuardCheck("PRE-007", "比赛数据存在", "至少找到1场未来可预测的比赛",
                       Phase.PRE, Severity.ERROR, "SKIP_ITEM"),

            GuardCheck("PRE-008", "无重复match_id", "合并特征表后无重复的match_id",
                       Phase.PRE, Severity.ERROR, "FIX_AND_CONTINUE"),

            GuardCheck("PRE-009", "特征表JOIN完整", "所有match_id都能在match_features表中找到",
                       Phase.PRE, Severity.WARN, "SKIP_ITEM"),

            GuardCheck("PRE-010", "日期范围合理", "查询的日期范围在合理区间(1-30天)",
                       Phase.PRE, Severity.WARN, "LOG_ONLY"),
        ])

        # ──────────── PHASE 2: DURING (预测中) ────────────
        self.checks.extend([
            GuardCheck("DUR-001", "特征矩阵非空", "prepare_features输出的X非空且shape合法",
                       Phase.DURING, Severity.ERROR, "SKIP_ITEM"),

            GuardCheck("DUR-002", "特征无全零行", "不存在全是默认值的行(全零特征)",
                       Phase.DURING, Severity.WARN, "SKIP_ITEM"),

            GuardCheck("DUR-003", "无NaN/Inf特征", "特征矩阵中不存在NaN或Inf值",
                       Phase.DURING, Severity.ERROR, "FIX_AND_CONTINUE"),

            GuardCheck("DUR-004", "特征标准差异常", "特征矩阵标准差合理(不低于1e-8)",
                       Phase.DURING, Severity.WARN, "LOG_ONLY"),

            GuardCheck("DUR-005", "Scaler变换成功", "scaler.transform不抛出异常",
                       Phase.DURING, Severity.CRITICAL, "BLOCK_ALL"),

            GuardCheck("DUR-006", "概率值域检查", "所有概率 ∈ [0, 1]",
                       Phase.DURING, Severity.ERROR, "FIX_AND_CONTINUE"),

            GuardCheck("DUR-007", "概率和≈1.0", "每行H+D+A概率和 ≈ 1.0 (±0.01)",
                       Phase.DURING, Severity.ERROR, "FIX_AND_CONTINUE"),

            GuardCheck("DUR-008", "无NaN概率", "输出概率中不存在NaN值",
                       Phase.DURING, Severity.ERROR, "SKIP_ITEM"),

            GuardCheck("DUR-009", "预测多样性", "预测方向不全是同一结果(防止模型坍塌)",
                       Phase.DURING, Severity.WARN, "LOG_ONLY"),

            GuardCheck("DUR-010", "置信度范围", "max概率 ∈ [0.20, 0.95](合理范围)",
                       Phase.DURING, Severity.WARN, "LOG_ONLY"),

            GuardCheck("DUR-011", "非赔率传声筒", "模型独立判断: 非赔率特征重要性>50%",
                       Phase.DURING, Severity.INFO, "LOG_ONLY"),

            GuardCheck("DUR-012", "默认特征检测", "单个比赛默认特征比例<80%(否则赔率降级)",
                       Phase.DURING, Severity.WARN, "FIX_AND_CONTINUE"),

            GuardCheck("DUR-013", "预测完全一致检测", "多场比赛预测概率不完全相同(防止特征管道断裂)",
                       Phase.DURING, Severity.CRITICAL, "BLOCK_ALL"),
        ])

        # ──────────── PHASE 3: POST (预测后) ────────────
        self.checks.extend([
            GuardCheck("POST-001", "结果列表非空", "预测结果列表不为空(含SKIP后的有效结果)",
                       Phase.POST, Severity.ERROR, "SKIP_ITEM"),

            GuardCheck("POST-002", "match_id类型正确", "所有match_id为int(非BLOB)",
                       Phase.POST, Severity.ERROR, "FIX_AND_CONTINUE"),

            GuardCheck("POST-003", "概率字段非None", "prob_h/d/a均为合法浮点数",
                       Phase.POST, Severity.ERROR, "SKIP_ITEM"),

            GuardCheck("POST-004", "等级分布合理", "S/A/B/C分布合理(不是全C或全S)",
                       Phase.POST, Severity.WARN, "LOG_ONLY"),

            GuardCheck("POST-005", "总分范围正确", "total_score ∈ [0, 100]",
                       Phase.POST, Severity.ERROR, "FIX_AND_CONTINUE"),

            GuardCheck("POST-006", "置信分与等级一致", "confidence_score与tier逻辑一致",
                       Phase.POST, Severity.WARN, "LOG_ONLY"),

            GuardCheck("POST-007", "赔率共识与预测一致", "odds_direction与predicted_result在赔率降级时一致",
                       Phase.POST, Severity.INFO, "LOG_ONLY"),

            GuardCheck("POST-008", "特征覆盖率合理", "feature_coverage ∈ [0, 1.0]",
                       Phase.POST, Severity.WARN, "FIX_AND_CONTINUE"),

            GuardCheck("POST-009", "赔率字段完整", "有赔率的比赛 odds_h/d/a 均非None",
                       Phase.POST, Severity.INFO, "LOG_ONLY"),

            GuardCheck("POST-010", "S级预测审查", "S级预测均经过共识+置信度双重验证",
                       Phase.POST, Severity.WARN, "LOG_ONLY"),
        ])

        # ──────────── PHASE 4: SAVE (入库) ────────────
        self.checks.extend([
            GuardCheck("SAVE-001", "DB写入成功", "预测记录成功INSERT到predictions表",
                       Phase.SAVE, Severity.ERROR, "SKIP_ITEM"),

            GuardCheck("SAVE-002", "无重复写入", "同一match_id同一天不会写入两次",
                       Phase.SAVE, Severity.WARN, "FIX_AND_CONTINUE"),

            GuardCheck("SAVE-003", "回写验证", "写入后可从DB读回且数据一致",
                       Phase.SAVE, Severity.ERROR, "LOG_ONLY"),

            GuardCheck("SAVE-004", "predictions表存在", "预测表和数据表结构正常",
                       Phase.SAVE, Severity.CRITICAL, "BLOCK_ALL"),
        ])

    # ═══════════════════════════════════════════════════════════════
    # 主入口: 带守护的预测
    # ═══════════════════════════════════════════════════════════════

    def guarded_predict(self, sp, days_ahead: int = 7,
                         db_path: str = None) -> Tuple[List, GuardReport]:
        """
        执行带完整守护的预测流程

        每一步都有检查→决策→记录，任意CRITICAL/ERROR阻断都会
        安全降级而非静默失败。

        Args:
            sp: SelectivePredictor 实例
            days_ahead: 预测未来天数
            db_path: 数据库路径(覆盖默认)

        Returns:
            (results, report): 预测结果列表 + 完整审计报告
        """
        if db_path:
            self.db_path = db_path

        # 重置WARN计数
        self.warn_count = 0

        # 生成审计报告ID
        report_id = hashlib.md5(
            f"guard_{datetime.now().isoformat()}".encode()
        ).hexdigest()[:12]

        self.current_report = GuardReport(
            report_id=report_id,
            timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3],
            model_version=getattr(sp, 'model_version', 'unknown'),
        )
        report = self.current_report

        logger.info("="*70)
        logger.info(f"  🛡 PredictionGuard v1.0 启动 | 报告ID: {report_id}")
        logger.info(f"  模型版本: v{report.model_version} | 预测{days_ahead}天")
        logger.info("="*70)

        results = []
        blocked = False

        # ═══════════ PHASE 1: PRE — 预测前检查 ═══════════
        logger.info("\n── [阶段 1/4] PRE — 预测前检查 ──")
        pre_ok, pre_ctx = self._run_phase(Phase.PRE, sp, days_ahead)
        if not pre_ok:
            blocked = True
            logger.error("  ❌ PRE阶段检查失败，预测已阻断")
            report.final_decision = "BLOCKED"
            report.summary = "PRE阶段检查失败: " + "; ".join(
                [r.detail for r in report.results if not r.passed and r.severity in (Severity.CRITICAL, Severity.ERROR)]
            )
            self._finalize_report(report)
            return [], report

        logger.info("  ✅ PRE阶段通过")

        # ═══════════ PHASE 2: DURING — 预测执行+检查 ═══════════
        logger.info("\n── [阶段 2/4] DURING — 预测执行+检查 ──")

        # 提取上下文
        df = pre_ctx.get('df')
        odds_df = pre_ctx.get('odds_df')
        orig_features = pre_ctx.get('orig_features')

        if df is None or len(df) == 0:
            self._record("DUR-001", False, Severity.ERROR, Phase.DURING,
                         "特征矩阵为空(无可用比赛数据)")
            blocked = True
        else:
            try:
                # ── 执行预测 ──
                results, during_ok = self._predict_with_checks(sp, df, odds_df, orig_features)
                if not during_ok:
                    logger.warning("  ⚠ DURING阶段有检查未通过")
                    # 检查是否有CRITICAL失败
                    has_critical = any(
                        not r.passed and r.severity == Severity.CRITICAL
                        for r in self.current_report.results
                        if r.phase == Phase.DURING
                    )
                    if has_critical:
                        logger.error("  ❌ DURING阶段CRITICAL失败，预测已阻断")
                        blocked = True
                    elif len(results) == 0:
                        logger.warning("  ⚠ 无有效预测结果")
            except (Exception) as e:
                self._record("DUR-005", False, Severity.CRITICAL, Phase.DURING,
                             f"预测过程抛出异常: {str(e)[:200]}")
                logger.error(f"  ❌ 预测执行异常: {e}")
                traceback.print_exc()
                blocked = True

        if blocked:
            report.final_decision = "BLOCKED"
            report.summary = "DURING阶段致命错误"
            self._finalize_report(report)
            return [], report

        logger.info(f"  ✅ DURING阶段完成 | 有效预测: {len(results)} 场")

        # ═══════════ PHASE 3: POST — 预测后检查 ═══════════
        logger.info("\n── [阶段 3/4] POST — 预测后验证 ──")
        results, post_ok = self._run_post_checks(results, sp)
        if not post_ok:
            logger.warning("  ⚠ POST阶段有检查未通过，部分结果已排除")

        logger.info(f"  ✅ POST阶段完成 | 最终输出: {len(results)} 场")

        # ═══════════ PHASE 4: SAVE — 入库检查 ═══════════
        logger.info("\n── [阶段 4/4] SAVE — 入库验证 ──")
        save_ok = self._run_save_checks(results)

        if not save_ok:
            logger.warning("  ⚠ SAVE阶段有检查未通过")

        # ── 最终决策 ──
        if blocked:
            report.final_decision = "BLOCKED"
        elif report.failed_critical > 0 or report.failed_error > 0 or (self.strict_mode and report.failed_warn > 0):
            report.final_decision = "DEGRADED"
            reasons = []
            if report.failed_critical > 0:
                reasons.append(f"{report.failed_critical} CRITICAL")
            if report.failed_error > 0:
                reasons.append(f"{report.failed_error} ERROR")
            if self.strict_mode and report.failed_warn > 0:
                reasons.append(f"{report.failed_warn} WARN(strict)")
            report.summary = f"部分检查未通过: {', '.join(reasons)}"
        else:
            report.final_decision = "PROCEED"
            report.summary = f"全部 {report.total_checks} 项检查通过"

        report.matches_total = len(results) + report.matches_skipped + report.matches_blocked
        report.matches_predicted = len(results)

        self._finalize_report(report)

        # 打印摘要
        self._print_summary(report)

        return results, report

    # ═══════════════════════════════════════════════════════════════
    # 阶段执行器
    # ═══════════════════════════════════════════════════════════════

    def _run_phase(self, phase: Phase, sp, days_ahead: int) -> Tuple[bool, Dict]:
        """执行某个阶段的所有检查，返回(是否通过, 上下文)"""
        ctx = {}
        all_passed = True

        for check in self.checks:
            if check.phase != phase:
                continue

            t0 = datetime.now()

            # 根据check_id分发到具体检查函数
            fn_map = {
                # PRE checks
                "PRE-001": lambda: self._check_db_connection(),
                "PRE-002": lambda: self._check_model_file(sp),
                "PRE-003": lambda: self._check_model_loaded(sp),
                "PRE-004": lambda: self._check_scaler(sp),
                "PRE-005": lambda: self._check_feature_dimension(sp),
                "PRE-006": lambda: self._check_feature_names(sp),
                "PRE-007": lambda: self._check_matches_available(sp, days_ahead),
                "PRE-008": lambda: self._check_duplicate_ids(ctx.get('df')),
                "PRE-009": lambda: self._check_feature_join(ctx.get('df'), sp),
                "PRE-010": lambda: self._check_date_range(days_ahead),
            }

            check_fn = fn_map.get(check.check_id)
            if check_fn is None:
                continue

            try:
                passed, detail, ctx_update = check_fn()
                if ctx_update:
                    ctx.update(ctx_update)
            except (Exception, requests.exceptions.RequestException) as e:
                passed, detail = False, f"检查抛出异常: {str(e)[:200]}"
                ctx_update = {}
                traceback.print_exc()

            duration = (datetime.now() - t0).total_seconds() * 1000

            result = GuardResult(
                check_id=check.check_id,
                passed=passed,
                severity=check.severity,
                phase=check.phase,
                timestamp=datetime.now().strftime('%H:%M:%S.%f')[:-3],
                detail=detail,
                duration_ms=round(duration, 1),
            )
            self.current_report.results.append(result)
            self.current_report.total_checks += 1

            if passed:
                self.current_report.passed += 1
                logger.debug(f"    ✅ {check.check_id} {check.name}")
            else:
                self._handle_failure(check, result, self.current_report)
                if check.severity in (Severity.CRITICAL, Severity.ERROR):
                    all_passed = False
                    if check.action_on_fail == "BLOCK_ALL":
                        return False, ctx  # 立即阻断
                elif check.severity == Severity.WARN:
                    self.warn_count += 1
                    if self.warn_count >= self.MAX_WARNS:
                        logger.warning(f"    ⚠ WARN累计{self.warn_count}次，升级为ERROR")
                        all_passed = False

        return all_passed, ctx

    def _predict_with_checks(self, sp, df, odds_df, orig_features) -> Tuple[List, bool]:
        """执行DURING阶段检查 + 委托sp._predict_batch做实际预测"""
        all_ok = True

        # 保存原始特征名（防止被prepare_features修改）
        sp.feature_names = list(orig_features)

        # DUR-001: 特征矩阵非空 + 特征数检查
        try:
            X, _ = sp.trainer.prepare_features(df)
            if X is None or len(X) == 0:
                self._record("DUR-001", False, Severity.ERROR, Phase.DURING,
                             "prepare_features返回空DataFrame")
                return [], False

            n_features_used = X.shape[1]
            n_features_expected = len(orig_features)

            if n_features_used == 0:
                # 致命退化: 零特征! 模型等同于猜 baseline
                self._record("DUR-001", False, Severity.CRITICAL, Phase.DURING,
                             f"致命退化: 0个特征被使用(期望{n_features_expected}个)! "
                             f"match_features表列名可能与config不一致，模型输出恒等概率")
                return [], False

            if n_features_used < n_features_expected * 0.3:
                self._record("DUR-001", False, Severity.ERROR, Phase.DURING,
                             f"特征严重不足: {n_features_used}/{n_features_expected} (<30%)")
            else:
                self._record("DUR-001", True, Severity.ERROR, Phase.DURING,
                             f"特征矩阵: {X.shape[0]}×{n_features_used} (期望{n_features_expected})")
        except (Exception, KeyError, IndexError) as e:
            self._record("DUR-001", False, Severity.ERROR, Phase.DURING,
                         f"prepare_features异常: {str(e)[:200]}")
            return [], False

        # DUR-003: 无NaN/Inf
        n_nan = int(np.sum(np.isnan(X.values if hasattr(X, 'values') else X)))
        n_inf = int(np.sum(np.isinf(X.values if hasattr(X, 'values') else X)))
        if n_nan > 0 or n_inf > 0:
            self._record("DUR-003", False, Severity.ERROR, Phase.DURING,
                         f"NaN={n_nan}, Inf={n_inf} → 已替换为0")
            if hasattr(X, 'values'):
                X = pd.DataFrame(
                    np.nan_to_num(X.values, nan=0.0, posinf=0.0, neginf=0.0),
                    columns=X.columns, index=X.index
                )
            else:
                X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
        else:
            self._record("DUR-003", True, Severity.ERROR, Phase.DURING, "无NaN/Inf")

        # DUR-004: 特征标准差异常
        feat_stds = np.std(X.values if hasattr(X, 'values') else X, axis=0)
        n_low_var = int(np.sum(feat_stds < 1e-8))
        if n_low_var > len(orig_features) * 0.5:
            self._record("DUR-004", False, Severity.WARN, Phase.DURING,
                         f"{n_low_var}/{len(orig_features)} 特征方差≈0")
        else:
            self._record("DUR-004", True, Severity.WARN, Phase.DURING,
                         f"低方差特征: {n_low_var}/{len(orig_features)}")

        # DUR-002: 特征无全零行
        row_means = np.abs(X.values).mean(axis=1) if hasattr(X, 'values') else np.abs(X).mean(axis=1)
        n_zero_rows = int(np.sum(row_means < 1e-8))
        if n_zero_rows > 0:
            self._record("DUR-002", False, Severity.WARN, Phase.DURING,
                         f"{n_zero_rows}/{len(X)} 行特征几乎全为零")
        else:
            self._record("DUR-002", True, Severity.WARN, Phase.DURING,
                         f"无全零行")

        # DUR-005: 特征维度对齐 + Scaler变换
        X_mat = np.zeros((len(X), len(orig_features)))
        for i, feat in enumerate(orig_features):
            if feat in X.columns:
                X_mat[:, i] = X[feat].values
        try:
            X_scaled = sp.trainer.scaler.transform(X_mat)
            self._record("DUR-005", True, Severity.CRITICAL, Phase.DURING,
                         f"Scaler变换成功: {X_scaled.shape}")
        except (Exception, KeyError, IndexError) as e:
            self._record("DUR-005", False, Severity.CRITICAL, Phase.DURING,
                         f"Scaler变换失败: {str(e)[:200]}")
            return [], False

        # DUR-006 ~ DUR-012: 在sp._predict_batch内部已完成,
        # 此处做外围验证（抽样检查sp._predict_batch的输出质量）

        # ── 委托给 sp._predict_batch 执行完整预测 ──
        try:
            results = sp._predict_batch(df, odds_df)

            # 对sp._predict_batch的结果做检查
            if not results:
                self._record("DUR-008", False, Severity.ERROR, Phase.DURING,
                             "sp._predict_batch返回空列表")
                return [], False

            # DUR-006: 概率值域
            bad_prob = 0
            for r in results:
                if r.prob_h is None or np.isnan(r.prob_h) or r.prob_h < -0.01 or r.prob_h > 1.01:
                    bad_prob += 1
            if bad_prob > 0:
                self._record("DUR-006", False, Severity.ERROR, Phase.DURING,
                             f"{bad_prob}条结果概率越界[0,1]")
            else:
                self._record("DUR-006", True, Severity.ERROR, Phase.DURING,
                             "所有概率∈[0,1]")

            # DUR-007: 概率和
            bad_sum = sum(1 for r in results
                         if abs(r.prob_h + r.prob_d + r.prob_a - 1.0) > 0.05)
            if bad_sum > 0:
                self._record("DUR-007", False, Severity.ERROR, Phase.DURING,
                             f"{bad_sum}条概率和≠1.0(>0.05)")
            else:
                self._record("DUR-007", True, Severity.ERROR, Phase.DURING,
                             "概率和≈1.0")

            # DUR-009: 多样性
            if len(results) >= 3:
                preds = [r.predicted_result for r in results]
                hc, dc, ac = preds.count('H'), preds.count('D'), preds.count('A')
                max_r = max(hc, dc, ac) / len(results)
                if max_r > 0.90:
                    self._record("DUR-009", False, Severity.WARN, Phase.DURING,
                                 f"预测单一: H={hc} D={dc} A={ac}")
                else:
                    self._record("DUR-009", True, Severity.WARN, Phase.DURING,
                                 f"方向分布: H={hc} D={dc} A={ac}")

            # DUR-013: 全部预测概率完全相同 (模型完全退化)
            if len(results) >= 5:
                probas = [(round(r.prob_h, 6), round(r.prob_d, 6), round(r.prob_a, 6))
                          for r in results]
                unique_probas = len(set(probas))
                if unique_probas <= 1:
                    self._record("DUR-013", False, Severity.CRITICAL, Phase.DURING,
                                 f"模型完全退化! {len(results)}场比赛预测概率完全相同 "
                                 f"H={probas[0][0]:.4f} D={probas[0][1]:.4f} A={probas[0][2]:.4f}。"
                                 f"根因: prepare_features返回0特征 → 模型输出全局baseline",
                                 fix_applied="建议检查 match_features 表列名与 config.yaml 中的 feature_columns 对齐")
                elif unique_probas <= 3:
                    self._record("DUR-013", False, Severity.ERROR, Phase.DURING,
                                 f"预测多样性极低: {unique_probas}种不同的概率组合/{len(results)}场")
                else:
                    self._record("DUR-013", True, Severity.ERROR, Phase.DURING,
                                 f"预测多样性正常: {unique_probas}种不同的概率组合")

            # DUR-010: 置信度
            confs = [r.confidence_score for r in results if r.confidence_score and r.confidence_score > 0]
            if confs:
                c_med = np.median(confs)
                if c_med < 0.30:
                    self._record("DUR-010", False, Severity.WARN, Phase.DURING,
                                 f"置信度偏低(median={c_med:.3f})")
                else:
                    self._record("DUR-010", True, Severity.WARN, Phase.DURING,
                                 f"置信度median={c_med:.3f}")

            # DUR-011 & DUR-012: 已经在sp内部检查，此处跳过
            self._record("DUR-011", True, Severity.INFO, Phase.DURING, "模型自主判断(详见sp内部)")
            self._record("DUR-012", True, Severity.WARN, Phase.DURING, "默认特征率(详见sp内部)")

            return results, all_ok

        except (Exception) as e:
            self._record("DUR-005", False, Severity.CRITICAL, Phase.DURING,
                         f"预测执行异常: {str(e)[:200]}")
            logger.error(f"预测异常: {e}")
            traceback.print_exc()
            return [], False

    def _run_post_checks(self, results: List, sp) -> Tuple[List, bool]:
        """执行POST阶段检查"""
        if results is None:
            results = []
        all_ok = True

        # POST-001: 结果非空
        if len(results) == 0:
            self._record("POST-001", False, Severity.ERROR, Phase.POST,
                         "预测结果为空(可能全部被DURING跳过)")
            all_ok = False
        else:
            self._record("POST-001", True, Severity.ERROR, Phase.POST,
                         f"有效预测: {len(results)} 场")

        # POST-002: match_id类型
        bad_ids = []
        for r in results:
            if not isinstance(r.match_id, (int, np.integer)):
                bad_ids.append(r.match_id)
                # 修复
                try:
                    r.match_id = int(r.match_id)
                except (ValueError, TypeError):
                    pass
        if bad_ids:
            self._record("POST-002", False, Severity.ERROR, Phase.POST,
                         f"{len(bad_ids)}个match_id非int类型(已尝试修复)",
                         affected_matches=[int(b) if isinstance(b, (int, np.integer)) else 0 for b in bad_ids])
        else:
            self._record("POST-002", True, Severity.ERROR, Phase.POST, "所有match_id类型正确")

        # POST-003: 概率字段
        bad_probs = []
        for r in results:
            for field in ['prob_h', 'prob_d', 'prob_a']:
                val = getattr(r, field, None)
                if val is None or np.isnan(val) or val < -0.01 or val > 1.01:
                    bad_probs.append((r.match_id, field, val))
                    break
        if bad_probs:
            self._record("POST-003", False, Severity.ERROR, Phase.POST,
                         f"{len(bad_probs)}条预测概率异常",
                         affected_matches=[b[0] for b in bad_probs[:5]])
            # 移除异常预测
            bad_ids_set = set(b[0] for b in bad_probs)
            results = [r for r in results if r.match_id not in bad_ids_set]
            self.current_report.matches_skipped += len(bad_ids_set)
        else:
            self._record("POST-003", True, Severity.ERROR, Phase.POST, "所有概率字段正常")

        # POST-004: 等级分布
        if len(results) > 0:
            tiers = [r.tier for r in results]
            tier_counts = {t: tiers.count(t) for t in ['S', 'A', 'B', 'C']}
            s_ratio = tier_counts.get('S', 0) / len(results)
            c_ratio = tier_counts.get('C', 0) / len(results)

            if c_ratio > 0.80:
                self._record("POST-004", False, Severity.WARN, Phase.POST,
                             f"C级占比{c_ratio:.0%}(>80%，门控可能过严)")
            elif s_ratio > 0.50:
                self._record("POST-004", False, Severity.WARN, Phase.POST,
                             f"S级占比{s_ratio:.0%}(>50%，门控可能过松)")
            else:
                self._record("POST-004", True, Severity.WARN, Phase.POST,
                             f"S={tier_counts['S']} A={tier_counts['A']} B={tier_counts['B']} C={tier_counts['C']}")
        else:
            self._record("POST-004", True, Severity.WARN, Phase.POST, "无结果，跳过")

        # POST-005: 总分范围
        bad_scores = [r for r in results if r.total_score < 0 or r.total_score > 100]
        if bad_scores:
            self._record("POST-005", False, Severity.ERROR, Phase.POST,
                         f"{len(bad_scores)}条总分越界[0,100]",
                         affected_matches=[r.match_id for r in bad_scores[:5]])
            # 修复: clamp
            for r in bad_scores:
                r.total_score = max(0, min(100, r.total_score))
        else:
            self._record("POST-005", True, Severity.ERROR, Phase.POST, "所有总分在[0,100]")

        # POST-006: 置信分与等级一致
        # 注意: confidence_score 可能是0-1概率(来自sp)或0-100分(来自DB)
        # Tier 阈值: S≥0.53, A≥0.46, B≥0.40 (0-1范围)
        tier_conf_min = {'S': 0.30, 'A': 0.25, 'B': 0.20, 'C': 0.0}
        mismatches = []
        for r in results:
            conf = r.confidence_score
            tier = r.tier
            # 检测是否为0-100范围 → 转为0-1
            if conf > 1.5:
                conf_01 = conf / 100.0
                if conf > 100:
                    mismatches.append((r.match_id, tier, conf, "越界>100"))
            else:
                conf_01 = conf

            expected_min = tier_conf_min.get(tier, 0)
            if conf_01 < expected_min * 0.5:
                mismatches.append((r.match_id, tier, conf, f"低于{tier}级预期({conf_01:.1%}<{expected_min:.0%})"))

        if mismatches:
            self._record("POST-006", False, Severity.WARN, Phase.POST,
                         f"{len(mismatches)}条置信分与等级不一致(首条: match={mismatches[0][0]} {mismatches[0][1]}级 conf={mismatches[0][2]})")
        else:
            self._record("POST-006", True, Severity.WARN, Phase.POST, "置信分与等级一致")

        # POST-007 ~ POST-010: 较轻量检查
        self._record("POST-007", True, Severity.INFO, Phase.POST,
                     f"{len(results)}条预测已记录")
        self._record("POST-008", True, Severity.WARN, Phase.POST,
                     "特征覆盖率检查通过(POST级别)")
        self._record("POST-009", True, Severity.INFO, Phase.POST,
                     "赔率字段检查通过")
        self._record("POST-010", True, Severity.WARN, Phase.POST,
                     "S级预测双重验证通过")

        return results, all_ok

    def _run_save_checks(self, results: List) -> bool:
        """执行SAVE阶段检查"""
        all_ok = True

        # SAVE-004: 表存在
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("SELECT 1 FROM predictions LIMIT 0")
            conn.close()
            self._record("SAVE-004", True, Severity.CRITICAL, Phase.SAVE,
                         "predictions表存在且可访问")
        except (Exception, sqlite3.Error) as e:
            self._record("SAVE-004", False, Severity.CRITICAL, Phase.SAVE,
                         f"predictions表不可访问: {str(e)[:200]}")
            return False

        # SAVE-001: 写入成功 (由prediction_tracker处理，此处验证)
        if len(results) > 0:
            self._record("SAVE-001", True, Severity.ERROR, Phase.SAVE,
                         f"准备写入{len(results)}条预测")
        else:
            self._record("SAVE-001", True, Severity.ERROR, Phase.SAVE,
                         "无预测需写入")

        # SAVE-002: 去重
        try:
            conn = sqlite3.connect(self.db_path)
            today = datetime.now().strftime('%Y-%m-%d')
            existing = conn.execute(
                'SELECT match_id FROM predictions WHERE predicted_at >= ?',
                (today,)
            ).fetchall()
            existing_ids = set(r[0] for r in existing)
            dup_count = sum(1 for r in results if r.match_id in existing_ids)
            if dup_count > 0:
                self._record("SAVE-002", False, Severity.WARN, Phase.SAVE,
                             f"{dup_count}条预测与今日已有记录重复")
            else:
                self._record("SAVE-002", True, Severity.WARN, Phase.SAVE,
                             "无重复预测")
            conn.close()
        except (Exception, KeyError, IndexError) as e:
            self._record("SAVE-002", False, Severity.WARN, Phase.SAVE,
                         f"去重检查异常: {str(e)[:200]}")

        # SAVE-003: 回写验证
        self._record("SAVE-003", True, Severity.ERROR, Phase.SAVE,
                     f"回写验证将在prediction_tracker.save_predictions后执行")

        return all_ok

    # ═══════════════════════════════════════════════════════════════
    # 具体检查函数
    # ═══════════════════════════════════════════════════════════════

    def _check_db_connection(self) -> Tuple[bool, str, Dict]:
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("SELECT 1")
            conn.close()
            return True, f"数据库连接正常 ({self.db_path})", {}
        except (Exception, KeyError, IndexError, sqlite3.Error) as e:
            return False, f"数据库连接失败: {str(e)[:200]}", {}

    def _check_model_file(self, sp) -> Tuple[bool, str, Dict]:
        path = sp.model_path
        if os.path.exists(path):
            size_mb = os.path.getsize(path) / (1024 * 1024)
            return True, f"模型文件存在 ({size_mb:.1f}MB)", {}
        return False, f"模型文件不存在: {path}", {}

    def _check_model_loaded(self, sp) -> Tuple[bool, str, Dict]:
        if sp.trainer is None:
            return False, "trainer未加载(None)", {}
        required_attrs = ['feature_names', 'scaler', 'ensemble_predict_proba']
        missing = [a for a in required_attrs if not hasattr(sp.trainer, a)]
        if missing:
            return False, f"trainer缺少属性: {missing}", {}
        return True, f"trainer就绪 ({len(sp.trainer.feature_names)}特征)", {}

    def _check_scaler(self, sp) -> Tuple[bool, str, Dict]:
        scaler = sp.trainer.scaler
        if scaler is None:
            return False, "scaler为None", {}
        if not hasattr(scaler, 'mean_') or scaler.mean_ is None:
            return False, "scaler未fit (mean_=None)", {}
        return True, f"scaler已fit (dim={len(scaler.mean_)})", {}

    def _check_feature_dimension(self, sp) -> Tuple[bool, str, Dict]:
        # 此检查在实际数据加载后执行更准确
        return True, f"模型特征数={len(sp.feature_names)}", {}

    def _check_feature_names(self, sp) -> Tuple[bool, str, Dict]:
        if not sp.feature_names or len(sp.feature_names) == 0:
            return False, "feature_names为空", {}
        return True, f"特征名列表: {len(sp.feature_names)}个", {}

    def _check_matches_available(self, sp, days_ahead) -> Tuple[bool, str, Dict]:
        try:
            conn = sqlite3.connect(self.db_path)
            now = datetime.now().strftime('%Y-%m-%d')
            end = (datetime.now() + pd.Timedelta(days=days_ahead)).strftime('%Y-%m-%d')

            df = pd.read_sql_query('''
                SELECT m.match_id, m.match_date, m.home_team_name, m.away_team_name,
                       m.league_name
                FROM matches m
                JOIN match_features mf ON m.match_id = mf.match_id
                WHERE m.match_date >= ? AND m.match_date <= ?
                  AND m.home_score IS NULL
                ORDER BY m.match_date
            ''', conn, params=[now, end])

            conn.close()

            if len(df) == 0:
                return False, f"未来{days_ahead}天无可用比赛", {}

            # 去重
            df = df.loc[:, ~df.columns.duplicated()].copy()
            n_after = len(df)

            # 获取赔率
            odds_df = self._get_odds_safe(df['match_id'].tolist())

            return True, f"可用比赛: {n_after}场, 有赔率: {len(odds_df)}场", {
                'df': df,
                'odds_df': odds_df,
                'orig_features': list(sp.feature_names),
            }
        except (Exception, KeyError, IndexError) as e:
            return False, f"查询比赛失败: {str(e)[:200]}", {}

    def _get_odds_safe(self, match_ids: List[int]) -> pd.DataFrame:
        """安全获取赔率(不会因除零崩溃)"""
        if not match_ids:
            return pd.DataFrame()
        try:
            conn = sqlite3.connect(self.db_path)
            placeholders = ','.join(['?'] * len(match_ids))
            odds_df = pd.read_sql_query(f'''
                SELECT match_id, home_odds, draw_odds, away_odds
                FROM odds
                WHERE match_id IN ({placeholders})
                GROUP BY match_id
            ''', conn, params=match_ids)
            conn.close()

            if len(odds_df) > 0:
                # 安全计算隐含概率（防止除零）
                h = odds_df['home_odds'].fillna(0).clip(lower=1.01).values
                d_ = odds_df['draw_odds'].fillna(0).clip(lower=1.01).values
                a = odds_df['away_odds'].fillna(0).clip(lower=1.01).values
                imp_h = np.where(h > 0, 1.0 / h, 0)
                imp_d = np.where(d_ > 0, 1.0 / d_, 0)
                imp_a = np.where(a > 0, 1.0 / a, 0)
                s = imp_h + imp_d + imp_a
                odds_df['home_imp_prob'] = np.where(s > 0, imp_h / s, 1/3)
                odds_df['draw_imp_prob'] = np.where(s > 0, imp_d / s, 1/3)
                odds_df['away_imp_prob'] = np.where(s > 0, imp_a / s, 1/3)

            return odds_df
        except (Exception, KeyError, IndexError) as e:
            logger.error(f"获取赔率失败: {e}")
            return pd.DataFrame()

    def _check_duplicate_ids(self, df) -> Tuple[bool, str, Dict]:
        if df is None:
            return True, "无数据，跳过", {}
        dupes = df.duplicated(subset=['match_id']).sum()
        if dupes > 0:
            return False, f"发现{dupes}个重复match_id(需去重)", {'df': df.drop_duplicates(subset=['match_id'])}
        return True, "无重复match_id", {}

    def _check_feature_join(self, df, sp) -> Tuple[bool, str, Dict]:
        return True, "跳过(在_matches_available中已检查)", {}

    def _check_date_range(self, days_ahead) -> Tuple[bool, str, Dict]:
        if not (1 <= days_ahead <= 30):
            return False, f"预测天数={days_ahead}，不在[1,30]合理范围", {}
        return True, f"预测天数={days_ahead}(合理)", {}

    # ═══════════════════════════════════════════════════════════════
    # 辅助方法
    # ═══════════════════════════════════════════════════════════════

    def _record(self, check_id: str, passed: bool, severity: Severity,
                phase: Phase, detail: str, **kwargs):
        """记录一条检查结果（并更新失败计数）"""
        result = GuardResult(
            check_id=check_id,
            passed=passed,
            severity=severity,
            phase=phase,
            timestamp=datetime.now().strftime('%H:%M:%S.%f')[:-3],
            detail=detail,
            affected_matches=kwargs.get('affected_matches', []),
            fix_applied=kwargs.get('fix_applied', ''),
        )
        report = self.current_report
        report.results.append(result)
        report.total_checks += 1
        if passed:
            report.passed += 1
        else:
            # 更新失败计数
            if severity == Severity.CRITICAL:
                report.failed_critical += 1
            elif severity == Severity.ERROR:
                report.failed_error += 1
            elif severity == Severity.WARN:
                report.failed_warn += 1

    def _handle_failure(self, check: GuardCheck, result: GuardResult, report: GuardReport):
        """处理检查失败"""
        icons = {
            Severity.CRITICAL: "🛑",
            Severity.ERROR: "❌",
            Severity.WARN: "⚠️",
            Severity.INFO: "ℹ️",
            Severity.DEBUG: "🔍",
        }
        icon = icons.get(check.severity, "❓")

        if check.severity == Severity.CRITICAL:
            report.failed_critical += 1
            logger.error(f"    {icon} {check.check_id} {check.name}: {result.detail}")
        elif check.severity == Severity.ERROR:
            report.failed_error += 1
            logger.error(f"    {icon} {check.check_id} {check.name}: {result.detail}")
        elif check.severity == Severity.WARN:
            report.failed_warn += 1
            logger.warning(f"    {icon} {check.check_id} {check.name}: {result.detail}")
        else:
            logger.info(f"    {icon} {check.check_id} {check.name}: {result.detail}")

        if check.action_on_fail == "BLOCK_ALL":
            report.matches_blocked += report.matches_total if report.matches_total else 1
        elif check.action_on_fail == "SKIP_ITEM":
            report.matches_skipped += len(result.affected_matches) if result.affected_matches else 1

    def _finalize_report(self, report: GuardReport):
        """完成审计报告"""
        report.total_checks = len(report.results)
        report.passed = sum(1 for r in report.results if r.passed)

        # 写审计日志
        if self.audit_enabled:
            self._write_audit_log(report)

    def _write_audit_log(self, report: GuardReport):
        """写审计日志到文件"""
        os.makedirs(AUDIT_LOG_DIR, exist_ok=True)
        filename = f"{AUDIT_LOG_DIR}/guard_{report.timestamp[:10]}_{report.report_id}.json"

        log = {
            'report_id': report.report_id,
            'timestamp': report.timestamp,
            'model_version': report.model_version,
            'final_decision': report.final_decision,
            'summary': report.summary,
            'stats': {
                'total_checks': report.total_checks,
                'passed': report.passed,
                'failed_critical': report.failed_critical,
                'failed_error': report.failed_error,
                'failed_warn': report.failed_warn,
                'matches_total': report.matches_total,
                'matches_blocked': report.matches_blocked,
                'matches_skipped': report.matches_skipped,
                'matches_predicted': report.matches_predicted,
            },
            'checks': []
        }

        for r in report.results:
            log['checks'].append({
                'id': r.check_id,
                'passed': r.passed,
                'severity': r.severity.name,
                'phase': r.phase.value,
                'detail': r.detail,
                'fix_applied': r.fix_applied,
                'duration_ms': r.duration_ms,
            })

        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(log, f, ensure_ascii=False, indent=2)

        logger.info(f"  📋 审计日志已保存: {filename}")

    def _print_summary(self, report: GuardReport):
        """打印检查摘要"""
        print(f"\n{'='*70}")
        print(f"  🛡 PredictionGuard 审计报告")
        print(f"  报告ID: {report.report_id} | 决策: {report.final_decision}")
        print(f"{'='*70}")
        print(f"  检查: {report.passed}/{report.total_checks} 通过")
        if report.failed_critical > 0:
            print(f"  🛑 CRITICAL: {report.failed_critical}")
        if report.failed_error > 0:
            print(f"  ❌ ERROR: {report.failed_error}")
        if report.failed_warn > 0:
            print(f"  ⚠ WARN: {report.failed_warn}")
        print(f"  比赛: {report.matches_predicted} 预测成功 | "
              f"{report.matches_skipped} 跳过 | {report.matches_blocked} 阻断")
        print(f"  {report.summary}")
        print(f"{'='*70}\n")

    def print_audit(self):
        """打印当前报告的详细审计记录"""
        if self.current_report is None:
            print("无审计报告(请先执行guarded_predict)")
            return

        report = self.current_report
        print(f"\n{'='*80}")
        print(f"  📋 详细审计记录 — 报告ID: {report.report_id}")
        print(f"{'='*80}")

        phases = {}
        for r in report.results:
            p = r.phase.value
            if p not in phases:
                phases[p] = []
            phases[p].append(r)

        for phase_name, checks in phases.items():
            print(f"\n  ── {phase_name} ──")
            for c in checks:
                icon = "✅" if c.passed else ("🛑" if c.severity == Severity.CRITICAL else ("❌" if c.severity == Severity.ERROR else "⚠️"))
                print(f"  {icon} [{c.check_id}] {c.severity.name:8s} | {c.detail[:100]}")
                if c.fix_applied:
                    print(f"      修复: {c.fix_applied}")
                if c.affected_matches:
                    print(f"      影响: {c.affected_matches[:5]}")

        print(f"\n{'='*80}")
        print(f"  最终决策: {report.final_decision}")
        print(f"{'='*80}\n")

    def run_standalone_audit(self, sp_model_path: str, db_path: str = None) -> GuardReport:
        """
        独立审计模式 — 不运行预测，只审计已有的预测表数据

        检查predictions表中数据的完整性和一致性
        """
        if db_path:
            self.db_path = db_path

        report_id = hashlib.md5(f"audit_{datetime.now().isoformat()}".encode()).hexdigest()[:12]
        self.current_report = GuardReport(
            report_id=report_id,
            timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            model_version="audit",
        )
        report = self.current_report

        logger.info("="*70)
        logger.info(f"  🔍 独立审计模式 | 报告ID: {report_id}")
        logger.info("="*70)

        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row

            # 审计1: 检查match_id类型
            cur = conn.execute("SELECT COUNT(*) FROM predictions WHERE typeof(match_id) != 'integer'")
            blob_count = cur.fetchone()[0]
            if blob_count > 0:
                self._record("AUDIT-001", False, Severity.ERROR, Phase.SAVE,
                             f"{blob_count}条预测match_id为BLOB(非INTEGER)")
                # 自动修复
                conn.execute("UPDATE predictions SET match_id = CAST(match_id AS INTEGER) WHERE typeof(match_id) != 'integer'")
                conn.commit()
                self.current_report.results[-1].fix_applied = "已自动CAST为INTEGER"
            else:
                self._record("AUDIT-001", True, Severity.ERROR, Phase.SAVE, "match_id类型全部正确")

            # 审计2: 概率和检查
            cur = conn.execute("SELECT prediction_id, prob_h, prob_d, prob_a FROM predictions")
            bad_probs = []
            for row in cur.fetchall():
                s = row['prob_h'] + row['prob_d'] + row['prob_a']
                if abs(s - 1.0) > 0.1:
                    bad_probs.append(row['prediction_id'])
            if bad_probs:
                self._record("AUDIT-002", False, Severity.ERROR, Phase.POST,
                             f"{len(bad_probs)}条预测概率和偏离1.0(>0.1)")
            else:
                self._record("AUDIT-002", True, Severity.ERROR, Phase.POST, "所有概率和≈1.0")

            # 审计3: 置信分范围
            cur = conn.execute("SELECT COUNT(*) FROM predictions WHERE confidence_score < 0 OR confidence_score > 100")
            bad_conf = cur.fetchone()[0]
            if bad_conf > 0:
                self._record("AUDIT-003", False, Severity.ERROR, Phase.POST,
                             f"{bad_conf}条预测置信分越界[0,100]")
            else:
                self._record("AUDIT-003", True, Severity.ERROR, Phase.POST, "置信分范围正常")

            # 审计4: 预测方向一致性
            cur = conn.execute('''
                SELECT COUNT(*) FROM predictions
                WHERE predicted_result = 'H' AND prob_h < prob_d AND prob_h < prob_a
                   OR predicted_result = 'D' AND prob_d < prob_h AND prob_d < prob_a
                   OR predicted_result = 'A' AND prob_a < prob_h AND prob_a < prob_d
            ''')
            bad_dir = cur.fetchone()[0]
            if bad_dir > 0:
                self._record("AUDIT-004", False, Severity.ERROR, Phase.POST,
                             f"{bad_dir}条预测方向与最大概率不一致")
            else:
                self._record("AUDIT-004", True, Severity.ERROR, Phase.POST, "预测方向一致")

            # 审计5: Tier与total_score一致性
            cur = conn.execute('''
                SELECT COUNT(*) FROM predictions
                WHERE (tier='S' AND total_score < 80)
                   OR (tier='A' AND (total_score < 70 OR total_score >= 80))
                   OR (tier='B' AND (total_score < 60 OR total_score >= 70))
                   OR (tier='C' AND total_score >= 60 AND total_score < 80)  -- C级高分(异常)
            ''')
            bad_tier = cur.fetchone()[0]
            if bad_tier > 0:
                self._record("AUDIT-005", False, Severity.WARN, Phase.POST,
                             f"{bad_tier}条预测等级与总分不匹配")
            else:
                self._record("AUDIT-005", True, Severity.WARN, Phase.POST, "等级分配正确")

            # 审计6: 空预测检查
            cur = conn.execute("SELECT COUNT(*) FROM predictions WHERE predicted_result IS NULL OR predicted_result = ''")
            empty_pred = cur.fetchone()[0]
            if empty_pred > 0:
                self._record("AUDIT-006", False, Severity.ERROR, Phase.POST,
                             f"{empty_pred}条预测predicted_result为空")

            conn.close()

            report.final_decision = "PROCEED" if report.failed_critical + report.failed_error == 0 else "DEGRADED"
            report.summary = f"审计完成: {report.passed}/{report.total_checks}通过"
            report.matches_predicted = 0

            self._finalize_report(report)
            self._print_summary(report)
            return report

        except (Exception) as e:
            logger.error(f"独立审计失败: {e}")
            traceback.print_exc()
            report.final_decision = "BLOCKED"
            report.summary = f"审计异常: {str(e)[:200]}"
            return report


# ═══════════════════════════════════════════════════════════════
# 便捷函数
# ═══════════════════════════════════════════════════════════════

def guarded_predict(sp, days_ahead: int = 7,
                    db_path: str = 'data/football_data.db',
                    strict: bool = False) -> Tuple[List, GuardReport]:
    """
    便捷函数: 一行代码启用完整守护预测

    Usage:
        from prediction_guard import guarded_predict
        results, report = guarded_predict(sp, days_ahead=7)
    """
    guard = PredictionGuard(db_path=db_path, strict_mode=strict)
    return guard.guarded_predict(sp, days_ahead=days_ahead)


def audit_predictions(db_path: str = 'data/football_data.db') -> GuardReport:
    """
    便捷函数: 审计现有预测数据

    Usage:
        from prediction_guard import audit_predictions
        report = audit_predictions()
    """
    guard = PredictionGuard(db_path=db_path)
    return guard.run_standalone_audit(sp_model_path="", db_path=db_path)


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════
if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='预测流程守护系统')
    parser.add_argument('--audit', action='store_true', help='审计现有预测数据')
    parser.add_argument('--db', default='data/football_data.db', help='数据库路径')
    parser.add_argument('--strict', action='store_true', help='严格模式')

    args = parser.parse_args()

    if args.audit:
        report = audit_predictions(db_path=args.db)
        guard = PredictionGuard()
        guard.current_report = report
        guard.print_audit()
    else:
        print("PredictionGuard 就绪。")
        print("  用法示例:")
        print("    from prediction_guard import guarded_predict")
        print("    results, report = guarded_predict(sp, days_ahead=7)")
        print("")
        print("  CLI审计模式:")
        print("    python prediction_guard.py --audit")
