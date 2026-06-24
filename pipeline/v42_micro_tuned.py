#!/usr/bin/env python
"""
哨响AI v4.2 — 10赛季70/20/10分层回测 + 微调验证
===================================================
微调项:
  1. argmax → D软阈值 (D>28% & D>max(H,A)×85% → 预测D)
  2. spread安全区分级 (中热/均衡降权, 强热门信任)
  3. OTSM LOCKED信号门控 (LOCKED>0.5信任增强)
  4. drift质量分级 (stable=好, chaotic=逆信号)
  5. 联赛D先验注入 (27联赛校准值)

数据切分:
  训练集: 2012-2018 (69.1%, ~215K)
  修正集: 2019-2022 (18.3%, ~57K)
  验证集: 2023-2025 (12.6%, ~39K)
"""
import sqlite3
import json
import time
from pathlib import Path
from collections import defaultdict

PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH = PROJECT_ROOT / "data" / "football_data.db"
REPORT_DIR = PROJECT_ROOT / "pipeline" / "reports"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

# ═══════════════════════════════════════════════════════════
# v4.2 微调配置
# ═══════════════════════════════════════════════════════════

# 微调1: D软阈值参数
D_SOFT_THRESHOLD = {
    'abs_min': 0.28,        # D概率绝对值下限
    'relax_factor': 0.85,   # D vs max(H,A) 的相对系数
}

# 微调2: spread安全区分级 (回测U型曲线)
SPREAD_ZONES = {
    'strong_fav': {'lo': 0.50, 'hi': 999,   'd_strategy': 'never',    'conf_boost': 0.06},
    'medium_fav': {'lo': 0.20, 'hi': 0.50,  'd_strategy': 'relaxed',  'conf_boost': -0.05},
    'slight_fav': {'lo': 0.08, 'hi': 0.20,  'd_strategy': 'relaxed',  'conf_boost': -0.08},
    'balanced':    {'lo': 0.03, 'hi': 0.08, 'd_strategy': 'aggressive','conf_boost': -0.10},
    'ultra_even':  {'lo': 0.00, 'hi': 0.03, 'd_strategy': 'cautious', 'conf_boost': -0.02},
    'away_fav':    {'lo': -999, 'hi': -0.50,'d_strategy': 'never',    'conf_boost': 0.06},
    'away_medium': {'lo': -0.50,'hi': -0.20,'d_strategy': 'relaxed',  'conf_boost': -0.05},
}

# 微调3: OTSM门控
OTSM_GATE = {
    'locked_high': 0.50,   # LOCKED可信门槛 (原>0.8, 降到0.5扩大覆盖)
    'conf_boost_locked': 0.08,  # LOCKED状态下的置信加成
}

# 微调4: drift质量分级
DRIFT_QUALITY = {
    'stable_max': 0.02,    # 稳定赔率上限 → 好信号
    'chaotic_min': 0.12,   # 剧烈波动下限 → 逆信号
}

# 微调5: 联赛D先验 (从校准数据)
LEAGUE_D_PRIORS = {
    "意乙": 0.329, "阿乙": 0.328, "意丙1": 0.311, "法乙": 0.310,
    "西乙": 0.307, "西丙": 0.298, "葡甲": 0.285, "阿甲": 0.281,
    "日职乙": 0.280, "英冠": 0.279, "英甲": 0.275, "英乙": 0.274,
    "德乙": 0.273, "法甲": 0.272, "意甲": 0.270, "德甲": 0.260,
    "西甲": 0.255, "英超": 0.248, "荷甲": 0.253, "葡超": 0.261,
    "巴甲": 0.258, "俄超": 0.265, "土超": 0.268, "J联赛": 0.272,
}
DEFAULT_D_RATE = 0.257

# ═══════════════════════════════════════════════════════════
# v4.3 新增: 庄家盘口深度信号 (来自 v3.2 多层赔率分析)
# ═══════════════════════════════════════════════════════════
# 核心逻辑: 赔率数字可以被操控，但庄家用真金白银开的盘口深度说不了谎
#   - 浅盘(让球不足) + 高水位 = 庄家不信 → D升权
#   - 低OU线(≤2.5) = 低比分环境 → D升权
#   - 深盘 + 低水位 = 庄家极度自信 → 信任H/A

HANDICAP_DEPTH_SIGNAL = {
    # 让球深度与赔率预期的比值 → D修正
    # 比值<0.5 → 让球严重不足 → 庄家不信 → D+15%
    'shallow_ratio': 0.5,    # 实际让球/预期让球 < 此值 = 浅盘危险
    'shallow_d_boost': 0.15,  # 浅盘D升权
    # 比值>1.5 → 让球超过预期 → 庄家极度自信 → 信任
    'deep_ratio': 1.5,
    'deep_d_penalty': -0.10,
}

