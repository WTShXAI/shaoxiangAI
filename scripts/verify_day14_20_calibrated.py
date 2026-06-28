"""哨响AI v6.0 — Day14-Day20 全量校准回测 — OCR让球 + API matchday + 防泄露"""
import sys, os, json
os.chdir('D:/Architecture v4.0')
sys.path.insert(0, 'D:/Architecture v4.0')
sys.path.insert(0, 'D:/Architecture v4.0/predictors/components')

from pipeline.predictors.data_classes import MatchInput
from pipeline.predictors.pipeline import FullLinkagePipeline

# ─── 1. Load API matchday mapping ───
EN_CN = {
    'Iran': '伊朗', 'New Zealand': '新西兰', 'France': '法国', 'Senegal': '塞内加尔',
    'Iraq': '伊拉克', 'Norway': '挪威', 'Argentina': '阿根廷', 'Algeria': '阿尔及利亚',
    'Austria': '奥地利', 'Jordan': '约旦', 'Portugal': '葡萄牙',
    'Democratic Republic of the Congo': '民主刚果', 'England': '英格兰', 'Croatia': '克罗地亚',
    'Ghana': '加纳', 'Uzbekistan': '乌兹别克', 'Colombia': '哥伦比亚',
    'Czech Republic': '捷克', 'South Africa': '南非', 'Switzerland': '瑞士',
    'Bosnia and Herzegovina': '波黑', 'Canada': '加拿大', 'Qatar': '卡塔尔',
    'Mexico': '墨西哥', 'South Korea': '韩国', 'United States': '美国', 'Australia': '澳大利亚',
    'Scotland': '苏格兰', 'Morocco': '摩洛哥', 'Brazil': '巴西', 'Haiti': '海地',
    'Turkey': '土耳其', 'Paraguay': '巴拉圭', 'Netherlands': '荷兰', 'Sweden': '瑞典',
    'Germany': '德国', 'Ivory Coast': '科特迪瓦', 'Ecuador': '厄瓜多尔', 'Curaçao': '库拉索',
    'Tunisia': '突尼斯', 'Japan': '日本', 'Spain': '西班牙', 'Saudi Arabia': '沙特',
    'Belgium': '比利时', 'Egypt': '埃及', 'Uruguay': '乌拉圭',
    'Cape Verde': '佛得角', 'Panama': '巴拿马', 'Costa Rica': '哥斯达黎加',
    'Dominican Republic': '多米尼加', 'Serbia': '塞尔维亚', 'Denmark': '丹麦',
    'Peru': '秘鲁', 'Chile': '智利', 'Nigeria': '尼日利亚', 'Cameroon': '喀麦隆',
    'Ukraine': '乌克兰', 'Poland': '波兰', 'Italy': '意大利', 'Greece': '希腊',
    'Russia': '俄罗斯', 'Slovakia': '斯洛伐克', 'Hungary': '匈牙利', 'Romania': '罗马尼亚',
    'Venezuela': '委内瑞拉', 'Bolivia': '玻利维亚', 'Finland': '芬兰',
    'Ireland': '爱尔兰', 'Wales': '威尔士', 'Slovenia': '斯洛文尼亚',
    'North Macedonia': '北马其顿', 'Albania': '阿尔巴尼亚', 'Georgia': '格鲁吉亚',
    'Armenia': '亚美尼亚', 'Cyprus': '塞浦路斯', 'Luxembourg': '卢森堡',
    'Montenegro': '黑山', 'Iceland': '冰岛', 'Bulgaria': '保加利亚',
    'Lithuania': '立陶宛', 'Latvia': '拉脱维亚', 'Estonia': '爱沙尼亚',
    'Moldova': '摩尔多瓦', 'Kosovo': '科索沃', 'Andorra': '安道尔',
    'San Marino': '圣马力诺', 'Liechtenstein': '列支敦士登', 'Gibraltar': '直布罗陀',
    'Malta': '马耳他', 'Azerbaijan': '阿塞拜疆', 'Kazakhstan': '哈萨克斯坦',
    'Belarus': '白俄罗斯', 'Israel': '以色列', 'Faroe Islands': '法罗群岛',
}

with open('data/api_cache/wc26__get_games.json', encoding='utf-8') as f:
    api = json.load(f)

MATCHDAY_MAP = {}
for g in api['games']:
    h = EN_CN.get(g.get('home_team_name_en', ''), '')
    a = EN_CN.get(g.get('away_team_name_en', ''), '')
    md = int(g.get('matchday', 1))
    if h and a:
        MATCHDAY_MAP[(h, a)] = md

def get_matchday(home_cn, away_cn):
    return MATCHDAY_MAP.get((home_cn, away_cn)) or MATCHDAY_MAP.get((away_cn, home_cn)) or 1

# ─── 2. Load OCR handicap/OU ───
with open('data/wc2026_ocr_full.json', encoding='utf-8') as f:
    ocr = json.load(f)

