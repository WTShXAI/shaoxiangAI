#!/usr/bin/env python
"""
哨响AI v4.2 — 2026世界杯28场验证
===================================
赔率来源: digital-sanctuary.net (赛前赔率)
赛果来源: FIFA官方
"""
import json, time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
REPORT_DIR = PROJECT_ROOT / "pipeline" / "reports"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

# ═══════════════ v4.2 微调配置 ═══════════════
D_SOFT = {'abs_min': 0.26, 'relax_factor': 0.80}
WC_D_RATE = 0.268
DEFAULT_D_RATE = 0.257

def predict_v42(h_odds, d_odds, a_odds):
    """v4.2 微调版预测器 (5项微调融合)"""
    inv = 1/h_odds + 1/d_odds + 1/a_odds
    h, d, a = (1/h_odds)/inv, (1/d_odds)/inv, (1/a_odds)/inv
    spread = abs(h - a)

    # 微调5: 世界杯D先验
    d_boosted = d * (WC_D_RATE / DEFAULT_D_RATE)

    # 微调2: spread安全区
    if spread > 0.50:
        d_boosted *= 0.60  # 强热门永不预测D
    elif 0.03 <= spread < 0.08:
        d_boosted *= 1.15  # 均衡黄金区
    elif 0.08 <= spread < 0.50:
        d_boosted *= 1.08

    # 微调1: D软阈值决策
    threshold = max(D_SOFT['abs_min'], max(h, a) * D_SOFT['relax_factor'])
    if d_boosted > threshold and d_boosted > max(h, a) * 0.85:
        return 'D', h, d_boosted, a
    elif h >= a:
        return 'H', h, d_boosted, a
    else:
        return 'A', h, d_boosted, a

# ═══════════════ 28场世界杯完整数据 ═══════════════
WC2026 = [
    # date, home, away, H_score, A_score, result, H_odds, D_odds, A_odds
    ('6/11','墨西哥','南非',2,0,'H',1.42,4.435,8.45),
    ('6/11','韩国','捷克',2,1,'H',2.702,3.08,2.836),
    ('6/12','加拿大','波黑',1,1,'D',1.888,3.55,4.895),
    ('6/12','美国','巴拉圭',4,1,'H',2.142,3.335,4.02),
    ('6/13','巴西','摩洛哥',1,1,'D',1.44,4.20,6.00),
    ('6/13','澳大利亚','土耳其',2,0,'H',4.00,3.60,1.80),
    ('6/13','海地','苏格兰',0,1,'A',6.25,4.75,1.38),
    ('6/13','卡塔尔','瑞士',1,1,'D',7.50,4.75,1.36),
    ('6/14','德国','库拉索',7,1,'H',1.02,21.0,41.0),
    ('6/14','科特迪瓦','厄瓜多尔',1,0,'H',3.10,3.25,2.15),
    ('6/14','荷兰','日本',2,2,'D',1.80,3.70,3.75),
    ('6/14','瑞典','突尼斯',5,1,'H',1.85,3.50,3.70),
    ('6/15','西班牙','佛得角',0,0,'D',1.07,10.0,29.0),
    ('6/15','比利时','埃及',1,1,'D',1.61,3.80,4.75),
    ('6/15','沙特阿拉伯','乌拉圭',1,1,'D',5.75,4.50,1.42),
    ('6/15','伊朗','新西兰',2,2,'D',1.57,3.80,5.00),
    ('6/16','法国','塞内加尔',3,1,'H',1.42,4.50,6.25),
    ('6/16','伊拉克','挪威',1,4,'A',8.50,5.75,1.22),
    ('6/16','阿根廷','阿尔及利亚',3,0,'H',1.30,4.75,8.00),
    ('6/17','奥地利','约旦',3,1,'H',1.30,4.75,9.00),
    ('6/17','英格兰','克罗地亚',4,2,'H',1.57,4.00,4.75),
    ('6/17','加纳','巴拿马',1,0,'H',1.80,3.50,3.90),
    ('6/17','葡萄牙','民主刚果',1,1,'D',1.27,5.00,10.0),
    ('6/17','乌兹别克斯坦','哥伦比亚',1,3,'A',6.50,4.50,1.38),
    ('6/18','捷克','南非',1,1,'D',1.919,3.735,4.41),
    ('6/18','瑞士','波黑',4,1,'H',1.591,4.31,6.46),
    ('6/18','加拿大','卡塔尔',6,0,'H',1.313,5.76,12.4),
    ('6/18','墨西哥','韩国',1,0,'H',2.081,3.40,4.15),
]

