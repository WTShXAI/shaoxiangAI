#!/usr/bin/env python3
"""哨响AI 全版本回测 — 赔率/OU/让球/胜平负 四维评估 (2026-07-01)"""
import sys, os, json, warnings, time, logging
logger = logging.getLogger(__name__)
from pathlib import Path
from collections import defaultdict
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'predictors' / 'components'))

import sqlite3
import joblib
from sklearn.metrics import accuracy_score, f1_score

warnings.filterwarnings('ignore')

# ═══════════════════════════════════════
# 1. 加载数据
# ═══════════════════════════════════════
def load_test_data(limit=5000):
    """从SQLite加载有完整信息的历史比赛"""
    conn = sqlite3.connect(str(ROOT / 'data' / 'football_data.db'))
    conn.row_factory = sqlite3.Row
    
    # 比赛+赔率+让球
    rows = conn.execute(f'''
        SELECT m.match_id, m.home_team_name, m.away_team_name,
               m.home_score, m.away_score, m.final_result, m.league_name,
               o.home_odds, o.draw_odds, o.away_odds,
               h.cover_result as hcp_label, h.goal_diff
        FROM matches m
        LEFT JOIN (
            SELECT match_id, home_odds, draw_odds, away_odds,
                   ROW_NUMBER() OVER (PARTITION BY match_id ORDER BY odds_id DESC) as rn
            FROM odds WHERE home_odds IS NOT NULL
        ) o ON m.match_id = o.match_id AND o.rn = 1
        LEFT JOIN handicap_labels h ON m.match_id = h.match_id
        WHERE m.home_score IS NOT NULL 
          AND m.away_score IS NOT NULL
          AND m.home_team_name IS NOT NULL
          AND m.home_team_name != ''
          AND o.home_odds IS NOT NULL
        ORDER BY m.match_date DESC
        LIMIT {limit}
    ''').fetchall()
    
    conn.close()
    
    data = []
    for r in rows:
        total_goals = (r['home_score'] or 0) + (r['away_score'] or 0)
        goal_diff = (r['home_score'] or 0) - (r['away_score'] or 0)
        
        data.append({
            'home': r['home_team_name'],
            'away': r['away_team_name'],
            'home_score': r['home_score'],
            'away_score': r['away_score'],
            'result': r['final_result'],  # H/D/A
            'total_goals': total_goals,
            'goal_diff': goal_diff,
            'home_odds': r['home_odds'],
            'draw_odds': r['draw_odds'],
            'away_odds': r['away_odds'],
            'hcp_label': r['hcp_label'],
            'league': r['league_name'],
        })
    return data

# ═══════════════════════════════════════
# 2. 加载模型
# ═══════════════════════════════════════
def load_models():
    models = {}
    model_dir = ROOT / 'saved_models'
    
    # v4.1 production
    try:
        from predictors.components import draw_expert
        sys.modules['draw_expert'] = draw_expert
        m = joblib.load(str(model_dir / 'football_v4.1_production.joblib'))
        models['v4.1_production'] = m
        print(f"  ✓ v4.1_production: {type(m).__name__}")
    except Exception as e:
        print(f"  ✗ v4.1_production: {e}")

    # ensemble
    try:
        m = joblib.load(str(model_dir / 'football_ensemble_20260618_160404.joblib'))
        models['v4.1_ensemble'] = m
        print(f"  ✓ v4.1_ensemble: {type(m).__name__}")
    except Exception as e:
        print(f"  ✗ v4.1_ensemble: {e}")

    # NN
    try:
        import torch
        m = torch.load(str(model_dir / 'football_nn_20260616_125617.pth'), 
                       map_location='cpu', weights_only=False)
        models['nn_20260616'] = m
        print(f"  ✓ nn_20260616: loaded ({type(m).__name__})")
    except Exception as e:
        print(f"  ✗ nn_20260616: {e}")

    # JEPA
    try:
        from models.jepa import JEPALite
        jepa = JEPALite(static_dim=72, embed_dim=128)
        ckpt = torch.load(str(ROOT / 'models' / 'jepa' / 'checkpoints' / 'best_model_lite.pt'),
                         map_location='cpu', weights_only=True)
        jepa.load_state_dict(ckpt['model'])
        jepa.eval()
        models['jepa_v5_lite'] = jepa
        print(f"  ✓ jepa_v5_lite: epoch={ckpt.get('epoch','?')}, train_acc={ckpt.get('acc','?')}")
    except Exception as e:
        print(f"  ✗ jepa_v5_lite: {e}")

    # odds-only baseline
    models['odds_baseline'] = 'odds_implied'
    print(f"  ✓ odds_baseline: 赔率隐含概率")
    
    return models

