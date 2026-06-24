"""
ModelBridge — 模型桥接层
版本统一从 saved_models/model_registry.json 读取活跃版本
委托给 EnsembleTrainer（支持 NN 子模型 + 正确的 Stacking 维度）
"""
import os
import sys
import logging
from typing import Optional, Dict
import yaml
import numpy as np
import joblib

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.environ.get(
    'PROJECT_ROOT',
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
sys.path.insert(0, _PROJECT_ROOT)

# 从 config.yaml 读取模型路径（修复NEW-4: 路径指向config/config.yaml）
def _load_config():
    # 优先 config/config.yaml
    cfg_path = os.path.join(_PROJECT_ROOT, 'config', 'config.yaml')
    if not os.path.isfile(cfg_path):
        # fallback: 项目根的 config.yaml
        cfg_path = os.path.join(_PROJECT_ROOT, 'config.yaml')
    if os.path.isfile(cfg_path):
        try:
            with open(cfg_path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
        except Exception:
            pass
    return {}

def _resolve_model_path():
    """从 config 解析模型搜索路径列表（去重+规范化）"""
    cfg = _load_config()
    # 修复NEW-5: 默认模型名从v3.2更新为v4.1
    mp = cfg.get('model', {}).get('model_path', 'saved_models/football_v4.1_production.joblib')
    # 支持逗号分隔多个候选
    candidates = [p.strip() for p in mp.split(',') if p.strip()]
    paths = []
    seen = set()
    for c in candidates:
        full = c if os.path.isabs(c) else os.path.join(_PROJECT_ROOT, c)
        full = os.path.normpath(full)
        if full not in seen:
            paths.append(full)
            seen.add(full)
    # 兜底：直接搜 saved_models/ 下最新 .joblib
    saved_dir = os.path.join(_PROJECT_ROOT, 'saved_models')
    if os.path.isdir(saved_dir):
        for f in sorted(os.listdir(saved_dir), reverse=True):
            if f.endswith('.joblib') and 'production' in f:
                full = os.path.normpath(os.path.join(saved_dir, f))
                if full not in seen:
                    paths.append(full)
                    seen.add(full)
                break
    return paths

_MODEL_DIR = os.path.join(_PROJECT_ROOT, 'saved_models')
_SEARCH_ORDER = _resolve_model_path()


def _find_best_model() -> str:
    """按优先级查找可用模型"""
    for candidate in _SEARCH_ORDER:
        if os.path.isfile(candidate):
            return candidate
    # fallback: 找任意 .joblib
    if os.path.isdir(_MODEL_DIR):
        for f in sorted(os.listdir(_MODEL_DIR), reverse=True):
            if f.endswith('.joblib') and 'ensemble' in f.lower():
                return os.path.join(_MODEL_DIR, f)
    raise FileNotFoundError(f"找不到可用模型在 {_MODEL_DIR}")


class ModelBridge:
    """模型桥接 — 委托 EnsembleTrainer，支持 NN 子模型
    版本统一从 saved_models/model_registry.json 读取
    """

    def __init__(self, model_path: str = None):
        self.model_path = model_path or _find_best_model()
        self.model_name = os.path.basename(self.model_path)
        self._trainer = None   # EnsembleTrainer 实例
        self._loaded = False

    @property
    def available(self) -> bool:
        return self._loaded

    @property
    def trainer(self):
        """返回 EnsembleTrainer 实例（含所有子模型）；供 VIP/高级消费者复用

        注意: 返回 None 表示回退到轻量模式（无 NN/Stacking 支持）
        """
        self._ensure_loaded()
        return self._trainer

    def _ensure_loaded(self):
        if self._loaded:
            return
        logger.info(f"加载模型: {self.model_name}")
        try:
            from ensemble_trainer import EnsembleTrainer
            self._trainer = EnsembleTrainer.load_pipeline(self.model_path)
            self._loaded = True
            logger.info(f"模型就绪: {self.model_name} | {len(self._trainer.feature_names)}特征 "
                        f"| NN={'✓' if self._trainer.nn_model else '✗'}")
            # 交叉校验 config feature_columns vs model feature_names
            cfg = _load_config()
            cfg_features = set(cfg.get('data', {}).get('feature_columns', [])
                               or cfg.get('model', {}).get('feature_columns', []))
            model_features = set(self._trainer.feature_names)
            if cfg_features:
                model_only = model_features - cfg_features
                config_only = cfg_features - model_features
                common = model_features & cfg_features
                if model_only or config_only:
                    logger.warning(
                        f"特征集不匹配: 模型={len(model_features)} / config={len(cfg_features)} / "
                        f"交集={len(common)} | 仅模型: {sorted(model_only)[:5]}{'...' if len(model_only)>5 else ''} | "
                        f"仅config: {sorted(config_only)[:5]}{'...' if len(config_only)>5 else ''}"
                    )
        except Exception as e:
            logger.error(f"EnsembleTrainer 加载失败，使用轻量 fallback: {e}")
            self._legacy_load()

    def _legacy_load(self):
        """轻量 fallback（仅用于 EnsembleTrainer 不可用时）"""
        self._model_data = joblib.load(self.model_path)
        self._xgb = self._model_data.get('xgb_model')
        self._lgb = self._model_data.get('lgb_model')
        self._oe = self._model_data.get('odds_expert_model')
        self._meta = self._model_data.get('meta_learner')
        self._feature_names = self._model_data.get('feature_names', [])
        self._odds_feature_names = self._model_data.get('odds_feature_names', [])
        self._config = self._model_data.get('config', {})
        self._n_features = len(self._feature_names)
        self._trainer = None
        self._loaded = True
        logger.warning(f"轻量 fallback 模式（无 NN 支持）| {self._n_features}特征")

    def predict(self, features, odds_data=None, external_heuristic_proba=None) -> dict:
        """执行预测
        Args:
            features: numpy array or dict of features
            odds_data: optional odds features dict
            external_heuristic_proba: 外部HeuristicPredictor输出 (3,) or (1,3)
        Returns:
            {"home": float, "draw": float, "away": float}
        """
        self._ensure_loaded()

        try:
            # ── EnsembleTrainer 路径（主路径，含NN）──
            if self._trainer is not None:
                feat_names = self._trainer.feature_names
                n_feats = len(feat_names)

                if isinstance(features, dict):
                    vec = np.zeros(n_feats, dtype=np.float32)
                    missing_count = 0
                    matched_count = 0
                    for i, name in enumerate(feat_names):
                        if name in features:
                            vec[i] = float(features[name])
                            matched_count += 1
                        else:
                            missing_count += 1
                    if missing_count > 0:
                        logger.warning(
                            f"特征缺失: {missing_count}/{n_feats} 个特征不在输入中 "
                            f"(已匹配 {matched_count})，缺失位补 0.0"
                        )
                    X = vec.reshape(1, -1)
                elif isinstance(features, np.ndarray):
                    X = features.reshape(1, -1) if features.ndim == 1 else features
                    if X.shape[1] > n_feats:
                        logger.warning(f"输入特征维度偏高截断: {X.shape[1]} → {n_feats}")
                        X = X[:, :n_feats]
                    elif X.shape[1] < n_feats:
                        logger.warning(f"输入特征维度偏低补零: {X.shape[1]} → {n_feats}")
                        X = np.hstack([X, np.zeros((X.shape[0], n_feats - X.shape[1]))])
                else:
                    return {"home": 0.33, "draw": 0.34, "away": 0.33}

                # 标准化
                if self._trainer.scaler is not None:
                    X_scaled = self._trainer.scaler.transform(X)
                else:
                    X_scaled = X

                proba = self._trainer.ensemble_predict_proba(
                    X_scaled,
                    external_heuristic_proba=external_heuristic_proba,
                )[0]
                proba = proba / proba.sum()
                return {
                    "home": float(proba[0]),
                    "draw": float(proba[1]),
                    "away": float(proba[2]),
                }

            # ── 轻量 fallback 路径 ──
            return self._legacy_predict(features, odds_data)

        except Exception as e:
            logger.error(f"模型预测失败: {e}", exc_info=True)
            return {"home": 0.33, "draw": 0.34, "away": 0.33}

    def _legacy_predict(self, features, odds_data=None) -> dict:
        """旧版轻量预测（EnsembleTrainer 不可用时的 fallback）"""
        feat_names = self._feature_names
        n_feats = self._n_features

        if isinstance(features, dict):
            vec = np.zeros(n_feats)
            for i, name in enumerate(feat_names):
                if name in features:
                    vec[i] = float(features[name])
            X = vec.reshape(1, -1)
        elif isinstance(features, np.ndarray):
            X = features.reshape(1, -1) if features.ndim == 1 else features
            if X.shape[1] > n_feats:
                X = X[:, :n_feats]
            elif X.shape[1] < n_feats:
                X = np.hstack([X, np.zeros((X.shape[0], n_feats - X.shape[1]))])
        else:
            return {"home": 0.33, "draw": 0.34, "away": 0.33}

        xgb_p = self._xgb.predict_proba(X)[0] if self._xgb else np.array([0.33, 0.34, 0.33])
        lgb_p = self._lgb.predict_proba(X)[0] if self._lgb else np.array([0.33, 0.34, 0.33])
        oe_p = np.array([0.33, 0.34, 0.33])
        if odds_data and self._oe:
            try:
                oe_vec = np.array([float(odds_data.get(k, 0)) for k in self._odds_feature_names]).reshape(1, -1)
                oe_p = self._oe.predict_proba(oe_vec)[0]
            except Exception:
                pass

        # P0: 缓存OE输出(供D-gate融合读取)
        self._last_oe_proba = {"home": float(oe_p[0]), "draw": float(oe_p[1]), "away": float(oe_p[2])}

        ens = self._config.get('models', {}).get('ensemble', {}) if self._config else {}
        w_lgb = ens.get('lightgbm_weight', 0.30)
        w_xgb = ens.get('xgboost_weight', 0.30)
        w_h = ens.get('heuristic_weight', 0.14)
        w_oe = ens.get('odds_expert_weight', 0.10)
        w_sum = w_lgb + w_xgb + w_h + w_oe
        if w_sum > 0:
            w_lgb /= w_sum; w_xgb /= w_sum; w_h /= w_sum; w_oe /= w_sum

        raw = w_lgb * lgb_p + w_xgb * xgb_p + w_h * np.array([0.33, 0.34, 0.33]) + w_oe * oe_p
        raw = raw / raw.sum() if raw.sum() > 0 else np.array([0.33, 0.34, 0.33])

        # D 先验
        dp = self._config.get('draw_prior', {}) if self._config else {}
        d_boost = dp.get('d_probability_boost', 0)
        if d_boost:
            raw[1] += d_boost
            raw = raw / raw.sum()

        return {"home": float(raw[0]), "draw": float(raw[1]), "away": float(raw[2])}

    def get_oe_output(self) -> Optional[Dict]:
        """P0: 返回上次predict()调用后缓存的OE子模型输出 {home, draw, away}"""
        if self._trainer and hasattr(self._trainer, '_last_submodel_probas'):
            sub = self._trainer._last_submodel_probas
            if sub and 'odds_expert' in sub:
                oe = sub['odds_expert']
                if oe is not None and len(oe) > 0:
                    p = oe[0]  # first sample
                    return {"home": float(p[0]), "draw": float(p[1]), "away": float(p[2])}
        # Legacy fallback: 从上次 _legacy_predict 缓存
        if hasattr(self, '_last_oe_proba') and self._last_oe_proba is not None:
            return self._last_oe_proba
        return None

    def get_de_output(self) -> Optional[float]:
        """v4.0: 返回DrawExpert P(Draw) (0-1), None if not available"""
        if self._trainer and hasattr(self._trainer, '_last_submodel_probas'):
            sub = self._trainer._last_submodel_probas
            if sub and 'draw_expert' in sub:
                de = sub['draw_expert']
                if de is not None and len(de) > 0:
                    de_arr = np.asarray(de)
                    if de_arr.ndim == 2 and de_arr.shape[1] >= 2:
                        return float(de_arr[0, 1])  # P(draw) from binary [P(not draw), P(draw)]
                    return float(de_arr.flat[0])  # single-value array
        return None

    # ── 向后兼容属性 ──
    @property
    def _feature_names(self):
        if self._trainer:
            return self._trainer.feature_names
        return getattr(self, '__feature_names', [])

    @_feature_names.setter
    def _feature_names(self, v):
        self.__feature_names = v

    @property
    def _n_features(self):
        if self._trainer:
            return len(self._trainer.feature_names)
        return getattr(self, '__n_features', 0)

    @_n_features.setter
    def _n_features(self, v):
        self.__n_features = v

    @property
    def _config(self):
        if self._trainer:
            return self._trainer.config
        return getattr(self, '__config', {})

    @_config.setter
    def _config(self, v):
        self.__config = v


_bridge_instance: ModelBridge = None


def get_model_bridge() -> ModelBridge:
    """获取全局 ModelBridge 单例"""
    global _bridge_instance
    if _bridge_instance is None:
        _bridge_instance = ModelBridge()
    _bridge_instance._ensure_loaded()
    return _bridge_instance


def reload_model() -> ModelBridge:
    """强制重新加载模型"""
    global _bridge_instance
    _bridge_instance = None
    return get_model_bridge()
