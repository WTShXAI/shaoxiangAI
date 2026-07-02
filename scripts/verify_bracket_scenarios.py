"""验证赛会赛制师关键情景 vs 实际赛果"""
import json, sys; sys.path.insert(0,'D:/Architecture v4.0')
from pipeline.reverse_engine import TournamentArchitect

with open('D:/Architecture v4.0/data/wc2026_72matches_with_odds.json',encoding='utf-8') as f:
    data = json.load(f)

ta = TournamentArchitect()

key_teams = [
    ('巴西','C', '枝硬—C1打2D, 末轮vs摩洛哥同积7分争头名'),
    ('德国','E', '暗雷—E1第三池含C3巴西, 末轮vs库拉索已锁定'),
    ('法国','I', '暗雷—I1第三池含C(巴西)和H(西班牙), 末轮vs伊拉克'),
    ('阿根廷','J', '枝软—J1打2K/2L非欧洲豪门, 末轮vs奥地利'),
    ('英格兰','L', '正常—L1第三池E/H/I/J/K, 末轮vs克罗地亚'),
    ('葡萄牙','K', '正常—K1第三池D/E/I/J/L, 末轮vs民主刚果'),
]

print('=' * 65)
print('赛会赛制师 动机推断 vs 实际赛果 交叉验证')
print('=' * 65)

for team, group, note in key_teams:
    conflict = ta.check_motivation_conflict(team, group, 1)
    path = ta.get_opponent_path(group, 1)
    
    matches = [m for m in data if m.get('home')==team or m.get('away')==team]
    # 找出该队得失球
    total_gf = sum(m.get('hs',0) if m.get('home')==team else m.get('aws',0) for m in matches)
    total_ga = sum(m.get('aws',0) if m.get('home')==team else m.get('hs',0) for m in matches)
    
    print(f"\n{'─'*60}")
    print(f"  {team}({group}1) — {note}")
    print(f"  路径: {path['note'][:80]}")
    print(f"  动机: conflict={conflict['has_conflict']} | {conflict['suggested_action']}")
    for r in conflict['reasoning']:
        print(f"    → {r}")
    
    # 赛果汇总
    w = sum(1 for m in matches if (m.get('home')==team and m.get('hs',0)>m.get('aws',0)) or (m.get('away')==team and m.get('aws',0)>m.get('hs',0)))
    d = sum(1 for m in matches if m.get('hs',0)==m.get('aws',0))
    l = sum(1 for m in matches if (m.get('home')==team and m.get('hs',0)<m.get('aws',0)) or (m.get('away')==team and m.get('aws',0)<m.get('hs',0)))
    print(f"  战绩: {w}W {d}D {l}L | GF={total_gf} GA={total_ga} GD={total_gf-total_ga:+d}")
    
    # 关键末轮检查
    last_matches = [m for m in matches if '6/2' in m.get('date','') and int(m['date'].split('/')[1]) >= 23]
    if last_matches:
        lm = last_matches[0]
        print(f"  末轮({lm['date']}): {lm.get('home','')} {lm.get('hs','?')}-{lm.get('aws','?')} {lm.get('away','')}")

print(f"\n{'='*65}")
print("验证结论:")

# Brazil check
brazil_matches = [m for m in data if m.get('home')=='巴西' or m.get('away')=='巴西']
brazil_last = [m for m in brazil_matches if '6/2' in m.get('date','') and int(m['date'].split('/')[1]) >= 23]
if brazil_last:
    lm = brazil_last[0]
    br_home = lm.get('home')=='巴西'
    br_scored = lm.get('hs',0) if br_home else lm.get('aws',0)
    br_conceded = lm.get('aws',0) if br_home else lm.get('hs',0)
    print(f"  🇧🇷 巴西末轮: {'主场' if br_home else '客场'} {br_scored}-{br_conceded} — ", end='')
    if lm.get('hs',0)==lm.get('aws',0):
        print("平局! 印证'枝硬→可能保守'推断 ✅")
    elif (br_home and br_scored <= 1) or (not br_home and br_scored <= 1):
        print(f"小比分, 部分印证动机推断")
    else:
        print(f"大比分, 动机推断未成立")

# Germany check  
ger_matches = [m for m in data if m.get('home')=='德国' or m.get('away')=='德国']
ger_last = [m for m in ger_matches if '6/2' in m.get('date','') and int(m['date'].split('/')[1]) >= 23]
if ger_last:
    lm = ger_last[0]
    print(f"  🇩🇪 德国末轮(已锁定): {lm.get('home','')} {lm.get('hs','?')}-{lm.get('aws','?')} {lm.get('away','')} — 是否轮换/小球? ", end='')
    total_goals = (lm.get('hs',0) or 0) + (lm.get('aws',0) or 0)
    if total_goals <= 3:
        print(f"总球{total_goals}≤3, 印证'已锁定保守'推断 ✅")
    else:
        print(f"总球{total_goals}>3, 未印证")

# Argentina check
arg_matches = [m for m in data if m.get('home')=='阿根廷' or m.get('away')=='阿根廷']
arg_last = [m for m in arg_matches if '6/2' in m.get('date','') and int(m['date'].split('/')[1]) >= 23]
if arg_last:
    lm = arg_last[0]
    arg_home = lm.get('home')=='阿根廷'
    print(f"  🇦🇷 阿根廷末轮(已锁定): {'主场' if arg_home else '客场'} — 枝软→是否无压力正常打? ")
    print(f"     赛果: {lm.get('home','')} {lm.get('hs','?')}-{lm.get('aws','?')} {lm.get('away','')}")