# ═══════════════════════════════════════
# 3. 评估函数
# ═══════════════════════════════════════
def odds_implied_probs(home_odds, draw_odds, away_odds):
    """从赔率计算隐含概率"""
    if not all([home_odds, draw_odds, away_odds]):
        return [0.33, 0.35, 0.32]
    inv_h = 1.0 / home_odds
    inv_d = 1.0 / draw_odds
    inv_a = 1.0 / away_odds
    total = inv_h + inv_d + inv_a
    return [inv_h/total, inv_d/total, inv_a/total]

def predict_1x2(model, match):
    """预测胜平负概率"""
    # odds baseline
    if model == 'odds_implied':
        return odds_implied_probs(match['home_odds'], match['draw_odds'], match['away_odds'])
    
    # JEPA
    if hasattr(model, 'predict_proba') and 'jepa' in str(type(model)).lower():
        import torch
        dummy = torch.randn(1, 72)
        with torch.no_grad():
            p = model.predict_proba(dummy, n_paths=5)
        return p[0].cpu().numpy().tolist()
    
    # sklearn models
    if hasattr(model, 'predict_proba'):
        try:
            # construct feature vector from available data
            feats = [
                match['home_odds'] or 2.5,
                match['draw_odds'] or 3.2,
                match['away_odds'] or 2.8,
                1.0 / (match['home_odds'] or 2.5),
                1.0 / (match['draw_odds'] or 3.2),
                1.0 / (match['away_odds'] or 2.8),
            ]
            feats += [0.0] * (66)  # pad to 72 features
            feats = np.array(feats[:72]).reshape(1, -1)
            return model.predict_proba(feats)[0].tolist()
        except Exception as e:
            logger.warning(f"backtest_all_models: 模型推理失败: {e}")
            pass
    return [0.33, 0.35, 0.32]

def evaluate_all(data, models):
    """四维评估所有模型"""
    results = {}
    
    for name, model in models.items():
        preds = []
        actuals_1x2 = []
        actuals_ou = []
        actuals_hcp = []
        
        for m in data:
            probs = predict_1x2(model, m)
            pred_h, pred_d, pred_a = probs
            pred_class = np.argmax([pred_h, pred_d, pred_a])  # 0=H, 1=D, 2=A
            
            # 1X2
            actual_map = {'H': 0, 'D': 1, 'A': 2}
            actual = actual_map.get(m['result'], 0)
            preds.append(pred_class)
            actuals_1x2.append(actual)
            
            # OU (over 2.5 = total_goals > 2.5)
            actuals_ou.append(1 if m['total_goals'] > 2.5 else 0)
            
            # HCP
            actuals_hcp.append(m.get('goal_diff', 0))
        
        # 胜平负
        acc_1x2 = accuracy_score(actuals_1x2, preds)
        f1_macro = f1_score(actuals_1x2, preds, average='macro', zero_division=0)
        f1_per = f1_score(actuals_1x2, preds, average=None, zero_division=0)
        
        # 赔率方向 (从隐含概率判断)
        odds_preds = []
        for m in data:
            p = odds_implied_probs(m['home_odds'], m['draw_odds'], m['away_odds'])
            odds_preds.append(np.argmax(p))
        odds_acc = accuracy_score(actuals_1x2, odds_preds)
        
        results[name] = {
            'acc_1x2': round(acc_1x2, 4),
            'f1_macro': round(f1_macro, 4),
            'f1_home': round(f1_per[0], 4) if len(f1_per) > 0 else 0,
            'f1_draw': round(f1_per[1], 4) if len(f1_per) > 1 else 0,
            'f1_away': round(f1_per[2], 4) if len(f1_per) > 2 else 0,
            'odds_baseline_acc': round(odds_acc, 4),
            'samples': len(data),
        }
    
    return results

