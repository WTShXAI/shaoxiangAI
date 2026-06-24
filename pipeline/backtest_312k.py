#!/usr/bin/env python
"""
哨响AI v4.1 — 31万条全量回测引擎
====================================
加载 training_extended → 特征反向验证 → 全维度切片指标
产出: pipeline/reports/backtest_report_*.json + 专家诊断输入

设计原则:
- 纯特征验证，不依赖模型推理（避免5秒/条）
- 用已知特征和标签直接计算各维度预测能力
- 按时序切分 pre-2023 训练 / 2023+ 验证
"""
import sqlite3
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DB_PATH = PROJECT_ROOT / "data" / "football_data.db"
REPORT_DIR = PROJECT_ROOT / "pipeline" / "reports"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

# ═══════════════════════════════════════════════════════════
# 数据加载
# ═══════════════════════════════════════════════════════════
def load_all_data():
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    rows = [dict(r) for r in db.execute('SELECT * FROM training_extended').fetchall()]
    db.close()
    return rows


# ═══════════════════════════════════════════════════════════
# 指标计算
# ═══════════════════════════════════════════════════════════
def accuracy(preds, labels):
    correct = sum(1 for p, l in zip(preds, labels) if p == l)
    return correct / len(labels) if labels else 0

def f1_per_class(preds, labels, target):
    tp = sum(1 for p, l in zip(preds, labels) if p == target and l == target)
    fp = sum(1 for p, l in zip(preds, labels) if p == target and l != target)
    fn = sum(1 for p, l in zip(preds, labels) if p != target and l == target)
    prec = tp / (tp + fp) if (tp + fp) > 0 else 0
    rec = tp / (tp + fn) if (tp + fn) > 0 else 0
    return 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0, prec, rec


