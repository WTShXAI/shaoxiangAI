"""
quant_trading.db 初始化 — 类股票量化交易系统核心库
====================================================
设计原则:
  1. 独立 SQLite, 与 football_data.db 完全隔离 (避免污染训练数据)
  2. 单一事实源: bankroll/strategies/positions/orders/settlements/performance 全闭环
  3. 决策流: model_decision → orders (pending) → positions (confirmed) → settlements (settled)
  4. 凯利注码: 复用 scripts/bet_core.py (SSoT)
  5. 操盘手视图: operator_recommendations 持久化, 供前端 OperatorTerminal 展示
  6. 回测框架: backtest_runs + strategy_performance 统一记录

用法:
    python scripts/quant_init_db.py            # 初始化/迁移
    python scripts/quant_init_db.py --seed     # 同时灌入种子策略
"""
import sqlite3, os, json
from datetime import datetime, timezone

DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "quant_trading.db",
)

SCHEMA_SQL = """
-- 资金账户 (单例, id 必须 = 1)
CREATE TABLE IF NOT EXISTS bankroll (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    initial_capital REAL NOT NULL DEFAULT 10000.0,
    current_balance REAL NOT NULL DEFAULT 10000.0,
    reserved_balance REAL NOT NULL DEFAULT 0.0,
    high_water_mark REAL NOT NULL DEFAULT 10000.0,
    drawdown_pct REAL NOT NULL DEFAULT 0.0,
    total_pnl REAL NOT NULL DEFAULT 0.0,
    total_bets INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    pending_count INTEGER NOT NULL DEFAULT 0,
    mode TEXT NOT NULL DEFAULT 'paper' CHECK (mode IN ('paper', 'live')),
    updated_at TEXT NOT NULL
);

-- 策略注册表
CREATE TABLE IF NOT EXISTS strategies (
    strategy_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    sport TEXT NOT NULL DEFAULT 'soccer',
    market_type TEXT NOT NULL DEFAULT '1x2',
    description TEXT,
    parameters TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'retired')),
    model_source TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 模型决策 (审计源: 来自 bridge_service /api/predict/live 的模型输出)
CREATE TABLE IF NOT EXISTS model_decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT,
    match_id INTEGER,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    league TEXT,
    commence_time TEXT,
    direction TEXT NOT NULL,
    model_prob REAL NOT NULL,
    confidence REAL NOT NULL,
    market_prob REAL NOT NULL,
    book_odds_h REAL,
    book_odds_d REAL,
    book_odds_a REAL,
    book_odds REAL NOT NULL,
    edge_pct REAL NOT NULL,
    expected_value REAL NOT NULL,
    decision_text TEXT,
    sub_markets TEXT,
    operator_action TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (strategy_id) REFERENCES strategies(strategy_id)
);
CREATE INDEX IF NOT EXISTS idx_decisions_match ON model_decisions(home_team, away_team, commence_time);
CREATE INDEX IF NOT EXISTS idx_decisions_strategy ON model_decisions(strategy_id, created_at);

-- 持仓 (由 model_decision + 凯利计算衍生; pending -> confirmed -> settled)
CREATE TABLE IF NOT EXISTS positions (
    position_id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL,
    order_id INTEGER,
    match_id INTEGER,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    league TEXT,
    commence_time TEXT,
    side TEXT NOT NULL CHECK (side IN ('H', 'D', 'A')),
    book_odds REAL NOT NULL,
    model_prob REAL NOT NULL,
    market_prob REAL NOT NULL,
    edge_pct REAL NOT NULL,
    kelly_full REAL NOT NULL,
    kelly_half REAL NOT NULL,
    stake_pct REAL NOT NULL,
    stake_amount REAL NOT NULL,
    max_stake_cap REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'confirmed', 'settled', 'cancelled')),
    rejection_reason TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    settled_at TEXT,
    FOREIGN KEY (decision_id) REFERENCES model_decisions(decision_id),
    FOREIGN KEY (order_id) REFERENCES orders(order_id)
);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status, created_at);
CREATE INDEX IF NOT EXISTS idx_positions_match ON positions(home_team, away_team, commence_time);

-- 订单 (下单前 pending 闸门: paper 模式自动 confirm, live 模式需人工弹窗)
CREATE TABLE IF NOT EXISTS orders (
    order_id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL,
    bookmaker TEXT,
    side TEXT NOT NULL,
    book_odds REAL NOT NULL,
    stake_amount REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'confirmed', 'rejected', 'expired', 'filled')),
    confirmation_required INTEGER NOT NULL DEFAULT 1,
    rejection_reason TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    confirmed_at TEXT,
    FOREIGN KEY (decision_id) REFERENCES model_decisions(decision_id)
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status, created_at);

-- 结算 (实际赛果 + 盈亏)
CREATE TABLE IF NOT EXISTS settlements (
    settlement_id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL UNIQUE,
    match_id INTEGER,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    actual_result TEXT,
    actual_score TEXT,
    pnl REAL NOT NULL,
    roi_pct REAL NOT NULL,
    bankroll_before REAL NOT NULL,
    bankroll_after REAL NOT NULL,
    settled_at TEXT NOT NULL,
    notes TEXT,
    FOREIGN KEY (position_id) REFERENCES positions(position_id)
);
CREATE INDEX IF NOT EXISTS idx_settlements_date ON settlements(settled_at);

-- 绩效按日聚合 (资金曲线)
CREATE TABLE IF NOT EXISTS performance_daily (
    date TEXT PRIMARY KEY,
    bankroll_eod REAL NOT NULL,
    pnl_daily REAL NOT NULL,
    roi_daily_pct REAL,
    bets_count INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    pending_count INTEGER NOT NULL DEFAULT 0,
    drawdown_pct REAL,
    high_water_mark REAL,
    notes TEXT
);

-- 策略绩效 (按策略+周期聚合)
CREATE TABLE IF NOT EXISTS strategy_performance (
    strategy_id TEXT NOT NULL,
    period TEXT NOT NULL,  -- 'YYYY-MM' or 'all'
    total_bets INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    pending INTEGER NOT NULL DEFAULT 0,
    pnl_total REAL NOT NULL DEFAULT 0,
    roi_avg_pct REAL,
    sharpe_ratio REAL,
    max_drawdown_pct REAL,
    win_rate REAL,
    updated_at TEXT,
    PRIMARY KEY (strategy_id, period),
    FOREIGN KEY (strategy_id) REFERENCES strategies(strategy_id)
);

-- 操盘手建议 (operator_view 持久化)
CREATE TABLE IF NOT EXISTS operator_recommendations (
    rec_id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL,
    match_id INTEGER,
    home_team TEXT,
    away_team TEXT,
    league TEXT,
    commence_time TEXT,
    stake_hint TEXT,
    confidence_pct REAL,
    rules_fired TEXT,
    trap_score REAL,
    trap_verdict TEXT,
    verdict TEXT,
    operator_action TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (decision_id) REFERENCES model_decisions(decision_id)
);
CREATE INDEX IF NOT EXISTS idx_rec_decision ON operator_recommendations(decision_id);

-- 回测运行记录
CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id TEXT PRIMARY KEY,
    strategy_id TEXT,
    period_start TEXT,
    period_end TEXT,
    sample_size INTEGER,
    initial_bankroll REAL,
    final_bankroll REAL,
    pnl_total REAL,
    roi_pct REAL,
    sharpe REAL,
    max_drawdown_pct REAL,
    win_rate REAL,
    params TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (strategy_id) REFERENCES strategies(strategy_id)
);

-- 模型迁移记录
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL,
    description TEXT
);
"""

