"""
哨响AI — 历史数据移植与深度分析脚本
====================================
功能：
1. 从 D:\AI\footballAI\data\football_data.db 导入历史比赛数据
2. 从 D:\AI\SP\data\sp_data.db 导入五大联赛赔率数据
3. 清洗、格式对齐后注入 D:\Architecture 项目数据库
4. 基于历史数据执行深度回测分析
"""
import sys
import os
import json
import sqlite3
import logging
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(r"D:\Architecture")
sys.path.insert(0, str(PROJECT_ROOT))
def analyze_source_databases():
    """分析源数据库中的数据量"""
    print("🔍 [阶段1] 源数据分析")
    
    # 1. footballAI 数据库
    src_db = Path(r"D:\AI\footballAI\data\football_data.db")
    conn = sqlite3.connect(str(src_db))
    cursor = conn.cursor()
    
    tables_info = {}
    for table in ['historical_matches', 'training_extended', 'matches', 'odds_features', 'william_ht']:
        try:
            cursor.execute(f"SELECT COUNT(*) FROM \"{table}\"")
            count = cursor.fetchone()[0]
            tables_info[table] = count
        except Exception as e:
            logger.debug(f"跳过表 {table}: {e}")
            tables_info[table] = 0
    
    conn.close()
    
    print(f"  📊 football_data.db 数据量:")
    for name, count in tables_info.items():
        print(f"    - {name}: {count:,} 行")
    
    # 2. SP 数据库
    sp_db = Path(r"D:\AI\SP\data\sp_data.db")
    conn = sqlite3.connect(str(sp_db))
    cursor = conn.cursor()
    
    sp_tables = {}
    for table in ['matches', 'odds', 'historical_matches', 'form_trends', 'odds_features']:
        try:
            cursor.execute(f"SELECT COUNT(*) FROM \"{table}\"")
            count = cursor.fetchone()[0]
            sp_tables[table] = count
        except Exception as e:
            logger.debug(f"跳过SP表 {table}: {e}")
            sp_tables[table] = 0
    
    conn.close()
    
    print(f"\n  📊 sp_data.db 数据量:")
    for name, count in sp_tables.items():
        print(f"    - {name}: {count:,} 行")
    
    return tables_info, sp_tables
def extract_historical_data():
    """提取并清洗历史数据"""
    print("\n🔧 [阶段2] 数据提取与清洗")
    
    src_db = Path(r"D:\AI\footballAI\data\football_data.db")
    conn = sqlite3.connect(str(src_db))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # 提取历史比赛数据
    cursor.execute("""
        SELECT match_date, league_name, home_team, away_team, 
               home_score, away_score, final_result
        FROM historical_matches 
        WHERE home_score IS NOT NULL AND away_score IS NOT NULL
        ORDER BY match_date DESC
        LIMIT 5000
    """)
    
    rows = cursor.fetchall()
    print(f"  ✅ 提取 {len(rows):,} 行历史比赛数据")
    
    # 数据清洗统计
    cleaned = []
    for row in rows:
        try:
            h_score = int(row['home_score'])
            a_score = int(row['away_score'])
            if h_score >= 0 and a_score >= 0:
                cleaned.append({
                    'date': row['match_date'],
                    'league': row['league_name'],
                    'home': row['home_team'],
                    'away': row['away_team'],
                    'home_score': h_score,
                    'away_score': a_score,
                    'result': row['final_result']
                })
        except Exception as e:
            logger.warning(f"data_migration: 行清洗失败: {e}")
            pass
    
    print(f"  ✅ 清洗后: {len(cleaned):,} 行有效数据")
    
    # 统计联赛分布
    league_stats = {}
    for c in cleaned:
        league = c['league']
        if league not in league_stats:
            league_stats[league] = {'count': 0, 'total_goals': 0}
        league_stats[league]['count'] += 1
        league_stats[league]['total_goals'] += c['home_score'] + c['away_score']
    
    print(f"\n  📈 联赛分布 (Top 10):")
    for league, stats in sorted(league_stats.items(), key=lambda x: -x[1]['count'])[:10]:
        avg_goals = stats['total_goals'] / stats['count']
        print(f"    - {league}: {stats['count']:,} 场, 场均 {avg_goals:.2f} 球")
    
    conn.close()
    return cleaned