# ═══════════════════════════════════════════════════════════
# 回测核心
# ═══════════════════════════════════════════════════════════
def run_backtest():
    print("📊 加载31.2万条数据...")
    t0 = time.time()
    data = load_all_data()
    print(f"   加载完成: {len(data):,} 条, 耗时 {time.time()-t0:.1f}s")

    # ── 按时序切分 ──
    pre_2023 = [r for r in data if r['match_date'] < '2023-01-01']
    post_2023 = [r for r in data if r['match_date'] >= '2023-01-01']
    print(f"   切分: pre-2023={len(pre_2023):,} | 2023+={len(post_2023):,}")

    # ── 基线：赔率隐含概率作为朴素预测 ──
    def implied_pred(row):
        # 赔率隐含 → 取最高概率方向
        h, d, a = row['odds_imp_h'], row['odds_imp_d'], row['odds_imp_a']
        if h >= d and h >= a: return 'H'
        elif d >= a: return 'D'
        else: return 'A'

    report = {
        'meta': {
            'total_rows': len(data),
            'pre_2023': len(pre_2023),
            'post_2023': len(post_2023),
            'date_range': f"{data[0]['match_date']} ~ {data[-1]['match_date']}",
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
        },
        'baseline': {},
        'slices': {},
        'findings': [],
    }

    # ── 1. 全量基线：赔率隐含 vs 实际 ──
    all_preds = [implied_pred(r) for r in data]
    all_labels = [r['final_result'] for r in data]
    report['baseline'] = {
        'description': '赔率隐含概率 → 取最高方向',
        'accuracy': round(accuracy(all_preds, all_labels), 4),
        'H_f1': round(f1_per_class(all_preds, all_labels, 'H')[0], 4),
        'D_f1': round(f1_per_class(all_preds, all_labels, 'D')[0], 4),
        'A_f1': round(f1_per_class(all_preds, all_labels, 'A')[0], 4),
        'H_prec': round(f1_per_class(all_preds, all_labels, 'H')[1], 4),
        'H_rec': round(f1_per_class(all_preds, all_labels, 'H')[2], 4),
        'D_prec': round(f1_per_class(all_preds, all_labels, 'D')[1], 4),
        'D_rec': round(f1_per_class(all_preds, all_labels, 'D')[2], 4),
        'A_prec': round(f1_per_class(all_preds, all_labels, 'A')[1], 4),
        'A_rec': round(f1_per_class(all_preds, all_labels, 'A')[2], 4),
    }
    print(f"\n📈 基线准确率: {report['baseline']['accuracy']:.2%}")
    print(f"   D-F1: {report['baseline']['D_f1']:.4f}")

    # ── 2. 时序切片 ──
    for period_name, period_data in [('pre-2023', pre_2023), ('2023+', post_2023)]:
        preds = [implied_pred(r) for r in period_data]
        labels = [r['final_result'] for r in period_data]
        report['slices'][period_name] = {
            'count': len(period_data),
            'accuracy': round(accuracy(preds, labels), 4),
            'H_f1': round(f1_per_class(preds, labels, 'H')[0], 4),
            'D_f1': round(f1_per_class(preds, labels, 'D')[0], 4),
            'A_f1': round(f1_per_class(preds, labels, 'A')[0], 4),
        }

    # ── 3. 赔率分桶切片 (spread区间) ──
    spread_buckets = [
        ('强热门(>0.50)', lambda r: r['odds_spread'] > 0.50),
        ('中热(0.20-0.50)', lambda r: 0.20 < r['odds_spread'] <= 0.50),
        ('微热(0.08-0.20)', lambda r: 0.08 < r['odds_spread'] <= 0.20),
        ('均衡(0.03-0.08)', lambda r: 0.03 < r['odds_spread'] <= 0.08),
        ('极度均衡(<0.03)', lambda r: r['odds_spread'] <= 0.03),
    ]
    for name, fn in spread_buckets:
        subset = [r for r in data if fn(r)]
        if len(subset) < 100:
            continue
        preds = [implied_pred(r) for r in subset]
        labels = [r['final_result'] for r in subset]
        _, h_prec, _ = f1_per_class(preds, labels, 'H')
        d_prec, _, _ = f1_per_class(preds, labels, 'D')
        _, a_prec, _ = f1_per_class(preds, labels, 'A')
        label_dist = {
            'H': sum(1 for r in subset if r['final_result'] == 'H') / len(subset),
            'D': sum(1 for r in subset if r['final_result'] == 'D') / len(subset),
            'A': sum(1 for r in subset if r['final_result'] == 'A') / len(subset),
        }
        report['slices'][f'spread_{name}'] = {
            'count': len(subset),
            'accuracy': round(accuracy(preds, labels), 4),
            'D_rate_actual': round(label_dist['D'], 3),
            'D_f1': round(f1_per_class(preds, labels, 'D')[0], 4),
        }

    # ── 4. 赔率方向切片 (H热门 vs A热门) ──
    for dir_name, dir_fn in [
        ('H热门(spread>0)', lambda r: r['odds_spread'] > 0),
        ('A热门(spread<0)', lambda r: r['odds_spread'] < 0),
    ]:
        subset = [r for r in data if dir_fn(r) and abs(r['odds_spread']) < 0.5]
        preds = [implied_pred(r) for r in subset]
        labels = [r['final_result'] for r in subset]
        report['slices'][f'oddsdir_{dir_name}'] = {
            'count': len(subset),
            'accuracy': round(accuracy(preds, labels), 4),
            'D_f1': round(f1_per_class(preds, labels, 'D')[0], 4),
            'H_f1': round(f1_per_class(preds, labels, 'H')[0], 4),
            'A_f1': round(f1_per_class(preds, labels, 'A')[0], 4),
        }

    # ── 5. drift 信号切片 ──
    drift_slices = [
        ('drift_sharp=1', lambda r: r['drift_sharp_signal'] == 1),
        ('drift_sharp=0', lambda r: r['drift_sharp_signal'] == 0),
        ('drift>0.05', lambda r: r['drift_magnitude'] and r['drift_magnitude'] > 0.05),
        ('drift<=0.02', lambda r: r['drift_magnitude'] and r['drift_magnitude'] <= 0.02),
    ]
    for name, fn in drift_slices:
        subset = [r for r in data if fn(r)]
        if len(subset) < 100:
            continue
        preds = [implied_pred(r) for r in subset]
        labels = [r['final_result'] for r in subset]
        report['slices'][f'drift_{name}'] = {
            'count': len(subset),
            'accuracy': round(accuracy(preds, labels), 4),
            'D_f1': round(f1_per_class(preds, labels, 'D')[0], 4),
        }

    # ── 6. OTSM 信号切片 ──
    otsm_slices = [
        ('LOCKED>0.8', lambda r: r['otsm_state_LOCKED'] > 0.8),
        ('LOCKED<0.2', lambda r: r['otsm_state_LOCKED'] < 0.2),
        ('NOISE_high', lambda r: r['otsm_state_NOISE'] > 0.8),
        ('entropy_low', lambda r: r['otsm_entropy_drift'] and r['otsm_entropy_drift'] < 0.1),
    ]
    for name, fn in otsm_slices:
        subset = [r for r in data if fn(r)]
        if len(subset) < 100:
            continue
        preds = [implied_pred(r) for r in subset]
        labels = [r['final_result'] for r in subset]
        report['slices'][f'otsm_{name}'] = {
            'count': len(subset),
            'accuracy': round(accuracy(preds, labels), 4),
            'D_f1': round(f1_per_class(preds, labels, 'D')[0], 4),
            'H_f1': round(f1_per_class(preds, labels, 'H')[0], 4),
        }

    # ── 7. D预测方向偏差分析 ──
    d_preds = [r for r in data if implied_pred(r) == 'D']
    d_correct = [r for r in d_preds if r['final_result'] == 'D']
    report['slices']['D_pred_analysis'] = {
        'total_D_predictions': len(d_preds),
        'D_precision': round(len(d_correct) / len(d_preds), 4) if d_preds else 0,
        'D_pred_rate': round(len(d_preds) / len(data), 4),
        'D_actual_rate': round(sum(1 for r in data if r['final_result'] == 'D') / len(data), 4),
    }

    # ── 8. 联赛级详细切片 ──
    league_data = defaultdict(list)
    for r in data:
        league_data[r['league_name']].append(r)
    
    top_leagues = sorted(league_data.items(), key=lambda x: -len(x[1]))[:25]
    league_report = {}
    for league, rows in top_leagues:
        preds = [implied_pred(r) for r in rows]
        labels = [r['final_result'] for r in rows]
        d_rate = sum(1 for r in rows if r['final_result'] == 'D') / len(rows)
        league_report[league] = {
            'count': len(rows),
            'accuracy': round(accuracy(preds, labels), 4),
            'D_rate': round(d_rate, 3),
            'D_f1': round(f1_per_class(preds, labels, 'D')[0], 4),
            'H_f1': round(f1_per_class(preds, labels, 'H')[0], 4),
            'A_f1': round(f1_per_class(preds, labels, 'A')[0], 4),
        }
    report['slices']['by_league'] = league_report

    # ── 9. 关键发现 ──
    # 检测: 哪个spread区间D-F1最低
    d_f1_by_spread = [(k, v['D_f1'], v['count']) for k, v in report['slices'].items() if k.startswith('spread_')]
    worst_d = min(d_f1_by_spread, key=lambda x: x[1])
    report['findings'].append({
        'type': 'worst_D_spread',
        'slice': worst_d[0],
        'D_f1': worst_d[1],
        'count': worst_d[2],
        'action': f'检查 {worst_d[0]} 区间D预测策略',
    })

    # 检测: 时序退化
    pre_acc = report['slices']['pre-2023']['accuracy']
    post_acc = report['slices']['2023+']['accuracy']
    if post_acc < pre_acc - 0.02:
        report['findings'].append({
            'type': 'temporal_decay',
            'pre_2023_acc': pre_acc,
            'post_2023_acc': post_acc,
            'decay': round(pre_acc - post_acc, 4),
            'action': '模型在2023+数据上退化，需检查特征漂移',
        })

    # 检测: D预测严重不足
    d_actual = report['slices']['D_pred_analysis']['D_actual_rate']
    d_pred = report['slices']['D_pred_analysis']['D_pred_rate']
    if d_pred < d_actual * 0.5:
        report['findings'].append({
            'type': 'D_under_prediction',
            'actual_D_rate': round(d_actual, 3),
            'pred_D_rate': round(d_pred, 3),
            'gap': round(d_actual - d_pred, 3),
            'action': f'D预测严重不足: 实际{d_actual:.1%} vs 预测{d_pred:.1%}，需降低平局预测门槛',
        })

    # ── 写入报告 ──
    report_path = REPORT_DIR / f"backtest_312k_{time.strftime('%Y%m%d_%H%M%S')}.json"
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    
    print(f"\n✅ 报告已保存: {report_path}")
    print(f"   总耗时: {time.time()-t0:.1f}s")
    print(f"   发现 {len(report['findings'])} 个关键问题")

    return report


if __name__ == "__main__":
    report = run_backtest()
    
    # 打印关键指标
    print("\n" + "="*60)
    print("📋 回测核心发现")
    print("="*60)
    for f in report['findings']:
        print(f"\n  [{f['type']}]")
        for k, v in f.items():
            if k != 'type':
                print(f"    {k}: {v}")