OU_LINE_SIGNAL = {
    # OU线越低，越利好平局（低比分环境）
    'low_ou': 2.5,           # ≤此值 = 低OU环境
    'low_ou_d_boost': 0.09,  # 低OU D升权 (苏格兰vs摩洛哥: OU 2/2.5)
    'ultra_low_ou': 2.0,     # ≤此值 = 极低OU
    'ultra_low_ou_d_boost': 0.15,  # 极低OU D升权
}

WATER_LEVEL_SIGNAL = {
    # 水位越高 = 庄家越急着引诱投注 → 反向信号
    'trap_water': 2.00,      # ≥此值 = 诱盘水位 (美国让1球@2.02)
    'trap_d_boost': 0.07,    # 诱盘水位D升权
    'safe_water': 1.90,      # ≤此值 = 安全水位 (巴西@1.88)
    'safe_confidence': 0.04,  # 安全水位信任加成
}


def get_spread_zone(spread):
    """获取spread安全等级"""
    for name, cfg in SPREAD_ZONES.items():
        if cfg['lo'] <= spread < cfg['hi']:
            return name, cfg
    return 'unknown', {'d_strategy': 'never', 'conf_boost': 0}


def get_drift_quality(drift_mag):
    """drift质量分级"""
    if drift_mag is None:
        return 'moderate'
    if drift_mag <= DRIFT_QUALITY['stable_max']:
        return 'stable'
    elif drift_mag > DRIFT_QUALITY['chaotic_min']:
        return 'chaotic'
    return 'moderate'


def load_data():
    """加载全量数据"""
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    rows = [dict(r) for r in db.execute('SELECT * FROM training_extended').fetchall()]
    db.close()
    return rows


def split_by_season(data):
    """按时序70/20/10切分"""
    train = []   # 2012-2018
    correct = []  # 2019-2022
    validate = []  # 2023-2025
    for r in data:
        year = int(r['match_date'][:4])
        if year <= 2018:
            train.append(r)
        elif year <= 2022:
            correct.append(r)
        else:
            validate.append(r)
    return train, correct, validate


def predict_v42(row, phase='validate', handicap=None, ou_line=None, water_level=None):
    """
    v4.3 预测器 — 7项微调融合
    v4.2(5项): D软阈值 + spread安全区 + OTSM门控 + drift质量 + 联赛先验
    v4.3(+3项): 盘口深度 + OU线 + 水位 (来自v3.2多层赔率)
    """
    h, d, a = row['odds_imp_h'], row['odds_imp_d'], row['odds_imp_a']
    spread = row.get('odds_spread', 0) or 0
    drift_mag = row.get('drift_magnitude', 0) or 0
    lock_conf = row.get('otsm_state_LOCKED', 0) or 0
    league = row.get('league_name', '') or ''

    # 微调5: 联赛D先验注入
    league_d_rate = LEAGUE_D_PRIORS.get(league, DEFAULT_D_RATE)
    d_boosted = d * (league_d_rate / DEFAULT_D_RATE)

    # 微调2: spread安全区
    zone_name, zone_cfg = get_spread_zone(abs(spread))
    if zone_cfg['d_strategy'] == 'aggressive':
        d_boosted *= 1.15
    elif zone_cfg['d_strategy'] == 'relaxed':
        d_boosted *= 1.08
    elif zone_cfg['d_strategy'] == 'never':
        d_boosted *= 0.60
    
    # ── v4.3: 庄家信心信号 (盘口深度 + OU + 水位 + 平衡模式) ──
    bm_skepticism = 0  # 庄家怀疑度 [0,1]，越高越不信H/A预测

    if handicap is not None and handicap != 0:
        odds_home = row.get('odds_home') or row.get('open_home') or (1/h if h>0 else 2)
        odds_away = row.get('odds_away') or row.get('open_away') or (1/a if a>0 else 3)
        odds_ratio = odds_away / max(odds_home, 0.01) if odds_home else 1
        expected_hcp = max(0, (odds_ratio - 1) * 1.5)
        actual_hcp = abs(handicap)
        
        if expected_hcp > 0.2 and actual_hcp < expected_hcp * 0.4:
            bm_skepticism += 0.30  # 严重浅盘: 庄家极度不信 (美国案例)
        elif expected_hcp > 0.2 and actual_hcp < expected_hcp * 0.7:
            bm_skepticism += 0.15

    if ou_line is not None and ou_line > 0:
        if ou_line <= 2.0:
            bm_skepticism += 0.15
        elif ou_line <= 2.5:
            bm_skepticism += 0.09

    if water_level is not None and water_level >= 2.00:
        bm_skepticism += 0.07

    # v4.3 新增: 平衡+低OU模式识别 (苏格兰vs摩洛哥案例)
    if abs(h - a) < 0.25 and ou_line is not None and ou_line <= 2.5:
        bm_skepticism += 0.12  # 均衡+低OU → 经典平局候选

    # 庄家怀疑度 → D升权 + H/A降权
    if bm_skepticism > 0.15:
        d_boosted *= (1 + bm_skepticism * 0.5)
        h = h * (1 - bm_skepticism * 0.4)
        a = a * (1 - bm_skepticism * 0.4)

    # 微调3: OTSM门控
    if lock_conf > OTSM_GATE['locked_high']:
        pass
    elif lock_conf < 0.2:
        d_boosted *= 0.90

    # 微调4: drift质量
    dq = get_drift_quality(drift_mag)
    if dq == 'stable':
        pass
    elif dq == 'chaotic':
        d_boosted *= 0.85
    
    # 微调1: D软阈值决策
    d_threshold = max(D_SOFT_THRESHOLD['abs_min'], 
                      max(h, a) * D_SOFT_THRESHOLD['relax_factor'])
    
    if d_boosted > d_threshold and d_boosted > max(h, a) * 0.85:
        return 'D'
    elif h >= a:
        return 'H'
    else:
        return 'A'


