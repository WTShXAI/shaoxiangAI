"""
预测闭环追踪系统 v1.0
预测 → 入库 → 赛后回溯 → 自动复盘 → 参数调优

闭环流程:
  1. predict(): 对未来比赛做预测, 入库到 predictions 表
  2. evaluate(): 比赛结束后回填结果, 统计准确率
  3. report(): 生成复盘报告
  4. optimize(): 基于复盘数据自动调优门控参数
  5. challenge(): 10场挑战追踪
"""
import sys, os, json, logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict

import numpy as np
import sqlite3
import pandas as pd

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('PredictionTracker')


class PredictionTracker:
    """
    预测闭环追踪器
    
    数据库: predictions 表
    - prediction_id: 自增主键
    - match_id: 比赛ID
    - predicted_at: 预测时间
    - prob_h, prob_d, prob_a: 预测概率
    - predicted_result: 预测结果(H/D/A)
    - total_score: 综合评分
    - tier: 等级(S/A/B/C)
    - consensus_score, confidence_score, feature_score, odds_clarity_score: 门控分
    - home_score, away_score: 实际比分(赛后回填)
    - actual_result: 实际结果(赛后回填)
    - is_correct: 是否正确(赛后回填)
    - evaluated_at: 评估时间
    """
    
    def __init__(self, db_path: str = 'data/football_data.db'):
        self.db_path = db_path
        self._init_tables()
    
    def _init_tables(self):
        """初始化预测追踪表"""
        conn = sqlite3.connect(self.db_path)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS predictions (
                prediction_id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id INTEGER NOT NULL,
                predicted_at TEXT NOT NULL,
                prob_h REAL NOT NULL,
                prob_d REAL NOT NULL,
                prob_a REAL NOT NULL,
                predicted_result TEXT NOT NULL,
                total_score REAL NOT NULL,
                tier TEXT NOT NULL,
                consensus_score REAL DEFAULT 0,
                confidence_score REAL DEFAULT 0,
                feature_score REAL DEFAULT 0,
                odds_clarity_score REAL DEFAULT 0,
                odds_h REAL,
                odds_d REAL,
                odds_a REAL,
                odds_direction TEXT,
                feature_coverage REAL DEFAULT 0,
                default_ratio REAL DEFAULT 0,
                home_score INTEGER,
                away_score INTEGER,
                actual_result TEXT,
                is_correct INTEGER,
                evaluated_at TEXT,
                model_version TEXT,
                challenge_batch INTEGER DEFAULT 0
            )
        ''')
        
        conn.execute('''
            CREATE TABLE IF NOT EXISTS prediction_challenges (
                challenge_id INTEGER PRIMARY KEY AUTOINCREMENT,
                challenge_name TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                target_accuracy REAL DEFAULT 80.0,
                total_predictions INTEGER DEFAULT 0,
                correct_predictions INTEGER DEFAULT 0,
                current_accuracy REAL DEFAULT 0,
                status TEXT DEFAULT 'active'
            )
        ''')
        
        conn.execute('''
            CREATE TABLE IF NOT EXISTS gate_params_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                changed_at TEXT NOT NULL,
                param_name TEXT NOT NULL,
                old_value TEXT NOT NULL,
                new_value TEXT NOT NULL,
                reason TEXT
            )
        ''')
        
        conn.commit()
        conn.close()
        logger.info("预测追踪表已就绪")
    
    def save_predictions(self, results: List) -> int:
        """
        保存预测结果到数据库
        
        Args:
            results: SelectivePredictor 返回的 PredictionResult 列表
        
        Returns:
            保存的预测数量
        """
        conn = sqlite3.connect(self.db_path)
        saved = 0
        
        for r in results:
            # 检查是否已存在同match_id的预测(同一天内)
            existing = conn.execute(
                'SELECT prediction_id FROM predictions WHERE match_id = ? AND predicted_at >= date(?)',
                (int(r.match_id), datetime.now().strftime('%Y-%m-%d'))
            ).fetchone()
            
            if existing:
                # 更新已有预测
                conn.execute('''
                    UPDATE predictions SET
                        prob_h=?, prob_d=?, prob_a=?, predicted_result=?,
                        total_score=?, tier=?,
                        consensus_score=?, confidence_score=?,
                        feature_score=?, odds_clarity_score=?,
                        odds_h=?, odds_d=?, odds_a=?, odds_direction=?,
                        feature_coverage=?, default_ratio=?,
                        predicted_at=?
                    WHERE prediction_id=?
                ''', (
                    r.prob_h, r.prob_d, r.prob_a, r.predicted_result,
                    r.total_score, r.tier,
                    r.consensus_score, r.confidence_score,
                    r.feature_score, r.odds_clarity_score,
                    r.odds_h, r.odds_d, r.odds_a, r.odds_direction,
                    r.feature_coverage, r.default_ratio,
                    datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    existing[0]
                ))
            else:
                conn.execute('''
                    INSERT INTO predictions 
                    (match_id, predicted_at, prob_h, prob_d, prob_a,
                     predicted_result, total_score, tier,
                     consensus_score, confidence_score, feature_score, odds_clarity_score,
                     odds_h, odds_d, odds_a, odds_direction,
                     feature_coverage, default_ratio)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    int(r.match_id), r.predicted_at,
                    r.prob_h, r.prob_d, r.prob_a,
                    r.predicted_result, r.total_score, r.tier,
                    r.consensus_score, r.confidence_score,
                    r.feature_score, r.odds_clarity_score,
                    r.odds_h, r.odds_d, r.odds_a, r.odds_direction,
                    r.feature_coverage, r.default_ratio
                ))
                saved += 1
        
        conn.commit()
        conn.close()
        logger.info(f"保存了 {saved} 条预测(去重后)")
        return saved
    
    def evaluate(self, days_back: int = 7) -> Dict:
        """
        评估最近N天的预测结果
        
        1. 从matches表获取实际比分
        2. 回填到predictions表
        3. 统计准确率
        """
        conn = sqlite3.connect(self.db_path)
        
        # 获取待评估的预测(有预测但未评估的)
        cutoff = (datetime.now() - timedelta(days=days_back)).strftime('%Y-%m-%d')
        
        predictions = pd.read_sql_query('''
            SELECT p.prediction_id, p.match_id, p.predicted_result, p.tier,
                   p.prob_h, p.prob_d, p.prob_a, p.total_score,
                   m.match_date, m.home_team_name, m.away_team_name,
                   m.home_score, m.away_score, m.league_name
            FROM predictions p
            JOIN matches m ON p.match_id = m.match_id
            WHERE p.is_correct IS NULL
              AND m.home_score IS NOT NULL
              AND m.match_date >= ?
        ''', conn, params=[cutoff])
        
        if len(predictions) == 0:
            conn.close()
            return {'evaluated': 0, 'message': '无需评估的预测'}
        
        correct = 0
        for _, p in predictions.iterrows():
            # 判断实际结果
            if p['home_score'] > p['away_score']:
                actual = 'H'
            elif p['home_score'] == p['away_score']:
                actual = 'D'
            else:
                actual = 'A'
            
            is_correct = 1 if p['predicted_result'] == actual else 0
            correct += is_correct
            
            # 回填
            conn.execute('''
                UPDATE predictions SET
                    home_score=?, away_score=?, actual_result=?,
                    is_correct=?, evaluated_at=?
                WHERE prediction_id=?
            ''', (
                int(p['home_score']), int(p['away_score']), actual,
                is_correct,
                datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                int(p['prediction_id'])
            ))
        
        conn.commit()
        
        # 统计各等级准确率
        stats = {}
        for tier in ['S', 'A', 'B', 'C']:
            tier_df = predictions[predictions['tier'] == tier]
            if len(tier_df) == 0:
                continue
            
            tier_correct = 0
            for _, p in tier_df.iterrows():
                actual = 'H' if p['home_score'] > p['away_score'] else \
                         ('D' if p['home_score'] == p['away_score'] else 'A')
                if p['predicted_result'] == actual:
                    tier_correct += 1
            
            stats[tier] = {
                'count': len(tier_df),
                'correct': tier_correct,
                'accuracy': round(tier_correct / len(tier_df) * 100, 1)
            }
        
        conn.close()
        
        result = {
            'evaluated': len(predictions),
            'correct': correct,
            'accuracy': round(correct / len(predictions) * 100, 1),
            'tier_stats': stats
        }
        
        logger.info(f"评估完成: {len(predictions)}条, 准确率{result['accuracy']}%")
        return result
    
    def get_challenge_progress(self, challenge_id: Optional[int] = None) -> Dict:
        """
        获取10场挑战进度
        
        自动找到最新active挑战或指定ID
        """
        conn = sqlite3.connect(self.db_path)
        
        if challenge_id:
            challenge = conn.execute(
                'SELECT * FROM prediction_challenges WHERE challenge_id=?',
                (challenge_id,)
            ).fetchone()
        else:
            challenge = conn.execute(
                'SELECT * FROM prediction_challenges WHERE status="active" ORDER BY challenge_id DESC LIMIT 1'
            ).fetchone()
        
        if not challenge:
            conn.close()
            return {'status': 'no_active_challenge'}
        
        # 获取该挑战相关的预测
        # (首次创建后，通过challenge_batch关联)
        predictions = pd.read_sql_query('''
            SELECT p.*, m.home_team_name, m.away_team_name, 
                   m.match_date, m.home_score, m.away_score, m.league_name
            FROM predictions p
            JOIN matches m ON p.match_id = m.match_id
            WHERE p.is_correct IS NOT NULL
            ORDER BY p.predicted_at DESC
            LIMIT 50
        ''', conn)
        
        conn.close()
        
        # 统计
        results = []
        for _, p in predictions.iterrows():
            actual = 'H' if p['home_score'] > p['away_score'] else \
                     ('D' if p['home_score'] == p['away_score'] else 'A')
            results.append({
                'match_date': p['match_date'],
                'home_team': p['home_team_name'],
                'away_team': p['away_team_name'],
                'predicted': p['predicted_result'],
                'actual': actual,
                'correct': bool(p['is_correct']),
                'score': f"{p['home_score']}-{p['away_score']}",
                'tier': p['tier'],
                'total_score': p['total_score']
            })
        
        total = len(results)
        correct = sum(1 for r in results if r['correct'])
        
        return {
            'challenge': {
                'id': challenge[0],
                'name': challenge[1],
                'started_at': challenge[2],
                'target': challenge[4],
                'status': challenge[7]
            },
            'progress': {
                'total_evaluated': total,
                'correct': correct,
                'accuracy': round(correct / total * 100, 1) if total > 0 else 0,
                'remaining_to_target': max(0, 10 - total) if total < 10 else 0
            },
            'recent_results': results[:15]
        }
    
    def start_challenge(self, name: str = "10场80%准确率挑战") -> int:
        """开始新的10场挑战"""
        conn = sqlite3.connect(self.db_path)
        
        # 关闭旧挑战
        conn.execute('UPDATE prediction_challenges SET status="completed", ended_at=? WHERE status="active"',
                    (datetime.now().strftime('%Y-%m-%d %H:%M:%S'),))
        
        # 创建新挑战
        cursor = conn.execute('''
            INSERT INTO prediction_challenges 
            (challenge_name, started_at, target_accuracy, status)
            VALUES (?, ?, 80.0, 'active')
        ''', (name, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        
        challenge_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        logger.info(f"新挑战 [{challenge_id}] '{name}' 已开始! 目标: 80%准确率")
        return challenge_id
    
    def generate_report(self, days_back: int = 30) -> str:
        """
        生成复盘报告
        
        Returns:
            Markdown格式的报告文本
        """
        conn = sqlite3.connect(self.db_path)
        
        cutoff = (datetime.now() - timedelta(days=days_back)).strftime('%Y-%m-%d')
        
        # 总体统计
        overall = pd.read_sql_query('''
            SELECT COUNT(*) as total,
                   SUM(is_correct) as correct,
                   ROUND(AVG(is_correct)*100, 1) as accuracy
            FROM predictions
            WHERE is_correct IS NOT NULL
              AND predicted_at >= ?
        ''', conn, params=[cutoff])
        
        # 按等级统计
        by_tier = pd.read_sql_query('''
            SELECT tier,
                   COUNT(*) as total,
                   SUM(is_correct) as correct,
                   ROUND(AVG(is_correct)*100, 1) as accuracy,
                   ROUND(AVG(total_score), 1) as avg_score
            FROM predictions
            WHERE is_correct IS NOT NULL
              AND predicted_at >= ?
            GROUP BY tier
            ORDER BY tier
        ''', conn, params=[cutoff])
        
        # 按联赛统计
        by_league = pd.read_sql_query('''
            SELECT m.league_name,
                   COUNT(*) as total,
                   SUM(p.is_correct) as correct,
                   ROUND(AVG(p.is_correct)*100, 1) as accuracy
            FROM predictions p
            JOIN matches m ON p.match_id = m.match_id
            WHERE p.is_correct IS NOT NULL
              AND p.predicted_at >= ?
            GROUP BY m.league_name
            HAVING total >= 5
            ORDER BY accuracy DESC
        ''', conn, params=[cutoff])
        
        # 最近错误预测
        errors = pd.read_sql_query('''
            SELECT m.match_date, m.home_team_name, m.away_team_name,
                   m.home_score, m.away_score,
                   p.predicted_result, p.tier, p.total_score,
                   p.prob_h, p.prob_d, p.prob_a
            FROM predictions p
            JOIN matches m ON p.match_id = m.match_id
            WHERE p.is_correct = 0
              AND p.predicted_at >= ?
            ORDER BY m.match_date DESC
            LIMIT 10
        ''', conn, params=[cutoff])
        
        conn.close()
        
        # 构建报告
        lines = []
        lines.append(f"# 预测闭环复盘报告")
        lines.append(f"")
        lines.append(f"**报告时间**: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        lines.append(f"**统计范围**: 最近{days_back}天")
        lines.append(f"")
        
        # 总览
        if len(overall) > 0 and overall.iloc[0]['total'] > 0:
            row = overall.iloc[0]
            lines.append(f"## 总览")
            lines.append(f"")
            lines.append(f"| 指标 | 数值 |")
            lines.append(f"|------|------|")
            lines.append(f"| 总预测数 | {int(row['total'])} |")
            lines.append(f"| 正确数 | {int(row['correct'])} |")
            lines.append(f"| **整体准确率** | **{row['accuracy']}%** |")
            lines.append(f"")
            
            # 分级
            if len(by_tier) > 0:
                lines.append(f"## 分级准确率")
                lines.append(f"")
                lines.append(f"| 等级 | 预测数 | 正确数 | 准确率 | 平均分 |")
                lines.append(f"|------|--------|--------|--------|--------|")
                for _, t in by_tier.iterrows():
                    icon = {'S': '★', 'A': '▲', 'B': '○', 'C': '·'}.get(t['tier'], '')
                    lines.append(f"| {icon} {t['tier']} | {int(t['total'])} | {int(t['correct'])} | {t['accuracy']}% | {t['avg_score']} |")
                lines.append(f"")
            
            # 联赛排行
            if len(by_league) > 0:
                lines.append(f"## 联赛准确率 (≥5场)")
                lines.append(f"")
                lines.append(f"| 联赛 | 预测数 | 准确率 |")
                lines.append(f"|------|--------|--------|")
                for _, l in by_league.iterrows():
                    lines.append(f"| {l['league_name'][:30]} | {int(l['total'])} | {l['accuracy']}% |")
                lines.append(f"")
            
            # 错题本
            if len(errors) > 0:
                lines.append(f"## 最近错题本 (Top {min(10, len(errors))})")
                lines.append(f"")
                lines.append(f"| 日期 | 比赛 | 比分 | 预测 | 概率 | 等级 |")
                lines.append(f"|------|------|------|------|------|------|")
                for _, e in errors.iterrows():
                    prob_str = f"H={e['prob_h']:.1%} D={e['prob_d']:.1%} A={e['prob_a']:.1%}"
                    lines.append(f"| {e['match_date']} | {e['home_team_name'][:20]} vs {e['away_team_name'][:20]} | {int(e['home_score'])}-{int(e['away_score'])} | {e['predicted_result']} | {prob_str} | {e['tier']} |")
                lines.append(f"")
        else:
            lines.append(f"## 暂无数据")
            lines.append(f"")
            lines.append(f"最近{days_back}天没有已评估的预测记录。")
            lines.append(f"")
        
        lines.append(f"---")
        lines.append(f"*由 PredictionTracker 自动生成*")
        
        return '\n'.join(lines)
    
    def optimize_gates(self, min_samples: int = 30) -> Dict:
        """
        基于复盘数据自动调优门控参数
        
        目标: 让各等级的准确率达到预期
        - S级: ≥80%
        - A级: ≥70%  
        - B级: ≥60%
        """
        conn = sqlite3.connect(self.db_path)
        
        # 获取所有已评估的预测
        df = pd.read_sql_query('''
            SELECT predicted_result, actual_result, is_correct,
                   tier, total_score, confidence_score,
                   consensus_score, feature_score, odds_clarity_score
            FROM predictions
            WHERE is_correct IS NOT NULL
            ORDER BY predicted_at DESC
            LIMIT 500
        ''', conn)
        
        conn.close()
        
        if len(df) < min_samples:
            return {
                'status': 'insufficient_data',
                'message': f'需要至少{min_samples}条记录，当前{len(df)}条',
                'suggestions': []
            }
        
        suggestions = []
        
        # 分析各等级准确率
        tier_acc = {}
        for tier in ['S', 'A', 'B', 'C']:
            tier_df = df[df['tier'] == tier]
            if len(tier_df) > 0:
                tier_acc[tier] = tier_df['is_correct'].mean() * 100
        
        logger.info(f"当前等级准确率: {tier_acc}")
        
        # S级分析: 如果S级准确率<80%, 需要提升门槛
        if tier_acc.get('S', 100) < 80 and tier_acc.get('S', 0) > 0:
            s_df = df[df['tier'] == 'S']
            # 分析哪些S级预测错了
            s_errors = s_df[s_df['is_correct'] == 0]
            avg_conf_error = s_errors['confidence_score'].mean() if len(s_errors) > 0 else 0
            avg_cons_error = s_errors['consensus_score'].mean() if len(s_errors) > 0 else 0
            
            suggestions.append({
                'type': 'S级门槛提升',
                'reason': f'S级准确率仅{tier_acc["S"]:.0f}%, 低于80%目标',
                'action': f'提升置信度门槛或共识要求',
                'detail': f'S级错误平均置信分={avg_conf_error:.0f}, 共识分={avg_cons_error:.0f}'
            })
        
        # 检查是否过于保守(S级太少)
        s_count = len(df[df['tier'] == 'S'])
        total = len(df)
        if s_count > 0 and s_count / total < 0.02 and tier_acc.get('S', 100) > 85:
            suggestions.append({
                'type': 'S级覆盖率低',
                'reason': f'S级仅占{s_count/total*100:.1f}%, 可能过于保守',
                'action': f'可适当降低S级门槛以增加预测量'
            })
        
        # B级分析: 如果B级准确率太低
        if tier_acc.get('B', 100) < 55:
            suggestions.append({
                'type': 'B级质量不足',
                'reason': f'B级准确率仅{tier_acc["B"]:.0f}%, 低于55%',
                'action': f'提升B级门槛或减少B级预测'
            })
        
        return {
            'status': 'analyzed',
            'total_samples': len(df),
            'current_tier_accuracy': tier_acc,
            'suggestions': suggestions
        }


# ── 命令行接口 ──
if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='预测闭环追踪系统')
    parser.add_argument('action', choices=['eval', 'report', 'challenge', 'optimize', 'start'],
                       help='操作: eval评估 report报告 challenge进度 optimize优化 start开始挑战')
    parser.add_argument('--days', type=int, default=7, help='统计天数')
    parser.add_argument('--name', default='10场80%准确率挑战', help='挑战名称')
    
    args = parser.parse_args()
    
    tracker = PredictionTracker()
    
    if args.action == 'eval':
        result = tracker.evaluate(days_back=args.days)
        print(f"\n评估结果 (最近{args.days}天):")
        print(f"  评估数量: {result['evaluated']}")
        if result['evaluated'] > 0:
            print(f"  正确数: {result['correct']}")
            print(f"  准确率: {result['accuracy']}%")
            for tier, stats in result.get('tier_stats', {}).items():
                print(f"  [{tier}] {stats['count']}场, 准确率{stats['accuracy']}%")
    
    elif args.action == 'report':
        report = tracker.generate_report(days_back=args.days)
        print(report)
        # 同时保存
        report_path = f'output/prediction_report_{datetime.now().strftime("%Y%m%d_%H%M")}.md'
        os.makedirs('output', exist_ok=True)
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(report)
        print(f"\n报告已保存至: {report_path}")
    
    elif args.action == 'challenge':
        progress = tracker.get_challenge_progress()
        print(f"\n10场挑战进度:")
        if progress.get('status') == 'no_active_challenge':
            print("  当前无活跃挑战，请先运行 'start' 开始新挑战")
        else:
            c = progress['challenge']
            p = progress['progress']
            print(f"  挑战: {c['name']}")
            print(f"  目标: {c['target']}%准确率")
            print(f"  进度: {p['correct']}/{p['total_evaluated']} = {p['accuracy']}%")
            if p['remaining_to_target'] > 0:
                print(f"  还需评估 {p['remaining_to_target']} 场达到10场")
            else:
                print(f"  {'★ 达成目标!' if p['accuracy'] >= 80 else '✗ 未达标'}")
            
            if progress.get('recent_results'):
                print(f"\n  最近结果:")
                for r in progress['recent_results'][:10]:
                    icon = '✓' if r['correct'] else '✗'
                    print(f"  {icon} {r['match_date']} {r['home_team'][:15]:15s} vs {r['away_team'][:15]:15s} "
                          f"预测{r['predicted']} 实际{r['actual']} [{r['tier']}]")
    
    elif args.action == 'optimize':
        result = tracker.optimize_gates()
        print(f"\n参数优化建议:")
        print(f"  总样本: {result.get('total_samples', 0)}")
        print(f"  等级准确率: {result.get('current_tier_accuracy', {})}")
        for s in result.get('suggestions', []):
            print(f"\n  [{s['type']}]")
            print(f"  原因: {s['reason']}")
            print(f"  建议: {s['action']}")
    
    elif args.action == 'start':
        cid = tracker.start_challenge(args.name)
        print(f"\n新挑战已开始!")
        print(f"  挑战ID: {cid}")
        print(f"  名称: {args.name}")
        print(f"  目标: 80%准确率, 10场景计")
        print(f"  已自动关闭之前活跃的挑战")
