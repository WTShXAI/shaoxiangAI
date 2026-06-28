"""
哨响AI - OTSM 特征计算与模型集成 v1.0
==============================================
将 OTSM 实时信号逆向填充为模型输入特征。

两个数据源:
  1. historical_matches (training_extended): open/close odds → 2-snapshot OTSM 特征
  2. odds_timeline: 多快照 → 实时 OTSM 特征 (更精确)

输出:
  - match_features_otsm 表 (存储计算好的 OTSM 特征)
  - 更新 config.yaml 添加 OTSM 特征列
  - 生成重新训练脚本

使用:
  python compute_otsm_features.py --compute-all   # 计算所有比赛的 OTSM 特征
  python compute_otsm_features.py --verify       # 验证特征质量
  python compute_otsm_features.py --update-config # 更新 config.yaml
"""
import os
import sys
import json
import logging
import sqlite3
import numpy as np
from datetime import datetime, timezone
from typing import Dict, List, Optional

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger('OTSMFeatures')

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "football_data.db")
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")

# ── OTSM 特征定义 ─────────────────────────────────────────────────
OTSМ_FEATURES = [
    "otsm_lock_confidence",     # [0,1] 庄家锁定期确信度 (核心信号)
    "otsm_entropy_drift",       # [-1,1] 熵漂移 (负=收敛, 正=发散)
    "otsm_water_accel",        # [-1,1] 水位加速度 (负=庄家自信)
    "otsm_kelly_fluct",        # [0,+] 凯利涨落 (大=市场重估)
    "otsm_state_LOCKED",        # 0/1 one-hot: 是否 LOCKED 状态
    "otsm_state_ACTIVE",        # 0/1 one-hot: 是否 ACTIVE 状态
    "otsm_state_NOISE",        # 0/1 one-hot: 是否 NOISE 状态
    "otsm_n_snapshots_norm",   # [0,1] 快照数归一化 (多=可靠)
    "otsm_entropy_rate",       # 熵漂移速率 (变化速度)
]

OTSМ_DEFAULTS = {
    "otsm_lock_confidence": 0.0,
    "otsm_entropy_drift": 0.0,
    "otsm_water_accel": 0.0,
    "otsm_kelly_fluct": 0.0,
    "otsm_state_LOCKED": 0,
    "otsm_state_ACTIVE": 0,
    "otsm_state_NOISE": 1,    # 默认 NOISE (无信号)
    "otsm_n_snapshots_norm": 0.0,
    "otsm_entropy_rate": 0.0,
}

def ensure_otsm_table(db_path: str = DB_PATH):
    """确保 match_features_otsm 表存在"""
    conn = sqlite3.connect(db_path)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS match_features_otsm (
            match_id INTEGER PRIMARY KEY,
            otsm_lock_confidence REAL DEFAULT 0.0,
            otsm_entropy_drift REAL DEFAULT 0.0,
            otsm_water_accel REAL DEFAULT 0.0,
            otsm_kelly_fluct REAL DEFAULT 0.0,
            otsm_state_LOCKED INTEGER DEFAULT 0,
            otsm_state_ACTIVE INTEGER DEFAULT 0,
            otsm_state_NOISE INTEGER DEFAULT 1,
            otsm_n_snapshots_norm REAL DEFAULT 0.0,
            otsm_entropy_rate REAL DEFAULT 0.0,
            otsm_computed_at TEXT,
            otsm_source TEXT DEFAULT 'training_extended',
            UNIQUE(match_id)
        )
    ''')
    conn.commit()
    conn.close()
    logger.info("match_features_otsm 表已就绪")

def compute_from_training_extended(db_path: str = DB_PATH, limit: int = 50000) -> int:
    """
    从 training_extended 表计算 OTSM 特征 (2-snapshot 模式)
    
    这是历史数据的回测模式，只有开盘和收盘两个快照。
    但仍能提供有价值的 OTSM 信号。
    """
    import sys

    from bookmaker_sim.odds_temporal_sm import OddsTemporalStateMachine, OddsSnapshot
    
    sm = OddsTemporalStateMachine(db_path=db_path)
    if not sm.thresholds:
        logger.info("拟合 OTSM 阈值...")
        sm.fit_thresholds(sample_size=50000)
    
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    query = f'''
        SELECT ext_id, match_date, league_name, home_team, away_team,
               open_home, open_draw, open_away,
               odds_home, odds_draw, odds_away
        FROM training_extended
        WHERE open_home IS NOT NULL
          AND odds_home IS NOT NULL
        LIMIT {limit}
    '''
    rows = conn.execute(query).fetchall()
    
    logger.info(f"从 training_extended 计算 OTSM 特征: {len(rows)} 条")
    
    count = 0
    for row in rows:
        try:
            open_snap = OddsSnapshot(
                float(row["open_home"]),
                float(row["open_draw"]),
                float(row["open_away"]),
            )
            close_snap = OddsSnapshot(
                float(row["odds_home"]),
                float(row["odds_draw"]),
                float(row["odds_away"]),
            )
            
            pv = sm.compute_phase_vector(open_snap, close_snap)
            state, lock_conf, probs = sm.infer_state(pv)
            
            # 写入特征表
            conn.execute('''
                INSERT OR REPLACE INTO match_features_otsm
                    (match_id, otsm_lock_confidence, otsm_entropy_drift,
                     otsm_water_accel, otsm_kelly_fluct,
                     otsm_state_LOCKED, otsm_state_ACTIVE, otsm_state_NOISE,
                     otsm_n_snapshots_norm, otsm_entropy_rate,
                     otsm_computed_at, otsm_source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                row["ext_id"],
                lock_conf,
                pv.entropy_drift,
                pv.water_accel,
                pv.kelly_fluctuation,
                1 if state.value == "LOCKED" else 0,
                1 if state.value == "ACTIVE" else 0,
                1 if state.value == "NOISE" else 0,
                0.1,  # 2-snapshot 模式, 归一化为 0.1
                abs(pv.entropy_drift),  # 2-snapshot 的速率 = 绝对值
                datetime.now(timezone.utc).isoformat(),
                "training_extended",
            ))
            count += 1
            
        except (Exception) as e:
            logger.debug(f"计算失败 ext_id={row['ext_id']}: {e}")
    
    conn.commit()
    conn.close()
    
    logger.info(f"training_extended OTSM 特征计算完成: {count} 条")
    return count

