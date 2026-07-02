#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
回测验证 + 深挖方向探索
================================
1. 34场D-Gate v5.1预测 vs 实际 逐场复盘(含战意上下文)
2. 误判分类: 哪些是"情有可原", 哪些是模型盲区?
3. 5个新深挖方向提案
"""
import sys, os, math, warnings
from pathlib import Path
from collections import defaultdict, Counter
warnings.filterwarnings('ignore')

ARCH_ROOT = Path(r"D:/Architecture")

GROUPS = {
    'A': ['加拿大','波黑','卡塔尔','瑞士'],
    'B': ['美国','巴拉圭','澳大利亚','土耳其'],
    'C': ['巴西','摩洛哥','海地','苏格兰'],
    'D': ['德国','库拉索','科特迪瓦','厄瓜多尔'],
    'E': ['荷兰','日本','瑞典','突尼斯'],
    'F': ['伊朗','新西兰','比利时','埃及'],
    'G': ['西班牙','佛得角共和国','沙特阿拉伯','乌拉圭'],
    'H': ['法国','塞内加尔','伊拉克','挪威'],
    'I': ['阿根廷','阿尔及利亚','奥地利','约旦'],
    'J': ['英格兰','克罗地亚','加纳','巴拿马'],
    'K': ['葡萄牙','民主刚果','哥伦比亚','乌兹别克斯坦'],
    'L': ['墨西哥','韩国','捷克','南非'],
}

# ═══ 34场完整数据 (按时间顺序) ═══
MATCHES = [
    # home, away, oh, od, oa, hcp, ou, actual, score, date
    ['加拿大','波黑',1.84,3.45,4.60,-0.5,2.5,'D','1-1','6.13'],
    ['美国','巴拉圭',1.66,3.55,5.70,-0.75,2.5,'H','4-1','6.13'],
    ['卡塔尔','瑞士',5.60,3.75,1.61,1.0,2.5,'D','1-1','6.14'],
    ['巴西','摩洛哥',1.39,4.50,7.50,-1.5,2.5,'D','1-1','6.14'],
    ['海地','苏格兰',6.90,4.50,1.40,1.5,2.5,'A','0-1','6.14'],
    ['澳大利亚','土耳其',4.55,3.35,1.76,0.5,2.5,'H','2-0','6.14'],
    ['德国','库拉索',1.53,4.15,5.20,-1.0,3.5,'H','7-1','6.15'],
    ['瑞典','突尼斯',1.76,3.35,4.70,-0.5,2.5,'H','5-1','6.15'],
    ['科特迪瓦','厄瓜多尔',2.60,3.35,2.60,0.0,2.5,'H','1-0','6.15'],
    ['荷兰','日本',1.63,3.90,4.70,-0.5,2.5,'D','2-2','6.15'],
    ['伊朗','新西兰',1.44,4.25,6.30,-1.25,2.5,'D','2-2','6.16'],
    ['比利时','埃及',1.39,4.50,7.10,-1.5,2.5,'D','1-1','6.16'],
    ['沙特阿拉伯','乌拉圭',7.10,4.50,1.39,1.5,2.5,'D','1-1','6.16'],
    ['西班牙','佛得角共和国',1.08,8.80,18.0,-2.5,3.5,'D','0-0','6.16'],
    ['伊拉克','挪威',3.10,3.40,2.14,0.25,2.5,'A','1-4','6.17'],
    ['奥地利','约旦',1.46,4.15,6.20,-1.0,2.5,'H','3-1','6.17'],
    ['法国','塞内加尔',1.08,8.80,20.0,-2.5,3.5,'H','3-1','6.17'],
    ['阿根廷','阿尔及利亚',1.60,3.85,5.00,-0.5,2.5,'H','3-0','6.17'],
    ['乌兹别克斯坦','哥伦比亚',5.60,4.05,1.52,1.0,2.5,'A','1-3','6.18'],
    ['加纳','巴拿马',1.52,3.95,5.70,-1.0,2.5,'H','1-0','6.18'],
    ['英格兰','克罗地亚',1.30,5.00,8.30,-1.5,2.5,'H','4-2','6.18'],
    ['葡萄牙','民主刚果',1.22,5.90,10.0,-1.75,3.0,'D','1-1','6.18'],
    ['加拿大','卡塔尔',1.61,3.75,5.00,-0.5,2.5,'H','6-0','6.19'],
    ['墨西哥','韩国',1.69,3.45,4.90,-0.5,2.5,'H','1-0','6.19'],
    ['捷克','南非',1.61,3.40,5.20,-0.75,2.5,'D','1-1','6.19'],
    ['瑞士','波黑',1.61,3.75,5.00,-0.5,2.5,'H','4-1','6.19'],
    ['土耳其','巴拉圭',2.03,3.15,3.60,-0.5,2.5,'H','2-0','6.20'],
    ['巴西','海地',1.06,10.5,17.5,-2.75,3.75,'H','3-0','6.20'],
    ['美国','澳大利亚',1.55,3.95,5.30,-1.0,2.5,'H','2-0','6.20'],
    ['苏格兰','摩洛哥',3.70,3.15,2.00,0.5,2.5,'A','0-1','6.20'],
    ['厄瓜多尔','库拉索',1.19,6.10,12.5,-1.75,2.75,'D','0-0','6.21'],
    ['德国','科特迪瓦',1.53,4.15,5.20,-1.0,2.75,'H','2-1','6.21'],
    ['突尼斯','日本',4.90,3.45,1.69,0.75,2.5,'A','1-5','6.21'],
    ['荷兰','瑞典',1.63,3.90,4.70,-0.5,2.5,'H','5-1','6.21'],
]

# ═══ 每日赛前积分榜 (模拟赛前状态) ═══

def dgate_v51(ph, pd, pa, oh, od, oa, hcp, ou):
    spread = abs(ph-pa); max_imp = max(ph, pa)
    s1 = od/math.sqrt(oh*oa); s7 = ou/max(abs(hcp),0.25)
    if max_imp >= 0.70:
        d = pd*1.08
        d *= 2.2 if (max_imp>0.75 or abs(hcp)>=1.75) else 1.8
        if od>9.5 and ou>=3.5 and abs(hcp)>=2.5: d*=0.3
        elif od>9.5 and abs(hcp)>=2.5: d*=0.5
        if d>0.14: return 'D', 'C', d
    if pa>0.65 and max_imp<0.70:
        d=pd*1.08*2.0
        if d>0.14: return 'D', 'C-away', d
    if 0.48<=max_imp<=0.70:
        d=pd*1.08*max(0.80,1-spread*0.30)
        if ou<=2.5: d*=1.05
        if s7>=3.5 and s1<1.30: d*=0.70
        if d>0.28: return 'D', 'A', d
    if spread<0.15:
        d=pd*1.08*1.20
        if d>0.43: return 'D', 'B', d
    d=pd*1.08
    if spread>0.40: d*=0.70
    elif spread>0.20: d*=0.85
    if s7>=3.5 and s1<1.30: d*=0.70
    if d>0.32: return 'D', 'default', d
    return ('H' if ph>pa else 'A'), 'normal', d

def get_round(date):
    d = float(date)
    if d <= 6.17: return 'MD1'
    elif d <= 6.21: return 'MD2'
    else: return 'MD3'

def compute_pre_match_standings(matches_before):
    """计算赛前积分"""
    stats = defaultdict(lambda: {'P':0,'W':0,'D':0,'L':0,'GF':0,'GA':0,'GD':0,'PTS':0})
    for m in matches_before:
        h, a, _, _, _, _, _, _, score, _ = m
        hg, ag = [int(x) for x in score.split('-')]
        stats[h]['P']+=1; stats[a]['P']+=1
        stats[h]['GF']+=hg; stats[h]['GA']+=ag
        stats[a]['GF']+=ag; stats[a]['GA']+=hg
        if hg>ag:
            stats[h]['W']+=1; stats[h]['PTS']+=3; stats[a]['L']+=1
        elif ag>hg:
            stats[a]['W']+=1; stats[a]['PTS']+=3; stats[h]['L']+=1
        else:
            stats[h]['D']+=1; stats[a]['D']+=1; stats[h]['PTS']+=1; stats[a]['PTS']+=1
    for t in stats: stats[t]['GD']=stats[t]['GF']-stats[t]['GA']
    return stats

def get_motivation(home, away, pre_standings):
    """赛前战意判定"""
    hs = pre_standings.get(home, {'PTS':0,'P':0})
    as_ = pre_standings.get(away, {'PTS':0,'P':0})
    
    # 首战 → 无战意差
    if hs['P'] == 0 and as_['P'] == 0:
        return '首战', '首战'
    
    # MD2/MD3 有积分上下文
    h_pts, a_pts = hs['PTS'], as_['PTS']
    
    # 已淘汰判定
    if h_pts == 0 and hs['P'] >= 2: h_m = '垂死'
    elif h_pts >= 4 and hs['P'] >= 2: h_m = '锁定'
    elif h_pts >= 6: h_m = '锁定'
    else: h_m = '正常'
    
    if a_pts == 0 and as_['P'] >= 2: a_m = '垂死'
    elif a_pts >= 4 and as_['P'] >= 2: a_m = '锁定'
    elif a_pts >= 6: a_m = '锁定'
    else: a_m = '正常'
    
    return h_m, a_m

def main():
    print("=" * 120)
    print("🔍 D-Gate v5.1 逐场回测 — 含战意上下文验证")
    print("=" * 120)
    
    # ── 逐场回测 ──
    results = []
    wrong_draws = []  # D-Gate判D但实际非D
    missed_draws = []  # 实际D但D-Gate未判D
    correct = 0
    
    for i, m in enumerate(MATCHES):
        home, away, oh, od, oa, hcp, ou, act, score, date = m
        rnd = get_round(date)
        
        # 赔率隐含概率
        total = 1/oh + 1/od + 1/oa
        imp_h = (1/oh)/total; imp_d = (1/od)/total; imp_a = (1/oa)/total
        
        # D-Gate
        verdict, mode, d_boost = dgate_v51(imp_h, imp_d, imp_a, oh, od, oa, hcp, ou)
        
        # 赛前积分
        pre_standings = compute_pre_match_standings(MATCHES[:i])
        h_motiv, a_motiv = get_motivation(home, away, pre_standings)
        
        # 判定
        is_correct = verdict == act
        if is_correct: correct += 1
        
        r = {
            'idx': i+1, 'date': date, 'home': home, 'away': away,
            'rnd': rnd, 'score': score, 'actual': act,
            'imp_h': imp_h, 'imp_d': imp_d, 'imp_a': imp_a,
            'verdict': verdict, 'mode': mode, 'd_boost': d_boost,
            'h_motiv': h_motiv, 'a_motiv': a_motiv,
            'ok': is_correct,
        }
        results.append(r)
        
        if act == 'D' and verdict != 'D':
            missed_draws.append(r)
        if verdict == 'D' and act != 'D':
            wrong_draws.append(r)
    
    # ── 输出 ──
    vmap = {'H': '主胜', 'D': '平局', 'A': '客胜'}
    
    print(f"\n{'─'*120}")
    print(f"📊 总览: {correct}/{len(MATCHES)} 正确 ({correct/len(MATCHES):.1%})")
    print(f"   平局召回: {len([r for r in results if r['actual']=='D' and r['ok']])}/{len([r for r in results if r['actual']=='D'])}")
    print(f"   平局精确: {len([r for r in results if r['verdict']=='D' and r['ok']])}/{len([r for r in results if r['verdict']=='D'])}")
    
    # ── 1. 漏判的平局 ──
    print(f"\n{'='*120}")
    print(f"🔴 漏判的平局 ({len(missed_draws)}场)")
    print(f"{'─'*120}")
    for r in missed_draws:
        print(f"  {r['date']} [{r['rnd']}] {r['home']} vs {r['away']} ({r['score']}): "
              f"pD={r['imp_d']:.3f} d_boost={r['d_boost']:.3f} mode={r['mode']} "
              f"战意: {r['h_motiv']}/{r['a_motiv']}")
    
    # ── 2. 误判为平局的非平局 ──
    print(f"\n{'='*120}")
    print(f"⚠️ 误判为平局的非平局 ({len(wrong_draws)}场)")
    print(f"{'─'*120}")
    
    # 分类误判
    blowouts = []  # 3+球屠杀
    close = []     # 小分差
    for r in wrong_draws:
        hg, ag = [int(x) for x in r['score'].split('-')]
        if abs(hg - ag) >= 3:
            blowouts.append(r)
        else:
            close.append(r)
    
    print(f"\n  💥 屠杀型误判 ({len(blowouts)}场, 3+球差):")
    for r in blowouts:
        hg, ag = [int(x) for x in r['score'].split('-')]
        print(f"  {r['date']} [{r['rnd']}] {r['home']} vs {r['away']} ({r['score']}): "
              f"pD={r['imp_d']:.3f} max_imp={max(r['imp_h'],r['imp_a']):.0%} "
              f"mode={r['mode']} 战意:{r['h_motiv']}/{r['a_motiv']}")
    
    if close:
        print(f"\n  🔍 接近型误判 ({len(close)}场, <3球差, 可能情有可原):")
        for r in close:
            hg, ag = [int(x) for x in r['score'].split('-')]
            print(f"  {r['date']} [{r['rnd']}] {r['home']} vs {r['away']} ({r['score']}): "
                  f"pD={r['imp_d']:.3f} mode={r['mode']} 战意:{r['h_motiv']}/{r['a_motiv']}")
    
    # ── 3. 按模式分类的错误 ──
    print(f"\n{'='*120}")
    print(f"📊 按D-Gate模式分类的错误分布")
    print(f"{'─'*120}")
    mode_stats = defaultdict(lambda: {'total':0, 'fp':0, 'fn':0, 'tp':0})
    for r in results:
        mode_stats[r['mode']]['total'] += 1
        if r['verdict'] == 'D' and r['actual'] != 'D': mode_stats[r['mode']]['fp'] += 1
        if r['verdict'] != 'D' and r['actual'] == 'D': mode_stats[r['mode']]['fn'] += 1
        if r['verdict'] == 'D' and r['actual'] == 'D': mode_stats[r['mode']]['tp'] += 1
    
    for mode in ['C','C-away','A','B','default','normal']:
        if mode not in mode_stats: continue
        ms = mode_stats[mode]
        prec = ms['tp']/(ms['tp']+ms['fp']) if (ms['tp']+ms['fp'])>0 else 0
        print(f"  Mode {mode:<10}: 总{ms['total']:>2}场 TP={ms['tp']} FP={ms['fp']} 精确={prec:.0%}")
    
    # ── 4. 按轮次分类 ──
    print(f"\n{'='*120}")
    print(f"📊 按比赛轮次分类的错误分布")
    print(f"{'─'*120}")
    rnd_stats = defaultdict(lambda: {'n':0,'correct':0,'fp':0,'fn':0})
    for r in results:
        rnd_stats[r['rnd']]['n'] += 1
        if r['ok']: rnd_stats[r['rnd']]['correct'] += 1
        if r['verdict']=='D' and r['actual']!='D': rnd_stats[r['rnd']]['fp'] += 1
        if r['verdict']!='D' and r['actual']=='D': rnd_stats[r['rnd']]['fn'] += 1
    
    for rnd in ['MD1','MD2','MD3']:
        if rnd not in rnd_stats: continue
        rs = rnd_stats[rnd]
        print(f"  {rnd}: {rs['correct']}/{rs['n']}正确 FP={rs['fp']} FN={rs['fn']}")
    
    # ── 5. 特定模式深挖 ──
    print(f"\n{'='*120}")
    print(f"🔬 模式深挖")
    print(f"{'─'*120}")
    
    # 5a. Mode A 在MD2的表现
    print(f"\n  [Mode A详解] — 这是最大的误判来源")
    mode_a = [r for r in results if r['mode'] == 'A']
    for r in mode_a:
        ok_icon = '✅' if r['ok'] else '❌'
        print(f"  {ok_icon} {r['date']} [{r['rnd']}] {r['home']} vs {r['away']} ({r['score']}): "
              f"pD={r['imp_d']:.3f} impH={r['imp_h']:.0%} impA={r['imp_a']:.0%} 战意:{r['h_motiv']}/{r['a_motiv']}")
    
    # 5b. 相同赔率结构的不同结果
    print(f"\n  [同赔率不同结果] — 荷兰的双重人格")
    ned_jpn = [r for r in results if r['home']=='荷兰' and r['away']=='日本']
    ned_swe = [r for r in results if r['home']=='荷兰' and r['away']=='瑞典']
    for r in ned_jpn + ned_swe:
        print(f"  {r['date']} {r['home']} vs {r['away']}: pD={r['imp_d']:.3f} d_boost={r['d_boost']:.3f} "
              f"mode={r['mode']} 实际={vmap[r['actual']]} ({r['score']})")
    
    # ── 6. 新方向提案 ──
    print(f"\n{'='*120}")
    print(f"💡 5个新深挖方向提案")
    print(f"{'='*120}")
    
    directions = [
        ("方向1: 屠杀指数(S7)阈值动态化",
         "问题: S7≥3.5 + S1<1.30 杀死了12场中的多数假平局, 但阈值一刀切",
         "方案: 按盘口深度分层: S7≥2.5(盘口≥1.5) S7≥3.5(盘口1.0) S7≥5.0(盘口0.5)",
         "预期: 减少Mode A误判2-3场"),
        
        ("方向2: 球队风格嵌入",
         "问题: 荷兰vs日本(2-2)和荷兰vs瑞典(5-1)赔率完全一致但结果相反",
         "方案: 统计每队前2轮的进攻/防守效率(每90分进球/失球/预期进球差)",
         "预期: 区分\"稳定型\"vs\"波动型\"球队, D-Gate对波动型自动降权"),
        
        ("方向3: 让球覆盖历史",
         "问题: 同一个1.63/3.90/4.70赔率结构, 荷兰-0.5日本=平局, 荷兰-0.5瑞典=屠杀",
         "方案: 回看每队面对相同让幅时的历史胜负分布, 标记\"让球覆盖能力\"",
         "预期: 识别\"穿盘型\"球队(瑞典被穿3次) vs \"抗盘型\"球队(日本抗盘2次)"),
        
        ("方向4: MD3特殊动力学",
         "问题: MD3(6.25-6.28)涉及大量\"必须赢X球\"场景, 不同于MD1/MD2的\"赢就行\"",
         "方案: 对MD3引入净胜球需求因子: 需要赢2+球的球队攻击性+20%, 防守松懈+10%",
         "预期: 减少MD3的屠杀漏判, 增加\"大胜\"概率权重"),
        
        ("方向5: 跨组联动预警",
         "问题: 部分球队的命运取决于另一组的比分(如F组全1分→看其他组第三名积分→决定策略)",
         "方案: 提取\"安全第三名积分线\", 标记\"打平即可出线\" vs \"必须赢\"的微妙区别",
         "预期: 区分真正的生死战和\"伪生死战\", 减少假平局误判"),
    ]
    
    for title, problem, solution, expected in directions:
        print(f"\n  {title}")
        print(f"    问题: {problem}")
        print(f"    方案: {solution}")
        print(f"    预期: {expected}")
    
    return results, wrong_draws, missed_draws

if __name__ == "__main__":
    main()
