"""
[DEPRECATED] P0-1: 此文件直接使用 joblib.load 绕过 ModelBridge，存在数据泄露风险。
生产预测请使用 agents.model_bridge.ModelBridge.predict()
哨响AI - 批量预测优化器
=======================
- 懒加载：首次预测时才加载模型，减少冷启动时间
- 批量处理：一次加载模型，预测多场比赛
- 连接池：数据库连接的复用管理
- 预热：后台线程预加载模型

用法:
    predictor = BatchPredictor(model_dir="saved_models/")
    results = predictor.predict_batch(matches_list)
"""

import os
import sys
import time
import json
import logging
import threading
from typing import Any, Dict, List, Optional, Callable
from dataclasses import dataclass, field
from contextlib import contextmanager

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════
# 模型懒加载器
# ══════════════════════════════════════════════════

@dataclass
class ModelHandle:
    """模型句柄：记录模型元数据 + 懒加载"""
    path: str
    version: str = ""
    feature_count: int = 0
    train_timestamp: str = ""
    _model: Any = None       # 懒加载
    _loading: bool = False   # 防止并发加载
    _load_time_ms: float = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def load(self, force_reload: bool = False):
        """加载模型（线程安全）"""
        if self._model is not None and not force_reload:
            return self._model

        with self._lock:
            # 双重检查
            if self._model is not None and not force_reload:
                return self._model

            if self._loading:
                # 等待其他线程加载完成
                while self._loading:
                    time.sleep(0.1)
                return self._model

            self._loading = True
            try:
                import joblib
                start = time.perf_counter()
                self._model = joblib.load(self.path)
                self._load_time_ms = (time.perf_counter() - start) * 1000
                logger.info(
                    f"[Model] 加载完成: {os.path.basename(self.path)} "
                    f"({self._load_time_ms:.0f}ms)"
                )
                return self._model
            except (Exception, KeyError, IndexError) as e:
                logger.error(f"[Model] 加载失败 {self.path}: {e}")
                raise
            finally:
                self._loading = False

    def unload(self):
        """卸载模型释放内存"""
        with self._lock:
            self._model = None
            import gc
            gc.collect()


# ══════════════════════════════════════════════════
# 批量预测引擎
# ══════════════════════════════════════════════════