def compute_metrics(preds, labels):
    """计算全维度指标"""
    correct = sum(1 for p, l in zip(preds, labels) if p == l)
    acc = correct / len(labels) if labels else 0
    
    # 每类指标
    metrics = {}
    for cls in ['H', 'D', 'A']:
        tp = sum(1 for p, l in zip(preds, labels) if p == cls and l == cls)
        fp = sum(1 for p, l in zip(preds, labels) if p == cls and l != cls)
        fn = sum(1 for p, l in zip(preds, labels) if p != cls and l == cls)
        tn = sum(1 for p, l in zip(preds, labels) if p != cls and l != cls)
        
        prec = tp / (tp + fp) if (tp + fp) > 0 else 0
        rec = tp / (tp + fn) if (tp + fn) > 0 else 0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0
        
        metrics[f'{cls}_f1'] = round(f1, 4)
        metrics[f'{cls}_prec'] = round(prec, 4)
        metrics[f'{cls}_rec'] = round(rec, 4)
        metrics[f'{cls}_tp'] = tp
        metrics[f'{cls}_fp'] = fp
        metrics[f'{cls}_fn'] = fn
    
    metrics['accuracy'] = round(acc, 4)
    
    # 预测分布
    n = len(labels)
    metrics['pred_H_rate'] = round(sum(1 for p in preds if p == 'H') / n, 4)
    metrics['pred_D_rate'] = round(sum(1 for p in preds if p == 'D') / n, 4)
    metrics['pred_A_rate'] = round(sum(1 for p in preds if p == 'A') / n, 4)
    
    # 实际分布
    metrics['actual_H_rate'] = round(sum(1 for l in labels if l == 'H') / n, 4)
    metrics['actual_D_rate'] = round(sum(1 for l in labels if l == 'D') / n, 4)
    metrics['actual_A_rate'] = round(sum(1 for l in labels if l == 'A') / n, 4)
    
    return metrics