SEED_STRATEGIES = [
    {
        "strategy_id": "v71_wc_1x2",
        "name": "v7.1 WC规则流水线 + DrawExpert",
        "version": "v7.1",
        "sport": "soccer",
        "market_type": "1x2",
        "description": "哨响AI v7.1 预测引擎: WC2026 规则流水线, DrawGate(0.688)+DrawTightGate(0.18)+mid-range 过滤",
        "model_source": "pipeline.engine.WCEngine",
        "params": {"draw_gate": 0.688, "draw_tight_gate": 0.18},
    },
    {
        "strategy_id": "v6_value_layer_1x2",
        "name": "v6.0 价值层 (跨庄最优价+soft-line分歧)",
        "version": "v6.0",
        "sport": "soccer",
        "market_type": "1x2",
        "description": "跨庄最优价价值层: 共识隐含概率 × best_odds → 半凯利, 仅多庄分歧子集触发真edge",
        "model_source": "pipeline.deep_report.compute_value_layer",
        "params": {"frac_kelly": 0.5, "max_stake_frac": 0.10, "require_disagreement": True},
    },
    {
        "strategy_id": "oip_correct_score",
        "name": "OIP 比分价值层 (Poisson)",
        "version": "v6.0",
        "sport": "soccer",
        "market_type": "correct_score",
        "description": "Poisson 比分模型: λ_home/λ_away → 5×5 比分概率 → fair odds → 与跨庄CS盘口对比",
        "model_source": "pipeline.score_model.PoissonScoreModel",
        "params": {"max_goals": 5, "wc_overconf": 0.85},
    },
    {
        "strategy_id": "draw_consensus_booster",
        "name": "双庄平局共识 booster",
        "version": "v6.0",
        "sport": "soccer",
        "market_type": "draw",
        "description": "WH×IW 双庄平局共识 strong 时, 平局预警阈值 0.26→0.24 提前触发",
        "model_source": "pipeline.draw_signal.draw_alert_with_booster",
        "params": {"threshold_strong": 0.24, "threshold_default": 0.26, "lift": 1.51},
    },
]


