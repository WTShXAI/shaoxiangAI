"""哨响AI v6.0 — Day16-Day24 全量回测 — 防泄露 + 正确matchday"""
import sys, os, json
os.chdir('D:/Architecture')
sys.path.insert(0, 'D:/Architecture')
sys.path.insert(0, 'D:/Architecture/predictors/components')

from pipeline.predictors.data_classes import MatchInput
from pipeline.predictors.pipeline import FullLinkagePipeline

# ─── API Schedule matchday mapping (from wc26__get_games.json) ───
# (home_cn, away_cn) -> matchday
MATCHDAY_MAP = {}
with open('data/api_cache/wc26__get_games.json', encoding='utf-8') as f:
    api_data = json.load(f)

# EN->CN mapping (based on OCR data)
EN_CN = {
    'Iran': '伊朗', 'New Zealand': '新西兰', 'France': '法国', 'Senegal': '塞内加尔',
    'Iraq': '伊拉克', 'Norway': '挪威', 'Argentina': '阿根廷', 'Algeria': '阿尔及利亚',
    'Austria': '奥地利', 'Jordan': '约旦', 'Portugal': '葡萄牙',
    'Democratic Republic of the Congo': '民主刚果', 'England': '英格兰', 'Croatia': '克罗地亚',
    'Ghana': '巴拿马', 'Uzbekistan': '乌兹别克', 'Colombia': '哥伦比亚',
    'Czech Republic': '捷克', 'South Africa': '南非', 'Switzerland': '瑞士',
    'Bosnia and Herzegovina': '波黑', 'Canada': '加拿大', 'Qatar': '卡塔尔',
    'Mexico': '墨西哥', 'South Korea': '韩国', 'United States': '美国', 'Australia': '澳大利亚',
    'Scotland': '苏格兰', 'Morocco': '摩洛哥', 'Brazil': '巴西', 'Haiti': '海地',
    'Turkey': '土耳其', 'Paraguay': '巴拉圭', 'Netherlands': '荷兰', 'Sweden': '瑞典',
    'Germany': '德国', 'Ivory Coast': '科特迪瓦', 'Ecuador': '厄瓜多尔', 'Curaçao': '库拉索',
    'Tunisia': '突尼斯', 'Japan': '日本', 'Spain': '西班牙', 'Saudi Arabia': '沙特',
    'Belgium': '比利时', 'Egypt': '埃及', 'Uruguay': '乌拉圭',
    'Cape Verde': '佛得角', 'Costa Rica': '哥斯达黎加',
    'Panama': '巴拿马', 'Ghana': '加纳',
}

for g in api_data['games']:
    h = g.get('home_team_name_en', '')
    a = g.get('away_team_name_en', '')
    md = int(g.get('matchday', 1))
    h_cn = EN_CN.get(h, h)
    a_cn = EN_CN.get(a, a)
    MATCHDAY_MAP[(h_cn, a_cn)] = md

def get_matchday(home_cn, away_cn):
    """Get correct matchday, try both directions"""
    return MATCHDAY_MAP.get((home_cn, away_cn)) or MATCHDAY_MAP.get((away_cn, home_cn)) or 1

# ─── OCR handicap/OU mapping ───
OCR_HCP = {}
OCR_OU = {}
with open('data/wc2026_ocr_full.json', encoding='utf-8') as f:
    ocr = json.load(f)

for m in ocr.get('matches', []):
    h = m.get('home_team', '')
    a = m.get('away_team', '')
    parsed = m.get('parsed', {}) or {}
    if h and a:
        key = (h, a)
        hcp_str = parsed.get('handicap', '0')
        ou_str = parsed.get('ou_line', '2.5')
        try:
            hcp_val = float(hcp_str.replace('+', ''))
        except:
            hcp_val = 0.0
        try:
            # Handle "2.5/3" -> 2.75
            ou_val = sum(float(x) for x in ou_str.split('/')) / max(len(ou_str.split('/')), 1)
        except:
            ou_val = 2.5
        OCR_HCP[key] = hcp_val
        OCR_OU[key] = ou_val

# ─── Load matches ───
with open('data/wc2026_72matches_with_odds.json', encoding='utf-8') as f:
    all_matches = json.load(f)

# ─── Run days 16-24 ───
pipeline = FullLinkagePipeline()
daily_results = {}
cum_dir = cum_exact = cum_draw = cum_total = 0

