"""
D-Gate 工具函数模块 — 统一数据/工具入口 (2026-06-28)
======================================================
从 d_gate_v52.py 提取, 供 d_gate_engine.py / d_gate_v52.py 共用.

包含:
  - ALL_RESULTS: 34场赛果数据库
  - COVER_DB: 球队穿盘/风格数据库
  - 球队风格、效率调整、让球陷阱检测等工具函数
"""
import math
from collections import defaultdict

# ═══════════════════════════════════════
# 1. 穿盘/抗盘标记 — 从34场赛果提取
# ═══════════════════════════════════════

# 34场完整赛果: (主队, 客队, 主进球, 客进球, 盘口(主队让), 日期)
ALL_RESULTS = [
    # ══ Matchday 1 (6.11-6.18) ══
    ('墨西哥','南非',2,0,-0.5,'6.11'),
    ('韩国','捷克',2,1,-0.25,'6.12'),
    ('加拿大','波黑',1,1,-0.5,'6.12'),
    ('美国','巴拉圭',4,1,-0.75,'6.13'),
    ('卡塔尔','瑞士',1,1,1.0,'6.13'),
    ('巴西','摩洛哥',1,1,-1.5,'6.13'),
    ('海地','苏格兰',0,1,1.5,'6.14'),
    ('澳大利亚','土耳其',2,0,0.5,'6.14'),
    ('德国','库拉索',7,1,-1.0,'6.14'),
    ('科特迪瓦','厄瓜多尔',1,0,0.0,'6.14'),
    ('荷兰','日本',2,2,-0.5,'6.14'),
    ('瑞典','突尼斯',5,1,-0.5,'6.15'),
    ('西班牙','佛得角共和国',0,0,-2.5,'6.15'),
    ('比利时','埃及',1,1,-1.5,'6.15'),
    ('沙特阿拉伯','乌拉圭',1,1,1.5,'6.15'),
    ('伊朗','新西兰',2,2,-1.25,'6.16'),
    ('法国','塞内加尔',3,1,-2.5,'6.16'),
    ('伊拉克','挪威',1,4,0.25,'6.16'),
    ('阿根廷','阿尔及利亚',3,0,-0.5,'6.17'),
    ('奥地利','约旦',3,1,-1.0,'6.17'),
    ('葡萄牙','民主刚果',1,1,-1.75,'6.17'),
    ('英格兰','克罗地亚',4,2,-1.5,'6.17'),
    ('加纳','巴拿马',1,0,-1.0,'6.17'),
    ('乌兹别克斯坦','哥伦比亚',1,3,1.0,'6.18'),
    # ══ Matchday 2 (6.18-6.21) ══
    ('捷克','南非',1,1,-0.75,'6.18'),
    ('瑞士','波黑',4,1,-0.5,'6.18'),
    ('加拿大','卡塔尔',6,0,-0.5,'6.18'),
    ('墨西哥','韩国',1,0,-0.5,'6.19'),
    ('美国','澳大利亚',2,0,-1.0,'6.19'),
    ('苏格兰','摩洛哥',0,1,0.5,'6.19'),
    ('巴西','海地',3,0,-2.75,'6.20'),
    ('土耳其','巴拉圭',0,1,0.5,'6.20'),
    ('厄瓜多尔','库拉索',0,0,-1.75,'6.21'),
    ('德国','科特迪瓦',2,1,-1.0,'6.21'),
    ('突尼斯','日本',1,5,0.75,'6.21'),
    ('荷兰','瑞典',5,1,-0.5,'6.21'),
    # ══ Matchday 2 continued (6.22-6.24) ══
    ('乌拉圭','佛得角共和国',0,0,-1.25,'6.22'),
    ('新西兰','埃及',0,1,1.0,'6.22'),
    ('比利时','伊朗',0,1,-1.25,'6.22'),
    ('西班牙','沙特阿拉伯',2,0,-2.75,'6.22'),
    ('挪威','塞内加尔',3,2,-1.0,'6.23'),
    ('法国','伊拉克',3,0,-2.5,'6.23'),
    ('约旦','阿尔及利亚',1,2,0.25,'6.23'),
    ('阿根廷','奥地利',2,0,-1.5,'6.23'),
    ('葡萄牙','乌兹别克斯坦',5,0,-2.75,'6.24'),
    ('英格兰','加纳',0,0,-1.75,'6.24'),
    ('克罗地亚','巴拿马',1,0,-1.25,'6.24'),
    ('哥伦比亚','民主刚果',1,0,-1.0,'6.24'),
]

