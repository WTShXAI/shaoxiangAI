"""
哨响AI - 集成模型训练器 v3.2
============================
v3.2 核心架构 (时间切分诚实OOF + Stacking + D-Gate融合):
1. 时间切分策略: pre-2023训练 / 2023+诚实OOF (8631样本)
2. Stacking: LightGBM Meta-Learner 替代固定权重加权平均
3. D-Gate融合: 两阶段Draw预测 + NN辅助信号
4. 5模型集成: LightGBM + XGBoost + OddsExpert + Heuristic + NeuralNet
5. OddsExpert: 312K赔率专精模型
6. 联赛感知D先验: 按联赛实际D率微调
7. 赔率漂移特征: drift_magnitude/direction/sharp_signal
8. 自动概率校准: CalibratorSuite

v3.2 指标: Acc=59.20%, Draw-F1=0.504, AUC=0.814 (vs v3.1: 53.48%/0.323/0.704)

继承:
- CalibratorSuite自动概率校准
- 5-fold时序交叉验证
- 赔率衍生特征深度开采
- 阈值优化 (per-class)
"""
import sys, os, logging, yaml, joblib, time, warnings, json
from datetime import datetime, timezone
from typing import Dict, List, Tuple, Optional, Any
import numpy as np
import pandas as pd
from collections import Counter

warnings.filterwarnings('ignore')

# ── PyTorch 依赖检查 (NN子模型) ──
try:
    import torch
    import torch.nn as nn
    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False

# ── 依赖检查 ──
try:
    import xgboost as xgb
    XGB_AVAILABLE = True
except ImportError:
    XGB_AVAILABLE = False

try:
    import lightgbm as lgb
    LGB_AVAILABLE = True
except ImportError:
    LGB_AVAILABLE = False

try:
    from sklearn.model_selection import TimeSeriesSplit
    from sklearn.preprocessing import StandardScaler
    from sklearn.linear_model import Ridge, LogisticRegression
    from sklearn.metrics import (
        accuracy_score, classification_report, confusion_matrix,
        roc_auc_score, log_loss, brier_score_loss, matthews_corrcoef
    )
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.utils.class_weight import compute_class_weight
    from sklearn.feature_selection import SelectFromModel
except ImportError as e:
    raise ImportError(f"scikit-learn 依赖缺失: {e}")

# ── 全局开关: 一键控制 v6 高阶模块 ──
ENABLE_ADVANCED_OPTIMIZATION = True  # False 时完全还原 v3.2 原生逻辑

def get_active_version() -> str:
    """从 model_registry.json 读取当前活跃版本号 (单一版本来源)"""
    try:
        import json, os
        registry_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            'saved_models', 'model_registry.json'
        )
        if os.path.exists(registry_path):
            with open(registry_path, 'r') as f:
                registry = json.load(f)
            return registry.get('active', '3.2')
    except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
        logging.getLogger(__name__).warning("读取模型注册表失败: %s", e)
    return '3.2'