def run_pipeline():
    """主流水线"""
    print("📊 加载31.2万条数据...")
    t0 = time.time()
    data = load_data()
    train, correct, validate = split_by_season(data)
    print(f"   训练集: {len(train):,} | 修正集: {len(correct):,} | 验证集: {len(validate):,}")
    print(f"   加载+切分耗时: {time.time()-t0:.1f}s")

    # ── 1. 训练集: 计算基线 + 网格搜索最优阈值 ──
    print("\n🔧 修正集超参数搜索...")
    best_threshold = D_SOFT_THRESHOLD.copy()
    best_d_f1 = 0
    
    # 网格搜索 relax_factor 和 abs_min
    for relax in [0.80, 0.82, 0.85, 0.88, 0.90]:
        for abs_min in [0.26, 0.28, 0.30, 0.32]:
            # 临时修改阈值
            D_SOFT_THRESHOLD['relax_factor'] = relax
            D_SOFT_THRESHOLD['abs_min'] = abs_min
            
            preds = [predict_v42(r, 'correct') for r in correct]
            labels = [r['final_result'] for r in correct]
            metrics = compute_metrics(preds, labels)
            
            if metrics['D_f1'] > best_d_f1 and metrics['accuracy'] > 0.50:
                best_d_f1 = metrics['D_f1']
                best_threshold = {'abs_min': abs_min, 'relax_factor': relax}
    
    D_SOFT_THRESHOLD.update(best_threshold)
    print(f"   最优D阈值: abs_min={best_threshold['abs_min']}, relax={best_threshold['relax_factor']}")
    print(f"   修正集最佳D-F1: {best_d_f1:.4f}")

    # ── 2. 全量评估 ──
    report = {
        'version': 'v4.2-micro',
        'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
        'split': {
            'train': {'years': '2012-2018', 'count': len(train), 'pct': round(len(train)/len(data)*100,1)},
            'correct': {'years': '2019-2022', 'count': len(correct), 'pct': round(len(correct)/len(data)*100,1)},
            'validate': {'years': '2023-2025', 'count': len(validate), 'pct': round(len(validate)/len(data)*100,1)},
        },
        'tuning_params': best_threshold,
        'phase_results': {},
    }

    for phase_name, phase_data in [('训练集(2012-2018)', train), ('修正集(2019-2022)', correct), ('验证集(2023-2025)', validate)]:
        preds = [predict_v42(r, phase_name) for r in phase_data]
        labels = [r['final_result'] for r in phase_data]
        m = compute_metrics(preds, labels)
        report['phase_results'][phase_name] = m
        
        print(f"\n{'='*60}")
        print(f"📈 {phase_name} ({m['pred_H_rate']+m['pred_D_rate']+m['pred_A_rate']:.0%} → 对比实际)")
        print(f"   Acc: {m['accuracy']:.2%}")
        print(f"   H-F1: {m['H_f1']:.4f} (P={m['H_prec']:.4f} R={m['H_rec']:.4f})")
        print(f"   D-F1: {m['D_f1']:.4f} (P={m['D_prec']:.4f} R={m['D_rec']:.4f})  ← 关键指标")
        print(f"   A-F1: {m['A_f1']:.4f} (P={m['A_prec']:.4f} R={m['A_rec']:.4f})")
        print(f"   预测分布: H={m['pred_H_rate']:.1%} D={m['pred_D_rate']:.1%} A={m['pred_A_rate']:.1%}")
        print(f"   实际分布: H={m['actual_H_rate']:.1%} D={m['actual_D_rate']:.1%} A={m['actual_A_rate']:.1%}")

    # ── 3. spread切片 (验证集) ──
    print(f"\n{'='*60}")
    print("📊 验证集 spread 切片分析")
    zone_results = {}
    for zone_name, zone_cfg in SPREAD_ZONES.items():
        if zone_cfg['lo'] >= 999 or zone_cfg['lo'] <= -999:
            continue
        subset = [r for r in validate if abs(r.get('odds_spread',0) or 0) >= zone_cfg['lo'] and abs(r.get('odds_spread',0) or 0) < zone_cfg['hi']]
        if len(subset) < 50:
            continue
        preds = [predict_v42(r, 'validate') for r in subset]
        labels = [r['final_result'] for r in subset]
        m = compute_metrics(preds, labels)
        zone_results[zone_name] = {'count': len(subset), **m}
        d_gap = m['pred_D_rate'] - m['actual_D_rate']
        print(f"  {zone_name:15s} {len(subset):>6,d}场 Acc={m['accuracy']:.2%} D-F1={m['D_f1']:.4f} D预测={m['pred_D_rate']:.1%} D实际={m['actual_D_rate']:.1%} 差距={d_gap:+.1%}")

    report['spread_zones'] = zone_results

    # ── 写入报告 ──
    report_path = REPORT_DIR / f"v42_micro_tuned_{time.strftime('%Y%m%d_%H%M%S')}.json"
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"\n✅ 报告已保存: {report_path}")

    return report


if __name__ == "__main__":
    report = run_pipeline()
    
    # 最终对比
    val = report['phase_results']['验证集(2023-2025)']
    print("\n" + "="*60)
    print("🎯 最终验证集指标 (2023-2025, 39K场)")
    print("="*60)
    print(f"  准确率: {val['accuracy']:.2%}")
    print(f"  D-F1:   {val['D_f1']:.4f} (前值: 0.0059 → {val['D_f1']/0.0059:.0f}x 提升)")
    print(f"  D召回:  {val['D_rec']:.1%} (前值: 0.3%)")
    print(f"  D精确:  {val['D_prec']:.1%} (前值: 37.2%)")
    print(f"  H-F1:   {val['H_f1']:.4f}")
    print(f"  A-F1:   {val['A_f1']:.4f}")
    print(f"\n  最优参数: abs_min={report['tuning_params']['abs_min']}, relax={report['tuning_params']['relax_factor']}")