def build_cover_database():
    db = defaultdict(lambda: {
        'as_fav': 0, 'covered': 0, 'as_dog': 0, 'anti_covered': 0,
        'blowouts': 0, 'total': 0, 'goals_for': 0, 'goals_against': 0, 'draws': 0,
    })
    for h, a, hg, ag, hcp, _date in ALL_RESULTS:
        db[h]['total'] += 1; db[a]['total'] += 1
        db[h]['goals_for'] += hg; db[h]['goals_against'] += ag
        db[a]['goals_for'] += ag; db[a]['goals_against'] += hg
        if hg == ag:
            db[h]['draws'] += 1; db[a]['draws'] += 1
        margin = hg - ag
        if hcp < 0:
            db[h]['as_fav'] += 1; db[a]['as_dog'] += 1
            if margin > abs(hcp): db[h]['covered'] += 1
            if margin > hcp: db[a]['anti_covered'] += 1
        elif hcp > 0:
            db[h]['as_dog'] += 1; db[a]['as_fav'] += 1
            if margin > -hcp: db[h]['anti_covered'] += 1
            if ag - hg > abs(hcp): db[a]['covered'] += 1
        else:
            db[h]['as_fav'] += 1; db[a]['as_fav'] += 1
            if margin > 0: db[h]['covered'] += 1
            elif margin < 0: db[a]['covered'] += 1
        if abs(margin) >= 3:
            db[h]['blowouts'] += 1; db[a]['blowouts'] += 1
    for team in db:
        d = db[team]; n = d['total']
        d['cover_rate'] = d['covered'] / d['as_fav'] if d['as_fav'] > 0 else 0.5
        d['anti_rate'] = d['anti_covered'] / d['as_dog'] if d['as_dog'] > 0 else 0.5
        d['blowout_ratio'] = d['blowouts'] / n if n > 0 else 0
        d['draw_ratio'] = d['draws'] / n if n > 0 else 0
        d['gf90'] = d['goals_for'] / n if n > 0 else 1.5
        d['ga90'] = d['goals_against'] / n if n > 0 else 1.5
        if n >= 2:
            if d['gf90'] >= 2.5 and d['ga90'] >= 2.0: d['style'] = '互捅型'
            elif d['gf90'] >= 1.5 and d['ga90'] <= 1.0: d['style'] = '稳赢型'
            elif d['gf90'] <= 1.0 and d['ga90'] <= 1.0 and d['draw_ratio'] >= 0.5: d['style'] = '沉闷型'
            else: d['style'] = '均衡型'
        else: d['style'] = '均衡型'
    return db

COVER_DB = build_cover_database()

# ═══════════════════════════════════════
# 2. 同赔率对照表
# ═══════════════════════════════════════

def build_similar_odds_db():
    history = []
    for h, a, hg, ag, hcp, _date in ALL_RESULTS:
        history.append({
            'home': h, 'away': a, 'hg': hg, 'ag': ag,
            'hcp': hcp, 'margin': hg - ag,
            'outcome': 'H' if hg > ag else ('D' if hg == ag else 'A'),
        })
    return history

ODDS_HISTORY = build_similar_odds_db()

def find_similar_matches(imp_h, imp_a, hcp, max_results=5):
    similar = []
    for m in ODDS_HISTORY:
        hcp_diff = abs(m['hcp'] - hcp)
        if hcp_diff <= 0.75: similar.append(m)
    similar.sort(key=lambda m: abs(m['hcp'] - hcp))
    return similar[:max_results]

# ═══════════════════════════════════════
# 3. 工具函数
# ═══════════════════════════════════════

def get_s7_threshold(hcp):
    abs_hcp = abs(hcp)
    if abs_hcp >= 1.75: return 6.0
    elif abs_hcp >= 1.0: return 4.5
    elif abs_hcp >= 0.5: return 3.5
    else: return 2.5

def get_cover_adjustment(home, away):
    h = COVER_DB.get(home, {}); a = COVER_DB.get(away, {})
    adjustments = []; multiplier = 1.0
    if h.get('style') == '互捅型': multiplier *= 0.85; adjustments.append(f'{home}互捅型')
    if a.get('style') == '互捅型': multiplier *= 0.85; adjustments.append(f'{away}互捅型')
    if h.get('style') == '沉闷型': multiplier *= 1.03; adjustments.append(f'{home}沉闷型')
    if a.get('style') == '沉闷型': multiplier *= 1.03; adjustments.append(f'{away}沉闷型')
    if h.get('blowout_ratio', 0) >= 0.5 and h.get('total', 0) >= 2:
        multiplier *= 0.90; adjustments.append(f'{home}屠杀率高')
    if a.get('blowout_ratio', 0) >= 0.5 and a.get('total', 0) >= 2:
        multiplier *= 0.90; adjustments.append(f'{away}屠杀率高')
    if h.get('draw_ratio', 0.25) >= 0.5 and h.get('total', 0) >= 2:
        multiplier *= 1.06; adjustments.append(f'{home}平局率高')
    if a.get('draw_ratio', 0.25) >= 0.5 and a.get('total', 0) >= 2:
        multiplier *= 1.06; adjustments.append(f'{away}平局率高')
    note = ';'.join(adjustments) if adjustments else '无调整'
    return multiplier, note