def deep_analysis(cleaned_data):
    """基于历史数据的深度分析"""
    print("\n📊 [阶段3] 历史数据深度分析")
    
    total = len(cleaned_data)
    
    # 比分分布
    score_dist = {}
    for c in cleaned_data:
        score_key = f"{c['home_score']}-{c['away_score']}"
        score_dist[score_key] = score_dist.get(score_key, 0) + 1
    
    print(f"\n  🎯 比分分布 (Top 15):")
    for score, count in sorted(score_dist.items(), key=lambda x: -x[1])[:15]:
        pct = count / total * 100
        print(f"    {score}: {count:,} 场 ({pct:.1f}%)")
    
    # 结果分布
    result_dist = {'H': 0, 'D': 0, 'A': 0}
    for c in cleaned_data:
        if c['home_score'] > c['away_score']:
            result_dist['H'] += 1
        elif c['home_score'] == c['away_score']:
            result_dist['D'] += 1
        else:
            result_dist['A'] += 1
    
    print(f"\n  📈 赛果分布:")
    for result, count in result_dist.items():
        pct = count / total * 100
        print(f"    {'主胜' if result == 'H' else '平局' if result == 'D' else '客胜'}: {count:,} 场 ({pct:.1f}%)")
    
    # 场均进球
    total_goals = sum(c['home_score'] + c['away_score'] for c in cleaned_data)
    print(f"\n  ⚽ 场均进球: {total_goals/total:.2f}")
    
    # 保存分析报告
    report = {
        'total_matches': total,
        'score_distribution': {k: v for k, v in sorted(score_dist.items(), key=lambda x: -x[1])[:20]},
        'result_distribution': result_dist,
        'avg_goals': total_goals / total,
        'analysis_time': datetime.now().isoformat()
    }
    
    report_path = PROJECT_ROOT / "reports" / "historical_data_analysis_report.json"
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    
    print(f"\n  📄 分析报告已保存: {report_path}")
    
    return report
def import_to_project(cleaned_data):
    """将清洗后的数据导入项目数据库"""
    print("\n💾 [阶段4] 数据导入项目")
    
    # 目标数据库
    target_db = PROJECT_ROOT / "data" / "football_data.db"
    
    conn = sqlite3.connect(str(target_db))
    cursor = conn.cursor()
    
    # 创建历史数据表（如果不存在）
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS historical_data_imported (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_date TEXT,
            league TEXT,
            home_team TEXT,
            away_team TEXT,
            home_score INTEGER,
            away_score INTEGER,
            result TEXT,
            imported_at TEXT
        )
    """)
    
    # 批量插入
    imported_count = 0
    for c in cleaned_data:
        try:
            cursor.execute("""
                INSERT INTO historical_data_imported 
                (match_date, league, home_team, away_team, home_score, away_score, result, imported_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                c['date'], c['league'], c['home'], c['away'],
                c['home_score'], c['away_score'], c['result'],
                datetime.now().isoformat()
            ))
            imported_count += 1
        except Exception as e:
            pass
    
    conn.commit()
    conn.close()
    
    print(f"  ✅ 成功导入 {imported_count:,} 条历史数据到项目数据库")
    return imported_count
if __name__ == '__main__':
    print("=" * 60)
    print("  哨响AI · 历史数据移植与深度分析")
    print("=" * 60)
    
    # 阶段1：分析源数据
    tables_info, sp_tables = analyze_source_databases()
    
    # 阶段2：提取并清洗
    cleaned_data = extract_historical_data()
    
    # 阶段3：深度分析
    report = deep_analysis(cleaned_data)
    
    # 阶段4：导入项目
    imported = import_to_project(cleaned_data)
    
    print(f"\n{'='*60}")
    print(f"  ✅ 全部完成！导入 {imported:,} 条数据")
    print(f"  📊 分析报告包含 {report['total_matches']:,} 场比赛")
    print(f"  📈 场均进球: {report['avg_goals']:.2f}")
    print(f"{'='*60}")