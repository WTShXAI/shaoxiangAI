#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
D-Gate v5.2.5 — 增强版平局检测引擎
===================================
v5.2.4 → v5.2.5:
- 风格标签≥2场: blowout_ratio/draw_ratio至少2场才生效
- C-away降倍提门槛: ×2.0→×1.5, threshold 0.14→0.35
- Default窄spread探测: spread<0.25+Elo差<150→微提平局
- 球星效应: STAR_PLAYERS字典, 哈兰德/马内/姆巴佩等
v5.2.3 → v5.2.4:
- Mode C: 大盘口(abs(hcp)≥1.75)降boost(2.2→1.4)+提门槛(0.14→0.45)
v5.1 → v5.2 三项升级:
1. S7动态阈值 — 按盘口深度分层, 拯救加拿大vs波黑漏判
2. 穿盘/抗盘标记 — 34场赛果提取球队覆盖能力
3. 同赔率对照 — 本届历史相似赔率的胜负分布
"""
import math
from collections import defaultdict

# ═══════════════════════════════════════
# 1. 穿盘/抗盘标记 — 从34场赛果提取
# ═══════════════════════════════════════

# 34场完整赛果: (主队, 客队, 主进球, 客进球, 盘口(主队让), 日期)
ALL_RESULTS = [
    ('加拿大','波黑',1,1,-0.5,'6.13'), ('美国','巴拉圭',4,1,-0.75,'6.13'),
    ('卡塔尔','瑞士',1,1,1.0,'6.14'), ('巴西','摩洛哥',1,1,-1.5,'6.14'),
    ('海地','苏格兰',0,1,1.5,'6.14'), ('澳大利亚','土耳其',2,0,0.5,'6.14'),
    ('德国','库拉索',7,1,-1.0,'6.15'), ('瑞典','突尼斯',5,1,-0.5,'6.15'),
    ('科特迪瓦','厄瓜多尔',1,0,0.0,'6.15'), ('荷兰','日本',2,2,-0.5,'6.15'),
    ('伊朗','新西兰',2,2,-1.25,'6.16'), ('比利时','埃及',1,1,-1.5,'6.16'),
    ('沙特阿拉伯','乌拉圭',1,1,1.5,'6.16'), ('西班牙','佛得角共和国',0,0,-2.5,'6.16'),
    ('伊拉克','挪威',1,4,0.25,'6.17'), ('奥地利','约旦',3,1,-1.0,'6.17'),
    ('法国','塞内加尔',3,1,-2.5,'6.17'), ('阿根廷','阿尔及利亚',3,0,-0.5,'6.17'),
    ('乌兹别克斯坦','哥伦比亚',1,3,1.0,'6.18'), ('加纳','巴拿马',1,0,-1.0,'6.18'),
    ('英格兰','克罗地亚',4,2,-1.5,'6.18'), ('葡萄牙','民主刚果',1,1,-1.75,'6.18'),
    ('加拿大','卡塔尔',6,0,-0.5,'6.19'), ('墨西哥','韩国',1,0,-0.5,'6.19'),
    ('捷克','南非',1,1,-0.75,'6.19'), ('瑞士','波黑',4,1,-0.5,'6.19'),
    ('土耳其','巴拉圭',2,0,-0.5,'6.20'), ('巴西','海地',3,0,-2.75,'6.20'),
    ('美国','澳大利亚',2,0,-1.0,'6.20'), ('苏格兰','摩洛哥',0,1,0.5,'6.20'),
    ('厄瓜多尔','库拉索',0,0,-1.75,'6.21'), ('德国','科特迪瓦',2,1,-1.0,'6.21'),
    ('突尼斯','日本',1,5,0.75,'6.21'), ('荷兰','瑞典',5,1,-0.5,'6.21'),
]

def build_cover_database():
    """
    构建穿盘/抗盘数据库
    cover_rate: 作为让球方时覆盖盘口的比例
    anti_cover_rate: 作为受让方时抗住盘口的比例
    blowout_rate: 比赛出现3+球差的比例
    """
    db = defaultdict(lambda: {
        'as_fav': 0,       # 作为让球方的比赛数
        'covered': 0,      # 穿盘次数
        'as_dog': 0,       # 作为受让方的比赛数
        'anti_covered': 0, # 抗盘成功次数
        'blowouts': 0,     # 3+球差的比赛(无论输赢)
        'total': 0,        # 总比赛数
        'goals_for': 0,    # 总进球
        'goals_against': 0, # 总失球
        'draws': 0,        # 平局次数
    })
    
    for h, a, hg, ag, hcp, _date in ALL_RESULTS:
        db[h]['total'] += 1
        db[a]['total'] += 1
        db[h]['goals_for'] += hg
        db[h]['goals_against'] += ag
        db[a]['goals_for'] += ag
        db[a]['goals_against'] += hg
        if hg == ag:
            db[h]['draws'] += 1
            db[a]['draws'] += 1
        
        # 穿盘/抗盘
        margin = hg - ag
        if hcp < 0:  # 主队让球
            db[h]['as_fav'] += 1
            db[a]['as_dog'] += 1
            if margin > abs(hcp):  # 主队穿盘
                db[h]['covered'] += 1
            if margin > hcp:  # 客队抗盘(输不超过让球)
                db[a]['anti_covered'] += 1
        elif hcp > 0:  # 客队让球
            db[h]['as_dog'] += 1
            db[a]['as_fav'] += 1
            if margin > -hcp:  # 主队抗盘
                db[h]['anti_covered'] += 1
            if ag - hg > abs(hcp):  # 客队穿盘
                db[a]['covered'] += 1
        else:  # 平手盘
            db[h]['as_fav'] += 1
            db[a]['as_fav'] += 1
            if margin > 0:
                db[h]['covered'] += 1
            elif margin < 0:
                db[a]['covered'] += 1
        
        # 屠杀标记
        if abs(margin) >= 3:
            db[h]['blowouts'] += 1
            db[a]['blowouts'] += 1
    
    # 计算比率
    for team in db:
        d = db[team]
        n = d['total']
        d['cover_rate'] = d['covered'] / d['as_fav'] if d['as_fav'] > 0 else 0.5
        d['anti_rate'] = d['anti_covered'] / d['as_dog'] if d['as_dog'] > 0 else 0.5
        d['blowout_ratio'] = d['blowouts'] / n if n > 0 else 0
        d['draw_ratio'] = d['draws'] / n if n > 0 else 0
        d['gf90'] = d['goals_for'] / n if n > 0 else 1.5
        d['ga90'] = d['goals_against'] / n if n > 0 else 1.5
        d['volatility'] = abs(d['gf90'] - d['ga90']) / max(d['gf90'], d['ga90'], 1)
        
        # 风格分类 (v5.2.1: 至少2场才分类, 单场归均衡型)
        if n >= 2:
            if d['gf90'] >= 2.5 and d['ga90'] >= 2.0:
                d['style'] = '互捅型'
            elif d['gf90'] >= 1.5 and d['ga90'] <= 1.0:
                d['style'] = '稳赢型'
            elif d['gf90'] <= 1.0 and d['ga90'] <= 1.0 and d['draw_ratio'] >= 0.5:
                d['style'] = '沉闷型'
            else:
                d['style'] = '均衡型'
        else:
            d['style'] = '均衡型'  # 单场数据不足以分类
    
    return db

COVER_DB = build_cover_database()

# ═══════════════════════════════════════
# 2. 同赔率对照表
# ═══════════════════════════════════════

def build_similar_odds_db():
    """
    从34场已完成比赛中提取(imp_h, imp_d, imp_a)三元组
    为每个待预测场次找相似赔率结构的历史结果
    """
    history = []
    for h, a, hg, ag, hcp, _date in ALL_RESULTS:
        # 从盘口反推大概赔率区间(用比分结果 + 盘口)
        history.append({
            'home': h, 'away': a, 'hg': hg, 'ag': ag,
            'hcp': hcp, 'margin': hg - ag,
            'outcome': 'H' if hg > ag else ('D' if hg == ag else 'A'),
        })
    return history

ODDS_HISTORY = build_similar_odds_db()

def find_similar_matches(imp_h, imp_a, hcp, max_results=5):
    """
    找本届已赛的相似赔率结构比赛
    相似度: imp_h接近 + hcp接近
    """
    similar = []
    for m in ODDS_HISTORY:
        # 用盘口作为主要相似度指标(因为赔率隐含概率需要反推)
        hcp_diff = abs(m['hcp'] - hcp)
        if hcp_diff <= 0.75:  # 盘口差不超过0.75
            similar.append(m)
    
    # 按盘口差排序
    similar.sort(key=lambda m: abs(m['hcp'] - hcp))
    return similar[:max_results]

# ═══════════════════════════════════════
# 3. D-Gate v5.2 核心引擎
# ═══════════════════════════════════════

def get_s7_threshold(hcp):
    """S7动态阈值 — 按盘口深度分层 (主场让球为负)"""
    abs_hcp = abs(hcp)
    if abs_hcp >= 1.75:
        return 6.0   # 深盘(≥1.75): S7高是正常的, 极高才惩罚
    elif abs_hcp >= 1.0:
        return 4.5   # 中盘(1.0-1.75)
    elif abs_hcp >= 0.5:
        return 3.5   # 浅盘(0.5-1.0): 统一阈值
    else:
        return 2.5   # 平手/平半: S7大=极不正常

def get_cover_adjustment(home, away):
    """
    穿盘/抗盘调整因子
    返回 (pD_adjust_multiplier, note)
    >1.0 = 提高平局概率, <1.0 = 降低
    """
    h = COVER_DB.get(home, {})
    a = COVER_DB.get(away, {})
    
    adjustments = []
    multiplier = 1.0
    
    # 互捅型球队 → 降低平局概率 (强信号)
    if h.get('style') == '互捅型':
        multiplier *= 0.85
        adjustments.append(f'{home}互捅型')
    if a.get('style') == '互捅型':
        multiplier *= 0.85
        adjustments.append(f'{away}互捅型')
    
    # 沉闷型球队 → 微提平局概率 (弱信号, 避免过度)
    if h.get('style') == '沉闷型':
        multiplier *= 1.03
        adjustments.append(f'{home}沉闷型')
    if a.get('style') == '沉闷型':
        multiplier *= 1.03
        adjustments.append(f'{away}沉闷型')
    
    # 高屠杀率 → 降低 (v5.2.5: 至少2场才有效)
    if h.get('blowout_ratio', 0) >= 0.5 and h.get('total', 0) >= 2:
        multiplier *= 0.90
        adjustments.append(f'{home}屠杀率高')
    if a.get('blowout_ratio', 0) >= 0.5 and a.get('total', 0) >= 2:
        multiplier *= 0.90
        adjustments.append(f'{away}屠杀率高')
    
    # 高平局率 → 提高 (v5.2.5: 至少2场才有效)
    if h.get('draw_ratio', 0.25) >= 0.5 and h.get('total', 0) >= 2:
        multiplier *= 1.06
        adjustments.append(f'{home}平局率高')
    if a.get('draw_ratio', 0.25) >= 0.5 and a.get('total', 0) >= 2:
        multiplier *= 1.06
        adjustments.append(f'{away}平局率高')
    
    note = ';'.join(adjustments) if adjustments else '无调整'
    return multiplier, note

def get_similar_odds_warning(imp_h, imp_a, hcp):
    """
    同赔率对照预警
    返回 (warning_level, note)
    """
    similar = find_similar_matches(imp_h, imp_a, hcp, max_results=3)
    if not similar:
        return 'none', ''
    
    outcomes = [m['outcome'] for m in similar]
    n = len(outcomes)
    draws = outcomes.count('D')
    blowouts = sum(1 for m in similar if abs(m['margin']) >= 3)
    
    notes = []
    for m in similar:
        notes.append(f"{m['home']}vs{m['away']}({m['hg']}-{m['ag']})")
    
    note_str = '; '.join(notes)
    
    if draws >= n * 0.5 and blowouts == 0:
        return 'draw_bias', f'同类赔率{draws}/{n}平局: {note_str}'
    elif blowouts >= n * 0.5:
        return 'blowout_bias', f'同类赔率{blowouts}/{n}屠杀: {note_str}'
    elif draws > 0 and blowouts > 0:
        return 'mixed', f'同类赔率混合({draws}平{blowouts}屠): {note_str}'
    else:
        return 'clean', f'同类赔率均分胜负: {note_str}'

def dgate_v52(ph, pd, pa, oh, od, oa, hcp, ou, home='', away='', cs_other=None):
    """
    D-Gate v5.2.2 — 三增强版平局检测 + cs_other校验
    
    参数:
        ph/pd/pa: 模型调整后的概率
        oh/od/oa: 原始赔率
        hcp: 让球盘口(主队让球)
        ou: 大小球线
        home/away: 球队名(用于穿盘标记)
        cs_other: '其它比分'赔率 (None=不校验, <5.0→否决平局)
    
    返回:
        (verdict, mode, d_boost, signals_list)
    """
    spread = abs(ph - pa)
    max_imp = max(ph, pa)
    s1_draw_cheapness = od / math.sqrt(oh * oa)
    s7_ou_hcp_ratio = ou / max(abs(hcp), 0.25)
    
    signals = []
    verdict = None  # 延迟判定, 最后统一处理cs_other
    final_mode = 'normal'
    final_d = 0.0
    
    # ── 穿盘/抗盘调整因子 ──
    cover_mult, cover_note = 1.0, ''
    if home and away:
        cover_mult, cover_note = get_cover_adjustment(home, away)
        if cover_note != '无调整':
            signals.append(f'球队风格:{cover_note}')
        # P3: 进攻效率差 → pD boost
        eff_mult, eff_note = get_efficiency_adjustment(home, away)
        if eff_note != '无虚高':
            cover_mult *= eff_mult
            signals.append(f'效率差:{eff_note}')
    
    # ── 同赔率对照 ──
    odds_warn = 'none'
    if home and away:
        odds_warn, odds_note = get_similar_odds_warning(ph, pa, hcp)
        if odds_warn != 'none' and odds_warn != 'clean':
            signals.append(f'同类赔率:{odds_note}')
    
    # ═══════════════════════════════════
    # Layer 1: Mode C — 超热门翻车 (≥70%)
    # ═══════════════════════════════════
    if max_imp >= 0.70:
        d_boost = pd * 1.08
        
        # v5.2.4: 大盘口(=Elo差>200)→降boost+高门槛, 防假阳性
        if max_imp > 0.75 or abs(hcp) >= 1.75:
            d_boost *= 1.4   # 原2.2 (大盘口碾压概率高,不是翻车)
        else:
            d_boost *= 1.8
        
        # v5.2: S7动态阈值
        s7_thresh = get_s7_threshold(hcp)
        if od > 9.5 and ou >= 3.5 and abs(hcp) >= 2.5:
            d_boost *= 0.3
        elif od > 9.5 and abs(hcp) >= 2.5:
            d_boost *= 0.5
        
        # 穿盘风格调整
        d_boost *= cover_mult
        
        # 同类赔率屠杀警告 (Mode C: 只有明确屠杀偏才降权)
        if odds_warn == 'blowout_bias':
            d_boost *= 0.85
        
        # v5.2.4: 大盘口→门槛0.45(原0.14), 防单场噪声污染
        threshold = 0.45 if abs(hcp) >= 1.75 else 0.14
        if d_boost > threshold and verdict is None:
            verdict, final_mode, final_d = 'D', 'C', d_boost
    
    # ═══════════════════════════════════
    # Layer 1b: Mode C-away (v5.2.5: 降倍提门槛)
    # ═══════════════════════════════════
    if pa > 0.65 and max_imp < 0.70:
        d_boost = pd * 1.08 * 1.5  # 原2.0
        d_boost *= cover_mult
        threshold = 0.35  # 原0.14
        if d_boost > threshold and verdict is None:
            verdict, final_mode, final_d = 'D', 'C-away', d_boost
    
    # ═══════════════════════════════════
    # Layer 2: Mode A — 中等热门 (48-70%)
    # ═══════════════════════════════════
    if 0.48 <= max_imp <= 0.70:
        d_boost = pd * 1.08
        suppress = max(0.80, 1.0 - spread * 0.30)
        d_boost *= suppress
        
        if ou <= 2.5:
            d_boost *= 1.05
        
        # 🔑 v5.2.2: S7动态阈值 + S1宽松(1.30→1.35)
        s7_thresh = get_s7_threshold(hcp)
        s7_s1 = s7_ou_hcp_ratio
        s1 = s1_draw_cheapness
        if s7_s1 >= s7_thresh and s1 < 1.35:
            d_boost *= 0.70
            signals.append(f'S7={s7_s1:.1f}≥{s7_thresh}惩罚')
        
        # 穿盘风格调整
        d_boost *= cover_mult
        
        # 同类赔率警告 (Mode A: 仅强信号调整)
        if odds_warn == 'blowout_bias':
            d_boost *= 0.85
            signals.append('同类赔率偏屠杀→降权')
        elif odds_warn == 'draw_bias':
            d_boost *= 1.06
            signals.append('同类赔率偏平局→微调')
        # mixed → 不调整 (既有可能平局也有可能屠杀, 不定向干预)
        
        threshold = 0.28
        if d_boost > threshold and verdict is None:
            verdict, final_mode, final_d = 'D', 'A', d_boost
    
    # ═══════════════════════════════════
    # Layer 3: Mode B — 均衡赛 (高门槛, 球队风格仅微调)
    # ═══════════════════════════════════
    if spread < 0.15 and ou <= 2.75:  # 限制大小球, 排除互爆型均衡赛
        d_boost = pd * 1.08 * 1.20
        # Mode B保守: 沉闷型只给×1.02, 互捅型×0.88
        if home and away:
            h_style = COVER_DB.get(home, {}).get('style', '')
            a_style = COVER_DB.get(away, {}).get('style', '')
            if h_style == '沉闷型' and a_style == '沉闷型':
                d_boost *= 1.02  # 双沉闷→微提(避免过度)
            if h_style == '互捅型' or a_style == '互捅型':
                d_boost *= 0.88  # 互捅型→强降
        threshold = 0.44  # v5.2.1: 微提门槛(防双沉闷型误触)
        if d_boost > threshold and verdict is None:
            verdict, final_mode, final_d = 'D', 'B', d_boost
    
    # ═══════════════════════════════════
    # Layer 4: Default (v5.2.5: 窄spread平局探测)
    # ═══════════════════════════════════
    d_boost = pd * 1.08
    if spread > 0.40:
        d_boost *= 0.70
    elif spread > 0.20:
        d_boost *= 0.85
    
    # v5.2.5: 窄spread + 低Elo差 → 平局概率偏高 (均衡但非Mode B)
    if spread < 0.25 and abs(hcp) < 1.0:
        d_boost *= 1.15
        signals.append(f'窄spread={spread:.2f}+低Elo差→微提平局')
    
    # v5.2.2: S7动态阈值 + S1宽松
    s7_thresh = get_s7_threshold(hcp)
    s7_s1 = s7_ou_hcp_ratio
    s1 = s1_draw_cheapness
    if s7_s1 >= s7_thresh and s1 < 1.35:
        d_boost *= 0.70
        signals.append(f'S7={s7_s1:.1f}≥{s7_thresh}惩罚')
    
    # 球队风格
    d_boost *= cover_mult
    
    threshold = 0.32
    if d_boost > threshold and verdict is None:
        verdict, final_mode, final_d = 'D', 'default', d_boost
    
    # 未被任何模式触发 → 默认胜负判型
    if verdict is None:
        verdict = 'H' if ph > pa else 'A'
    
    # ═══════════════════════════════════
    # 🔑 P0: cs_other 校验 — 否决平局
    # ═══════════════════════════════════
    if cs_other is not None and cs_other < 5.0 and verdict == 'D':
        original_verdict = verdict
        verdict = 'H' if ph > pa else 'A'
        final_mode = f'{final_mode}(cs否决)'
        signals.append(f'cs={cs_other:.1f}<5.0→否决平局')
    
    return verdict, final_mode, final_d, signals

# ═══════════════════════════════════════
# P4: 球星效应 (v5.2.5)
# ═══════════════════════════════════════

STAR_PLAYERS = {
    # 球队 → 球星列表 (影响预期进球)
    '挪威': {'stars': ['哈兰德'], 'goal_boost': 0.4},
    '塞内加尔': {'stars': ['马内'], 'goal_boost': 0.3},
    '法国': {'stars': ['姆巴佩'], 'goal_boost': 0.4},
    '英格兰': {'stars': ['凯恩'], 'goal_boost': 0.3},
    '葡萄牙': {'stars': ['C罗'], 'goal_boost': 0.3},
    '阿根廷': {'stars': ['梅西'], 'goal_boost': 0.3},
    '巴西': {'stars': ['维尼修斯'], 'goal_boost': 0.3},
    '荷兰': {'stars': ['范戴克'], 'goal_boost': 0.2},
    '哥伦比亚': {'stars': ['迪亚斯'], 'goal_boost': 0.2},
    '克罗地亚': {'stars': ['莫德里奇'], 'goal_boost': 0.2},
}

def get_star_adjustment(home, away):
    """
    球星效应: 有超级球星的球队进攻力更强
    返回 (h_boost, a_boost, note)
    """
    hs = STAR_PLAYERS.get(home, {})
    as_ = STAR_PLAYERS.get(away, {})
    hb = hs.get('goal_boost', 0) if hs else 0
    ab = as_.get('goal_boost', 0) if as_ else 0
    note = ''
    if hs:
        note += f'{home}({",".join(hs.get("stars",[]))})+{hb} '
    if as_:
        note += f'{away}({",".join(as_.get("stars",[]))})+{ab}'
    return hb, ab, note.strip() or '无球星加成'

# ═══════════════════════════════════════
# P2: 球队专项特征
# ═══════════════════════════════════════

def get_schedule_pressure(home, away, standings=None):
    """
    赛程压力差 — 识别"先易后难"vs"先难后易"
    
    参数:
        standings: {team: {'pts':int, 'remaining':[opponents], 'must_win':bool}}
    
    返回:
        (pressure_level, note)
        pressure_level: 'home_urgency'/'away_urgency'/'neutral'
    """
    if not standings or home not in standings or away not in standings:
        return 'neutral', '无积分数据'
    
    hs = standings[home]
    as_ = standings[away]
    
    # 已淘汰 → 无战意
    h_dead = hs.get('eliminated', False)
    a_dead = as_.get('eliminated', False)
    if h_dead and a_dead:
        return 'dead_rubber', '双方已淘汰(荣誉战)'
    if h_dead:
        return 'away_urgency', f'{home}已淘汰(战意丧失)'
    if a_dead:
        return 'home_urgency', f'{away}已淘汰(战意丧失)'
    
    # 必须赢才能出线
    h_must = hs.get('must_win', False)
    a_must = as_.get('must_win', False)
    
    # 末轮对手强度
    h_final_strength = hs.get('final_opponent_strength', 0.5)
    a_final_strength = as_.get('final_opponent_strength', 0.5)
    
    # 核心逻辑: 末轮对手越强 → 本轮越需要拿分
    h_pressure = h_final_strength + (0.3 if h_must else 0)
    a_pressure = a_final_strength + (0.3 if a_must else 0)
    
    diff = h_pressure - a_pressure
    if diff > 0.25:
        return 'home_urgency', f'主队赛程压力更大({h_pressure:.1f}vs{a_pressure:.1f})'
    elif diff < -0.25:
        return 'away_urgency', f'客队赛程压力更大({h_pressure:.1f}vs{a_pressure:.1f})'
    else:
        return 'neutral', '赛程压力均衡'

def get_aerial_threat_index(team):
    """
    空中威胁指数 v2 — 基于GF/GA比 + 对强队进球能力
    
    核心: 对强队能进球的弱队 → 大概率靠定位球/头球
    公式: (GF vs strong opponents) / total_gf × GF/GA ratio
    
    返回: 0.0-1.0, >0.65 = 高空威胁显著
    """
    t = COVER_DB.get(team, {})
    n = t.get('total', 0)
    if n < 1:
        return 0.5  # 无数据
    
    gf = t.get('gf90', 0)
    ga = t.get('ga90', 1.0)
    
    # 基础: GF/GA比 (进攻效率 vs 防守漏洞)
    base = gf / max(ga, 0.5)
    
    # 对强队的进球能力代理: cover_rate高但GA也高 = 互捅型但能进球
    cover = t.get('cover_rate', 0.5)
    if cover >= 0.5 and ga >= 2.0:
        # 防守差但进攻能补 → 定位球/头球依赖
        return min(1.0, (gf * 0.6 + cover * 0.4) / max(ga, 1.0) * 1.5)
    
    # 标准: GF/GA 归一化
    aerial = min(1.0, (gf + cover * 0.5) / max(ga, 1.0) * 0.8)
    
    return max(0.3, min(0.85, aerial))  # 钳制在0.3-0.85避免极端

def get_team_features(home, away, standings=None):
    """
    P2: 球队专项特征综合提取
    
    返回:
        dict with:
        - schedule_pressure: 'home_urgency'/'away_urgency'/'neutral'
        - aerial_home: float 0-1
        - aerial_away: float 0-1
        - adjustment_mult: float (pD乘数)
        - notes: list[str]
    """
    features = {
        'schedule_pressure': 'neutral',
        'aerial_home': get_aerial_threat_index(home),
        'aerial_away': get_aerial_threat_index(away),
        'adjustment_mult': 1.0,
        'notes': []
    }
    
    # 赛程压力
    pressure, note = get_schedule_pressure(home, away, standings)
    features['schedule_pressure'] = pressure
    if pressure != 'neutral':
        features['notes'].append(note)
    
    # 高空威胁调整: 防空弱队(GA>2.0) vs 高空强队(aerial>0.6) → 平局↓
    h_aerial = features['aerial_home']
    a_aerial = features['aerial_away']
    h_ga = COVER_DB.get(home, {}).get('ga90', 1.5)
    a_ga = COVER_DB.get(away, {}).get('ga90', 1.5)
    
    if h_aerial > 0.6 and a_ga > 2.0:
        features['adjustment_mult'] *= 0.92
        features['notes'].append(f'{home}高空优势vs{away}防空弱')
    if a_aerial > 0.6 and h_ga > 2.0:
        features['adjustment_mult'] *= 0.92
        features['notes'].append(f'{away}高空优势vs{home}防空弱')
    
    # 赛程压力 → 平局概率调整
    if pressure == 'dead_rubber':
        features['adjustment_mult'] *= 1.05  # 荣誉战平局概率略升
        features['notes'].append('荣誉战(平局率偏高)')
    elif pressure == 'home_urgency':
        features['adjustment_mult'] *= 0.90  # 主队必须赢→平局↓
    elif pressure == 'away_urgency':
        features['adjustment_mult'] *= 0.90  # 客队必须赢→平局↓
    
    return features

# ═══════════════════════════════════════
# P3: 进攻效率差 — 识别虚高球队
# ═══════════════════════════════════════

def get_efficiency_adjustment(home, away):
    """
    进攻效率调整 — 弱队刷数据 vs 同档真实力
    
    核心: 球队GF可能被屠杀弱队拉高(如Canada 6-0卡塔尔)
    只看同档比赛(|hcp|<1.0)的效率差, 给pD一个boost
    
    返回:
        (pD_multiplier, note)
        >1.0 = 提高平局概率(效率差说明两队比赔率暗示的更接近)
    """
    h = COVER_DB.get(home, {})
    a = COVER_DB.get(away, {})
    
    # 使用同档GF作为真实进攻力
    h_all_gf = h.get('gf90', 1.5)
    a_all_gf = a.get('gf90', 1.5)
    h_all_ga = h.get('ga90', 1.5)
    a_all_ga = a.get('ga90', 1.5)
    
    # 简易同档GF代理: 如果GF极高(>2.5)且有一场屠杀(blowout>0.5), 可能被虚高
    h_blowout = h.get('blowout_ratio', 0)
    a_blowout = a.get('blowout_ratio', 0)
    h_total = h.get('total', 0)
    a_total = a.get('total', 0)
    
    adjustments = []
    multiplier = 1.0
    
    # 虚高检测: GF>2.5 + 屠杀率>0 + 场次≥2 → 可能有弱队屠杀拉高均值
    if h_total >= 2 and h_all_gf > 2.5 and h_blowout > 0:
        # 估算去掉屠杀后的GF: 假设屠杀场进3+球, 扣除2球
        adjusted_h_gf = max(1.0, h_all_gf - (2.0 / h_total))
        if adjusted_h_gf < h_all_gf * 0.75:
            multiplier *= 1.08
            adjustments.append(f'{home}进攻虚高(GF{h_all_gf:.1f}→真{adjusted_h_gf:.1f})')
    
    if a_total >= 2 and a_all_gf > 2.5 and a_blowout > 0:
        adjusted_a_gf = max(1.0, a_all_gf - (2.0 / a_total))
        if adjusted_a_gf < a_all_gf * 0.75:
            multiplier *= 1.08
            adjustments.append(f'{away}进攻虚高(GF{a_all_gf:.1f}→真{adjusted_a_gf:.1f})')
    
    # 防守韧性: GA低但对手弱→可能被高估; GA高但对手强→可能被低估
    # 简化: 双方都低GA → 平局↑
    if h_total >= 2 and a_total >= 2:
        if h_all_ga < 1.5 and a_all_ga < 1.5:
            multiplier *= 1.04
            adjustments.append('双方防守均佳→平局概率↑')
    
    note = ';'.join(adjustments) if adjustments else '无虚高'
    return multiplier, note

# ═══════════════════════════════════════
# 4. 球队风格速查
# ═══════════════════════════════════════
def print_team_styles():
    """打印所有球队的风格分类"""
    print("╔" + "═"*78 + "╗")
    print("║  球队风格数据库 (34场赛果提取)                                    ║")
    print("╠" + "═"*78 + "╣")
    print(f"║ {'球队':<16} {'赛':>2} {'GF90':>5} {'GA90':>5} {'屠杀率':>5} {'平局率':>5} {'穿盘率':>5} {'风格':<12} ║")
    print("╠" + "═"*78 + "╣")
    
    sorted_teams = sorted(COVER_DB.items(), key=lambda x: -x[1]['total'])
    for team, d in sorted_teams:
        if d['total'] == 0:
            continue
        print(f"║ {team:<16} {d['total']:>2} {d['gf90']:>5.1f} {d['ga90']:>5.1f} "
              f"{d['blowout_ratio']:>5.0%} {d['draw_ratio']:>5.0%} {d['cover_rate']:>5.0%} {d['style']:<12} ║")
    
    print("╚" + "═"*78 + "╝")

if __name__ == "__main__":
    print_team_styles()