def get_similar_odds_warning(imp_h, imp_a, hcp):
    similar = find_similar_matches(imp_h, imp_a, hcp, max_results=3)
    if not similar: return 'none', ''
    outcomes = [m['outcome'] for m in similar]; n = len(outcomes)
    draws = outcomes.count('D'); blowouts = sum(1 for m in similar if abs(m['margin']) >= 3)
    notes = [f"{m['home']}vs{m['away']}({m['hg']}-{m['ag']})" for m in similar]
    note_str = '; '.join(notes)
    if draws >= n * 0.5 and blowouts == 0: return 'draw_bias', f'同类赔率{draws}/{n}平局: {note_str}'
    elif blowouts >= n * 0.5: return 'blowout_bias', f'同类赔率{blowouts}/{n}屠杀: {note_str}'
    elif draws > 0 and blowouts > 0: return 'mixed', f'同类赔率混合({draws}平{blowouts}屠): {note_str}'
    else: return 'clean', f'同类赔率均分胜负: {note_str}'

def detect_handicap_trap(strong_team, weak_team, current_hcp, home, away):
    """检测首轮平局+次轮让球加深的庄家诱盘模式"""
    if not strong_team or strong_team not in COVER_DB: return None
    td = COVER_DB[strong_team]; total = td.get('total', 0); draws = td.get('draws', 0)
    if draws < 1: return None
    if draws / max(total, 1) < 0.5: return None
    if abs(current_hcp) < 1.75: return None
    if td.get('blowout_ratio', 0) >= 0.33: return None
    if weak_team and weak_team in COVER_DB:
        wd = COVER_DB[weak_team]
        if wd.get('ga90', 0) >= 2.0 and wd.get('total', 0) >= 1: return None
    depth = abs(current_hcp)
    return (f'让球陷阱: {strong_team} {total}场{draws}平(平局率{draws/max(total,1):.0%}) '
            f'+ 盘口{depth:.2f}球诱多 -> 平局概率_up')

def get_efficiency_adjustment(home, away):
    h = COVER_DB.get(home, {}); a = COVER_DB.get(away, {})
    adjustments = []; multiplier = 1.0
    h_total, a_total = h.get('total',0), a.get('total',0)
    if h_total >= 2 and h.get('gf90',0) > 2.5 and h.get('blowout_ratio',0) > 0:
        adjusted = max(1.0, h.get('gf90',0) - 2.0/h_total)
        if adjusted < h.get('gf90',0) * 0.75:
            multiplier *= 1.08; adjustments.append(f'{home}进攻虚高')
    if a_total >= 2 and a.get('gf90',0) > 2.5 and a.get('blowout_ratio',0) > 0:
        adjusted = max(1.0, a.get('gf90',0) - 2.0/a_total)
        if adjusted < a.get('gf90',0) * 0.75:
            multiplier *= 1.08; adjustments.append(f'{away}进攻虚高')
    if h_total >= 2 and a_total >= 2:
        if h.get('ga90',0) < 1.5 and a.get('ga90',0) < 1.5:
            multiplier *= 1.04; adjustments.append('双方防守均佳->平局概率_up')
    note = ';'.join(adjustments) if adjustments else '无虚高'
    return multiplier, note

# ═══════════════════════════════════════
# 4. 球星效应
# ═══════════════════════════════════════

STAR_PLAYERS = {
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
    hs = STAR_PLAYERS.get(home, {}); as_ = STAR_PLAYERS.get(away, {})
    hb = hs.get('goal_boost', 0) if hs else 0
    ab = as_.get('goal_boost', 0) if as_ else 0
    note = ''
    if hs: note += f'{home}({",".join(hs.get("stars",[]))})+{hb} '
    if as_: note += f'{away}({",".join(as_.get("stars",[]))})+{ab}'
    return hb, ab, note.strip() or '无球星加成'

def print_team_styles():
    """打印球队风格数据库（调试用）"""
    print("╔" + "="*78 + "╗")
    print("║  球队风格数据库 (34场赛果提取)                                    ║")
    print("╠" + "="*78 + "╣")
    print(f"║ {'球队':<16} {'赛':>2} {'GF90':>5} {'GA90':>5} {'屠杀率':>5} {'平局率':>5} {'穿盘率':>5} {'风格':<12} ║")
    print("╠" + "="*78 + "╣")
    sorted_teams = sorted(COVER_DB.items(), key=lambda x: -x[1]['total'])
    for team, d in sorted_teams:
        if d['total'] == 0: continue
        print(f"║ {team:<16} {d['total']:>2} {d['gf90']:>5.1f} {d['ga90']:>5.1f} "
              f"{d['blowout_ratio']:>5.0%} {d['draw_ratio']:>5.0%} {d['cover_rate']:>5.0%} {d['style']:<12} ║")
    print("╚" + "="*78 + "╝")

if __name__ == "__main__":
    print_team_styles()
