"""
feature_consistency — feature_cols 与 match_features 表启动一致性断言 (E1 P1-10).

背景:
    draw_expert_v3_focal 模型的 77 维 feature_cols 是特征工程的"契约"。
    若 match_features 表的列与 feature_cols 不一致(缺失列), 特征提取会静默退化
    为纯规则, 模型永不触发 → 与"无破坏性改动"原则冲突且难以察觉。

策略:
    - verify_feature_cols(strict=False): 默认仅记 CRITICAL 日志, 不抛错
      (保留 graceful degradation, 服务照常启动)。
    - strict=True (环境变量 FOOTBALL_STRICT_FEATURES=1): 缺失即抛 ValueError,
      启动失败 — 满足"缺失即抛错"的硬约束, 用于 CI / 生产强校验。
"""
import os
import glob
import sqlite3
import logging
import joblib

logger = logging.getLogger("feature_consistency")

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DB_PATH = os.path.join(_PROJECT_ROOT, "data", "football_data.db")
_SAVED_MODELS = os.path.join(_PROJECT_ROOT, "saved_models")


def load_feature_cols():
    """从 draw_expert v3 系列模型包取 77 维 feature_cols (单一事实源)."""
    candidates = [
        os.path.join(_SAVED_MODELS, "draw_expert_v3_focal.joblib"),
    ]
    candidates += sorted(glob.glob(os.path.join(_SAVED_MODELS, "draw_expert*.joblib")))
    for c in candidates:
        if not os.path.exists(c):
            continue
        try:
            pkg = joblib.load(c)
        except Exception as e:
            logger.warning("[feature_consistency] 加载 %s 失败: %s", c, e)
            continue
        fc = pkg.get("feature_cols") if isinstance(pkg, dict) else getattr(pkg, "feature_cols", None)
        if fc:
            return list(fc)
    logger.warning("[feature_consistency] 未找到任何含 feature_cols 的 draw_expert 模型包")
    return None


def match_features_columns(db_path: str = _DB_PATH):
    if not os.path.exists(db_path):
        logger.warning("[feature_consistency] DB 不存在: %s", db_path)
        return []
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute("PRAGMA table_info(match_features)").fetchall()
    finally:
        con.close()
    return [r[1] for r in rows]


def verify_feature_cols(strict: bool = False, db_path: str = _DB_PATH):
    """校验 feature_cols ⊆ match_features 列。

    Returns: (ok: bool, missing: list[str])
    strict=True 且缺失时抛 ValueError。
    """
    fc = load_feature_cols()
    if not fc:
        # 无法校验(模型未就绪) → 不阻塞, 交 wc_engine 的软降级处理
        return True, []
    cols = match_features_columns(db_path)
    if not cols:
        logger.warning("[feature_consistency] match_features 无列信息, 跳过校验")
        return True, []
    missing = [c for c in fc if c not in cols]
    if missing:
        msg = (f"feature_cols 缺失于 match_features 表: {len(missing)} 列 "
               f"(示例: {missing[:10]}) — 特征提取将静默退化为纯规则")
        if strict:
            raise ValueError(msg)
        logger.critical("[feature_consistency] %s", msg)
        return False, missing
    logger.info("[feature_consistency] feature_cols 一致性 OK (77 维全部命中 match_features)")
    return True, []
