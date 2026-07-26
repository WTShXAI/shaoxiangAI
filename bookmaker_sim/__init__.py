"""
哨响AI - 博彩公司模拟器 (Bookmaker Simulator) v1.0
===================================================

角色互换逆向推演框架的核心模块。

三层架构:
  ScoreDistSimulator    → 从预期进球参数模拟比分概率分布
  MarketDerivationEngine → 从比分分布推导所有玩法的公平赔率
  AdversarialOddsVerifier → 角色互换验证: "如果我是庄家" vs 真实赔率

核心洞察:
  博彩公司必须在所有玩法中维持内部一致性 (无套利约束)。
  如果他们拥有"私密预期", 这一预期的指纹会同时出现在
  1X2、让球、大小球、比分等所有派生的赔率中——
  单一市场可能被噪声掩盖, 但跨市场的一致性偏差无法隐藏。

用法:
    from bookmaker_sim import ScoreDistSimulator, MarketDerivationEngine, AdversarialOddsVerifier
"""

from .score_distribution import ScoreDistSimulator
from .market_derivation import MarketDerivationEngine
from .adversarial_verifier import AdversarialOddsVerifier
from .bayesian_odds_inverter import (
    BayesianOddsInverter,
    OTSMDriftBayesianIntegrator,
    LambdaInverter,
    BayesianLambdaPosterior,
    InversionResult,
    odds_to_probs_vector,
    compute_bayesian_features,
    quick_bayesian_invert,
    create_inverter,
    BAYESIAN_FEATURE_DEFS,
    BAYESIAN_DEFAULTS,
)

from .margin_likelihood_bridge import (
    BookmakerBayesInfer,
    BayesInferResult,
    LeagueMargins,
    LeagueCalibrator,
)

# Phase A — 组合/账户层
from .portfolio_manager import PortfolioManager, create_portfolio, Position, EquityPoint
from .performance_analyzer import (
    compute_all_metrics,
    sharpe_ratio,
    max_drawdown,
    calmar_ratio,
    win_rate,
    profit_loss_ratio,
    expected_value,
)

# Phase B — 策略注册表 + 回测引擎 + 上线校验
from .strategy_registry import (
    StrategyRegistry,
    StrategyMetadata,
    register_strategy,
    get_registry,
)
from .backtest_engine import BacktestEngine, BacktestResult
from .deployment_validator import (
    DeploymentValidator,
    ValidationReport,
    ValidationCheck,
    validate_deployment,
)

__version__ = "1.4.0"  # +Phase B 三件套