for target_day in range(16, 25):
    target_date = f'6/{target_day}'
    matches = [m for m in all_matches if m.get('date', '') == target_date]
    if not matches:
        continue
    
    day_results = []
    for m in matches:
        home = m.get('home', '')
        away = m.get('away', '')
        actual_score = f"{m.get('hs', 0)}-{m.get('aws', 0)}"
        
        # Get OCR handicap/OU
        hcp = OCR_HCP.get((home, away), m.get('handicap_float') or m.get('handicap', 0.0) or 0.0)
        ou = OCR_OU.get((home, away), m.get('ou_line_num') or m.get('ou_line', 2.5) or 2.5)
        matchday = get_matchday(home, away)
        
        mi = MatchInput(
            home=home, away=away,
            odds_h=m.get('1x2_home', 2.0) or 2.0,
            odds_d=m.get('1x2_draw', 3.4) or 3.4,
            odds_a=m.get('1x2_away', 3.8) or 3.8,
            hcp=hcp, ou_line=ou,
            matchday=matchday, stage='group',
        )
        
        try:
            result = pipeline.predict(mi)
            verdict = result.get('final_verdict', {})
            pred = str(verdict.get('primary', '?'))
            pred_score = str(verdict.get('best_score', '?'))
            strategy = verdict.get('rec_type', '?')
            
            hs, aws = m.get('hs', 0), m.get('aws', 0)
            if hs > aws: actual_dir = 'H'
            elif aws > hs: actual_dir = 'A'
            else: actual_dir = 'D'
            
            dir_ok = False
            if actual_dir == 'H' and ('主胜' in pred or '让胜' in pred): dir_ok = True
            elif actual_dir == 'A' and ('客胜' in pred or '让负' in pred): dir_ok = True
            elif actual_dir == 'D' and '平' in pred: dir_ok = True
            
            exact_ok = (pred_score == actual_score)
            
            day_results.append({
                'home': home, 'away': away, 'actual': actual_score,
                'pred': pred, 'score': pred_score, 'md': matchday,
                'dir': 'OK' if dir_ok else 'X', 'exact': 'OK' if exact_ok else '',
            })
        except Exception as e:
            print(f"  ERROR {home} vs {away}: {str(e)[:100]}")
    
    daily_results[target_date] = day_results
    d_ok = sum(1 for r in day_results if r['dir'] == 'OK')
    d_exact = sum(1 for r in day_results if r['exact'] == 'OK')
    d_draw = sum(1 for r in day_results if '平' in str(r.get('pred', '')))
    d_total = len(day_results)
    
    cum_dir += d_ok
    cum_exact += d_exact
    cum_draw += d_draw
    cum_total += d_total

# ─── Print summary ───
print()
print("=" * 72)
print("哨响AI v6.0 — Day16-Day24 全量回测 [防泄露 + 正确matchday]")
print("=" * 72)
print(f"{'Day':<8} {'日期':<8} {'准确率':<10} {'平局预测':<10} {'场次':<6} {'类型':<12}")
print("-" * 72)

for target_day in range(16, 25):
    target_date = f'6/{target_day}'
    if target_date not in daily_results:
        continue
    dr = daily_results[target_date]
    d_ok = sum(1 for r in dr if r['dir'] == 'OK')
    d_draw = sum(1 for r in dr if '平' in str(r.get('pred', '')))
    d_total = len(dr)
    mds = set(r['md'] for r in dr)
    md_str = ','.join(f'MD{m}' for m in sorted(mds))
    pct = f"{d_ok}/{d_total}={d_ok/max(d_total,1)*100:.0f}%"
    print(f"Day{target_day:<4} {target_date:<8} {pct:<10} {d_draw}/{d_total:<9} {d_total:<6} {md_str:<12}")

print("-" * 72)
print(f"累计    6/16-24  {cum_dir}/{cum_total}={cum_dir/max(cum_total,1)*100:.0f}%")
print("=" * 72)

print()
print("逐场明细:")
print("-" * 72)
for target_day in range(16, 25):
    target_date = f'6/{target_day}'
    if target_date not in daily_results:
        continue
    dr = daily_results[target_date]
    print(f"\n  --- {target_date} ---")
    for r in dr:
        s = '✅' if r['dir'] == 'OK' else '❌'
        e = ' 🎯' if r['exact'] == 'OK' else ''
        print(f"  {s} {r['home']} vs {r['away']}: {r['pred']}({r['score']}) vs {r['actual']}{e} | MD={r['md']}")
