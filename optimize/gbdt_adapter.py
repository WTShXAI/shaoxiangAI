"""
哨响AI - GBDT 数据格式适配层 (T08)
===================================
为 XGBoost / LightGBM / CatBoost 提供统一的数据预处理器。
所有模型共享同一套特征工程 & 数据预处理管线。

职责：
1. 从数据库加载训练数据 (matches JOIN match_features)
2. 特征预处理 (缺失填充 / 异常值裁剪 / 交互项生成)
3. 标签构建 (净胜球 → 3分类)
4. 训练/验证集时序分割
5. 标准化
"""

import sys, os, logging
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import train_test_split

logger = logging.getLogger(__name__)


# ─── 特征配置（从 config.yaml 统一加载） ────────────────────
def _load_feature_config():
    """从 config.yaml 读取 feature_columns 和 default_values"""
    import yaml
    _cfg_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config.yaml')
    with open(_cfg_path, 'r', encoding='utf-8') as f:
        cfg = yaml.safe_load(f)
    return cfg['data']['feature_columns'], cfg['data']['default_values']

FEATURE_COLS, DEFAULT_VALUES = _load_feature_config()

# 对称特征（可正可负）：裁剪时上下界取对称
SYMMETRIC_FEATURES: set = {
    'a1', 'a4', 'a5', 'a6', 'sigma_trap',
    'rank_diff_factor', 'form_momentum', 'h2h_factor', 'beta_dev',
}

# 交互项定义：(name, formula_lambda)
# 每个 lambda 签名为: fn(X: pd.DataFrame) -> pd.Series
def _build_interactions(X: pd.DataFrame) -> pd.DataFrame:
    """生成 6 个特征交互项 (与 ensemble_trainer 一致)"""
    out = X.copy()
    if 'a1' in X.columns and 'sigma_trap' in X.columns:
        out['ix_a1_sigma'] = X['a1'] * X['sigma_trap']
    if 'a2' in X.columns and 'lambda_crush' in X.columns:
        out['ix_a2_lambda'] = (X['a2'] - 0.5) * (X['lambda_crush'] - 1.0)
    if 'a3' in X.columns and 'epsilon_senti' in X.columns:
        out['ix_a3_epsilon'] = (X['a3'] - 0.5) * (X['epsilon_senti'] - 0.5)
    if 'a1' in X.columns and 'a2' in X.columns:
        out['ix_a1_a2'] = X['a1'] * (X['a2'] - 0.5)
    if 'rank_diff_factor' in X.columns and 'form_momentum' in X.columns:
        out['ix_rank_form'] = X['rank_diff_factor'] * X['form_momentum']
    if 'a1' in X.columns and 'a2' in X.columns and 'rank_diff_factor' in X.columns:
        out['ix_power_gap'] = np.abs(
            0.4 * X['a1'] + 0.4 * (X['a2'] - 0.5) + 0.2 * np.clip(X['rank_diff_factor'], -1, 1)
        )
    return out


@dataclass
class TrainingBundle:
    """统一训练数据包：三个模型共享相同的 split + scaler"""
    X_train: np.ndarray
    y_train: np.ndarray
    X_val: np.ndarray
    y_val: np.ndarray
    X_test: np.ndarray
    y_test: np.ndarray
    X_full: np.ndarray
    y_full: np.ndarray
    scaler: StandardScaler
    feature_names: List[str]
    train_size: int
    val_size: int
    test_size: int
    class_weights_train: np.ndarray = field(default_factory=lambda: np.ones(3))
    # 元数据（时间、联赛等）
    train_dates: Optional[pd.Series] = None
    test_dates: Optional[pd.Series] = None