class EnsembleTrainer:
    """
    足球预测集成模型训练器 v3.2

    核心架构:
    - Base Models: LightGBM + XGBoost + Heuristic + OddsExpert + NeuralNet (5模型)
    - Stacking: LightGBM Meta-Learner (诚实OOF训练，替代固定权重)
    - CalibratorSuite: 自动概率校准
    - 联赛感知: D先验按联赛实际D率微调
    - D-Gate融合: 两阶段Draw预测
    """

    def __init__(self, config_path: str = None):
        self.logger = self._setup_logger()
        self.config = self._load_config(config_path)
        self._init_paths()

        self.xgb_model = None
        self.lgb_model = None
        self.odds_expert_model = None  # 312K赔率专精模型
        self.nn_model = None           # v3.1: 神经网络子模型
        self.nn_scaler = None          # v3.1: NN专用scaler
        self.draw_expert_model = None  # v4.0: Draw专精二分类器
        self.scaler = None
        self.odds_scaler = None  # odds_expert专用scaler
        self.feature_names = []
        self.odds_feature_names = []  # odds_expert特征名
        self.eval_metrics = {}
        self.calibrator_suite = None
        self.meta_learner = None  # Stacking meta-learner
        self.league_d_rates = {}  # 联赛D率表
        self._last_submodel_probas = None  # P0: 缓存子模型输出(供D-gate融合用)
        self._odds_calibrator = None  # P1: 赔率逆向校准器(OddsInverseCalibrator实例)
        self._xg_calibrated_params = None  # P1: 校准后的xG/贝叶斯参数

    def _setup_logger(self):
        logger = logging.getLogger('EnsembleTrainer')
        logger.setLevel(logging.INFO)
        if not logger.handlers:
            formatter = logging.Formatter(
                '%(asctime)s [%(levelname)s] %(name)s - %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S'
            )
            handler = logging.StreamHandler()
            handler.setFormatter(formatter)
            logger.addHandler(handler)
        return logger

    def _load_config(self, config_path: str = None) -> Dict:
        if config_path is None:
            config_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), 'config.yaml'
            )
        with open(config_path, 'r', encoding='utf-8') as f:
            cfg = yaml.safe_load(f)
        self.config_path = config_path
        self.logger.info(f"配置加载成功: {config_path}")
        return cfg

    def _init_paths(self):
        root = self.config['paths']['project_root']
        root = root if os.path.isabs(root) else os.path.abspath(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), root)
        )
        for key in ['data_dir', 'model_dir', 'output_dir', 'log_dir', 'report_dir']:
            dir_path = os.path.join(root, self.config['paths'][key])
            os.makedirs(dir_path, exist_ok=True)
            setattr(self, f'_{key}', dir_path)
        self.db_path = os.path.join(root, self.config['database']['path'])

    # ══════════════════════════════════════════════════
    # 0.5 赔率逆向校准 (xG + 贝叶斯参数)
    # ══════════════════════════════════════════════════

    def _run_odds_calibration(self) -> Dict:
        """
        P1: 运行赔率逆向校准 (可一键开关)

        流程:
          1. 从 config.yaml 读取 odds_calibration 配置
          2. 调用 odds_inverse_calibrator 进行 xG 参数 + 贝叶斯参数优化
          3. 保存校准结果到 .joblib
          4. 更新 self._xg_calibrated_params 供后续特征工程使用

        控制开关: config.yaml → odds_calibration.enabled
        """
        cfg = self.config.get('odds_calibration', {})
        lambda_reg = cfg.get('lambda_reg', 0.2)
        max_samples = cfg.get('max_samples', 200000)
        train_years = cfg.get('train_years', [2012, 2022])
        val_years = cfg.get('val_years', [2023, 2025])
        fast_mode = cfg.get('fast_mode', True)

        from odds_inverse_calibrator import OddsInverseCalibrator

        calibrator = OddsInverseCalibrator(
            lambda_reg=lambda_reg,
            max_iter=200,
            verbose=True,
            risk_premium_optimize=cfg.get('risk_premium_optimize', True),
            heavy_rp_threshold=cfg.get('heavy_rp_threshold', 8.0),
            apply_risk_weight_loss=cfg.get('apply_risk_weight_loss', True),
        )

        result = calibrator.calibrate(
            db_path=self.db_path,
            train_start=f"{train_years[0]}-01-01",
            train_end=f"{train_years[1]}-12-31",
            val_start=f"{val_years[0]}-01-01",
            val_end=f"{val_years[1]}-12-31",
            max_samples=max_samples,
            fast_mode=fast_mode,
        )

        self._odds_calibrator = calibrator
        self._xg_calibrated_params = {
            'alpha': result.xg_params.get('alpha'),
            'beta': result.xg_params.get('beta'),
            'H_global': result.xg_params.get('H_global'),
            'S_league': result.xg_params.get('S_league'),
            'teams': result.xg_params.get('team_names', []),
            'team_idx': result.xg_params.get('team_idx', {}),
            'leagues': result.xg_params.get('league_names', []),
            'league_idx': result.xg_params.get('league_idx', {}),
            'bayes': result.bayes_params,
            'messages': result.messages,
            'metrics': result.metrics,
        }

        # 持久化
        save_path = os.path.join(self._model_dir, 'odds_calibrated_params.joblib')
        calibrator.save(save_path)
        self.logger.info(f"  校准参数已保存: {save_path}")

        return self._xg_calibrated_params

    # ══════════════════════════════════════════════════
    # 1. 数据加载与预处理
    # ══════════════════════════════════════════════════

    def load_training_data(self) -> pd.DataFrame:
        """从 SQLite 数据库加载训练数据 (含赔率特征+漂移特征+缺失指示器+真实赔率)"""
        import sqlite3
        self.logger.info("开始加载训练数据...")

        feat_cols = self.config['data']['feature_columns']
        cols_sql = ", ".join([f"mf.{c}" for c in feat_cols])

        conn = sqlite3.connect(self.db_path)

        query = f"""
        SELECT m.match_id, m.home_team_name, m.away_team_name, m.match_date,
               m.league_name, m.home_score, m.away_score,
               {cols_sql},
               o_avg.home_odds   AS odds_home,
               o_avg.draw_odds   AS odds_draw,
               o_avg.away_odds   AS odds_away,
               o_avg.return_rate AS odds_return_rate,
               (1.0/o_avg.home_odds) / (1.0/o_avg.home_odds + 1.0/o_avg.draw_odds + 1.0/o_avg.away_odds) AS odds_imp_h,
               (1.0/o_avg.draw_odds) / (1.0/o_avg.home_odds + 1.0/o_avg.draw_odds + 1.0/o_avg.away_odds) AS odds_imp_d,
               (1.0/o_avg.away_odds) / (1.0/o_avg.home_odds + 1.0/o_avg.draw_odds + 1.0/o_avg.away_odds) AS odds_imp_a,
               o_avg.away_odds - o_avg.home_odds AS odds_spread,
               (1.0/o_avg.home_odds + 1.0/o_avg.draw_odds + 1.0/o_avg.away_odds - 1.0) AS odds_overround,
               (1.0/o_avg.draw_odds) / (1.0/o_avg.home_odds + 1.0/o_avg.draw_odds + 1.0/o_avg.away_odds) - 0.333 AS odds_draw_dev,
               mf.odds_open_h, mf.odds_open_d, mf.odds_open_a,
               mf.odds_close_h, mf.odds_close_d, mf.odds_close_a
        FROM matches m
        JOIN match_features mf ON m.match_id = mf.match_id
        LEFT JOIN (
            SELECT match_id,
                   AVG(home_odds) AS home_odds,
                   AVG(draw_odds) AS draw_odds,
                   AVG(away_odds) AS away_odds,
                   AVG(return_rate) AS return_rate
            FROM odds
            WHERE home_odds > 0 AND draw_odds > 0 AND away_odds > 0
            GROUP BY match_id
        ) o_avg ON m.match_id = o_avg.match_id
        WHERE m.home_score IS NOT NULL AND m.away_score IS NOT NULL
        ORDER BY m.match_date
        """
        df = pd.read_sql_query(query, conn)
        conn.close()

        # 填充赔率缺失值
        odds_defaults = {
            'odds_home': 2.5, 'odds_draw': 3.3, 'odds_away': 2.8,
            'odds_return_rate': 0.95,
            'odds_imp_h': 0.40, 'odds_imp_d': 0.28, 'odds_imp_a': 0.32,
            'odds_spread': 0.0, 'odds_overround': 0.05, 'odds_draw_dev': 0.0,
            'drift_magnitude': 0.0, 'drift_direction': 0, 'drift_sharp_signal': 0,
            'drift_h_val': 0.0, 'drift_d': 0.0, 'drift_a_val': 0.0,
            'odds_open_h': 0.0, 'odds_open_d': 0.0, 'odds_open_a': 0.0,
            'odds_close_h': 0.0, 'odds_close_d': 0.0, 'odds_close_a': 0.0,
            'real_home_odds': 0.0, 'real_draw_odds': 0.0, 'real_away_odds': 0.0,
        }
        for col, default in odds_defaults.items():
            if col in df.columns:
                df[col] = df[col].fillna(default)

        n_with_odds = (df['odds_home'] != 2.5).sum() if 'odds_home' in df.columns else 0
        self.logger.info(f"数据加载完成: {len(df)} 条样本, {df['league_name'].nunique()} 个联赛")
        self.logger.info(f"赔率覆盖: {n_with_odds}/{len(df)} ({n_with_odds/len(df)*100:.1f}%)")
        return df

    def load_extended_odds_data(self) -> pd.DataFrame:
        """v2.5: 加载312K扩展赔率训练集 (从training_extended表)"""
        import sqlite3
        ext_cfg = self.config['data'].get('extended_training', {})
        if not ext_cfg.get('enabled', True):
            return pd.DataFrame()

        self.logger.info("加载扩展赔率训练集 (312K)...")
        conn = sqlite3.connect(self.db_path)
        table = ext_cfg.get('table', 'training_extended')

        try:
            df = pd.read_sql_query(f"SELECT * FROM {table} WHERE result_class IS NOT NULL", conn)
        except (Exception, KeyError, IndexError, requests.exceptions.RequestException, sqlite3.Error):
            self.logger.warning(f"表 {table} 不存在或为空，跳过扩展训练集")
            conn.close()
            return pd.DataFrame()

        conn.close()
        self.logger.info(f"扩展训练集: {len(df)} 条")
        return df

    def prepare_features(
        self, df: pd.DataFrame, add_interactions: bool = True,
    ) -> Tuple[pd.DataFrame, pd.Series]:
        """
        特征工程预处理管道 v2.5：
        1. 类型转换 + 缺失值填充
        2. 移除死特征 (weather_modifier, drift_d) + 高默认率特征
        3. 异常值裁剪
        4. 赔率衍生特征
        5. 漂移衍生特征 (v2.5)
        6. 缺失指示器 (v2.5)
        7. 特征交互项
        8. 构建三分类标签
        """
        feature_cols = self.config['data']['feature_columns']
        defaults = self.config['data']['default_values']
        threshold = self.config['data']['default_ratio_threshold']
        dead_features = self.config['data'].get('dead_features', [])

        # 确保所有特征列存在
        available_cols = [c for c in feature_cols if c in df.columns]
        # v2.5: 移除死特征黑名单
        available_cols = [c for c in available_cols if c not in dead_features]

        X = df[available_cols].copy()
        self.logger.info(f"可用特征: {len(available_cols)}/{len(feature_cols)} (排除死特征: {dead_features})")

        # ── 赔率衍生特征 ──
        odds_cols_added = 0
        for col in ['odds_imp_h', 'odds_imp_d', 'odds_imp_a',
                     'odds_spread', 'odds_overround', 'odds_draw_dev']:
            if col in df.columns:
                X[col] = pd.to_numeric(df[col], errors='coerce').fillna(
                    {'odds_imp_h': 0.40, 'odds_imp_d': 0.28, 'odds_imp_a': 0.32,
                     'odds_spread': 0.0, 'odds_overround': 0.05, 'odds_draw_dev': 0.0,
                    }.get(col, 0.0))
                odds_cols_added += 1

        # 赔率置信度
        if 'odds_imp_h' in X.columns and 'odds_imp_d' in X.columns and 'odds_imp_a' in X.columns:
            X['odds_confidence'] = np.sqrt(
                (X['odds_imp_h'] - 1/3)**2 +
                (X['odds_imp_d'] - 1/3)**2 +
                (X['odds_imp_a'] - 1/3)**2
            ) * 3.0
            odds_cols_added += 1

        # 赔率-H方向背离度
        if 'odds_imp_h' in X.columns and 'a1' in X.columns:
            X['odds_model_diverge'] = X['odds_imp_h'] - (0.33 + X['a1'] * 0.4)
            odds_cols_added += 1

        if odds_cols_added > 0:
            self.logger.info(f"赔率衍生特征: {odds_cols_added} 个")

        # v2.5-opt: 真实赔率特征 (从df直接加载, 用values避免索引问题)
        real_odds_added = 0
        for col in ['real_home_odds', 'real_draw_odds', 'real_away_odds']:
            if col in df.columns:
                vals = pd.to_numeric(df[col], errors='coerce').fillna(0.0).values
                X[col] = vals[:len(X)] if len(vals) >= len(X) else np.pad(vals, (0, len(X)-len(vals)))
                real_odds_added += 1

        # D专属赔率特征
        if 'real_draw_odds' in X.columns and (X['real_draw_odds'] > 0).any():
            # D赔率吸引力: 低于3.0的D赔率暗示平局
            mask_has_odds = X['real_draw_odds'] > 0
            X['draw_odds_attract'] = np.where(mask_has_odds,
                np.clip(1.0 - (X['real_draw_odds'] - 3.0) / 2.0, 0, 1), 0)
            real_odds_added += 1

            # 赔率隐含D概率 vs 均值D概率的偏差
            if 'odds_imp_d' in X.columns:
                X['draw_odds_vs_imp'] = np.where(mask_has_odds,
                    (1.0 / X['real_draw_odds']) - X['odds_imp_d'], 0)
                real_odds_added += 1

        if real_odds_added > 0:
            self.logger.info(f"真实赔率特征: {real_odds_added} 个")

        # v2.7: 赔率变化特征 (open → close)
        odds_move_added = 0
        has_open = all(c in df.columns for c in ['odds_open_h', 'odds_open_d', 'odds_open_a'])
        has_close = all(c in df.columns for c in ['odds_close_h', 'odds_close_d', 'odds_close_a'])
        if has_open and has_close:
            for suffix, col in [('h', 'home'), ('d', 'draw'), ('a', 'away')]:
                open_col = f'odds_open_{suffix}'
                close_col = f'odds_close_{suffix}'
                move_col = f'odds_move_{suffix}'
                # 赔率变化：正值=赔率上升=市场看衰，负值=赔率下降=市场看好
                X[move_col] = pd.to_numeric(df[close_col], errors='coerce').fillna(0) - \
                              pd.to_numeric(df[open_col], errors='coerce').fillna(0)
                odds_move_added += 1
            # 赔率变化幅度（绝对值之和）
            X['odds_move_magnitude'] = X['odds_move_h'].abs() + X['odds_move_d'].abs() + X['odds_move_a'].abs()
            odds_move_added += 1
            # 赔率变化方向一致性：open/close变化方向是否一致（最高赔率升高+最低赔率降低=市场共识）
            h_move = X['odds_move_h']
            d_move = X['odds_move_d']
            a_move = X['odds_move_a']
            # 最爱变化（最有可能的方向赔率是否下降）
            if 'odds_imp_h' in X.columns and 'odds_imp_d' in X.columns and 'odds_imp_a' in X.columns:
                fav = np.argmax([X['odds_imp_h'], X['odds_imp_d'], X['odds_imp_a']], axis=0)
                X['odds_fav_move'] = np.where(fav == 0, X['odds_move_h'],
                                              np.where(fav == 1, X['odds_move_d'], X['odds_move_a']))
                odds_move_added += 1
            self.logger.info(f"赔率变化特征: {odds_move_added} 个")

        # v2.7: 市场强度特征
        if 'real_home_odds' in X.columns and 'real_draw_odds' in X.columns and 'real_away_odds' in X.columns:
            # 最受欢迎方向的确信度 = max(1/odds) = 市场最强信号的强度
            X['market_fav_strength'] = np.maximum(
                (1.0 / X['real_home_odds'].replace(0, np.nan)).fillna(0),
                np.maximum(
                    (1.0 / X['real_draw_odds'].replace(0, np.nan)).fillna(0),
                    (1.0 / X['real_away_odds'].replace(0, np.nan)).fillna(0)
                )
            )
            # 市场分歧度 = odds_spread（主客赔率差）
            if 'odds_spread' in X.columns:
                X['market_disagreement'] = X['odds_spread'].abs()
            self.logger.info("市场强度特征: 已添加")

        # ── v2.5: 漂移衍生特征 ──
        drift_cols_added = 0
        # drift_magnitude (已从DB加载)
        if 'drift_magnitude' in X.columns:
            X['drift_magnitude'] = pd.to_numeric(X['drift_magnitude'], errors='coerce').fillna(0.0)
            drift_cols_added += 1
        # drift_direction
        if 'drift_direction' in X.columns:
            X['drift_direction'] = pd.to_numeric(X['drift_direction'], errors='coerce').fillna(0)
            drift_cols_added += 1
        # drift_sharp_signal
        if 'drift_sharp_signal' in X.columns:
            X['drift_sharp_signal'] = pd.to_numeric(X['drift_sharp_signal'], errors='coerce').fillna(0)
            drift_cols_added += 1

        # v2.5: 漂移交互特征
        if 'drift_magnitude' in X.columns and 'odds_confidence' in X.columns:
            X['ix_drift_confidence'] = X['drift_magnitude'] * X['odds_confidence']
            drift_cols_added += 1
        if 'drift_sharp_signal' in X.columns and 'odds_imp_d' in X.columns:
            X['ix_sharp_draw'] = X['drift_sharp_signal'] * (X['odds_imp_d'] - 0.25)
            drift_cols_added += 1

        # v2.5-opt: D专属漂移特征 (drift对D方向的信号)
        if 'drift_d' in X.columns:
            # D赔率下降 = 市场认为平局可能性增加
            X['drift_d_signal'] = np.where(X['drift_d'] < -0.03, 1,
                                           np.where(X['drift_d'] > 0.03, -1, 0))
            drift_cols_added += 1
        if 'drift_magnitude' in X.columns and 'match_evenness' in X.columns:
            # 高漂移+高均衡 = 平局信号加强
            X['ix_drift_even_draw'] = X['drift_magnitude'] * X['match_evenness']
            drift_cols_added += 1

        if drift_cols_added > 0:
            self.logger.info(f"漂移衍生特征: {drift_cols_added} 个")

        # ── v2.5: 缺失指示器 ──
        for indicator in ['miss_drift', 'miss_weather']:
            if indicator in X.columns:
                X[indicator] = pd.to_numeric(X[indicator], errors='coerce').fillna(1).astype(int)
        self.logger.info(f"缺失指示器: miss_drift, miss_weather")

        # ── 缺失值填充 ──
        for col in available_cols:
            X[col] = pd.to_numeric(X[col], errors='coerce')
            X[col] = X[col].fillna(defaults.get(col, 0.0))

        # ── 移除高默认值占比特征 ──
        removed = []
        for col in available_cols[:]:
            default_val = defaults.get(col, None)
            if default_val is not None:
                pct_default = (X[col] == default_val).mean()
                if pct_default > threshold:
                    removed.append(col)

        if removed:
            X = X.drop(columns=removed)
            available_cols = [c for c in available_cols if c not in removed]
            self.logger.warning(f"移除低质量特征: {removed}")

        # ── 异常值裁剪 ──
        for col in available_cols:
            q99 = X[col].abs().quantile(0.99)
            if q99 > X[col].abs().quantile(0.01) * 15:
                upper = q99 * 1.5
                X[col] = X[col].clip(-upper if col in ['a1','a4','a5','a6','sigma_trap',
                    'rank_diff_factor','form_momentum','h2h_factor','beta_dev'] else 0, upper)

        # ── 特征交互项 ──
        if add_interactions:
            ic = 0
            interactions = [
                ('a1', 'sigma_trap', 'ix_a1_sigma', lambda a, b: a * b),
                ('a2', 'lambda_crush', 'ix_a2_lambda', lambda a, b: (a - 0.5) * (b - 1.0)),
                ('a3', 'epsilon_senti', 'ix_a3_epsilon', lambda a, b: (a - 0.5) * (b - 0.5)),
                ('a1', 'a2', 'ix_a1_a2', lambda a, b: a * (b - 0.5)),
                ('rank_diff_factor', 'form_momentum', 'ix_rank_form', lambda a, b: a * b),
                ('a7', 'lambda_crush', 'ix_a7_lambda', lambda a, b: a * (b - 1.0)),
                ('a8', 'sigma_trap', 'ix_a8_sigma', lambda a, b: a * b),
                ('match_evenness', 'imp_d_norm', 'ix_even_impd', lambda a, b: a * b),
                ('odds_balance', 'match_evenness', 'ix_bal_even', lambda a, b: (1 - a) * b),
            ]
            for c1, c2, name, fn in interactions:
                if c1 in X.columns and c2 in X.columns:
                    X[name] = fn(X[c1], X[c2])
                    ic += 1

            # power_gap (综合信号)
            if all(c in X.columns for c in ['a1', 'a2', 'rank_diff_factor']):
                X['ix_power_gap'] = np.abs(
                    0.4 * X['a1'] + 0.4 * (X['a2'] - 0.5) + 0.2 * np.clip(X['rank_diff_factor'], -1, 1)
                )
                ic += 1

            # v2.5: 赔率漂移×平局特征
            if 'drift_magnitude' in X.columns and 'odds_imp_d' in X.columns:
                X['ix_drift_draw_odds'] = X['drift_magnitude'] * X['odds_imp_d']
                ic += 1

            if ic > 0:
                self.logger.info(f"特征交互项: {ic} 个")

        # ── 构建标签 (仅训练时有比分) ──
        has_scores = 'home_score' in df.columns and df['home_score'].notna().any()
        y_cls = None
        if has_scores:
            y_cls = pd.Series([
                0 if gd > 0 else (2 if gd < 0 else 1)
                for gd in (df['home_score'] - df['away_score'])
            ], name='result_class')

            dist = Counter(y_cls)
            self.logger.info(f"标签分布: 主胜={dist[0]} ({dist[0]/len(y_cls)*100:.1f}%) | "
                             f"平局={dist[1]} ({dist[1]/len(y_cls)*100:.1f}%) | "
                             f"客胜={dist[2]} ({dist[2]/len(y_cls)*100:.1f}%)")
        else:
            self.logger.info("无比分数据 (预测模式), 跳过标签构建")

        self.feature_names = list(X.columns)
        # v3.2+P1: 维度稳定性校验 — 若与已加载模型不一致，告警
        if hasattr(self, '_expected_feature_names') and self._expected_feature_names:
            missing = [f for f in self._expected_feature_names if f not in self.feature_names]
            extra = [f for f in self.feature_names if f not in self._expected_feature_names]
            if missing or extra:
                self.logger.warning(
                    f"特征维度漂移! 期望={len(self._expected_feature_names)} → 实际={len(self.feature_names)} | "
                    f"缺失={len(missing)} 多余={len(extra)}"
                )
                if missing:
                    self.logger.warning(f"缺失特征(前10): {missing[:10]}")
                    for f in missing:
                        X[f] = 0.0
                    self.feature_names = list(X.columns)
                    self.logger.warning(f"已填充缺失特征, 最终维度={len(self.feature_names)}")
        else:
            # 首次训练: 记录期望维度作为基准
            self._expected_feature_names = list(self.feature_names)
            self.logger.info(f"基准特征集已记录: {len(self._expected_feature_names)} 维")
        if has_scores:
            self.meta = df[['match_id', 'match_date', 'home_team_name', 'away_team_name',
                             'league_name', 'home_score', 'away_score']]

        # v2.5: 计算联赛D率表 (仅训练时)
        if has_scores:
            self._compute_league_d_rates(df)

        return X, y_cls

    def _compute_league_d_rates(self, df: pd.DataFrame):
        """v2.5: 计算各联赛实际D率 (用于联赛感知D先验)"""
        ldp_cfg = self.config.get('models', {}).get('league_draw_prior', {})
        if not ldp_cfg.get('enabled', True):
            return

        # 先用配置中的D率表
        config_rates = ldp_cfg.get('league_d_rates', {})
        global_d_rate = ldp_cfg.get('global_d_rate', 0.257)

        # 用实际数据补充
        if 'league_name' in df.columns:
            for league in df['league_name'].unique():
                mask = df['league_name'] == league
                league_data = df[mask]
                if len(league_data) >= 50:
                    actual_d = ((league_data['home_score'] - league_data['away_score']) == 0).mean()
                    if league not in config_rates:
                        self.league_d_rates[league] = actual_d
                    else:
                        self.league_d_rates[league] = config_rates[league]

        # 补充配置中的联赛
        for league, rate in config_rates.items():
            if league not in self.league_d_rates:
                self.league_d_rates[league] = rate

        self.global_d_rate = global_d_rate
        self.logger.info(f"联赛D率表: {len(self.league_d_rates)} 个联赛 (全局D率={global_d_rate:.3f})")

    def prepare_odds_features(self, ext_df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.Series]:
        """v2.5: 为odds_expert模型准备赔率专精特征"""
        ext_cfg = self.config['data'].get('extended_training', {})
        odds_cols = ext_cfg.get('odds_only_features', [])

        available = [c for c in odds_cols if c in ext_df.columns]
        X = ext_df[available].copy().fillna(0.0)

        # 确保基础特征存在
        for col in odds_cols:
            if col not in X.columns:
                X[col] = 0.0

        y = ext_df['result_class'].astype(int)
        self.odds_feature_names = list(X.columns)
        self.logger.info(f"Odds Expert特征: {len(self.odds_feature_names)} 个, 样本: {len(X)}")
        return X, y

    # ══════════════════════════════════════════════════
    # 2. 模型训练
    # ══════════════════════════════════════════════════

    def train_ridge(self, X_train, y_gd):
        """训练Ridge回归模型（预测净胜球，辅助校准）"""
        from sklearn.linear_model import Ridge
        self.ridge_model = Ridge(alpha=1.0, random_state=42)
        self.ridge_model.fit(X_train, y_gd)
        self.logger.info(f"Ridge回归训练完成")

    def train_xgboost(self, X_train, X_val, y_train, y_val) -> dict:
        if not XGB_AVAILABLE:
            self.logger.error("XGBoost 未安装！")
            return {}

        xgb_cfg = self.config['models']['xgboost']
        self.logger.info("=" * 60)
        self.logger.info("训练 XGBoost 分类器")
        self.logger.info("=" * 60)

        classes = np.array([0, 1, 2])
        cw_cfg = self.config['models'].get('class_weight', {})
        if cw_cfg.get('enabled', False):
            base_weights = compute_class_weight('balanced', classes=classes, y=y_train)
            class_weights = base_weights.copy()
            class_weights[0] *= cw_cfg.get('home_multiplier', 1.0)
            class_weights[1] *= cw_cfg.get('draw_multiplier', 1.0)
            class_weights[2] *= cw_cfg.get('away_multiplier', 1.0)
            class_weights = class_weights / class_weights.mean()
            sample_weights = np.array([class_weights[int(c)] for c in y_train])
        else:
            sample_weights = None

        params = {
            'objective': 'multi:softprob', 'num_class': 3,
            'eval_metric': ['mlogloss', 'merror'], 'random_state': 42,
            'verbosity': 0, 'n_jobs': -1,
            'n_estimators': xgb_cfg.get('n_estimators', 500),
            'max_depth': xgb_cfg.get('max_depth', 5),
            'learning_rate': xgb_cfg.get('learning_rate', 0.03),
            'subsample': xgb_cfg.get('subsample', 0.8),
            'colsample_bytree': xgb_cfg.get('colsample_bytree', 0.8),
            'min_child_weight': xgb_cfg.get('min_child_weight', 3),
            'reg_alpha': xgb_cfg.get('reg_alpha', 0.1),
            'reg_lambda': xgb_cfg.get('reg_lambda', 1.0),
            'gamma': xgb_cfg.get('gamma', 0.05),
            'early_stopping_rounds': xgb_cfg.get('early_stopping_rounds', 50),
            'tree_method': xgb_cfg.get('tree_method', 'hist'),
        }

        t0 = time.time()
        model = xgb.XGBClassifier(**params)
        model.fit(X_train, y_train, sample_weight=sample_weights,
                  eval_set=[(X_val, y_val)], verbose=False)
        train_time = time.time() - t0

        self.logger.info(f"训练完成 ({train_time:.0f}s), best_iteration={model.best_iteration}")

        # 概率校准
        if self.config['models']['calibration']['enabled']:
            calib_params = {k: v for k, v in params.items()
                           if k not in ('early_stopping_rounds', 'eval_metric')}
            model = xgb.XGBClassifier(**calib_params)
            model.fit(X_train, y_train, sample_weight=sample_weights, verbose=False)
            calibrated = CalibratedClassifierCV(
                estimator=model,
                method=self.config['models']['calibration']['method'],
                cv=5, n_jobs=1
            )
            calibrated.fit(X_train, y_train, sample_weight=sample_weights)
            self.xgb_model = calibrated
        else:
            self.xgb_model = model

        return {'train_time_s': round(train_time, 0)}

    def train_lightgbm(self, X_train, X_val, y_train, y_val) -> dict:
        if not LGB_AVAILABLE:
            self.logger.warning("LightGBM 未安装，跳过")
            return {}

        lgb_cfg = self.config['models'].get('lightgbm', {})
        self.logger.info("=" * 60)
        self.logger.info("训练 LightGBM 分类器")
        self.logger.info("=" * 60)

        classes = np.array([0, 1, 2])
        balanced_weights = compute_class_weight('balanced', classes=classes, y=y_train)
        balanced_weights[1] *= lgb_cfg.get('draw_weight_boost', 1.2)
        sample_weights = np.array([balanced_weights[int(c)] for c in y_train])
        self.logger.info(f"类别权重: H={balanced_weights[0]:.3f}, D={balanced_weights[1]:.3f}, A={balanced_weights[2]:.3f}")

        params = {
            'objective': 'multiclass', 'num_class': 3, 'metric': 'multi_logloss',
            'boosting_type': lgb_cfg.get('boosting_type', 'gbdt'),
            'num_leaves': lgb_cfg.get('num_leaves', 63),
            'max_depth': lgb_cfg.get('max_depth', 7),
            'learning_rate': lgb_cfg.get('learning_rate', 0.03),
            'n_estimators': lgb_cfg.get('n_estimators', 800),
            'subsample': lgb_cfg.get('subsample', 0.8),
            'subsample_freq': lgb_cfg.get('subsample_freq', 1),
            'colsample_bytree': lgb_cfg.get('colsample_bytree', 0.8),
            'min_child_samples': lgb_cfg.get('min_child_samples', 20),
            'reg_alpha': lgb_cfg.get('reg_alpha', 0.1),
            'reg_lambda': lgb_cfg.get('reg_lambda', 1.0),
            'min_split_gain': lgb_cfg.get('min_split_gain', 0.01),
            'random_state': 42, 'n_jobs': -1, 'verbose': -1,
        }

        t0 = time.time()
        model = lgb.LGBMClassifier(**params)
        model.fit(X_train, y_train, sample_weight=sample_weights,
                  eval_set=[(X_val, y_val)],
                  callbacks=[lgb.early_stopping(stopping_rounds=lgb_cfg.get('early_stopping_rounds', 50), verbose=False),
                             lgb.log_evaluation(period=0)])
        train_time = time.time() - t0

        self.logger.info(f"训练完成 ({train_time:.0f}s), best_iteration={getattr(model, 'best_iteration_', params['n_estimators'])}")

        # 概率校准
        if self.config['models']['calibration']['enabled']:
            calib_params = {k: v for k, v in params.items() if k != 'metric'}
            base_model = lgb.LGBMClassifier(**calib_params)
            base_model.fit(X_train, y_train, sample_weight=sample_weights)
            calibrated = CalibratedClassifierCV(estimator=base_model,
                method=self.config['models']['calibration']['method'], cv=5, n_jobs=1)
            calibrated.fit(X_train, y_train, sample_weight=sample_weights)
            self.lgb_model = calibrated
        else:
            self.lgb_model = model

        return {'train_time_s': round(train_time, 0)}

    def train_odds_expert(self) -> dict:
        """v2.5: 训练312K赔率专精LightGBM模型"""
        ext_df = self.load_extended_odds_data()
        if len(ext_df) < 1000:
            self.logger.warning("扩展训练集不足，跳过odds_expert训练")
            return {}

        self.logger.info("=" * 60)
        self.logger.info("训练 Odds Expert LightGBM (312K)")
        self.logger.info("=" * 60)

        X_odds, y_odds = self.prepare_odds_features(ext_df)

        # 时序分割
        split_idx = int(len(X_odds) * 0.90)
        X_tr, X_val = X_odds.iloc[:split_idx], X_odds.iloc[split_idx:]
        y_tr, y_val = y_odds.iloc[:split_idx], y_odds.iloc[split_idx:]

        # 标准化
        self.odds_scaler = StandardScaler()
        X_tr_scaled = self.odds_scaler.fit_transform(X_tr)
        X_val_scaled = self.odds_scaler.transform(X_val)

        oe_cfg = self.config['models'].get('odds_expert', {})
        classes = np.array([0, 1, 2])
        balanced_weights = compute_class_weight('balanced', classes=classes, y=y_tr)
        balanced_weights[1] *= oe_cfg.get('draw_weight_boost', 1.3)
        sample_weights = np.array([balanced_weights[int(c)] for c in y_tr])

        params = {
            'objective': 'multiclass', 'num_class': 3, 'metric': 'multi_logloss',
            'boosting_type': 'gbdt',
            'num_leaves': oe_cfg.get('num_leaves', 31),
            'max_depth': oe_cfg.get('max_depth', 6),
            'learning_rate': oe_cfg.get('learning_rate', 0.05),
            'n_estimators': oe_cfg.get('n_estimators', 600),
            'subsample': 0.8, 'subsample_freq': 1, 'colsample_bytree': 0.8,
            'min_child_samples': oe_cfg.get('min_child_samples', 50),
            'reg_alpha': oe_cfg.get('reg_alpha', 0.2),
            'reg_lambda': oe_cfg.get('reg_lambda', 1.5),
            'random_state': 42, 'n_jobs': -1, 'verbose': -1,
        }

        t0 = time.time()
        model = lgb.LGBMClassifier(**params)
        model.fit(X_tr_scaled, y_tr, sample_weight=sample_weights,
                  eval_set=[(X_val_scaled, y_val)],
                  callbacks=[lgb.early_stopping(50, verbose=False), lgb.log_evaluation(0)])
        train_time = time.time() - t0

        # 评估
        val_proba = model.predict_proba(X_val_scaled)
        val_acc = accuracy_score(y_val, np.argmax(val_proba, axis=1))
        self.logger.info(f"Odds Expert训练完成 ({train_time:.0f}s), 验证准确率: {val_acc*100:.2f}%")

        self.odds_expert_model = model
        return {'train_time_s': round(train_time, 0), 'val_accuracy': round(val_acc * 100, 2)}

    # ══════════════════════════════════════════════════
    # 3. 集成预测 (v2.5: 4模型 + Stacking, Ridge已移除)
    # ══════════════════════════════════════════════════

    def ensemble_predict_proba(self, X: np.ndarray,
                                league_names: Optional[List[str]] = None,
                                dynamic_weights_kw: Optional[Dict] = None,
                                elo_proba: Optional[np.ndarray] = None,
                                poisson_proba: Optional[np.ndarray] = None,
                                raw_features_list: Optional[List[Dict]] = None,
                                external_heuristic_proba: Optional[np.ndarray] = None) -> np.ndarray:
        """
        v2.5-opt-fix 五模型集成预测 (含Stacking meta-learner + 原始特征drift感知)
        raw_features_list: 原始特征dict列表，用于drift感知D boost判断
        external_heuristic_proba: 外部HeuristicPredictor输出 (n,3)，替换内部简化heuristic
        """
        # ── v2.5: 如果有Stacking meta-learner，使用它 ──
        stack_cfg = self.config.get('models', {}).get('stacking', {})
        if self.meta_learner is not None and stack_cfg.get('enabled', True):
            proba = self._predict_with_stacking(X, league_names,
                                                dynamic_weights_kw, elo_proba, poisson_proba,
                                                raw_features_list=raw_features_list,
                                                external_heuristic_proba=external_heuristic_proba)
            proba = self._apply_low_confidence(proba, raw_features_list)
            return proba

        # ── Fallback: 加权平均 ──
        proba = self._predict_with_weights(X, dynamic_weights_kw, elo_proba, poisson_proba, league_names)
        proba = self._apply_low_confidence(proba, raw_features_list)
        return proba

    def _get_base_model_probas(self, X, dynamic_weights_kw=None, elo_proba=None, poisson_proba=None):
        """获取6个base模型的概率输出 (v3.1: 新增NN子模型)"""
        # LightGBM
        if self.lgb_model is not None and hasattr(self.lgb_model, 'predict_proba'):
            proba_lgb = self.lgb_model.predict_proba(X)
        else:
            proba_lgb = np.ones((X.shape[0], 3)) / 3

        # XGBoost
        if self.xgb_model is not None and hasattr(self.xgb_model, 'predict_proba'):
            proba_xgb = self.xgb_model.predict_proba(X)
        else:
            proba_xgb = np.ones((X.shape[0], 3)) / 3

        # Heuristic
        proba_heuristic = self._heuristic_predict_proba(X, elo_proba=elo_proba, poisson_proba=poisson_proba)

        # Odds Expert (v2.5)
        proba_odds = self._odds_expert_predict_proba(X)

        # P1修复: OddsExpert OOD检测 — 均匀输出时自动降权(用核心模型均值替代)
        oe_is_ood = np.max(proba_odds, axis=1) - np.min(proba_odds, axis=1) < 0.02
        if np.any(oe_is_ood):
            # OOD时: 用LightGBM+XGBoost+Heuristic三模型均值替代(比均匀更有信息量)
            core_avg = (proba_lgb + proba_xgb + proba_heuristic) / 3.0
            # 但仍标记为低置信度(概率偏向温和), 不完全替代
            # 混合: 50%核心均值 + 50%均匀 → 比纯均匀好但不自信
            fallback = 0.5 * core_avg + 0.5 * np.ones_like(proba_odds) / 3.0
            proba_odds[oe_is_ood] = fallback[oe_is_ood]

        # Neural Network (v3.1)
        proba_nn = self._nn_predict_proba(X)

        return proba_lgb, proba_xgb, proba_heuristic, proba_odds, proba_nn

    def _nn_predict_proba(self, X: np.ndarray) -> np.ndarray:
        """v3.1: 神经网络子模型推理（Draw F1强项）"""
        if self.nn_model is None or not TORCH_AVAILABLE:
            return np.ones((X.shape[0], 3)) / 3.0
        try:
            device = next(self.nn_model.parameters()).device
            X_tensor = torch.tensor(X, dtype=torch.float32).to(device)
            self.nn_model.eval()
            with torch.no_grad():
                logits = self.nn_model(X_tensor)
                proba = torch.softmax(logits, dim=1).cpu().numpy()
            return proba
        except Exception as e:
            self.logger.warning(f"NN推理失败，使用均匀分布: {e}")
            return np.ones((X.shape[0], 3)) / 3.0

    def load_nn_model(self, nn_path: str) -> bool:
        """v3.1: 从 .pth 文件加载 FootballNN 模型"""
        if not TORCH_AVAILABLE:
            self.logger.warning("PyTorch 未安装，NN子模型不可用")
            return False
        if not os.path.exists(nn_path):
            self.logger.warning(f"NN模型文件不存在: {nn_path}")
            return False
        try:
            # 动态导入 FootballNN (避免循环依赖)
            import importlib.util
            nn_script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     'scripts', 'train_neural_net.py')
            spec = importlib.util.spec_from_file_location('train_neural_net', nn_script)
            nn_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(nn_module)
            FootballNN = nn_module.FootballNN

            # 加载权重
            checkpoint = torch.load(nn_path, map_location='cpu', weights_only=False)
            input_dim = len(self.feature_names) if self.feature_names else 72
            model = FootballNN(input_dim=input_dim)
            if isinstance(checkpoint, dict) and 'model_state_dict' in checkpoint:
                model.load_state_dict(checkpoint['model_state_dict'])
            else:
                model.load_state_dict(checkpoint)
            model.eval()
            self.nn_model = model
            self.logger.info(f"NN子模型加载成功: {nn_path}")
            return True
        except Exception as e:
            self.logger.warning(f"NN模型加载失败: {e}")
            return False

    def _predict_with_stacking(self, X, league_names=None, dynamic_weights_kw=None,
                                elo_proba=None, poisson_proba=None,
                                raw_features_list=None,
                                external_heuristic_proba=None):
        """v3.3: Stacking集成（6模型，XGBoost仅H/A二维, 剔除Draw噪声）
        external_heuristic_proba: 外部HeuristicPredictor输出 (n,3)，替换内部简化版
        """
        proba_lgb, proba_xgb, proba_heuristic, proba_odds, proba_nn = \
            self._get_base_model_probas(X, dynamic_weights_kw, elo_proba, poisson_proba)

        # P0: 缓存子模型输出(供外部D-gate融合读取OE独立概率)
        self._last_submodel_probas = {
            'lgb': proba_lgb, 'xgb': proba_xgb,
            'heuristic': proba_heuristic, 'odds_expert': proba_odds,
            'nn': proba_nn,
        }
        # v4.0: 缓存DrawExpert P(Draw)
        if self.draw_expert_model is not None:
            de_pdraw = self.draw_expert_model.predict_proba(X)
            self._last_submodel_probas['draw_expert'] = de_pdraw

        # ── 用外部 HeuristicPredictor 替换内部简化版 ──
        if external_heuristic_proba is not None:
            n_samples = proba_heuristic.shape[0]
            ext = np.asarray(external_heuristic_proba, dtype=np.float64)
            if ext.ndim == 1:
                ext = ext.reshape(1, 3)
            if ext.shape[0] == 1 and n_samples > 1:
                ext = np.tile(ext, (n_samples, 1))
            if ext.shape == proba_heuristic.shape:
                proba_heuristic = ext
                self.logger.debug(f"[Stacking] 使用外部HeuristicPredictor替换内部简化heuristic")

        # 构建meta特征: v3.3 XGBoost仅H/A二维(剔除Draw噪声), 14维概率
        # 向后兼容: 自动检测旧meta-learner(21维)→保留XGB完整3维
        proba_xgb_meta = proba_xgb[:, [0, 2]]  # default: 剔除Draw

        # 添加key features到meta特征 (与训练时一致)
        key_feat_indices = []
        for fname in ['odds_confidence', 'match_evenness', 'imp_d_norm']:
            if fname in self.feature_names:
                key_feat_indices.append(self.feature_names.index(fname))

        # 添加drift features到meta特征
        drift_feat_indices = []
        for fname in ['drift_magnitude', 'drift_direction', 'drift_d']:
            if fname in self.feature_names:
                drift_feat_indices.append(self.feature_names.index(fname))

        # 检测旧meta-learner: 若期望维度=21(15+3+3)且无DrawExpert, 回退XGB完整3维
        # v4.0的21维是 14+1+3+3 (含DrawExpert, XGB仅H/A), 不应触发此逻辑
        n_old_total = 15 + len(key_feat_indices) + len(drift_feat_indices)
        is_old_meta = (
            hasattr(self.meta_learner, 'n_features_in_') 
            and self.meta_learner.n_features_in_ == n_old_total
            and self.draw_expert_model is None  # v4.0有DrawExpert, 不走旧逻辑
        )
        if is_old_meta:
            proba_xgb_meta = proba_xgb  # 旧模型: 保留XGB完整3维

        meta_features = np.hstack([proba_lgb, proba_xgb_meta, proba_heuristic, proba_odds, proba_nn])

        # v4.0: DrawExpert P(Draw) — 当模型包含Draw专精二分类器时追加
        if self.draw_expert_model is not None and X.ndim > 1:
            de_pdraw = self.draw_expert_model.predict_proba(X)
            meta_features = np.hstack([meta_features, de_pdraw])

        if key_feat_indices and X.ndim > 1:
            key_feats = X[:, key_feat_indices]
            meta_features = np.hstack([meta_features, key_feats])

        # 添加drift特征到meta (让meta-learner感知赔率漂移, v3.3维度自动对齐)
        if drift_feat_indices and X.ndim > 1:
            drift_feats = X[:, drift_feat_indices]
            expected_dim = meta_features.shape[1] + len(drift_feat_indices)
            if hasattr(self.meta_learner, 'n_features_in_') and expected_dim == self.meta_learner.n_features_in_:
                meta_features = np.hstack([meta_features, drift_feats])
            elif not hasattr(self.meta_learner, 'n_features_in_'):
                pass

        # Meta-learner预测 (v3.2: 维度已对齐，无需兼容层)
        proba_ensemble = self.meta_learner.predict_proba(meta_features)

        # ── ENABLE_ADVANCED_OPTIMIZATION 全局开关 ──
        # False 时跳过所有 post-v3.2 高级后处理，保留 v3.2 原生逻辑:
        #   Stacking预测 + CalibratorSuite校准 = v3.2 完整流程
        if ENABLE_ADVANCED_OPTIMIZATION:
            # P_final 冷门补偿 (post-v3.2)
            pfinal_cfg = self.config.get('models', {}).get('p_final', {})
            if pfinal_cfg.get('enabled', False) and X is not None and len(X) > 0:
                proba_ensemble = self._apply_p_final(proba_ensemble, X)

            # 动态D先验 — 赔率漂移感知 (post-v3.2)
            draw_prior_cfg = self.config.get('models', {}).get('draw_prior', {})
            if draw_prior_cfg.get('enabled', True):
                d_boost = draw_prior_cfg.get('d_probability_boost', 0.05)
                drift_d_threshold = draw_prior_cfg.get('drift_d_threshold', -0.5)
                drift_mag_threshold = draw_prior_cfg.get('drift_mag_threshold', 1.0)
                drift_boost_max = draw_prior_cfg.get('drift_boost_max', 0.04)
                
                has_raw_drift = raw_features_list is not None and len(raw_features_list) > 0
                
                for i in range(len(proba_ensemble)):
                    if has_raw_drift and i < len(raw_features_list):
                        raw = raw_features_list[i]
                        drift_d_val = raw.get('drift_d', 0.0) if isinstance(raw, dict) else 0.0
                        drift_mag_val = raw.get('drift_magnitude', 0.0) if isinstance(raw, dict) else 0.0
                    else:
                        drift_d_idx = self.feature_names.index('drift_d') if 'drift_d' in self.feature_names else None
                        drift_mag_idx = self.feature_names.index('drift_magnitude') if 'drift_magnitude' in self.feature_names else None
                        drift_d_val = X[i, drift_d_idx] if (X.ndim > 1 and drift_d_idx is not None) else 0.0
                        drift_mag_val = X[i, drift_mag_idx] if (X.ndim > 1 and drift_mag_idx is not None) else 0.0
                        if abs(drift_d_val) > 2.0:
                            drift_d_val = drift_d_val * 0.3
                        if abs(drift_mag_val) > 3.0:
                            drift_mag_val = drift_mag_val * 0.5
                    
                    if drift_d_val < drift_d_threshold and drift_mag_val > drift_mag_threshold:
                        drift_bonus = min(abs(drift_d_val) * 0.06, drift_boost_max)
                        dynamic_boost = d_boost + drift_bonus
                    else:
                        dynamic_boost = d_boost
                    
                    league = league_names[i] if league_names and i < len(league_names) else None
                    if league and league in self.league_d_rates:
                        global_d_rate = self.config.get('models', {}).get('league_draw_prior', {}).get('global_d_rate', 0.257)
                        dynamic_boost *= (self.league_d_rates[league] / global_d_rate)
                    proba_ensemble[i, 1] += dynamic_boost
                
                proba_ensemble = proba_ensemble / proba_ensemble.sum(axis=1, keepdims=True)

        # CalibratorSuite校准 (v3.2 原生: 始终有效)
        calib_enabled = self.config.get('models', {}).get('calibration', {}).get('enabled', True)
        if calib_enabled and self.calibrator_suite is not None:
            try:
                proba_ensemble = self.calibrator_suite.predict(proba_ensemble)
                proba_ensemble = proba_ensemble / proba_ensemble.sum(axis=1, keepdims=True)
            except (Exception, KeyError, IndexError, requests.exceptions.RequestException):
                pass

        return proba_ensemble

    def _predict_with_weights(self, X, dynamic_weights_kw=None, elo_proba=None,
                              poisson_proba=None, league_names=None):
        """加权平均fallback (v3.1: 含NN权重)"""
        pf_cfg = self.config.get('models', {}).get('p_fusion', {})

        if pf_cfg.get('enabled', False) and X is not None and len(X) > 0:
            dynamic_weights = self._p_fusion_weights(X[0] if X.ndim > 1 else X)
        else:
            dynamic_weights = None

        if dynamic_weights is not None:
            w_lgb = dynamic_weights.get('lightgbm', 0.28)
            w_xgb = dynamic_weights.get('xgboost', 0.28)
            w_heuristic = dynamic_weights.get('heuristic', 0.14)
            w_odds = dynamic_weights.get('odds_expert', 0.10)
            w_nn = dynamic_weights.get('neural_net', 0.10)
        elif dynamic_weights_kw is not None:
            w_lgb = dynamic_weights_kw.get('lightgbm', 0.28)
            w_xgb = dynamic_weights_kw.get('xgboost', 0.28)
            w_heuristic = dynamic_weights_kw.get('heuristic', 0.14)
            w_odds = dynamic_weights_kw.get('odds_expert', 0.10)
            w_nn = dynamic_weights_kw.get('neural_net', 0.10)
        else:
            ens_cfg = self.config['models']['ensemble']
            w_lgb = ens_cfg.get('lightgbm_weight', 0.28)
            w_xgb = ens_cfg.get('xgboost_weight', 0.28)
            w_heuristic = ens_cfg.get('heuristic_weight', 0.14)
            w_odds = ens_cfg.get('odds_expert_weight', 0.10)
            w_nn = ens_cfg.get('neural_net_weight', 0.10)  # v3.1

        # 如果NN不可用，权重归零（其余按比例重分配）
        if self.nn_model is None:
            w_nn = 0.0

        proba_lgb, proba_xgb, proba_heuristic, proba_odds, proba_nn = \
            self._get_base_model_probas(X, dynamic_weights_kw, elo_proba, poisson_proba)

        # P0: 缓存子模型输出(fallback路径同样缓存)
        self._last_submodel_probas = {
            'lgb': proba_lgb, 'xgb': proba_xgb,
            'heuristic': proba_heuristic, 'odds_expert': proba_odds,
            'nn': proba_nn,
        }

        total_w = w_lgb + w_xgb + w_heuristic + w_odds + w_nn
        if total_w > 0:
            w_lgb /= total_w; w_xgb /= total_w
            w_heuristic /= total_w; w_odds /= total_w; w_nn /= total_w

        proba_ensemble = (w_lgb * proba_lgb + w_xgb * proba_xgb +
                         w_heuristic * proba_heuristic +
                         w_odds * proba_odds + w_nn * proba_nn)
        proba_ensemble = proba_ensemble / proba_ensemble.sum(axis=1, keepdims=True)

        # P_final
        pfinal_cfg = self.config.get('models', {}).get('p_final', {})
        if pfinal_cfg.get('enabled', False):
            proba_ensemble = self._apply_p_final(proba_ensemble, X)

        # D先验
        draw_prior_cfg = self.config.get('models', {}).get('draw_prior', {})
        if draw_prior_cfg.get('enabled', True):
            d_boost = draw_prior_cfg.get('d_probability_boost', 0.05)  # v2.6: 默认0.05
            proba_ensemble = self._apply_league_draw_prior(proba_ensemble, d_boost, league_names)

        # CalibratorSuite (v2.6-fix: 尊重config.enabled开关)
        calib_enabled_w = self.config.get('models', {}).get('calibration', {}).get('enabled', True)
        if calib_enabled_w and self.calibrator_suite is not None:
            try:
                proba_ensemble = self.calibrator_suite.predict(proba_ensemble)
                proba_ensemble = proba_ensemble / proba_ensemble.sum(axis=1, keepdims=True)
            except (Exception, requests.exceptions.RequestException):
                pass

        return proba_ensemble

    def _apply_league_draw_prior(self, proba, base_boost, league_names=None):
        """v2.5: 联赛感知D先验补偿"""
        ldp_cfg = self.config.get('models', {}).get('league_draw_prior', {})
        if not ldp_cfg.get('enabled', True) or league_names is None:
            # 无联赛信息时用全局boost
            proba[:, 1] += base_boost
            proba = proba / proba.sum(axis=1, keepdims=True)
            return proba

        global_d_rate = ldp_cfg.get('global_d_rate', 0.257)
        for i in range(len(proba)):
            league = league_names[i] if i < len(league_names) else None
            if league and league in self.league_d_rates:
                league_d = self.league_d_rates[league]
                # 按联赛D率与全局D率的比例缩放boost
                ratio = league_d / global_d_rate
                proba[i, 1] += base_boost * ratio
            else:
                proba[i, 1] += base_boost

        proba = proba / proba.sum(axis=1, keepdims=True)
        return proba

    # ═════════════════════════════════════════════════
    # P0修复: 低置信度模式 — 高默认值比例时向均匀分布收缩
    # ═════════════════════════════════════════════════
    def _apply_low_confidence(self, proba, raw_features_list=None):
        """
        高默认值比例时触发低置信度模式, 向均匀分布收缩
        硬编码阈值: threshold=0.30, max_shrink=0.50
        (后续可迁移到config.yaml的low_confidence配置块)
        """
        if raw_features_list is None or len(raw_features_list) == 0:
            return proba

        threshold = 0.50   # 默认值比例阈值 (国家队比赛≈85%, 俱乐部通常<30%)
        max_shrink = 0.50  # 最大收缩比例

        default_vals = self.config['data'].get('default_values', {})

        for i in range(min(len(proba), len(raw_features_list))):
            raw = raw_features_list[i]
            if not isinstance(raw, dict):
                continue

            # 计算默认值比例
            default_count = 0
            n_feats = 0
            for name in self.feature_names:
                n_feats += 1
                default_val = default_vals.get(name, 0.0)
                feat_val = raw.get(name, default_val)
                if abs(feat_val - default_val) < 1e-6:
                    default_count += 1

            if n_feats == 0:
                continue
            default_ratio = default_count / n_feats

            # 触发低置信度
            if default_ratio > threshold:
                # 收缩比例: 随default_ratio线性增长, 最大max_shrink
                shrink = min(max_shrink, (default_ratio - threshold) * 1.5)
                uniform = np.array([1.0/3, 1.0/3, 1.0/3])
                proba[i] = (1.0 - shrink) * proba[i] + shrink * uniform
                proba[i] = proba[i] / proba[i].sum()

        return proba

    def _odds_expert_predict_proba(self, X: np.ndarray) -> np.ndarray:
        """v2.5: Odds Expert模型预测"""
        if self.odds_expert_model is None:
            return np.ones((X.shape[0], 3)) / 3

        # 从主特征中提取odds特征 (需要对齐特征名)
        try:
            odds_cols = self.config['data'].get('extended_training', {}).get('odds_only_features', [])
            # 尝试从已标准化的X中提取赔率相关特征
            odds_indices = []
            for col in odds_cols:
                if col in self.feature_names:
                    odds_indices.append(self.feature_names.index(col))

            if len(odds_indices) >= 5:
                X_odds = X[:, odds_indices] if X.ndim > 1 else X[odds_indices].reshape(1, -1)
                if self.odds_scaler is not None:
                    X_odds = self.odds_scaler.transform(X_odds)
                # 确保特征数匹配 OddsExpert 模型实际训练维度
                n_expected = (self.odds_expert_model.n_features_in_ 
                              if hasattr(self.odds_expert_model, 'n_features_in_') 
                              else len(self.odds_feature_names))
                if X_odds.shape[1] == n_expected:
                    return self.odds_expert_model.predict_proba(X_odds)
                elif X_odds.shape[1] > n_expected:
                    # 截断多余特征 (OddsExpert 特征名不匹配的已知问题, 优雅降级)
                    X_odds = X_odds[:, :n_expected]
                    return self.odds_expert_model.predict_proba(X_odds)
        except Exception as e:
            self.logger.debug(f"OddsExpert predict_proba 回退均匀分布: {type(e).__name__}: {e}")

        return np.ones((X.shape[0], 3)) / 3

    def _p_fusion_weights(self, features_row=None):
        """P_fusion 五维动态融合"""
        pf_cfg = self.config.get('models', {}).get('p_fusion', {})
        if not pf_cfg.get('enabled', False):
            return None

        w_lgb = pf_cfg.get('lgb_base', 0.28)
        w_xgb = pf_cfg.get('xgb_base', 0.28)
        w_odds = pf_cfg.get('odds_expert_base', 0.10)
        w_ridge = pf_cfg.get('ridge_base', 0.10)
        w_heuristic = pf_cfg.get('heuristic_base', 0.14)

        if features_row is not None and len(self.feature_names) > 0:
            try:
                sigma_idx = self.feature_names.index('sigma_trap') if 'sigma_trap' in self.feature_names else None
                crush_idx = self.feature_names.index('lambda_crush') if 'lambda_crush' in self.feature_names else None
                senti_idx = self.feature_names.index('epsilon_senti') if 'epsilon_senti' in self.feature_names else None
                odds_conf_idx = self.feature_names.index('odds_confidence') if 'odds_confidence' in self.feature_names else None

                if sigma_idx is not None:
                    sigma_val = abs(features_row[sigma_idx])
                    w_lgb += sigma_val * pf_cfg.get('sigma_boost_lgb', 0.05)
                    w_xgb -= sigma_val * pf_cfg.get('sigma_shift_xgb', 0.03)

                if crush_idx is not None and features_row[crush_idx] > 1.5:
                    w_ridge += pf_cfg.get('crush_boost_ridge', 0.05)

                if senti_idx is not None and abs(features_row[senti_idx] - 0.5) > 0.3:
                    w_heuristic += abs(features_row[senti_idx] - 0.5) * pf_cfg.get('senti_shift', 0.05)

                if odds_conf_idx is not None:
                    odds_conf = features_row[odds_conf_idx]
                    w_lgb += odds_conf * pf_cfg.get('odds_boost_gbdt', 0.03)
                    w_xgb += odds_conf * pf_cfg.get('odds_boost_gbdt', 0.03)
                    w_odds += odds_conf * 0.02  # v2.5: 高赔率置信度时odds_expert增权
                    w_heuristic -= odds_conf * pf_cfg.get('odds_shift_heur', 0.02)
            except (ValueError, IndexError):
                pass

        total = w_lgb + w_xgb + w_odds + w_ridge + w_heuristic
        if total > 0:
            w_lgb /= total; w_xgb /= total; w_odds /= total
            w_ridge /= total; w_heuristic /= total

        return {
            'lightgbm': float(np.clip(w_lgb, 0.10, 0.45)),
            'xgboost': float(np.clip(w_xgb, 0.10, 0.45)),
            'odds_expert': float(np.clip(w_odds, 0.03, 0.20)),
            'ridge': float(np.clip(w_ridge, 0.03, 0.20)),
            'heuristic': float(np.clip(w_heuristic, 0.05, 0.30)),
        }

    # ── 辅助函数 (继承v2.4) ──

    def _apply_p_final(self, proba, X):
        pfinal_cfg = self.config.get('models', {}).get('p_final', {})
        threshold = pfinal_cfg.get('sigma_trap_threshold', 0.15)
        upset_mult = pfinal_cfg.get('upset_multiplier', 1.25)
        crush_mult = pfinal_cfg.get('crush_suppress', 0.80)
        try:
            sigma_idx = self.feature_names.index('sigma_trap') if 'sigma_trap' in self.feature_names else None
            crush_idx = self.feature_names.index('lambda_crush') if 'lambda_crush' in self.feature_names else None
            for i in range(len(proba)):
                sigma_val = abs(X[i, sigma_idx]) if sigma_idx is not None and sigma_idx < X.shape[1] else 0.0
                crush_val = X[i, crush_idx] if crush_idx is not None and crush_idx < X.shape[1] else 1.0
                if sigma_val > threshold:
                    min_idx = np.argmin(proba[i])
                    proba[i, min_idx] *= upset_mult
                elif crush_val > 2.0:
                    max_idx = np.argmax(proba[i])
                    proba[i, max_idx] *= crush_mult
                proba[i] = proba[i] / proba[i].sum()
        except (ValueError, IndexError):
            pass
        return proba

    def _goal_diff_to_softmax(self, gd, temperature_scale=1.0, max_prob=0.65):
        """Ridge输出→三分类概率 (v2.5-opt-fix: temperature_scale可调，>1软化极端输出)
        max_prob: 硬性上限，任何类别概率不超过此值 (P0修复: 防止Ridge极端输出带偏Stacking)
        """
        abs_gd = np.abs(gd)
        base_temps = np.clip(0.8 + 0.6 * np.minimum(abs_gd, 2.0), 0.8, 2.0)
        # P0修复: 应用温度缩放，temperature_scale>1时软化极端概率
        temperatures = base_temps * temperature_scale
        draw_penalty = 0.3 + abs_gd * 0.15
        home_logit = gd / temperatures
        away_logit = -gd / temperatures
        draw_logit = -abs_gd / temperatures - draw_penalty * (1.0 / temperature_scale)
        logits = np.column_stack([home_logit, draw_logit, away_logit])
        logits = logits - logits.max(axis=1, keepdims=True)
        exp_logits = np.exp(logits)
        proba = exp_logits / exp_logits.sum(axis=1, keepdims=True)
        # P0修复: 硬性上限 — 任何类别超过max_prob就向其他类别分流
        max_proba = np.max(proba, axis=1)
        needs_clip = max_proba > max_prob
        if np.any(needs_clip):
            idx = np.where(needs_clip)[0]
            for i in idx:
                over = proba[i] - max_prob
                over_sum = np.sum(over[over > 0])
                if over_sum > 0:
                    # 把超出部分按比例分给其他类别
                    proba[i] = np.clip(proba[i], 0, max_prob)
                    deficit = over_sum / 2.0  # 平分给两个未超的类别
                    for c in range(3):
                        if proba[i, c] < max_prob:
                            proba[i, c] += deficit
                    proba[i] = proba[i] / proba[i].sum()
        return proba

    def _heuristic_predict_proba(self, X, elo_proba=None, poisson_proba=None):
        n = X.shape[0]
        proba = np.full((n, 3), 1.0 / 3)
        try:
            elo_cfg = self.config.get('models', {}).get('elo', {})
            poisson_cfg = self.config.get('models', {}).get('poisson', {})
            elo_weight = elo_cfg.get('heuristic_integration', {}).get('weight', 0.30) if elo_cfg.get('enabled', False) and elo_proba is not None else 0
            poisson_weight = poisson_cfg.get('heuristic_integration', {}).get('weight', 0.25) if poisson_cfg.get('enabled', False) and poisson_proba is not None else 0

            a1_idx = self.feature_names.index('a1') if 'a1' in self.feature_names else None
            a2_idx = self.feature_names.index('a2') if 'a2' in self.feature_names else None
            rank_idx = self.feature_names.index('rank_diff_factor') if 'rank_diff_factor' in self.feature_names else None
            power_gap_idx = self.feature_names.index('ix_power_gap') if 'ix_power_gap' in self.feature_names else None
            h2h_idx = self.feature_names.index('h2h_factor') if 'h2h_factor' in self.feature_names else None
            form_idx = self.feature_names.index('form_momentum') if 'form_momentum' in self.feature_names else None

            h = np.full(n, 0.33); d = np.full(n, 0.33); a = np.full(n, 0.33)
            if a1_idx is not None: h += X[:, a1_idx] * 0.4; a -= X[:, a1_idx] * 0.3
            if a2_idx is not None: h += (X[:, a2_idx] - 0.5) * 0.5
            if rank_idx is not None: h += X[:, rank_idx] * 0.0005; a -= X[:, rank_idx] * 0.0005
            if power_gap_idx is not None: d += np.maximum(0.0, 0.20 - X[:, power_gap_idx] * 0.5)
            if h2h_idx is not None: h += X[:, h2h_idx] * 0.15; a -= X[:, h2h_idx] * 0.10
            if form_idx is not None: h += X[:, form_idx] * 0.10; a -= X[:, form_idx] * 0.05

            h = np.maximum(h, 0.05); d = np.maximum(d, 0.05); a = np.maximum(a, 0.05)
            total = h + d + a
            proba[:, 0] = h / total; proba[:, 1] = d / total; proba[:, 2] = a / total
        except Exception as e:
            self.logger.warning(f"_heuristic_predict_proba 异常回退均匀分布: {type(e).__name__}: {e}")
            # proba 已在函数开头初始化为均匀分布

        if elo_proba is not None and elo_weight > 0:
            proba = (1.0 - elo_weight) * proba + elo_weight * elo_proba
            proba = proba / proba.sum(axis=1, keepdims=True)
        if poisson_proba is not None and poisson_weight > 0:
            proba = (1.0 - poisson_weight) * proba + poisson_weight * poisson_proba
            proba = proba / proba.sum(axis=1, keepdims=True)
        return proba

    # ══════════════════════════════════════════════════
    # 4. Stacking Meta-Learner (v2.5)
    # ══════════════════════════════════════════════════

    def _train_stacking_meta_learner(self, X_train, y_train, league_names_train=None):
        """v3.3: 训练Stacking meta-learner (XGB仅H/A二维剔除Draw噪声, NN替换Ridge)"""
        stack_cfg = self.config.get('models', {}).get('stacking', {})
        if not stack_cfg.get('enabled', True):
            self.logger.info("Stacking已禁用，使用加权平均")
            return

        # v3.1: 确保NN模型已加载，用于OOF预测
        nn_available = self.nn_model is not None
        if not nn_available:
            nn_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  'saved_models', 'football_nn_20260616_125617.pth')
            if os.path.exists(nn_path):
                nn_available = self.load_nn_model(nn_path)
        if not nn_available:
            self.logger.warning("NN模型不可用，meta-learner退化为5模型（不含NN）")

        self.logger.info("=" * 60)
        self.logger.info(f"训练 Stacking Meta-Learner (v3.3 XGB H/A only, NN={'✓' if nn_available else '✗'})")
        self.logger.info("=" * 60)

        n_folds = stack_cfg.get('cv_folds', 5)
        tscv = TimeSeriesSplit(n_splits=n_folds)

        # 收集OOF预测 + meta特征
        oof_probas = []
        oof_indices = []

        for fold_i, (train_idx, val_idx) in enumerate(tscv.split(X_train)):
            X_tr_fold = X_train[train_idx]
            X_val_fold = X_train[val_idx]
            y_tr_fold = y_train.iloc[train_idx]
            y_val_fold = y_train.iloc[val_idx]

            # 用完整参数训练fold模型 (v2.5-opt: 不再用简化参数)
            fold_lgb = self._train_lgb_fold_full(X_tr_fold, X_val_fold, y_tr_fold, y_val_fold)
            fold_xgb = self._train_xgb_fold_full(X_tr_fold, X_val_fold, y_tr_fold, y_val_fold)

            # 获取OOF概率
            proba_lgb = fold_lgb.predict_proba(X_val_fold) if fold_lgb else np.ones((len(X_val_fold), 3))/3
            proba_xgb = fold_xgb.predict_proba(X_val_fold) if fold_xgb else np.ones((len(X_val_fold), 3))/3

            # v3.1: NN OOF (用全量预训练模型，与heuristic/odds_expert同样策略)
            if nn_available:
                proba_nn_fold = self._nn_predict_proba(X_val_fold)
            else:
                proba_nn_fold = np.ones((len(X_val_fold), 3)) / 3

            # 启发式 OOF (v2.5-opt: 用真实启发式，不再用uniform)
            # 临时设置feature_names供启发式使用
            old_fn = self.feature_names
            proba_heur = self._heuristic_predict_proba(X_val_fold)
            self.feature_names = old_fn

            # Odds Expert OOF (用全量模型)
            proba_odds = self._odds_expert_predict_proba(X_val_fold)
            
            # P1修复: OddsExpert OOD检测 — 均匀输出时用核心模型均值替代
            oe_is_ood = np.max(proba_odds, axis=1) - np.min(proba_odds, axis=1) < 0.02
            if np.any(oe_is_ood):
                core_avg = (proba_lgb + proba_xgb + proba_heur) / 3.0
                fallback = 0.5 * core_avg + 0.5 * np.ones_like(proba_odds) / 3.0
                proba_odds[oe_is_ood] = fallback[oe_is_ood]

            # v3.1 Meta特征: lgb(3)+xgb(3)+heur(3)+odds(3)+nn(3)=15维 + key(3)+drift(3)=21维
            # 与 _predict_with_stacking 完全对齐
            key_feat_indices = []
            for fname in ['odds_confidence', 'match_evenness', 'imp_d_norm']:
                if fname in self.feature_names:
                    key_feat_indices.append(self.feature_names.index(fname))

            # P1修复: 添加drift特征到meta
            drift_feat_indices = []
            for fname in ['drift_magnitude', 'drift_direction', 'drift_d']:
                if fname in self.feature_names:
                    drift_feat_indices.append(self.feature_names.index(fname))

            meta_proba = np.hstack([proba_lgb, proba_xgb[:, [0, 2]], proba_heur, proba_odds, proba_nn_fold])

            # 添加key features到meta特征
            if key_feat_indices:
                key_feats = X_val_fold[:, key_feat_indices]
                meta_proba = np.hstack([meta_proba, key_feats])
            
            # 添加drift features到meta特征
            if drift_feat_indices:
                drift_feats = X_val_fold[:, drift_feat_indices]
                meta_proba = np.hstack([meta_proba, drift_feats])

            oof_probas.append(meta_proba)
            oof_indices.extend(val_idx.tolist())

            fold_acc = accuracy_score(y_val_fold, np.argmax(proba_lgb * 0.5 + proba_xgb * 0.5, axis=1))
            self.logger.info(f"  Fold {fold_i+1}/{n_folds}: OOF {len(meta_proba)} 样本, "
                           f"dim={meta_proba.shape[1]}, acc={fold_acc*100:.1f}%")

        # 合并所有OOF
        X_meta = np.vstack(oof_probas)
        # 确保y_meta对齐
        oof_indices = sorted(set(oof_indices))
        y_meta = y_train.iloc[oof_indices]

        # 训练meta-learner
        method = stack_cfg.get('method', 'logistic')
        use_cw = stack_cfg.get('use_class_weights', True)

        if use_cw:
            cw = compute_class_weight('balanced', classes=np.array([0,1,2]), y=y_meta)
            cw_dict = {i: w for i, w in enumerate(cw)}
        else:
            cw_dict = None

        # v2.5-opt: 用LightGBM作为meta-learner (比LR更强)
        if method == 'lightgbm' and LGB_AVAILABLE:
            self.meta_learner = lgb.LGBMClassifier(
                objective='multiclass', num_class=3,
                n_estimators=200, num_leaves=15, max_depth=4,
                learning_rate=0.05, subsample=0.8, colsample_bytree=0.8,
                min_child_samples=30, reg_alpha=0.3, reg_lambda=1.5,
                class_weight=cw_dict,
                random_state=42, n_jobs=-1, verbose=-1,
            )
            self.meta_learner.fit(X_meta, y_meta)
        elif method == 'logistic':
            self.meta_learner = LogisticRegression(
                C=0.5, max_iter=2000, random_state=42,
                class_weight=cw_dict, solver='lbfgs'
            )
            self.meta_learner.fit(X_meta, y_meta)
        else:
            # Default: LightGBM if available, else LR
            if LGB_AVAILABLE:
                self.meta_learner = lgb.LGBMClassifier(
                    objective='multiclass', num_class=3,
                    n_estimators=200, num_leaves=15, max_depth=4,
                    learning_rate=0.05, subsample=0.8, colsample_bytree=0.8,
                    min_child_samples=30, reg_alpha=0.3, reg_lambda=1.5,
                    class_weight=cw_dict,
                    random_state=42, n_jobs=-1, verbose=-1,
                )
                self.meta_learner.fit(X_meta, y_meta)
            else:
                self.meta_learner = LogisticRegression(
                    C=0.5, max_iter=2000, random_state=42,
                    class_weight=cw_dict
                )
                self.meta_learner.fit(X_meta, y_meta)

        # 评估meta-learner
        meta_proba = self.meta_learner.predict_proba(X_meta)
        meta_acc = accuracy_score(y_meta, np.argmax(meta_proba, axis=1))
        self.logger.info(f"Meta-learner OOF准确率: {meta_acc*100:.2f}%")

        # Per-class评估
        for cls_idx, cls_name in enumerate(['H', 'D', 'A']):
            mask = y_meta.values == cls_idx
            if mask.sum() > 0:
                cls_acc = accuracy_score(y_meta[mask], np.argmax(meta_proba[mask], axis=1))
                self.logger.info(f"  {cls_name}: OOF acc={cls_acc*100:.1f}% ({mask.sum()} 样本)")

    def _train_lgb_fold_full(self, X_tr, X_val, y_tr, y_val):
        """v2.5-opt: 用完整参数训练fold LGB (不再用简化参数)"""
        if not LGB_AVAILABLE:
            return None
        lgb_cfg = self.config['models'].get('lightgbm', {})
        classes = np.array([0, 1, 2])
        bw = compute_class_weight('balanced', classes=classes, y=y_tr)
        bw[1] *= lgb_cfg.get('draw_weight_boost', 1.2)
        sw = np.array([bw[int(c)] for c in y_tr])
        params = {
            'objective': 'multiclass', 'num_class': 3, 'metric': 'multi_logloss',
            'num_leaves': lgb_cfg.get('num_leaves', 63),
            'max_depth': lgb_cfg.get('max_depth', 7),
            'learning_rate': lgb_cfg.get('learning_rate', 0.03),
            'n_estimators': lgb_cfg.get('n_estimators', 800),
            'subsample': lgb_cfg.get('subsample', 0.8), 'subsample_freq': 1,
            'colsample_bytree': lgb_cfg.get('colsample_bytree', 0.8),
            'min_child_samples': lgb_cfg.get('min_child_samples', 20),
            'reg_alpha': lgb_cfg.get('reg_alpha', 0.1),
            'reg_lambda': lgb_cfg.get('reg_lambda', 1.0),
            'random_state': 42, 'n_jobs': -1, 'verbose': -1,
        }
        model = lgb.LGBMClassifier(**params)
        model.fit(X_tr, y_tr, sample_weight=sw,
                  eval_set=[(X_val, y_val)],
                  callbacks=[lgb.early_stopping(50, verbose=False), lgb.log_evaluation(0)])
        return model

    def _train_xgb_fold_full(self, X_tr, X_val, y_tr, y_val):
        """v2.5-opt: 用完整参数训练fold XGB"""
        if not XGB_AVAILABLE:
            return None
        xgb_cfg = self.config['models'].get('xgboost', {})
        params = {
            'objective': 'multi:softprob', 'num_class': 3,
            'eval_metric': ['mlogloss'], 'random_state': 42,
            'verbosity': 0, 'n_jobs': -1,
            'n_estimators': xgb_cfg.get('n_estimators', 500),
            'max_depth': xgb_cfg.get('max_depth', 5),
            'learning_rate': xgb_cfg.get('learning_rate', 0.03),
            'subsample': xgb_cfg.get('subsample', 0.8),
            'colsample_bytree': xgb_cfg.get('colsample_bytree', 0.8),
            'early_stopping_rounds': 50, 'tree_method': 'hist',
        }
        model = xgb.XGBClassifier(**params)
        model.fit(X_tr, y_tr, eval_set=[(X_val, y_val)], verbose=False)
        return model

    # ══════════════════════════════════════════════════
    # 5. Value Betting (v2.5)
    # ══════════════════════════════════════════════════

    def calculate_value_bet(self, proba: np.ndarray, odds: np.ndarray,
                             bankroll: float = 1000.0,
                             default_ratio: float = 0.0) -> List[Dict]:
        """
        v2.5-opt-fix: 计算Value Bet (Kelly Criterion + edge上限 + 置信度惩罚)
        
        Args:
            proba: (n, 3) 模型概率矩阵 [H, D, A]
            odds: (n, 3) 赔率矩阵 [H, D, A]
            bankroll: 当前资金
            default_ratio: 特征默认值比例 (0-1), >0.5时惩罚Kelly
        
        Returns:
            价值投注列表
        """
        vb_cfg = self.config.get('value_betting', {})
        if not vb_cfg.get('enabled', True):
            return []

        kelly_fraction = vb_cfg.get('kelly_fraction', 0.25)
        min_edge = vb_cfg.get('min_edge', 0.05)
        max_stake = vb_cfg.get('max_stake', 0.10)
        value_threshold = vb_cfg.get('value_threshold', 1.05)
        # P1修复: edge上限 — 超过此值的edge视为模型偏差而非真实价值
        max_edge = vb_cfg.get('max_edge', 0.20)

        bets = []
        labels = ['H', 'D', 'A']

        for i in range(len(proba)):
            for j in range(3):
                p_model = proba[i, j]
                o = odds[i, j]
                if o <= 1.0:
                    continue

                # 隐含概率
                p_implied = 1.0 / o

                # 价值检测
                value_ratio = p_model / p_implied if p_implied > 0 else 0

                if value_ratio < value_threshold:
                    continue

                # Kelly Criterion: f = (b*p - q) / b, b=odds-1, q=1-p
                b = o - 1.0
                q = 1.0 - p_model
                kelly_full = (b * p_model - q) / b if b > 0 else 0

                # 分数Kelly (减少方差)
                kelly_stake = kelly_full * kelly_fraction

                # 边缘检查
                edge = p_model - p_implied
                
                # P1修复: edge上限惩罚 — edge>max_edge时逐步降低Kelly
                if edge > max_edge:
                    # 超出部分按二次衰减惩罚
                    excess = edge - max_edge
                    penalty = max(0.1, 1.0 - excess * 3.0)  # edge=0.23→penalty=0.91, edge=0.35→penalty=0.55
                    kelly_stake *= penalty
                    # 同时将edge报告值截断
                    reported_edge = max_edge
                else:
                    reported_edge = edge
                    
                if reported_edge < min_edge:
                    continue

                # P1修复: 高默认值场景Kelly惩罚
                if default_ratio > 0.50:
                    # 默认值>50%时，按比例缩减Kelly (76%默认→缩减到原来的40%)
                    confidence_penalty = max(0.2, 1.0 - default_ratio)
                    kelly_stake *= confidence_penalty

                # 限制最大投注
                kelly_stake = min(kelly_stake, max_stake)
                stake_amount = max(0, kelly_stake * bankroll)

                if stake_amount > 0:
                    bets.append({
                        'match_idx': i,
                        'direction': labels[j],
                        'model_prob': round(float(p_model), 4),
                        'implied_prob': round(float(p_implied), 4),
                        'edge': round(float(reported_edge), 4),
                        'raw_edge': round(float(edge), 4),
                        'value_ratio': round(float(value_ratio), 4),
                        'odds': round(float(o), 2),
                        'kelly_stake_pct': round(float(kelly_stake * 100), 2),
                        'stake_amount': round(float(stake_amount), 2),
                        'expected_roi': round(float(reported_edge * o), 4),
                        'default_ratio': round(float(default_ratio), 4),
                    })

        return bets

    # ══════════════════════════════════════════════════
    # 6. 完整训练流程 v2.5
    # ══════════════════════════════════════════════════

    def train(self, df: pd.DataFrame = None) -> Dict[str, Any]:
        """
        v2.5 完整训练流程：
        1. 加载数据
        2. 特征预处理 (含死特征清理+漂移特征+缺失指示器)
        3. 5-fold时序CV
        4. 训练 Odds Expert (312K)
        5. 训练 LightGBM + XGBoost + Ridge
        6. CalibratorSuite自动概率校准
        7. Stacking meta-learner训练
        8. 阈值优化
        9. 集成评估 (含Value Betting评估)
        10. 保存模型管道 + 注册表
        """
        self.logger.info("=" * 70)
        self.logger.info("  哨响AI 集成模型训练 v2.5 (312K+Stacking+ValueBet+LeagueAware)")
        self.logger.info(f"  开始时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}")
        self.logger.info("=" * 70)

        # 1. 数据准备
        if df is None:
            df = self.load_training_data()
        if len(df) < 100:
            raise ValueError(f"训练数据不足 ({len(df)} 条)")

        # 1.5 P1: 赔率逆向校准 (xG + 贝叶斯参数) — 一站式开关
        if self.config.get('odds_calibration', {}).get('enabled', False):
            self.logger.info(f"\n{'='*60}")
            self.logger.info(f"  赔率逆向校准 (xG + 贝叶斯参数)")
            self.logger.info(f"{'='*60}")
            try:
                self._run_odds_calibration()
                self.logger.info("  ✓ 赔率逆向校准完成")
            except Exception as e:
                self.logger.warning(f"  ⚠ 校准失败: {e}, 回退原生xG")

        X, y = self.prepare_features(df)

        # 2. v2.5: 训练Odds Expert (312K)
        oe_info = {}
        if LGB_AVAILABLE:
            oe_info = self.train_odds_expert() or {}

        # 3. 时序CV
        n_cv_splits = self.config['data'].get('cv_splits', 5)
        cv_mean, cv_std = None, None
        if n_cv_splits >= 2 and len(X) >= 500:
            self.logger.info(f"\n{'='*60}")
            self.logger.info(f"  5-fold 时序交叉验证")
            self.logger.info(f"{'='*60}")
            tscv = TimeSeriesSplit(n_splits=n_cv_splits)
            cv_scores = []
            for fold_i, (train_idx, test_idx) in enumerate(tscv.split(X)):
                X_tr_fold = X.iloc[train_idx]; X_te_fold = X.iloc[test_idx]
                y_tr_fold = y.iloc[train_idx]; y_te_fold = y.iloc[test_idx]
                fold_scaler = StandardScaler()
                X_tr_s = fold_scaler.fit_transform(X_tr_fold)
                X_te_s = fold_scaler.transform(X_te_fold)
                fold_lgb = self._train_lgb_fold_full(X_tr_s, X_te_s, y_tr_fold, y_te_fold)
                fold_xgb = self._train_xgb_fold_full(X_tr_s, X_te_s, y_tr_fold, y_te_fold)
                p_lgb = fold_lgb.predict_proba(X_te_s) if fold_lgb else np.ones((len(X_te_s),3))/3
                p_xgb = fold_xgb.predict_proba(X_te_s) if fold_xgb else np.ones((len(X_te_s),3))/3
                p_ens = 0.5 * p_lgb + 0.5 * p_xgb
                cv_scores.append(accuracy_score(y_te_fold, np.argmax(p_ens, axis=1)))
                self.logger.info(f"  Fold {fold_i+1}/{n_cv_splits}: acc={cv_scores[-1]*100:.2f}%")
            cv_mean = np.mean(cv_scores); cv_std = np.std(cv_scores)
            self.logger.info(f"  CV结果: {cv_mean*100:.2f}% ± {cv_std*100:.2f}%")

        # 4. 最终训练 (支持时序切分，对齐v3.2/v7实验)
        if self.config['data'].get('time_based_split', False):
            cutoff = pd.Timestamp(self.config['data'].get('train_cutoff', '2023-01-01'))
            train_mask = self.meta['match_date'] < cutoff
            test_mask = ~train_mask
            X_train_df, X_test_df = X[train_mask.values], X[test_mask.values]
            y_train, y_test = y[train_mask.values], y[test_mask.values]
            meta_train = self.meta[train_mask.values].copy()
            meta_test = self.meta[test_mask.values].copy()
        else:
            split_idx = int(len(X) * (1.0 - self.config['data']['test_ratio']))
            X_train_df, X_test_df = X.iloc[:split_idx], X.iloc[split_idx:]
            y_train, y_test = y.iloc[:split_idx], y.iloc[split_idx:]
            meta_test = self.meta.iloc[split_idx:].copy()
            meta_train = self.meta.iloc[:split_idx].copy()

        self.logger.info(f"\n最终训练: 训练集 {len(X_train_df)} | 测试集 {len(X_test_df)}")

        # 5. 标准化
        self.scaler = StandardScaler()
        X_train_scaled = self.scaler.fit_transform(X_train_df)
        X_test_scaled = self.scaler.transform(X_test_df)

        # 6. 训练Ridge
        y_train_gd = meta_train['home_score'] - meta_train['away_score']
        self.train_ridge(X_train_scaled, y_train_gd.values)

        # 7. 训练XGBoost + LightGBM
        val_split = int(len(X_train_scaled) * 0.85)
        X_tr, X_val = X_train_scaled[:val_split], X_train_scaled[val_split:]
        y_tr, y_val = y_train.iloc[:val_split], y_train.iloc[val_split:]
        xgb_info = self.train_xgboost(X_tr, X_val, y_tr, y_val)
        lgb_info = {}
        if LGB_AVAILABLE:
            lgb_info = self.train_lightgbm(X_tr, X_val, y_tr, y_val) or {}

        # 8. CalibratorSuite (v2.6-fix: 尊重config开关)
        if self.config.get('models', {}).get('calibration', {}).get('enabled', True):
            self._fit_auto_calibration(X_train_scaled, y_train)
        else:
            self.logger.info("CalibratorSuite: 已禁用 (config: calibration.enabled=false)")
            self.calibrator_suite = None

        # 9. v2.5-opt: Stacking meta-learner (传入league_names)
        league_names_train = meta_train['league_name'].tolist() if 'league_name' in meta_train.columns else None
        self._train_stacking_meta_learner(X_train_scaled, y_train, league_names_train)

        # 10. 阈值优化
        self.optimal_thresholds = None
        th_cfg = self.config['models'].get('threshold_optimization', {})
        if th_cfg.get('enabled', True):
            val_split_th = int(len(X_train_scaled) * 0.85)
            X_th_val = X_train_scaled[val_split_th:]
            y_th_val = y_train.iloc[val_split_th:]
            league_names_th = meta_train.iloc[val_split_th:]['league_name'].tolist()
            proba_val = self.ensemble_predict_proba(X_th_val, league_names=league_names_th)
            threshold_result = self.optimize_thresholds(proba_val, y_th_val.values)
            self.optimal_thresholds = threshold_result['thresholds']

        # 11. 集成评估
        self.logger.info("\n" + "=" * 60)
        self.logger.info("集成模型评估 (v2.5)")
        self.logger.info("=" * 60)
        league_names_test = meta_test['league_name'].tolist() if 'league_name' in meta_test.columns else None
        eval_metrics = self._evaluate_ensemble(X_test_scaled, y_test, meta_test, league_names_test)
        self.eval_metrics = eval_metrics

        if cv_mean is not None:
            eval_metrics['cv_mean_accuracy'] = round(cv_mean * 100, 2)
            eval_metrics['cv_std_accuracy'] = round(cv_std * 100, 2)

        # 12. v2.5: Value Betting评估
        vb_metrics = self._evaluate_value_betting(X_test_scaled, meta_test)
        if vb_metrics:
            eval_metrics['value_betting'] = vb_metrics

        # 13. 保存 (消融实验模式跳过)
        ablation_mode = self.config.get('ablation_mode', False)
        if ablation_mode:
            pipeline_path = None
            self.logger.info("消融实验模式: 跳过模型保存和注册")
        else:
            pipeline_path = self.save_pipeline(xgb_info, lgb_info, oe_info)
            # 14. v2.5: 注册模型
            self._register_model(eval_metrics)

        return {
            'pipeline_path': pipeline_path,
            'evaluation': eval_metrics,
            'feature_names': self.feature_names,
            'n_features': len(self.feature_names),
            'n_samples': len(df),
            'cv_mean_accuracy': round(cv_mean * 100, 2) if cv_mean else None,
            'optimal_thresholds': self.optimal_thresholds,
        }

    def _evaluate_value_betting(self, X_test, meta_test):
        """v2.5-opt: 评估Value Betting表现 (使用真实赔率)"""
        vb_cfg = self.config.get('value_betting', {})
        if not vb_cfg.get('enabled', True):
            return None

        import sqlite3
        proba = self.ensemble_predict_proba(X_test)

        # v2.5-opt: 优先从特征中的real_odds获取，其次从数据库批量查询
        odds_matrix = np.zeros((len(proba), 3))
        odds_filled = 0

        # 方法1: 从特征矩阵中提取real_odds
        real_odds_indices = {}
        for fname in ['real_home_odds', 'real_draw_odds', 'real_away_odds']:
            if fname in self.feature_names:
                real_odds_indices[fname] = self.feature_names.index(fname)

        if len(real_odds_indices) == 3:
            h_idx = real_odds_indices['real_home_odds']
            d_idx = real_odds_indices['real_draw_odds']
            a_idx = real_odds_indices['real_away_odds']
            for i in range(len(proba)):
                h, d, a = X_test[i, h_idx], X_test[i, d_idx], X_test[i, a_idx]
                if h > 0 and d > 0 and a > 0:
                    odds_matrix[i] = [h, d, a]
                    odds_filled += 1

        # 方法2: 数据库批量查询补充
        if odds_filled < len(proba) * 0.5:
            try:
                conn = sqlite3.connect(self.db_path)
                if 'match_id' in meta_test.columns:
                    match_ids = meta_test['match_id'].values.tolist()
                    placeholders = ','.join(['?'] * len(match_ids))
                    cur = conn.execute(f"""
                        SELECT match_id, AVG(home_odds), AVG(draw_odds), AVG(away_odds)
                        FROM odds WHERE match_id IN ({placeholders}) AND home_odds > 0
                        GROUP BY match_id
                    """, match_ids)
                    db_odds = {row[0]: (row[1], row[2], row[3]) for row in cur.fetchall()}
                    conn.close()

                    for i, mid in enumerate(match_ids):
                        if odds_matrix[i, 0] == 0 and mid in db_odds:
                            h, d, a = db_odds[mid]
                            if h > 0 and d > 0 and a > 0:
                                odds_matrix[i] = [h, d, a]
                                odds_filled += 1
            except Exception as e:
                self.logger.debug(f"赔率回填跳过某行: {type(e).__name__}")

        # Fallback: 如果没有真实赔率，用隐含概率反推
        if odds_filled < len(proba) * 0.3:
            overround = 0.05
            for j in range(3):
                p = np.clip(proba[:, j], 0.05, 0.95)
                odds_matrix[:, j] = 1.0 / (p * (1 + overround))

        # 计算价值投注 (训练评估时不惩罚default_ratio，用0.0)
        bets = self.calculate_value_bet(proba, odds_matrix, default_ratio=0.0)
        if not bets:
            return {'total_bets': 0, 'roi': 0, 'real_odds_used': odds_filled}

        # 计算实际ROI
        correct = 0; total_stake = 0; total_return = 0
        for bet in bets:
            idx = bet['match_idx']
            direction = bet['direction']
            stake = bet['stake_amount']
            odds = bet['odds']
            actual = 0 if meta_test.iloc[idx]['home_score'] > meta_test.iloc[idx]['away_score'] else \
                     (2 if meta_test.iloc[idx]['home_score'] < meta_test.iloc[idx]['away_score'] else 1)
            pred_cls = {'H': 0, 'D': 1, 'A': 2}[direction]
            total_stake += stake
            if actual == pred_cls:
                total_return += stake * odds
                correct += 1

        roi = (total_return - total_stake) / total_stake * 100 if total_stake > 0 else 0
        win_rate = correct / len(bets) * 100 if bets else 0

        self.logger.info(f"Value Betting: {len(bets)} 注, 胜率={win_rate:.1f}%, ROI={roi:.1f}%, 真实赔率={odds_filled}场")
        return {
            'total_bets': len(bets),
            'win_rate': round(win_rate, 2),
            'roi_pct': round(roi, 2),
            'total_stake': round(total_stake, 2),
            'total_return': round(total_return, 2),
            'real_odds_used': odds_filled,
        }

    def _fit_auto_calibration(self, X_train, y_train):
        try:
            from optimize.calibration import CalibratorSuite
        except ImportError:
            self.logger.warning("CalibratorSuite 导入失败")
            return

        val_split = int(len(X_train) * 0.85)
        X_calib_val = X_train[val_split:]
        y_calib_val = y_train.iloc[val_split:]
        old_calib = self.calibrator_suite
        self.calibrator_suite = None
        raw_proba = self.ensemble_predict_proba(X_calib_val)
        self.calibrator_suite = old_calib

        suite = CalibratorSuite(methods=['platt', 'isotonic', 'beta', 'temperature'])
        suite.fit(y_calib_val.values, raw_proba)
        best = suite.best_method(metric='ece')
        self.logger.info(f"CalibratorSuite: 最优={best}")
        self.calibrator_suite = suite

    def optimize_thresholds(self, proba, y_val):
        best_acc = 0; best_thresholds = (0.0, 0.0, 0.0)
        th_cfg = self.config['models'].get('threshold_optimization', {})
        search = {
            'th_h': np.arange(th_cfg.get('th_h_min', -0.03), th_cfg.get('th_h_max', 0.04), th_cfg.get('th_h_step', 0.01)),
            'th_d': np.arange(th_cfg.get('th_d_min', -0.30), th_cfg.get('th_d_max', 0.00), th_cfg.get('th_d_step', 0.01)),
            'th_a': np.arange(th_cfg.get('th_a_min', -0.05), th_cfg.get('th_a_max', 0.04), th_cfg.get('th_a_step', 0.01)),
        }
        for th_h in search['th_h']:
            for th_d in search['th_d']:
                for th_a in search['th_a']:
                    preds = self._predict_with_thresholds(proba, (th_h, th_d, th_a))
                    acc = accuracy_score(y_val, preds)
                    if acc > best_acc:
                        best_acc = acc; best_thresholds = (float(th_h), float(th_d), float(th_a))
        self.logger.info(f"最优阈值: th_h={best_thresholds[0]:.2f}, th_d={best_thresholds[1]:.2f}, th_a={best_thresholds[2]:.2f}, acc={best_acc*100:.2f}%")
        return {'thresholds': best_thresholds, 'val_accuracy': round(best_acc * 100, 2)}

    @staticmethod
    def _predict_with_thresholds(proba, thresholds):
        th_h, th_d, th_a = thresholds
        n = proba.shape[0]
        preds = np.zeros(n, dtype=int)
        for i in range(n):
            scores = np.array([proba[i,0]-th_h, proba[i,1]-th_d, proba[i,2]-th_a])
            preds[i] = np.argmax(scores)
        return preds

    def _evaluate_ensemble(self, X_test, y_test, meta_test, league_names=None):
        proba = self.ensemble_predict_proba(X_test, league_names=league_names)
        
        # v2.6-fix: 使用最优阈值而非裸argmax
        if self.optimal_thresholds is not None:
            th_h, th_d, th_a = self.optimal_thresholds
            y_pred = self._predict_with_thresholds(proba, (th_h, th_d, th_a))
        else:
            y_pred = np.argmax(proba, axis=1)

        acc = accuracy_score(y_test, y_pred)
        try:
            y_onehot = np.zeros((len(y_test), 3))
            for i, c in enumerate(y_test): y_onehot[i, int(c)] = 1
            auc = roc_auc_score(y_onehot, proba, multi_class='ovr', average='macro')
        except: auc = 0.0
        mcc = matthews_corrcoef(y_test, y_pred)
        ll = log_loss(y_test, proba)

        report = classification_report(y_test, y_pred, target_names=['H','D','A'], output_dict=True, zero_division=0)

        self.logger.info(f"  准确率: {acc*100:.2f}%  AUC: {auc:.4f}  MCC: {mcc:.4f}  LogLoss: {ll:.4f}")
        self.logger.info(f"  D召回率: {report['D']['recall']*100:.1f}%  D精确率: {report['D']['precision']*100:.1f}%")

        # 联赛级别评估
        league_metrics = {}
        if 'league_name' in meta_test.columns:
            for league in meta_test['league_name'].unique():
                mask = (meta_test['league_name'] == league).values
                if mask.sum() >= 10:
                    league_metrics[league] = {
                        'accuracy': round(accuracy_score(y_test[mask], y_pred[mask]) * 100, 2),
                        'count': int(mask.sum())
                    }

        return {
            'accuracy': round(acc * 100, 2), 'auc_macro': round(auc, 4),
            'mcc': round(mcc, 4), 'log_loss': round(ll, 4),
            'per_class': {cls: {'precision': round(report[cls]['precision'], 4),
                                'recall': round(report[cls]['recall'], 4),
                                'f1': round(report[cls]['f1-score'], 4)}
                         for cls in ['H', 'D', 'A']},
            'by_league': league_metrics,
            'test_samples': len(y_test),
            'version': '2.5',
        }

    # ══════════════════════════════════════════════════
    # 7. 模型持久化 + 注册表 (v2.5)
    # ══════════════════════════════════════════════════

    def save_pipeline(self, xgb_info=None, lgb_info=None, oe_info=None, nn_info=None,
                      eval_metrics=None, save_path=None):
        if save_path:
            filepath = save_path
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
        else:
            timestamp = datetime.now(timezone.utc).strftime(self.config['output']['timestamp_format'])
            prefix = self.config['output']['model_prefix']
            filename = f"{prefix}_{timestamp}.joblib"
            filepath = os.path.join(self._model_dir, filename)

        # v3.1: NN模型用state_dict序列化（避免动态导入类的pickle问题）
        nn_state = None
        if self.nn_model is not None:
            try:
                import torch
                nn_state = {k: v.cpu().clone() for k, v in self.nn_model.state_dict().items()}
            except Exception as e:
                self.logger.warning("NN模型序列化失败: %s", e)

        pipeline = {
            'xgb_model': self.xgb_model,
            'lgb_model': self.lgb_model,
            'odds_expert_model': self.odds_expert_model,  # v2.5
            'nn_state_dict': nn_state,                    # v3.1: NN权重(可pickle)
            'draw_expert_model': self.draw_expert_model,  # v4.0: Draw专精二分类器
            'scaler': self.scaler,
            'odds_scaler': self.odds_scaler,  # v2.5
            'feature_names': self.feature_names,
            'odds_feature_names': self.odds_feature_names,  # v2.5
            'config': self.config,
            'eval_metrics': eval_metrics if eval_metrics is not None else self.eval_metrics,
            'calibrator_suite': self.calibrator_suite,
            'meta_learner': self.meta_learner,  # v2.5
            'league_d_rates': self.league_d_rates,  # v2.5
            'global_d_rate': getattr(self, 'global_d_rate', 0.257),  # v2.5
            'train_timestamp': datetime.now(timezone.utc).isoformat(),
            'version': self.model_version if hasattr(self, 'model_version') else '3.2',
            'xgb_info': xgb_info or {},
            'lgb_info': lgb_info or {},
            'odds_expert_info': oe_info or {},  # v2.5
            'nn_info': nn_info or {},            # v3.1
            'optimal_thresholds': getattr(self, 'optimal_thresholds', None),
            # ── v3.2 元数据别名（兼容外部读取）──
            'model_version': self.model_version if hasattr(self, 'model_version') else '3.2',
            'sub_models': {
                'xgb': self.xgb_model is not None,
                'lgb': self.lgb_model is not None,
                'odds_expert': self.odds_expert_model is not None,
                'ridge': hasattr(self, 'ridge_model') and self.ridge_model is not None,
                'heuristic': True,  # HeuristicPredictor 是规则，不算子模型文件
                'neural_net': self.nn_model is not None,
                'draw_expert': self.draw_expert_model is not None,  # v4.0
            },
            'meta_model': self.meta_learner,  # 别名，与 meta_learner 同值
        }

        joblib.dump(pipeline, filepath, compress=3)
        file_size = os.path.getsize(filepath) / (1024 * 1024)
        self.logger.info(f"\n模型管道已保存: {filepath} ({file_size:.1f} MB)")
        return filepath

    def _register_model(self, eval_metrics):
        """v3.2: 注册模型到registry (版本号从self.model_version动态读取)"""
        reg_cfg = self.config.get('model_registry', {})
        if not reg_cfg.get('enabled', True):
            return

        registry_path = reg_cfg.get('path', 'model_registry.json')
        if not os.path.isabs(registry_path):
            registry_path = os.path.join(self._model_dir, os.path.basename(registry_path))
        try:
            if os.path.exists(registry_path):
                with open(registry_path, 'r') as f:
                    registry = json.load(f)
            else:
                registry = {'versions': []}

            # 安全转换 numpy 类型为原生 Python 类型（防止 JSON 序列化失败）
            def _safe_float(v):
                if v is None:
                    return None
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None

            entry = {
                'version': getattr(self, 'model_version', '3.2'),
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'accuracy': _safe_float(eval_metrics.get('accuracy')),
                'auc': _safe_float(eval_metrics.get('auc_macro')),
                'mcc': _safe_float(eval_metrics.get('mcc')),
                'draw_f1': _safe_float(eval_metrics.get('per_class', {}).get('D', {}).get('f1')),
                'n_features': int(len(self.feature_names)),
                'models': ['lightgbm', 'xgboost', 'odds_expert', 'heuristic', 'neural_net'],
                'stacking': self.meta_learner is not None,
                'nn_integrated': self.nn_model is not None,
            }
            registry['versions'].append(entry)

            # 保留最近5个版本
            max_v = int(self.config.get('auto_pipeline', {}).get('registry', {}).get('max_versions', 5))
            registry['versions'] = registry['versions'][-max_v:]
            registry['current'] = entry

            with open(registry_path, 'w') as f:
                json.dump(registry, f, indent=2, ensure_ascii=False)
            self.logger.info(f"模型注册表已更新: {registry_path}")
        except (Exception, KeyError, IndexError, IOError, FileNotFoundError, requests.exceptions.RequestException) as e:
            self.logger.warning(f"注册表更新失败: {e}")

    @classmethod
    def load_pipeline(cls, filepath: str) -> 'EnsembleTrainer':
        logger = logging.getLogger('EnsembleTrainer')
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"模型文件不存在: {filepath}")
        pipeline = joblib.load(filepath)

        trainer = cls.__new__(cls)
        trainer.logger = logger
        trainer.xgb_model = pipeline['xgb_model']
        trainer.lgb_model = pipeline.get('lgb_model')
        trainer.odds_expert_model = pipeline.get('odds_expert_model')  # v2.5
        trainer.draw_expert_model = pipeline.get('draw_expert_model')  # v4.0
        
        # v3.1: 从state_dict恢复NN模型（兼容旧格式nn_model）
        trainer.nn_model = None
        nn_state = pipeline.get('nn_state_dict')
        if nn_state is not None and TORCH_AVAILABLE:
            try:
                import importlib.util
                nn_script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         'scripts', 'train_neural_net.py')
                spec = importlib.util.spec_from_file_location('train_neural_net', nn_script)
                nn_module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(nn_module)
                FootballNN = nn_module.FootballNN
                input_dim = len(pipeline['feature_names'])
                model = FootballNN(input_dim=input_dim)
                model.load_state_dict(nn_state)
                model.eval()
                trainer.nn_model = model
            except Exception as e:
                logger.warning(f"NN state_dict加载失败: {e}")
        elif pipeline.get('nn_model') is not None:
            # 兼容旧格式（含完整nn_model对象）
            trainer.nn_model = pipeline['nn_model']
        
        trainer.nn_scaler = pipeline.get('nn_scaler')                  # v3.1
        trainer.scaler = pipeline['scaler']
        trainer.odds_scaler = pipeline.get('odds_scaler')  # v2.5
        trainer.feature_names = pipeline['feature_names']
        trainer.odds_feature_names = pipeline.get('odds_feature_names', [])  # v2.5
        # P1修复: 截断 odds_feature_names 到 OddsExpert 实际训练特征数
        if (trainer.odds_expert_model is not None and 
            hasattr(trainer.odds_expert_model, 'n_features_in_') and
            len(trainer.odds_feature_names) != trainer.odds_expert_model.n_features_in_):
            n_expected = trainer.odds_expert_model.n_features_in_
            logger.warning(
                f"OddsExpert 特征名不匹配: 存储{len(trainer.odds_feature_names)} vs 模型期望{n_expected}, "
                f"截断为前{n_expected}个"
            )
            trainer.odds_feature_names = trainer.odds_feature_names[:n_expected]
        trainer.eval_metrics = pipeline.get('eval_metrics', {})
        trainer.calibrator_suite = pipeline.get('calibrator_suite')
        # ── v3.2 元数据别名（向后兼容）──
        trainer.meta_learner = pipeline.get('meta_model') or pipeline.get('meta_learner')
        # v3.2: model_version 优先从 model_version 字段读取，兼容旧 version 字段
        trainer.model_version = pipeline.get('model_version') or pipeline.get('version', '3.2')
        trainer.sub_models = pipeline.get('sub_models', {})
        trainer.league_d_rates = pipeline.get('league_d_rates', {})
        trainer.global_d_rate = pipeline.get('global_d_rate', 0.257)
        trainer.optimal_thresholds = pipeline.get('optimal_thresholds')
        trainer.pipeline = pipeline

        if 'config' in pipeline:
            trainer.config = pipeline['config']
            trainer.config_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(filepath))), 'config.yaml')
        else:
            config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.yaml')
            with open(config_path, 'r', encoding='utf-8') as f:
                trainer.config = yaml.safe_load(f)
            trainer.config_path = config_path
        trainer._init_paths()

        trainer.ridge_model = pipeline.get('ridge_model')  # v2.5 Ridge可能不存在
        nn_has = trainer.nn_model is not None or pipeline.get('nn_state_dict') is not None
        n_models = sum(1 for m in [trainer.lgb_model, trainer.xgb_model, trainer.ridge_model,
                                    trainer.odds_expert_model] if m is not None) + (1 if nn_has else 0)
        stacking = "Stacking" if trainer.meta_learner else "WeightedAvg"
        nn_status = "NN✓" if nn_has else "NN✗"
        logger.info(f"模型加载成功 | v{trainer.model_version} | {n_models}模型 | {stacking} | {nn_status} | "
                    f"{len(trainer.feature_names)}特征")
        return trainer

    # ══════════════════════════════════════════════════
    # 8. 单场预测 (v3.2: 含Stacking + Value Betting)
    # ══════════════════════════════════════════════════

    def predict_match(self, features: Dict[str, float],
                       context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        feat_array = np.zeros(len(self.feature_names))
        default_vals = self.config['data']['default_values']
        
        # P1修复: 计算特征默认值比例
        default_count = 0
        for i, name in enumerate(self.feature_names):
            default_val = default_vals.get(name, 0.0)
            feat_val = features.get(name, default_val)
            feat_array[i] = feat_val
            if abs(feat_val - default_val) < 1e-6:
                default_count += 1
        default_ratio = default_count / len(self.feature_names)

        X = self.scaler.transform([feat_array])
        league_name = context.get('league_name', None) if context else None
        league_names = [league_name] if league_name else None

        proba = self.ensemble_predict_proba(X, league_names=league_names,
                                             raw_features_list=[features])[0]

        # P1修复: 低置信度模式 — 高默认值时压缩极端概率
        if default_ratio > 0.50:
            # 压缩极值: 向均匀分布靠拢 (不确定性增大)
            # 66%默认→shrink=0.24, 向均匀收缩24%
            shrink = min(0.4, (default_ratio - 0.50) * 1.2)
            uniform = np.array([1/3, 1/3, 1/3])
            proba = (1 - shrink) * proba + shrink * uniform
            proba = proba / proba.sum()

        pred_idx = int(np.argmax(proba))
        labels = {0: 'H', 1: 'D', 2: 'A'}

        result = {
            'home_prob': round(float(proba[0]), 4),
            'draw_prob': round(float(proba[1]), 4),
            'away_prob': round(float(proba[2]), 4),
            'prediction': labels[pred_idx],
            'confidence': float(proba[pred_idx]),
            'default_ratio': round(float(default_ratio), 4),
            'low_confidence_mode': default_ratio > 0.50,
        }

        # v2.5-opt-fix: Value Betting (传入default_ratio)
        vb_cfg = self.config.get('value_betting', {})
        if vb_cfg.get('enabled', True):
            home_odds = context.get('home_odds', 0) if context else 0
            draw_odds = context.get('draw_odds', 0) if context else 0
            away_odds = context.get('away_odds', 0) if context else 0
            if home_odds > 0 and draw_odds > 0 and away_odds > 0:
                odds_arr = np.array([[home_odds, draw_odds, away_odds]])
                value_bets = self.calculate_value_bet(proba.reshape(1, 3), odds_arr,
                                                      default_ratio=default_ratio)
                if value_bets:
                    result['value_bet'] = value_bets[0]

        return result

    # ══════════════════════════════════════════════════
    # 9. 批量预测 + 双路径预测 (v2.7 — 显式双路融合)
    # ══════════════════════════════════════════════════

    def predict_batch(self, feature_dicts: List[Dict[str, float]],
                       league_names: Optional[List[str]] = None,
                       odds_feature_dicts: Optional[List[Dict[str, float]]] = None) -> np.ndarray:
        """
        v2.7: 批量预测 — 支持双路径

        Args:
            feature_dicts: 主模型特征 dict 列表 (match_features 路径, 47+ cols)
            league_names: 联赛名列表 (用于联赛感知 D 先验)
            odds_feature_dicts: OddsExpert 特征 dict 列表 (training_extended 路径, 16 cols)
                                如果为 None，OddsExpert 从主特征中提取赔率子集

        Returns:
            np.ndarray shape (n, 3): H/D/A 概率矩阵
        """
        n = len(feature_dicts)
        default_vals = self.config['data']['default_values']

        # ── 构建主特征矩阵 ──
        X_main = np.zeros((n, len(self.feature_names)))
        for i, fd in enumerate(feature_dicts):
            for j, name in enumerate(self.feature_names):
                X_main[i, j] = fd.get(name, default_vals.get(name, 0.0))
        X_main = self.scaler.transform(X_main)

        # ── 集成预测（内部调用 5 模型 + stacking）──
        proba = self.ensemble_predict_proba(
            X_main, league_names=league_names,
            raw_features_list=feature_dicts
        )
        return proba

    def predict_dual_path(self, feature_dicts: List[Dict[str, float]],
                           odds_feature_dicts: List[Dict[str, float]],
                           league_names: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        v2.7: 显式双路径预测 — 主模型路径 + OddsExpert 路径独立运行后融合

        用于以下场景：
        - match_features (47 rich cols) 和 training_extended (16 odds cols)
          对同一场比赛各有一份数据
        - 两条路径特征集不同，需分别标准化后各自预测再融合

        Returns:
            {
                'probabilities': (n,3) array  # 融合后 H/D/A
                'path_main': (n,3) array      # 主模型路径原始输出
                'path_odds': (n,3) array      # OddsExpert 路径原始输出
                'blend_weights': {'main': float, 'odds': float}
            }
        """
        n = len(feature_dicts)
        default_vals = self.config['data']['default_values']
        oe_cfg = self.config['models'].get('odds_expert', {})
        blend_cfg = self.config['models'].get('ensemble', {})

        # ── Path A: Main model (47 cols) ──
        X_main = np.zeros((n, len(self.feature_names)))
        for i, fd in enumerate(feature_dicts):
            for j, name in enumerate(self.feature_names):
                X_main[i, j] = fd.get(name, default_vals.get(name, 0.0))
        X_main_scaled = self.scaler.transform(X_main)

        # ── Path A prediction (4-model: LGB+XGB+Heuristic) ──
        proba_lgb, proba_xgb, proba_heuristic, _ = \
            self._get_base_model_probas(X_main_scaled)

        ens_cfg = self.config['models']['ensemble']
        w_lgb = ens_cfg.get('lightgbm_weight', 0.28)
        w_xgb = ens_cfg.get('xgboost_weight', 0.28)
        w_heuristic = ens_cfg.get('heuristic_weight', 0.14)

        total_main = w_lgb + w_xgb + w_heuristic
        path_main = (w_lgb * proba_lgb + w_xgb * proba_xgb +
                     w_heuristic * proba_heuristic) / total_main

        # ── Path B: OddsExpert (16 cols) ──
        X_odds = np.zeros((n, len(self.odds_feature_names)))
        for i, od in enumerate(odds_feature_dicts):
            for j, name in enumerate(self.odds_feature_names):
                X_odds[i, j] = od.get(name, default_vals.get(name, 0.0))
        if self.odds_scaler is not None:
            X_odds = self.odds_scaler.transform(X_odds)

        if self.odds_expert_model is not None and hasattr(self.odds_expert_model, 'predict_proba'):
            path_odds = self.odds_expert_model.predict_proba(X_odds)
        else:
            path_odds = np.ones((n, 3)) / 3

        # ── OOD 检测 ──
        oe_is_ood = np.max(path_odds, axis=1) - np.min(path_odds, axis=1) < 0.02
        if np.any(oe_is_ood):
            path_odds[oe_is_ood] = path_main[oe_is_ood]  # OOD 时用主模型替代

        # ── 融合 ──
        w_main = blend_cfg.get('main_model_weight', 0.70)
        w_odds = blend_cfg.get('odds_expert_weight', 0.10)
        # 归一化
        total = w_main + w_odds
        w_main /= total
        w_odds /= total

        proba_fused = w_main * path_main + w_odds * path_odds
        proba_fused = proba_fused / proba_fused.sum(axis=1, keepdims=True)

        # ── D 先验 + 校准 ──
        draw_prior_cfg = self.config.get('models', {}).get('draw_prior', {})
        if draw_prior_cfg.get('enabled', True):
            d_boost = draw_prior_cfg.get('d_probability_boost', 0.05)
            proba_fused = self._apply_league_draw_prior(proba_fused, d_boost, league_names)

        calib_enabled = self.config.get('models', {}).get('calibration', {}).get('enabled', True)
        if calib_enabled and self.calibrator_suite is not None:
            try:
                proba_fused = self.calibrator_suite.predict(proba_fused)
                proba_fused = proba_fused / proba_fused.sum(axis=1, keepdims=True)
            except (Exception, requests.exceptions.RequestException):
                pass

        return {
            'probabilities': proba_fused,
            'path_main': path_main,
            'path_odds': path_odds,
            'blend_weights': {'main': w_main, 'odds': w_odds},
            'odds_ood_mask': oe_is_ood if np.any(oe_is_ood) else None,
        }

    def _extract_odds_features(self, X_main: np.ndarray) -> Optional[np.ndarray]:
        """从主特征矩阵中提取 OddsExpert 所需的赔率子特征"""
        odds_cols = self.config['data'].get('extended_training', {}).get('odds_only_features', [])
        odds_indices = []
        for col in odds_cols:
            if col in self.feature_names:
                odds_indices.append(self.feature_names.index(col))

        if len(odds_indices) >= 5:
            X_odds = X_main[:, odds_indices] if X_main.ndim > 1 else X_main[odds_indices].reshape(1, -1)
            if self.odds_scaler is not None:
                X_odds = self.odds_scaler.transform(X_odds)
            return X_odds
        return None

# ══════════════════════════════════════════════════
# 便捷函数
# ══════════════════════════════════════════════════

def train_ensemble(config_path=None):
    trainer = EnsembleTrainer(config_path)
    result = trainer.train()
    return trainer

def load_ensemble(filepath):
    return EnsembleTrainer.load_pipeline(filepath)

if __name__ == '__main__':
    trainer = EnsembleTrainer()
    result = trainer.train()