def compute_from_timeline(db_path: str = DB_PATH) -> int:
    """
    从 odds_timeline 表计算实时 OTSM 特征 (多快照模式)
    
    这是生产级信号源，使用完整的赔率时序数据。
    """
    import sys

    from bookmaker_sim.odds_temporal_sm import OddsTemporalStateMachine
    
    sm = OddsTemporalStateMachine(db_path=db_path)
    if not sm.thresholds:
        sm.fit_thresholds()
    
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    # 获取所有有待时序数据的比赛
    match_ids = conn.execute('''
        SELECT DISTINCT match_id FROM odds_timeline
        WHERE match_id IN (
            SELECT match_id FROM matches WHERE home_score IS NULL
        )
    ''').fetchall()
    
    logger.info(f"从 odds_timeline 计算实时 OTSM 特征: {len(match_ids)} 场比赛")
    
    count = 0
    for row in match_ids:
        match_id = row["match_id"]
        result = sm.infer_realtime(match_id, db_path)
        
        if not result:
            continue
        
        # 归一化 n_snapshots (假设 10+ 快照 = 1.0)
        n_snap = result.state_probabilities.get("n_snapshots", 1)
        n_norm = min(n_snap / 10.0, 1.0)
        
        conn.execute('''
            INSERT OR REPLACE INTO match_features_otsm
                (match_id, otsm_lock_confidence, otsm_entropy_drift,
                 otsm_water_accel, otsm_kelly_fluct,
                 otsm_state_LOCKED, otsm_state_ACTIVE, otsm_state_NOISE,
                 otsm_n_snapshots_norm, otsm_entropy_rate,
                 otsm_computed_at, otsm_source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            match_id,
            result.lock_confidence,
            result.phase_vector.entropy_drift,
            result.phase_vector.water_accel,
            result.phase_vector.kelly_fluctuation,
            1 if result.state.value == "LOCKED" else 0,
            1 if result.state.value == "ACTIVE" else 0,
            1 if result.state.value == "NOISE" else 0,
            n_norm,
            result.state_probabilities.get("entropy_rate", 0.0),
            datetime.now(timezone.utc).isoformat(),
            "odds_timeline",
        ))
        count += 1
    
    conn.commit()
    conn.close()
    
    logger.info(f"odds_timeline OTSM 特征计算完成: {count} 条")
    return count

def update_config_with_otsm(config_path: str = CONFIG_PATH):
    """
    更新 config.yaml 添加 OTSM 特征列
    
    注意: 这需要重新训练模型才能生效
    """
    import yaml
    
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    
    # 检查是否已添加
    existing = config['data']['feature_columns']
    added = []
    
    for feat in OTSМ_FEATURES:
        if feat not in existing:
            existing.append(feat)
            added.append(feat)
    
    # 添加默认值
    defaults = config['data'].get('default_values', {})
    for feat, default in OTSМ_DEFAULTS.items():
        if feat not in defaults:
            defaults[feat] = default
    
    config['data']['default_values'] = defaults
    
    if added:
        with open(config_path, 'w', encoding='utf-8') as f:
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True)
        logger.info(f"config.yaml 已更新: 新增 {len(added)} 个 OTSM 特征")
        logger.info(f"新增特征: {added}")
        logger.warning("⚠️ 需要重新训练模型才能使用新特征!")
    else:
        logger.info("config.yaml 已包含 OTSM 特征，无需更新")

def verify_features(db_path: str = DB_PATH):
    """验证 OTSM 特征质量"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    # 统计
    total = conn.execute('SELECT COUNT(*) as c FROM match_features_otsm').fetchone()['c']
    
    if total == 0:
        logger.warning("match_features_otsm 为空，请先运行 --compute-all")
        conn.close()
        return
    
    # 特征分布
    logger.info(f"=== OTSM 特征验证 (n={total}) ===")
    
    for feat in ["otsm_lock_confidence", "otsm_entropy_drift", "otsm_n_snapshots_norm"]:
        stats = conn.execute(f'''
            SELECT 
                MIN({feat}) as min_val,
                MAX({feat}) as max_val,
                AVG({feat}) as avg_val,
                COUNT(CASE WHEN {feat} != 0 THEN 1 END) as non_zero
            FROM match_features_otsm
        ''').fetchone()
        
        pct_non_zero = stats['non_zero'] / total * 100 if total else 0
        logger.info(f"  {feat}: "
                  f"min={stats['min_val']:.3f}, max={stats['max_val']:.3f}, "
                  f"avg={stats['avg_val']:.3f}, "
                  f"非零={stats['non_zero']}({pct_non_zero:.1f}%)")
    
    # 状态分布
    for state in ["otsm_state_LOCKED", "otsm_state_ACTIVE", "otsm_state_NOISE"]:
        cnt = conn.execute(f'SELECT COUNT(*) as c FROM match_features_otsm WHERE {state}=1').fetchone()['c']
        logger.info(f"  {state}: {cnt} ({cnt/total*100:.1f}%)")
    
    # 与准确率的关系 (如果有实际结果)
    logger.info("\n=== OTSM 信号 vs 准确率 ===")
    
    # 加入实际结果
    query = '''
        SELECT 
            m.final_result,
            f.otsm_lock_confidence,
            f.otsm_state_LOCKED
        FROM match_features_otsm f
        JOIN training_extended t ON f.match_id = t.ext_id
        JOIN matches m ON f.match_id = m.match_id
        WHERE m.home_score IS NOT NULL
          AND t.final_result IS NOT NULL
        LIMIT 10000
    '''
    rows = conn.execute(query).fetchall()
    
    if rows:
        # LOCKED 状态准确率
        locked = [r for r in rows if r['otsm_state_LOCKED'] == 1]
        if locked:
            # 计算赔率隐含方向的正确率
            correct = sum(1 for r in locked 
                        if (r['final_result'] == 'H' and r['otsm_lock_confidence'] > 0.5))
            logger.info(f"  LOCKED 状态: {len(locked)} 场, "
                       f"高置信>0.5: {sum(1 for r in locked if r['otsm_lock_confidence']>0.5)} 场")
    
    conn.close()

# ── CLI ───────────────────────────────────────────────────────────

def main():
    import argparse
    
    parser = argparse.ArgumentParser(
        description="哨响AI - OTSM 特征计算与模型集成",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--compute-all", action="store_true",
                        help="计算所有比赛的 OTSM 特征 (training_extended + timeline)")
    parser.add_argument("--compute-historical", action="store_true",
                        help="仅计算历史比赛的 OTSM 特征 (training_extended)")
    parser.add_argument("--compute-realtime", action="store_true",
                        help="仅计算实时比赛的 OTSM 特征 (odds_timeline)")
    parser.add_argument("--verify", action="store_true",
                        help="验证 OTSM 特征质量")
    parser.add_argument("--update-config", action="store_true",
                        help="更新 config.yaml 添加 OTSM 特征列")
    parser.add_argument("--limit", type=int, default=50000,
                        help="历史数据处理上限 (默认50000)")
    
    args = parser.parse_args()
    
    if args.update_config:
        update_config_with_otsm()
        return
    
    if args.verify:
        verify_features()
        return
    
    if args.compute_all or args.compute_historical:
        ensure_otsm_table()
        n1 = compute_from_training_extended(limit=args.limit)
        logger.info(f"历史特征: {n1} 条")
    
    if args.compute_all or args.compute_realtime:
        ensure_otsm_table()
        n2 = compute_from_timeline()
        logger.info(f"实时特征: {n2} 条")
    
    if not any([args.compute_all, args.compute_historical, args.compute_realtime, 
                args.verify, args.update_config]):
        parser.print_help()

if __name__ == "__main__":
    main()
