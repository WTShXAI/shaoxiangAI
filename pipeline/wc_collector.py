#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Plan B: 2026世界杯时间序列赔率采集器
======================================
为剩余36场比赛(6.22-6.28)采集赛前8h/4h/1h多时间点赔率

数据源: 截图OCR (当前) → API接入 (长期)
存储: SQLite wc2026_odds_timeline 表

使用方式:
  1. 截图保存到 2026WC/{date}/ 目录 (文件名: 主队vs客队_T-8h.png 等)
  2. 运行 python pipeline/wc_collector.py --scan
"""
import sqlite3, json, os, sys
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict

ARCH_ROOT = Path(r"D:/Architecture v4.0")
DB_PATH = ARCH_ROOT / "data" / "wc2026_timeline.db"

# ═══════════════════════════════════════════════════════
# 数据库初始化
# ═══════════════════════════════════════════════════════

def init_database():
    """创建时间序列采集表"""
    os.makedirs(DB_PATH.parent, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS wc2026_matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_date TEXT NOT NULL,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            match_identifier TEXT UNIQUE NOT NULL,
            actual_result TEXT,
            actual_score TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
        CREATE TABLE IF NOT EXISTS wc2026_odds_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL REFERENCES wc2026_matches(id),
            snapshot_label TEXT NOT NULL,  -- 'T-8h', 'T-4h', 'T-1h', 'kickoff', 'live'
            timestamp TEXT NOT NULL,
            -- 全场1X2
            ft_home REAL, ft_draw REAL, ft_away REAL,
            -- 全场亚盘
            ft_ah_handicap REAL, ft_ah_home REAL, ft_ah_away REAL,
            -- 全场大小
            ft_ou_line REAL, ft_ou_over REAL, ft_ou_under REAL,
            -- 半场1X2
            ht_home REAL, ht_draw REAL, ht_away REAL,
            -- 半场亚盘
            ht_ah_handicap REAL, ht_ah_home REAL, ht_ah_away REAL,
            -- 半场大小
            ht_ou_line REAL, ht_ou_over REAL, ht_ou_under REAL,
            -- 正确比分 (关键!)
            cs_1_0 REAL, cs_0_0 REAL, cs_1_1 REAL, cs_2_1 REAL,
            cs_2_0 REAL, cs_0_1 REAL, cs_2_2 REAL,
            cs_other REAL,  -- "其它"比分赔率 ← 核心信号!
            -- 源数据
            source TEXT DEFAULT 'ocr',
            raw_json TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
        CREATE TABLE IF NOT EXISTS wc2026_derived_signals (
            match_id INTEGER NOT NULL REFERENCES wc2026_matches(id),
            -- 时间序列衍生
            h_drift_8h_1h REAL,    -- H赔率变化率 (T-1h - T-8h)/T-8h
            d_drift_8h_1h REAL,    -- D赔率变化率
            a_drift_8h_1h REAL,    -- A赔率变化率
            -- 其它比分信号
            other_score_t8 REAL,   -- T-8h的"其它"比分赔率
            other_score_t1 REAL,   -- T-1h的"其它"比分赔率
            other_score_drift REAL, -- "其它"比分赔率变化
            -- HT-FT错配
            ht_ft_hcp_ratio REAL,  -- HT让球/FT让球
            ht_ft_draw_ratio REAL, -- HT平赔/FT平赔
            created_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (match_id)
        )
        -- 索引
        CREATE INDEX IF NOT EXISTS idx_snapshots_match ON wc2026_odds_snapshots(match_id, snapshot_label)
    """)
    
    conn.commit()
    print(f"✅ 数据库初始化完成: {DB_PATH}")
    return conn