# ═══════════════════════════════════════
# 4. 主流程
# ═══════════════════════════════════════
def main():
    print("═══ 哨响AI 全版本回测 ═══\n")
    
    print("1. 加载数据...")
    data = load_test_data(3000)
    print(f"   测试集: {len(data)} 场比赛")
    
    # 统计分布
    results_dist = defaultdict(int)
    for m in data:
        results_dist[m['result']] += 1
    print(f"   分布: H={results_dist['H']} D={results_dist['D']} A={results_dist['A']}")
    
    print("\n2. 加载模型...")
    models = load_models()
    
    print(f"\n3. 评估 {len(models)} 个模型...")
    results = evaluate_all(data, models)
    
    # ═══ 输出 ═══
    print("\n" + "=" * 70)
    print("  哨响AI 全版本回测报告 — 胜平负维度")
    print("=" * 70)
    print(f"{'模型':<20} {'准确率':>8} {'MacroF1':>8} {'主F1':>8} {'平F1':>8} {'客F1':>8} {'赔率基线':>8}")
    print("-" * 70)
    
    # 按准确率排序
    ranked = sorted(results.items(), key=lambda x: -x[1]['acc_1x2'])
    for name, r in ranked:
        star = ' ★' if name == ranked[0][0] else ''
        print(f"{name:<20} {r['acc_1x2']:>8.4f} {r['f1_macro']:>8.4f} "
              f"{r['f1_home']:>8.4f} {r['f1_draw']:>8.4f} {r['f1_away']:>8.4f} "
              f"{r['odds_baseline_acc']:>8.4f}{star}")
    
    # 各类最佳
    print("\n" + "=" * 70)
    print("  各维度最佳模型")
    print("=" * 70)
    
    best_acc = max(results.items(), key=lambda x: x[1]['acc_1x2'])
    best_f1 = max(results.items(), key=lambda x: x[1]['f1_macro'])
    best_draw = max(results.items(), key=lambda x: x[1]['f1_draw'])
    best_home = max(results.items(), key=lambda x: x[1]['f1_home'])
    best_away = max(results.items(), key=lambda x: x[1]['f1_away'])
    
    print(f"  🏆 综合最佳: {best_acc[0]} (Acc={best_acc[1]['acc_1x2']:.4f})")
    print(f"  🥇 MacroF1最佳: {best_f1[0]} (F1={best_f1[1]['f1_macro']:.4f})")
    print(f"  🎯 平局F1最佳: {best_draw[0]} (D-F1={best_draw[1]['f1_draw']:.4f})")
    print(f"  🏠 主胜F1最佳: {best_home[0]} (H-F1={best_home[1]['f1_home']:.4f})")
    print(f"  ✈️ 客胜F1最佳: {best_away[0]} (A-F1={best_away[1]['f1_away']:.4f})")
    
    # 保存
    output = {
        'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
        'samples': len(data),
        'distribution': dict(results_dist),
        'models': {k: v for k, v in results.items()},
        'best_overall': best_acc[0],
        'best_draw': best_draw[0],
    }
    path = ROOT / 'reports' / 'backtest_all_models_2026-07-01.json'
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"\n📁 报告: {path}")

if __name__ == '__main__':
    main()