def init_db(seed: bool = False, db_path: str = DB_PATH) -> dict:
    """初始化 quant_trading.db
    Returns: {ok, path, tables_count, seed_count}
    """
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.executescript(SCHEMA_SQL)

    # 记录 migration
    cur.execute(
        "INSERT OR REPLACE INTO schema_migrations (version, applied_at, description) VALUES (?, ?, ?)",
        (1, datetime.now(timezone.utc).isoformat(), "init quant_trading.db (9 核心表)"),
    )

    # 单例 bankroll
    cur.execute(
        """INSERT OR IGNORE INTO bankroll
           (id, initial_capital, current_balance, high_water_mark, updated_at)
           VALUES (1, 10000.0, 10000.0, 10000.0, ?)""",
        (datetime.now(timezone.utc).isoformat(),),
    )

    seed_count = 0
    if seed:
        now = datetime.now(timezone.utc).isoformat()
        for s in SEED_STRATEGIES:
            try:
                cur.execute(
                    """INSERT OR REPLACE INTO strategies
                       (strategy_id, name, version, sport, market_type, description,
                        parameters, model_source, status, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        s["strategy_id"], s["name"], s["version"],
                        s["sport"], s["market_type"], s["description"],
                        json.dumps(s.get("params", {}), ensure_ascii=False),
                        s.get("model_source", ""),
                        "active", now, now,
                    ),
                )
                seed_count += 1
            except Exception as e:
                print(f"  策略 {s['strategy_id']} 写入失败: {e}")

    conn.commit()

    # 统计
    tables_count = len(cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall())

    conn.close()
    return {
        "ok": True,
        "path": db_path,
        "tables_count": tables_count,
        "seed_count": seed_count,
        "size_bytes": os.path.getsize(db_path),
    }


def verify_db(db_path: str = DB_PATH) -> dict:
    """校验 DB 完整性, 返回表/索引/约束统计"""
    if not os.path.exists(db_path):
        return {"exists": False}
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    tables = cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    out = {"exists": True, "path": db_path, "size_bytes": os.path.getsize(db_path), "tables": []}
    for (t,) in tables:
        n = cur.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        out["tables"].append({"name": t, "rows": n})
    conn.close()
    return out


if __name__ == "__main__":
    import sys
    seed = "--seed" in sys.argv
    print("=" * 60)
    print("  quant_trading.db 初始化")
    print("=" * 60)
    result = init_db(seed=seed)
    print(f"路径: {result['path']}")
    print(f"表数: {result['tables_count']}")
    print(f"种子策略: {result['seed_count']} 条")
    print(f"大小: {result['size_bytes']:,} 字节")

    print("\n--- 校验 ---")
    v = verify_db()
    for t in v["tables"]:
        print(f"  {t['name']:30s} {t['rows']:>6} 行")
    print(f"\n总大小: {v['size_bytes']:,} 字节 ({v['size_bytes']/1024:.1f} KB)")
