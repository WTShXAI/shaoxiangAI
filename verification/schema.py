"""验证账本 schema 与数据库路径 (T01).

账本为独立 SQLite (verification.db), 与 events.db 解耦, 仅追加 (INSERT),
永不 UPDATE / DELETE。建表幂等 (IF NOT EXISTS)。
"""
from __future__ import annotations

import os

# 仓库根 = verification 包的父目录 (D:/Architecture)
REPO_ROOT: str = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 验证账本独立库路径
VERIFICATION_DB: str = os.path.join(REPO_ROOT, "verification.db")

# 账本 DDL (append-only)
LEDGER_DDL: str = """
CREATE TABLE IF NOT EXISTS verification_ledger (
    row_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT    NOT NULL,                 -- 本次验证运行 ID (UUID)
    match_id        TEXT    NOT NULL,                 -- = daily_predictions.match_key
    model_source    TEXT    NOT NULL,                 -- candles_ensemble|market_baseline|KNN
    kickoff_utc     TEXT    NOT NULL,                 -- ISO 8601 UTC (结算真源时间戳)
    match_date      TEXT    NOT NULL,                 -- YYYY-MM-DD (本地, 取自 daily_predictions)
    chosen_outcome  TEXT    NOT NULL,                 -- home|draw|away
    predicted_prob  REAL,                             -- 所选结果模型概率; KNN 方向-only 为 NULL
    p_home          REAL, p_draw REAL, p_away REAL,   -- 完整预测概率 (KNN 为 NULL)
    chosen_dec_odds REAL    NOT NULL,                 -- 所选结果去水 decimal odds (=1/devig_p)
    devig_h         REAL, devig_d REAL, devig_a REAL,-- 全场去水 decimal odds (可追溯)
    paper_stake     REAL    NOT NULL DEFAULT 1.0,     -- 纸盘单位注 (=1)
    settled_home    INTEGER, settled_away INTEGER,    -- 赛果 (可追溯)
    settled_outcome TEXT,                             -- home|draw|away|NULL(void)
    payoff           REAL,                            -- 纸盘收益: 胜=dec_odds-1, 负=-stake; 未结算 NULL
    is_credible     INTEGER NOT NULL DEFAULT 1,       -- credible_1x2 守卫结果
    created_at      TEXT    NOT NULL,                 -- ISO 8601 UTC 写入时间
    devig_method    TEXT                              -- power|proportional (NULL=旧比例法行)
);
CREATE INDEX IF NOT EXISTS idx_vl_src_cred ON verification_ledger(model_source, is_credible);
CREATE INDEX IF NOT EXISTS idx_vl_match   ON verification_ledger(match_id, model_source);
"""

# 存量账本迁移 (幂等, 已存在列则静默跳过)
LEDGER_MIGRATIONS: tuple = (
    "ALTER TABLE verification_ledger ADD COLUMN devig_method TEXT",
)

__all__ = ["REPO_ROOT", "VERIFICATION_DB", "LEDGER_DDL", "LEDGER_MIGRATIONS"]