OCR_HCP = {}
OCR_OU = {}
for m in ocr.get('matches', []):
    h = m.get('home_team', '')
    a = m.get('away_team', '')
    parsed = m.get('parsed', {}) or {}
    if h and a:
        hcp_str = parsed.get('handicap', '0')
        ou_str = parsed.get('ou_line', '2.5')
        try:
            hcp_val = float(str(hcp_str).replace('+', ''))
        except:
            hcp_val = 0.0
        try:
            parts = str(ou_str).split('/')
            ou_val = sum(float(x) for x in parts) / len(parts)
        except:
            ou_val = 2.5
        OCR_HCP[(h, a)] = hcp_val
        OCR_OU[(h, a)] = ou_val

# ─── 3. Load match data ───
with open('data/wc2026_72matches_with_odds.json', encoding='utf-8') as f:
    all_matches = json.load(f)

# ─── 4. Run all days 14-20 ───
pipeline = FullLinkagePipeline()
daily_results = {}
cum_ok = cum_exact = cum_total = 0

for target_day in range(14, 21):
    target = f'6/{target_day}'
    matches = [m for m in all_matches if m.get('date', '') == target]
    if not matches:
        continue
    
    day_results = []
    for m in matches:
        home = m.get('home', '')
        away = m.get('away', '')
        actual = f"{m.get('hs', 0)}-{m.get('aws', 0)}"
        
        hcp = OCR_HCP.get((home, away), 0.0)
        ou = OCR_OU.get((home, away), 2.5)
        md = get_matchday(home, away)
        
        mi = MatchInput(
            home=home, away=away,
            odds_h=m.get('1x2_home', 2.0) or 2.0,
            odds_d=m.get('1x2_draw', 3.4) or 3.4,
            odds_a=m.get('1x2_away', 3.8) or 3.8,
            hcp=hcp, ou_line=ou,
            matchday=md, stage='group',
        )
        
        try:
            result = pipeline.predict(mi)
            v = result.get('final_verdict', {})
            pred = str(v.get('primary', '?'))
            pred_score = str(v.get('best_score', '?'))
            
            hs, aws = m['hs'], m['aws']
            is_ok = False
            if hs > aws and ('主胜' in pred or '让胜' in pred): is_ok = True
            elif aws > hs and ('客胜' in pred or '让负' in pred): is_ok = True
            elif hs == aws and '平' in pred: is_ok = True
            
            exact = str(pred_score) == actual
            
            day_results.append({
                'home': home, 'away': away, 'actual': actual, 'ok': is_ok,
                'pred': pred, 'score': pred_score, 'exact': exact,
                'md': md, 'hcp': hcp, 'ou': ou,
            })
        except Exception as e:
            print(f"  ERROR {home} vs {away}: {str(e)[:80]}")
    
    daily_results[target] = day_results
    d_ok = sum(1 for r in day_results if r['ok'])
    d_exact = sum(1 for r in day_results if r['exact'])
    d_draw = sum(1 for r in day_results if '平' in r['pred'])
    d_total = len(day_results)
    cum_ok += d_ok
    cum_exact += d_exact
    cum_total += d_total

# ─── 5. Print summary ───
print()
print("=" * 72)
print("哨响AI v6.0 — Day14-20 全量校准回测")
print("=" * 72)
print(f"{'Day':<8} {'日期':<8} {'准确率':<10} {'精确':<8} {'平局':<8} {'场次':<6}")
print("-" * 72)

for target_day in range(14, 21):
    target = f'6/{target_day}'
    if target not in daily_results: continue
    dr = daily_results[target]
    d_ok = sum(1 for r in dr if r['ok'])
    d_exact = sum(1 for r in dr if r['exact'])
    d_draw = sum(1 for r in dr if '平' in r['pred'])
    d_total = len(dr)
    pct = f"{d_ok}/{d_total}={d_ok/d_total*100:.0f}%"
    ex = f"{d_exact}"
    print(f"Day{target_day:<4} {target:<8} {pct:<10} {ex:<8} {d_draw}/{d_total:<7} {d_total}")

print("-" * 72)
print(f"累计  Day14-20  {cum_ok}/{cum_total}={cum_ok/cum_total*100:.0f}%")
print("=" * 72)

# ─── 6. Per-day detail ───
print()
for target_day in range(14, 21):
    target = f'6/{target_day}'
    if target not in daily_results: continue
    dr = daily_results[target]
    d_ok = sum(1 for r in dr if r['ok'])
    print(f"\n--- Day{target_day} ({target}): {d_ok}/{len(dr)} ---")
    for r in dr:
        icon = '✅' if r['ok'] else '❌'
        e = ' 🎯' if r['exact'] else ''
        print(f"  {icon} {r['home']} vs {r['away']}: {r['pred']}({r['score']}) vs {r['actual']}{e} | MD={r['md']} hcp={r['hcp']:+.2f}")