class BatchPredictor:
    """
    批量预测引擎

    - 模型懒加载（首次 predict_batch 时才加载）
    - 单次加载、多次预测
    - 支持模型热切换（不重启）
    - 内置缓存：相同特征不重复计算

    用法:
        bp = BatchPredictor(model_dir="saved_models/")
        bp.warmup()  # 后台预热

        # 批量预测
        results = bp.predict_batch([
            {"home_team": "Arsenal", "away_team": "Chelsea", ...},
            {"home_team": "Liverpool", "away_team": "Man City", ...},
        ])
    """

    def __init__(self, model_dir: str = None, model_path: str = None,
                 config_path: str = None, db_path: str = None):
        self.model_dir = model_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "saved_models"
        )
        self.model_path = model_path
        self.config_path = config_path
        self.db_path = db_path
        self._handle: Optional[ModelHandle] = None
        self._trainer: Any = None     # EnsembleTrainer 实例
        self._feature_names: List[str] = []
        self._defaults: Dict[str, float] = {}
        self._warmed_up = threading.Event()

        # 统计
        self._prediction_count = 0
        self._total_time_ms = 0.0

    # ══════════════════════════════════════════════════
    # 模型管理
    # ══════════════════════════════════════════════════

    def _find_best_model(self) -> Optional[str]:
        """查找最新模型文件"""
        if self.model_path and os.path.exists(self.model_path):
            return self.model_path

        if not os.path.isdir(self.model_dir):
            return None

        # 按 joblib 优先，再 pkl
        candidates = []
        for ext in [".joblib", ".pkl"]:
            for f in os.listdir(self.model_dir):
                if f.endswith(ext) and "football" in f.lower():
                    full = os.path.join(self.model_dir, f)
                    candidates.append((os.path.getmtime(full), full))

        candidates.sort(reverse=True)
        return candidates[0][1] if candidates else None

    def _init_model(self):
        """初始化模型（懒加载）"""
        if self._trainer is not None:
            return

        model_path = self._find_best_model()
        if model_path is None:
            raise FileNotFoundError(
                f"未找到模型文件: {self.model_dir}\n请先训练模型"
            )

        # 加载 EnsembleTrainer pipeline
        from ensemble_trainer import EnsembleTrainer
        self._trainer = EnsembleTrainer.load_pipeline(model_path)
        self._feature_names = self._trainer.feature_names

        # 读取配置中的默认值
        import yaml
        cfg_path = self.config_path or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config.yaml"
        )
        if os.path.exists(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
            self._defaults = config.get("data", {}).get("default_values", {})

        self._handle = ModelHandle(path=model_path, _model=self._trainer)

        logger.info(
            f"[BatchPredict] 模型就绪: {os.path.basename(model_path)} "
            f"| {len(self._feature_names)} 特征"
        )

    def warmup(self, blocking: bool = False):
        """
        预热模型加载

        Args:
            blocking: 是否阻塞等待加载完成
        """
        if self._warmed_up.is_set():
            return

        if blocking:
            self._init_model()
            self._warmed_up.set()
        else:
            # 后台加载
            def _bg_load():
                try:
                    self._init_model()
                except (Exception) as e:
                    logger.error(f"[BatchPredict] 预热失败: {e}")
                finally:
                    self._warmed_up.set()

            t = threading.Thread(target=_bg_load, daemon=True)
            t.start()
            logger.info("[BatchPredict] 模型后台预热中...")

    def reload_model(self):
        """热重载模型"""
        self._trainer = None
        self._handle = None
        self._warmed_up.clear()
        self._init_model()
        self._warmed_up.set()
        logger.info("[BatchPredict] 模型已热重载")

    @property
    def model_version(self) -> str:
        if self._handle is None:
            return "not_loaded"
        pipeline = self._handle._model
        if pipeline and isinstance(pipeline, dict):
            return pipeline.get("version", "unknown")
        return "unknown"

    @property
    def is_ready(self) -> bool:
        """模型是否就绪"""
        return self._trainer is not None

    # ══════════════════════════════════════════════════
    # 批量预测
    # ══════════════════════════════════════════════════

    def _build_feature_matrix(self,
                              matches: List[Dict]) -> List[Dict[str, float]]:
        """构建特征矩阵（填充默认值）"""
        feature_rows = []
        for m in matches:
            features = m.get("features", m)
            row = {}
            for col in self._feature_names:
                if col in features and features[col] is not None:
                    try:
                        row[col] = float(features[col])
                    except (ValueError, TypeError):
                        row[col] = self._defaults.get(col, 0.0)
                else:
                    row[col] = self._defaults.get(col, 0.0)
            feature_rows.append(row)
        return feature_rows

    def predict_batch(self, matches: List[Dict],
                      return_details: bool = False
                      ) -> List[Dict]:
        """
        批量预测多场比赛

        Args:
            matches: 比赛列表，每项含 features 字典或 feature 字段
            return_details: 是否返回完整概率分布

        Returns:
            [{match_index, prediction, confidence, home_prob, draw_prob, away_prob}, ...]
        """
        self.warmup(blocking=True)

        if not matches:
            return []

        start = time.perf_counter()

        # 构建特征矩阵
        feature_rows = self._build_feature_matrix(matches)

        # 批量预测
        proba = self._trainer.predict_batch(feature_rows)

        # 构建结果
        results = []
        for i in range(len(matches)):
            home_prob = float(proba[i, 0])
            draw_prob = float(proba[i, 1])
            away_prob = float(proba[i, 2])

            # 判定
            if home_prob >= max(draw_prob, away_prob):
                prediction = "H"
            elif draw_prob >= away_prob:
                prediction = "D"
            else:
                prediction = "A"

            r = {
                "match_index": i,
                "prediction": prediction,
                "confidence": round(max(home_prob, draw_prob, away_prob), 4),
            }

            if return_details:
                r.update({
                    "home_prob": round(home_prob, 4),
                    "draw_prob": round(draw_prob, 4),
                    "away_prob": round(away_prob, 4),
                })

            # 保留原始元数据
            if "match_id" in matches[i]:
                r["match_id"] = matches[i]["match_id"]
            if "home_team" in matches[i]:
                r["home_team"] = matches[i]["home_team"]
            if "away_team" in matches[i]:
                r["away_team"] = matches[i]["away_team"]

            results.append(r)

        elapsed = (time.perf_counter() - start) * 1000
        self._prediction_count += len(matches)
        self._total_time_ms += elapsed

        logger.info(
            f"[BatchPredict] {len(matches)} 场预测完成 "
            f"({elapsed:.0f}ms, {elapsed/len(matches):.1f}ms/场)"
        )

        return results

    def predict_single(self, features: Dict[str, float],
                       match_id: int = None) -> Dict:
        """单场预测（内部复用批量接口）"""
        item = {"features": features}
        if match_id:
            item["match_id"] = match_id
        results = self.predict_batch([item], return_details=True)
        return results[0] if results else {}

    # ══════════════════════════════════════════════════
    # 统计
    # ══════════════════════════════════════════════════

    @property
    def stats(self) -> Dict:
        return {
            "model_loaded": self.is_ready,
            "model_version": self.model_version,
            "feature_count": len(self._feature_names),
            "total_predictions": self._prediction_count,
            "total_time_ms": round(self._total_time_ms, 1),
            "avg_time_ms": round(
                self._total_time_ms / max(self._prediction_count, 1), 1
            ),
        }


# ══════════════════════════════════════════════════
# 全局单例
# ══════════════════════════════════════════════════

_global_batch_predictor: Optional[BatchPredictor] = None


def get_predictor(model_dir: str = None) -> BatchPredictor:
    """获取全局 BatchPredictor 单例"""
    global _global_batch_predictor
    if _global_batch_predictor is None:
        _global_batch_predictor = BatchPredictor(model_dir=model_dir)
    return _global_batch_predictor