def register_matches():
    """注册6.22-6.28剩余36场比赛"""
    matches = [
        # 6.22
        ('2026-06-22', '乌拉圭', '佛得角共和国'),
        ('2026-06-22', '新西兰', '埃及'),
        ('2026-06-22', '比利时', '伊朗'),
        ('2026-06-22', '西班牙', '沙特阿拉伯'),
        # 6.23
        ('2026-06-23', '挪威', '塞内加尔'),
        ('2026-06-23', '法国', '伊拉克'),
        ('2026-06-23', '约旦', '阿尔及利亚'),
        ('2026-06-23', '阿根廷', '奥地利'),
        # 6.24
        ('2026-06-24', '哥伦比亚', '民主刚果'),
        ('2026-06-24', '巴拿马', '克罗地亚'),
        ('2026-06-24', '英格兰', '加纳'),
        ('2026-06-24', '葡萄牙', '乌兹别克斯坦'),
        # 6.25
        ('2026-06-25', '南非', '韩国'),
        ('2026-06-25', '捷克', '墨西哥'),
        ('2026-06-25', '摩洛哥', '海地'),
        ('2026-06-25', '波黑', '卡塔尔'),
        ('2026-06-25', '瑞士', '加拿大'),
        ('2026-06-25', '苏格兰', '巴西'),
        # 6.26
        ('2026-06-26', '厄瓜多尔', '德国'),
        ('2026-06-26', '土耳其', '美国'),
        ('2026-06-26', '巴拉圭', '澳大利亚'),
        ('2026-06-26', '库拉索', '科特迪瓦'),
        ('2026-06-26', '日本', '瑞典'),
        ('2026-06-26', '突尼斯', '荷兰'),
        # 6.27
        ('2026-06-27', '乌拉圭', '西班牙'),
        ('2026-06-27', '佛得角共和国', '沙特阿拉伯'),
        ('2026-06-27', '埃及', '伊朗'),
        ('2026-06-27', '塞内加尔', '伊拉克'),
        ('2026-06-27', '挪威', '法国'),
        ('2026-06-27', '新西兰', '比利时'),
        # 6.28
        ('2026-06-28', '克罗地亚', '加纳'),
        ('2026-06-28', '哥伦比亚', '葡萄牙'),
        ('2026-06-28', '巴拿马', '英格兰'),
        ('2026-06-28', '民主刚果', '乌兹别克斯坦'),
        ('2026-06-28', '约旦', '阿根廷'),
        ('2026-06-28', '阿尔及利亚', '奥地利'),
    ]
    
    conn = init_database()
    for date, home, away in matches:
        ident = f'{date}_{home}vs{away}'
        conn.execute(
            'INSERT OR IGNORE INTO wc2026_matches (match_date, home_team, away_team, match_identifier) VALUES (?,?,?,?)',
            (date, home, away, ident)
        )
    conn.commit()
    print(f"✅ 注册 {len(matches)} 场比赛")
    conn.close()

def manual_insert_template():
    """手动采集模板 — 比赛前填写后运行"""
    template = {
        "match": "乌拉圭 vs 西班牙",
        "date": "2026-06-27",
        "snapshot_label": "T-8h",
        "timestamp": "2026-06-27T08:00:00",
        "ft_home": 4.70, "ft_draw": 3.90, "ft_away": 1.63,
        "ft_ah_handicap": -0.75, "ft_ah_home": 1.95, "ft_ah_away": 1.95,
        "ft_ou_line": 2.5, "ft_ou_over": 1.90, "ft_ou_under": 1.90,
        "ht_home": 3.40, "ht_draw": 2.15, "ht_away": 2.20,
        "ht_ah_handicap": -0.25, "ht_ah_home": 1.95, "ht_ah_away": 2.05,
        "ht_ou_line": 1.0, "ht_ou_over": 1.90, "ht_ou_under": 1.90,
        "cs_other": 7.50,  # ← 核心! "其它"比分赔率
        "source": "manual",
    }
    return template