def run():
    print("⚽ 2026世界杯 28场验证")
    print("=" * 92)
    print(f"{'#':>3s} {'日期':6s} {'主队':12s} {'客队':12s} {'赔率':>18s} {'预测':5s} {'实际':5s} {'H%':>6s} {'D%':>6s} {'A%':>6s} {'比分':>5s}")
    print("-" * 92)

    correct = 0
    d_correct = d_actual = 0
    details = []
    
    # 按spread分组统计
    zone_stats = {'strong':[], 'medium':[], 'balanced':[], 'ultra':[]}

    for i, (date, home, away, hs, aws, result, ho, do, ao) in enumerate(WC2026):
        pred, h_p, d_p, a_p = predict_v42(ho, do, ao)
        score = f'{hs}-{aws}'
        match = '✅' if pred == result else '❌'
        if pred == result: correct += 1
        if result == 'D':
            d_actual += 1
            if pred == 'D': d_correct += 1

        spread = abs(h_p - a_p)
        if spread > 0.50: zone_stats['strong'].append(match)
        elif 0.03 <= spread < 0.08: zone_stats['balanced'].append(match)
        elif 0.08 <= spread < 0.50: zone_stats['medium'].append(match)
        else: zone_stats['ultra'].append(match)

        odds_str = f'{ho}/{do}/{ao}'
        print(f'{i+1:3d} {date:6s} {home:12s} {away:12s} {odds_str:>18s} {pred:5s} {result:5s} {h_p:.0%} {d_p:.0%} {a_p:.0%} {score:>5s} {match}')

        details.append({
            'date':date,'home':home,'away':away,
            'odds':[ho,do,ao],'pred':pred,'actual':result,
            'probs':[round(h_p,3),round(d_p,3),round(a_p,3)],
            'score':score,'correct':pred==result
        })

    print("-" * 92)
    n = len(details)

    # 统计
    pred_dist = {'H':0,'D':0,'A':0}
    actual_dist = {'H':0,'D':0,'A':0}
    for d in details:
        actual_dist[d['actual']] += 1
        pred_dist[d['pred']] += 1

    print(f"\n{'='*60}")
    print("📈 v4.2 世界杯验证结果")
    print(f"{'='*60}")
    print(f"  准确率: {correct}/{n} = {correct/n:.1%}")
    print(f"  D召回:  {d_correct}/{d_actual} ({d_correct/d_actual:.1%})" if d_actual else "")
    print(f"  实际分布: H={actual_dist['H']} D={actual_dist['D']} A={actual_dist['A']}")
    print(f"  预测分布: H={pred_dist['H']} D={pred_dist['D']} A={pred_dist['A']}")

    # spread切片
    print(f"\n  spread切片:")
    for zone, results in zone_stats.items():
        if results:
            acc = sum(1 for r in results if r == '✅') / len(results)
            print(f"    {zone:10s}: {len(results):>2d}场 Acc={acc:.1%}")

    # 冷门分析
    upsets = [(i,d) for i,d in enumerate(details) if d['actual'] != 'H' and d['odds'][0] < 2.0]
    print(f"\n  冷门检测: {len(upsets)}场热门方未胜 (赔率<2.0)")
    for i, d in upsets:
        print(f"    #{i+1} {d['home']} vs {d['away']} 赔率{d['odds'][0]}/{d['odds'][1]}/{d['odds'][2]} 实际={d['actual']} 预测={d['pred']} {'✅' if d['correct'] else '❌'}")

    # 写入报告
    report = {
        'version':'v4.2-worldcup',
        'timestamp':time.strftime('%Y-%m-%d %H:%M:%S'),
        'matches':n,'correct':correct,'accuracy':round(correct/n,3),
        'd_recall':round(d_correct/d_actual,3) if d_actual else 0,
        'pred_dist':pred_dist,'actual_dist':actual_dist,
        'zone_stats':{k:round(sum(1 for r in v if r=='✅')/len(v),3) for k,v in zone_stats.items() if v},
        'details':details,
    }
    path = REPORT_DIR / f"wc2026_v42_{time.strftime('%Y%m%d_%H%M%S')}.json"
    with open(path,'w',encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"\n✅ 报告: {path}")

if __name__ == "__main__":
    run()