class GBDTDataAdapter:
    """
    统一的 GBDT 数据适配器。
    对外暴露一组 prep_* 方法，各模型只需调用即可获得标准化数据。
    """

    def __init__(
        self,
        db_path: str = 'data/football_data.db',
        feature_cols: Optional[List[str]] = None,
        enable_interactions: bool = True,
        test_ratio: float = 0.10,
        val_from_train_ratio: float = 0.15,
        random_state: int = 42,
    ):
        self.db_path = os.path.join(os.path.dirname(__file__), '..', db_path)
        self.feature_cols = feature_cols or FEATURE_COLS[:]
        self.enable_interactions = enable_interactions
        self.test_ratio = test_ratio
        self.val_from_train_ratio = val_from_train_ratio
        self.random_state = random_state
        self._df: Optional[pd.DataFrame] = None
        self._meta: Optional[pd.DataFrame] = None

    # ─── 数据加载 ───────────────────────────────────────────

    def load_from_db(self) -> pd.DataFrame:
        """从 SQLite 加载所有已完赛比赛 + 特征"""
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row

        # 检测实际可用的特征列
        cursor = conn.execute("PRAGMA table_info(match_features)")
        available_cols = set(row[1] for row in cursor.fetchall())
        usable_cols = [c for c in self.feature_cols if c in available_cols]

        cols_sql = ", ".join([f"mf.{c}" for c in usable_cols])
        query = f"""
        SELECT m.match_id, m.home_team_name, m.away_team_name, m.match_date,
               m.league_name, m.home_score, m.away_score,
               {cols_sql}
        FROM matches m
        JOIN match_features mf ON m.match_id = mf.match_id
        WHERE m.home_score IS NOT NULL AND m.away_score IS NOT NULL
        ORDER BY m.match_date
        """
        df = pd.read_sql_query(query, conn)
        conn.close()

        self.feature_cols = usable_cols
        self._df = df
        logger.info(f"[DataAdapter] 加载 {len(df)} 条训练数据, {len(usable_cols)} 特征")
        return df

    # ─── 特征预处理 ─────────────────────────────────────────

    def prepare_features(
        self,
        df: Optional[pd.DataFrame] = None,
        add_interactions: bool = True,
    ) -> Tuple[pd.DataFrame, pd.Series]:
        """
        特征工程：
        1. 缺失值填充 (DEFAULT_VALUES)
        2. 异常值裁剪 (99分位数 × 1.5)
        3. 可选交互特征
        4. 标签构建
        """
        if df is None:
            df = self._df
        if df is None:
            raise ValueError("没有可用数据，请先调用 load_from_db()")

        X = df[self.feature_cols].copy()

        # 1. 缺失值
        missing_report = {}
        for col in self.feature_cols:
            n_missing = X[col].isna().sum()
            X[col] = pd.to_numeric(X[col], errors='coerce').fillna(
                DEFAULT_VALUES.get(col, 0.0)
            )
            if n_missing > 0:
                missing_report[col] = n_missing
        if missing_report:
            pct = {
                k: f"{v}/{len(df)} ({v/len(df)*100:.1f}%)"
                for k, v in sorted(missing_report.items(), key=lambda x: -x[1])
            }
            logger.warning(f"[DataAdapter] 特征缺失: {pct}")

        # 2. 异常值裁剪
        clip_count = 0
        for col in self.feature_cols:
            q99 = X[col].abs().quantile(0.99)
            q01 = X[col].abs().quantile(0.01)
            if q99 > q01 * 15:
                upper = q99 * 1.5
                lower = -upper if col in SYMMETRIC_FEATURES else 0.0
                X[col] = X[col].clip(lower, upper)
                clip_count += 1
        if clip_count:
            logger.info(f"[DataAdapter] 异常值裁剪: {clip_count} 个特征")

        # 3. 交互项
        if add_interactions and self.enable_interactions:
            X = _build_interactions(X)

        # 4. 标签
        y_cls = pd.Series([
            0 if gd > 0 else (2 if gd < 0 else 1)
            for gd in (df['home_score'] - df['away_score'])
        ], name='result_class')

        dist = y_cls.value_counts().to_dict()
        logger.info(
            f"[DataAdapter] 标签: 主胜={dist.get(0,0)} ({dist.get(0,0)/len(y_cls)*100:.1f}%) | "
            f"平局={dist.get(1,0)} ({dist.get(1,0)/len(y_cls)*100:.1f}%) | "
            f"客胜={dist.get(2,0)} ({dist.get(2,0)/len(y_cls)*100:.1f}%)"
        )

        self._feature_cols_actual = list(X.columns)
        return X, y_cls

    # ─── 训练/验证/测试分割 ────────────────────────────────

    def create_bundle(
        self,
        df: Optional[pd.DataFrame] = None,
        draw_weight: float = 1.0,
        add_interactions: bool = True,
    ) -> TrainingBundle:
        """
        一次性完成: 加载 → 预处理 → 分割 → 标准化 → 返回 TrainingBundle
        """
        if df is None:
            df = self.load_from_db()

        X, y = self.prepare_features(df, add_interactions=add_interactions)

        if X.empty:
            raise RuntimeError("预处理后无有效数据")

        n = len(X)

        # 时序分割：最后 test_ratio 作为测试集
        split_idx = int(n * (1 - self.test_ratio))
        X_train_val, X_test = X.iloc[:split_idx], X.iloc[split_idx:]
        y_train_val, y_test = y.iloc[:split_idx], y.iloc[split_idx:]
        dates_train_val = df['match_date'].iloc[:split_idx] if 'match_date' in df.columns else None
        dates_test = df['match_date'].iloc[split_idx:] if 'match_date' in df.columns else None

        # 从训练集再分出验证集
        from sklearn.model_selection import train_test_split
        X_train, X_val, y_train, y_val = train_test_split(
            X_train_val, y_train_val,
            test_size=self.val_from_train_ratio,
            random_state=self.random_state,
            shuffle=False,  # 保持时序
        )

        # 标准化
        scaler = StandardScaler()
        X_train_s = scaler.fit_transform(X_train)
        X_val_s = scaler.transform(X_val)
        X_test_s = scaler.transform(X_test)
        X_full_s = scaler.transform(pd.concat([X_train, X_val, X_test]))

        # 类别权重 (balanced)
        from sklearn.utils.class_weight import compute_class_weight
        classes = np.array([0, 1, 2])
        class_w = compute_class_weight('balanced', classes=classes, y=y_train)
        class_w[1] *= max(draw_weight, 0.5)
        class_w = class_w / class_w.mean()  # 归一化

        bundle = TrainingBundle(
            X_train=X_train_s,
            y_train=y_train.values,
            X_val=X_val_s,
            y_val=y_val.values,
            X_test=X_test_s,
            y_test=y_test.values,
            X_full=X_full_s,
            y_full=y.values,
            scaler=scaler,
            feature_names=list(X_train.columns),
            train_size=len(X_train),
            val_size=len(X_val),
            test_size=len(X_test),
            class_weights_train=class_w,
            train_dates=dates_train_val,
            test_dates=dates_test,
        )

        logger.info(
            f"[DataAdapter] Bundle: train={bundle.train_size} val={bundle.val_size} "
            f"test={bundle.test_size} | 特征维度={X_train_s.shape[1]}"
        )
        return bundle

    @property
    def feature_count(self) -> int:
        return len(getattr(self, '_feature_cols_actual', self.feature_cols))


# ─── 便捷工厂函数 ───────────────────────────────────────────

def make_training_bundle(
    db_path: str = 'data/football_data.db',
    draw_weight: float = 1.0,
) -> TrainingBundle:
    """一键创建标准训练数据包"""
    adapter = GBDTDataAdapter(db_path=db_path)
    return adapter.create_bundle(draw_weight=draw_weight)