def insert_snapshot(data: Dict):
    """插入一条赔率快照"""
    conn = sqlite3.connect(str(DB_PATH))
    
    # Find match_id
    date = data['date']
    home, away = data['match'].split(' vs ')
    ident = f'{date}_{home}vs{away}'
    
    row = conn.execute('SELECT id FROM wc2026_matches WHERE match_identifier=?', (ident,)).fetchone()
    if not row:
        print(f"❌ 比赛未注册: {ident}")
        conn.close()
        return
    
    match_id = row[0]
    
    conn.execute("""
        INSERT INTO wc2026_odds_snapshots (
            match_id, snapshot_label, timestamp,
            ft_home, ft_draw, ft_away,
            ft_ah_handicap, ft_ah_home, ft_ah_away,
            ft_ou_line, ft_ou_over, ft_ou_under,
            ht_home, ht_draw, ht_away,
            ht_ah_handicap, ht_ah_home, ht_ah_away,
            ht_ou_line, ht_ou_over, ht_ou_under,
            cs_other, source, raw_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        match_id, data['snapshot_label'], data['timestamp'],
        data.get('ft_home'), data.get('ft_draw'), data.get('ft_away'),
        data.get('ft_ah_handicap'), data.get('ft_ah_home'), data.get('ft_ah_away'),
        data.get('ft_ou_line'), data.get('ft_ou_over'), data.get('ft_ou_under'),
        data.get('ht_home'), data.get('ht_draw'), data.get('ht_away'),
        data.get('ht_ah_handicap'), data.get('ht_ah_home'), data.get('ht_ah_away'),
        data.get('ht_ou_line'), data.get('ht_ou_over'), data.get('ht_ou_under'),
        data.get('cs_other'), data.get('source'), json.dumps(data, ensure_ascii=False),
    ))
    
    conn.commit()
    print(f"✅ 插入: {data['match']} [{data['snapshot_label']}]")
    conn.close()

def compute_derived_signals(match_id: int):
    """计算衍生信号 — 采集完成后运行"""
    conn = sqlite3.connect(str(DB_PATH))
    
    snapshots = conn.execute("""
        SELECT snapshot_label, ft_home, ft_draw, ft_away, cs_other,
               ht_ah_handicap, ft_ah_handicap, ht_draw, ft_draw
        FROM wc2026_odds_snapshots
        WHERE match_id=? AND snapshot_label IN ('T-8h', 'T-1h')
        ORDER BY snapshot_label
    """, (match_id,)).fetchall()
    
    if len(snapshots) < 2:
        print(f"  ⚠️ match_id={match_id}: 需要至少2个快照")
        conn.close()
        return
    
    t8 = next((s for s in snapshots if s[0] == 'T-8h'), None)
    t1 = next((s for s in snapshots if s[0] == 'T-1h'), None)
    
    signals = {}
    if t8 and t1:
        # 赔率漂移率
        signals['h_drift_8h_1h'] = (t1[1] - t8[1]) / t8[1] if t8[1] else 0
        signals['d_drift_8h_1h'] = (t1[2] - t8[2]) / t8[2] if t8[2] else 0
        signals['a_drift_8h_1h'] = (t1[3] - t8[3]) / t8[3] if t8[3] else 0
        
        # 其它比分赔率变化
        signals['other_score_t8'] = t8[4]
        signals['other_score_t1'] = t1[4]
        signals['other_score_drift'] = (t1[4] - t8[4]) / t8[4] if t8[4] and t8[4] > 0 else 0
        
        # HT-FT错配
        if t8[5] and t8[6]:
            signals['ht_ft_hcp_ratio'] = abs(t8[5]) / abs(t8[6]) if abs(t8[6]) > 0 else 1.0
        if t8[7] and t8[8]:
            signals['ht_ft_draw_ratio'] = (1/t8[7]) / (1/t8[8]) if t8[8] else 1.0
    
    conn.execute("""
        INSERT OR REPLACE INTO wc2026_derived_signals
        (match_id, h_drift_8h_1h, d_drift_8h_1h, a_drift_8h_1h,
         other_score_t8, other_score_t1, other_score_drift,
         ht_ft_hcp_ratio, ht_ft_draw_ratio)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (match_id,
          signals.get('h_drift_8h_1h'), signals.get('d_drift_8h_1h'), signals.get('a_drift_8h_1h'),
          signals.get('other_score_t8'), signals.get('other_score_t1'), signals.get('other_score_drift'),
          signals.get('ht_ft_hcp_ratio'), signals.get('ht_ft_draw_ratio')))
    
    conn.commit()
    print(f"✅ 衍生信号: match_id={match_id}")
    conn.close()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--init', action='store_true', help='初始化数据库+注册比赛')
    parser.add_argument('--compute', type=int, help='计算指定match_id的衍生信号')
    args = parser.parse_args()
    
    if args.init:
        register_matches()
        print("\n📋 手动采集流程:")
        print("  1. 比赛前3个时间点截图保存(1X2+半场+波胆)")
        print("  2. 使用 manual_insert_template() 生成JSON")
        print("  3. 调用 insert_snapshot(data) 存入数据库")
        print("  4. 赛后调用 compute_derived_signals() 计算信号")
        print("\n💡 核心字段: cs_other (其它比分赔率)")
        print("   高(>7.0) = 不可预测 = 平局土壤")
        print("   低(<5.0) = 可预测 = 屠杀信号")
    elif args.compute:
        compute_derived_signals(args.compute)
    else:
        print("用法: python wc_collector.py --init | --compute <match_id>")
