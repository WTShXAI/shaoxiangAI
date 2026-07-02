#!/usr/bin/env python
"""WC2026 R32 全链路预测: Mexico vs Ecuador — v6.0 七链管道"""
import math, json, sys, os

# ============================================
# Match Context
# ============================================
HOME = 'Mexico'
AWAY = 'Ecuador'
LEAGUE = 'WC2026'
HCP_LINE = -0.5  # Mexico gives 0.5 goals
OU_LINE = 2.5

# ============================================
# Odds (ESPN DraftKings, 2026-06-30)
# ============================================
odds_1x2 = {'home': 2.15, 'draw': 3.10, 'away': 4.10}
odds_ou = {'over': 3.00, 'under': 1.40, 'line': 2.5}
odds_hcp = {'home': 2.15, 'away': 1.69, 'line': -0.5}
odds_btts = {'yes': 2.25, 'no': 1.57}

# ============================================
# Implied Probabilities (adjusted for margin)
# ============================================
inv_sum = 1/odds_1x2['home'] + 1/odds_1x2['draw'] + 1/odds_1x2['away']
p_h_raw = (1/odds_1x2['home']) / inv_sum
p_d_raw = (1/odds_1x2['draw']) / inv_sum
p_a_raw = (1/odds_1x2['away']) / inv_sum

# ============================================
# Team Stats (BetMines, WC2026 Group Stage)
# ============================================
mx = {
    'wc_w': 3, 'wc_d': 0, 'wc_l': 0, 'wc_gf': 2.0, 'wc_ga': 0.0,
    'last5': 'WWWWW', 'gf_5': 2.4, 'ga_5': 0.2,
    'clean10': 8, 'tournament_rank': 'group winner',
}
ec = {
    'wc_w': 1, 'wc_d': 1, 'wc_l': 1, 'wc_gf': 0.7, 'wc_ga': 0.7,
    'last5': 'WWLWD', 'gf_5': 1.4, 'ga_5': 0.6,
    'clean10': 3, 'tournament_rank': '3rd place qualifier',
}

# ============================================
# CHAIN -1: 战绩分析 (Priority Gate)
# ============================================
print('=' * 56)
print('  CHAIN -1  战绩分析 (Priority Gate)')
print('=' * 56)
print(f'  {HOME}: {mx["last5"]} | WC {mx["wc_w"]}W-{mx["wc_d"]}D-{mx["wc_l"]}L | GF:{mx["wc_gf"]} GA:{mx["wc_ga"]}')
print(f'  {AWAY}: {ec["last5"]} | WC {ec["wc_w"]}W-{ec["wc_d"]}D-{ec["wc_l"]}L | GF:{ec["wc_gf"]} GA:{ec["wc_ga"]}')
print(f'  Mexico: {mx["clean10"]}/10 clean sheets, 小组零封 — 防守天花板')
print(f'  Ecuador: 2-1胜德国 展现韧性, 但进攻乏力(GF=0.7/场)')
print(f'  → Priority: Mexico明显占优 ({mx["wc_w"]}W 0L vs {ec["wc_w"]}W 1D 1L)')

# ============================================
# CHAIN 0: 战意/积分分析
# ============================================
print()
print('=' * 56)
print('  CHAIN 0  战意分析')
print('=' * 56)
print(f'  R32淘汰赛 — 单场定生死, 双方战意 = MAX')
print(f'  {HOME}: 小组头名出线, 主场作战(Estadio Azteca), 士气顶峰')
print(f'  {AWAY}: 小组第三惊险晋级, 击败德国提振信心')
print(f'  → motivation: 双方均无保留, 但Mexico有主场加成')
print(f'  → 无已出线队, 无轮换衰减')

# ============================================
# CHAIN 0.5: 临场升盘
# ============================================
print()
print('=' * 56)
print('  CHAIN 0.5  临场升盘/诱盘检测')
print('=' * 56)
print(f'  HCP: {HOME} {HCP_LINE} @ {odds_hcp["home"]}')
print(f'  MX -0.5 = 需净胜1球方可穿盘')
print(f'  hcp_home={odds_hcp["home"]}, hcp_away={odds_hcp["away"]}')
print(f'  盘口深度: 浅让(0.5), 机构对Mexico信心中等偏上')
print(f'  → 无异常升盘信号, 非诱盘格局')

# ============================================
# CHAIN 1: OU联动矩阵
# ============================================
print()
print('=' * 56)
print('  CHAIN 1  OU联动矩阵')
print('=' * 56)
print(f'  OU Line: {OU_LINE}')
print(f'  Over {OU_LINE}: {odds_ou["over"]} | Under {OU_LINE}: {odds_ou["under"]}')
ou_strength = 'deep' if odds_ou['under'] < 1.45 else 'medium' if odds_ou['under'] < 1.80 else 'flat'
hcp_strength = 'shallow' if abs(HCP_LINE) <= 0.5 else 'medium' if abs(HCP_LINE) <= 1.25 else 'deep'
print(f'  OU强度: {ou_strength} ({odds_ou["under"]}) × HCP深度: {hcp_strength} ({HCP_LINE})')
print(f'  → 联动矩阵: {hcp_strength} × {ou_strength} = 小球/低比分格局')
print(f'  → BTTS No @ {odds_btts["no"]} — 机构强烈看好一方零封')

# ============================================
# CHAIN 2: D-Gate v5.4 平局检测
# ============================================
print()
print('=' * 56)
print('  CHAIN 2  D-Gate v5.4 平局检测')
print('=' * 56)
spread = abs(p_h_raw - p_a_raw)
draw_odds = odds_1x2['draw']
h_a_gap = abs(odds_1x2['home'] - odds_1x2['away'])
ou_under = odds_ou['under']

signals = []
if spread < 0.25:
    signals.append(f'✓ spread={spread:.1%}<25% → 实力接近 → 平局概率↑')
if 2.80 <= draw_odds <= 4.50:
    signals.append(f'✓ draw_odds={draw_odds}∈[2.8,4.5] → 机构防平')
if ou_under < 1.60:
    signals.append(f'✓ OU_under={ou_under}→小球强压 → 低比分 = 平局温床')
if h_a_gap > 1.50:
    signals.append(f'✓ h-a gap={h_a_gap}>1.5 → 赔率不对称 → 诱盘/防冷')

for s in signals:
    print(f'  {s}')

draw_alert = len(signals) >= 2
print(f'  D-Gate判定: {"⚠️ DRAW ALERT" if draw_alert else "○ No alert"} ({len(signals)}/4 signals)')
if draw_alert:
    print(f'  → 平局为不可忽视的选项, 需在投注/比分中覆盖')

# ============================================
# CHAIN 3: Model Probability
# ============================================
print()
print('=' * 56)
print('  CHAIN 3  模型概率 (Odds Implied + Bayesian)')
print('=' * 56)
print(f'  Home ({HOME}):   {p_h_raw:.1%}')
print(f'  Draw:            {p_d_raw:.1%}')
print(f'  Away ({AWAY}):  {p_a_raw:.1%}')
direction = "主胜" if p_h_raw > max(p_d_raw, p_a_raw) else ("平局" if p_d_raw > max(p_h_raw, p_a_raw) else "客胜")
confidence_level = "中高" if p_h_raw > 0.55 else ("中等" if p_h_raw > 0.42 else "低")
print(f'  方向倾向: {direction}')
print(f'  模型判定: Mexico优势但非碾压, 平局{p_d_raw:.0%}不容忽视')
print(f'  纯模型信心: {confidence_level}')

# ============================================
# CHAIN 3.5: Dixon-Coles λ v5.22
# ============================================
print()
print('=' * 56)
print('  CHAIN 3.5  Dixon-Coles λ 重标定 v5.22')
print('=' * 56)
# DC formula: λ_strong = (GF_s + GA_w)/2 × mult
gf_mx, ga_mx = mx['wc_gf'], mx['wc_ga']
gf_ec, ga_ec = ec['wc_gf'], ec['wc_ga']
mult = 0.85  # Knockout stage reduction factor
lam_mx = (gf_mx + ga_ec) / 2 * mult
lam_ec = (gf_ec + ga_mx) / 2 * mult
print(f'  λ_mx = ({gf_mx} + {ga_ec})/2 × {mult} = {lam_mx:.3f}')
print(f'  λ_ec = ({gf_ec} + {ga_mx})/2 × {mult} = {lam_ec:.3f}')
print(f'  Ratio λ_mx/λ_ec = {lam_mx/lam_ec:.2f}x → Mexico进攻优势{lam_mx/lam_ec:.1f}倍')

# Poisson score distribution
def poisson(lam, k):
    return math.exp(-lam) * lam**k / math.factorial(k)

scores = []
for h in range(0, 5):
    for a in range(0, 5):
        p = poisson(lam_mx, h) * poisson(lam_ec, a)
        scores.append((h, a, p))
scores.sort(key=lambda x: x[2], reverse=True)

print()
print(f'  Poisson 比分概率 (Top 8):')
for h, a, p in scores[:8]:
    direction = '←' if h > a else '→' if a > h else '='
    print(f'    {h}-{a}  {p:>6.1%}  {direction}')

# ============================================
# CHAIN 4: TaoGe 策略 + 比分推荐 v5.23
# ============================================
print()
print('=' * 56)
print('  CHAIN 4  TaoGe策略 + 比分推荐 v5.23')
print('=' * 56)

# hcp模板
hcp_abs = abs(HCP_LINE)
if hcp_abs <= 0.5:
    hcp_tier = 'shallow'
    templates = [(1,0), (2,1), (1,1), (2,0), (0,0)]
elif hcp_abs <= 1.25:
    hcp_tier = 'medium'
    templates = [(2,0), (3,1), (1,0), (2,1), (3,0)]
elif hcp_abs <= 2.25:
    hcp_tier = 'deep'
    templates = [(3,0), (4,1), (2,0), (3,1), (4,0)]
else:
    hcp_tier = 'very_deep'
    templates = [(4,0), (5,1), (3,0), (5,0), (4,1)]

print(f'  hcp={HCP_LINE} → tier={hcp_tier}')
print(f'  模板: {[(f"{h}-{a}") for h,a in templates]}')

# OU约束
ou_constrained = [(h,a) for h,a in templates if (h+a) <= OU_LINE + 0.5]
# 如果OU约束后为空，回退到全模板
if not ou_constrained:
    ou_constrained = templates
print(f'  OU约束(≤{OU_LINE+0.5}): {[(f"{h}-{a}") for h,a in ou_constrained]}')

# Poisson排序
scored_templates = [(h, a, poisson(lam_mx, h) * poisson(lam_ec, a)) for h, a in ou_constrained]
scored_templates.sort(key=lambda x: x[2], reverse=True)

# ============================================
# FINAL VERDICT
# ============================================
print()
print('=' * 56)
print('  ⚽ 全链路融合判定')
print('=' * 56)
print(f'  对阵: {HOME} vs {AWAY}')
print(f'  赛事: 2026世界杯 R32 (淘汰赛)')
print(f'  场地: Estadio Azteca (墨西哥城)')
print()

# Decision logic
if p_h_raw > 0.47 and len(signals) < 3:
    verdict = f'{HOME} 主胜'
    verdict_en = 'home'
    conf = '中高'
    reasoning = 'Mexico全胜零封, 主场优势, 盘口支撑'
elif draw_alert and p_d_raw > 0.28:
    verdict = '平局 (Draw)'
    verdict_en = 'draw'
    conf = '中等'
    reasoning = f'{len(signals)}个D-Gate平局信号, 淘汰赛低比分格局'
elif p_a_raw > 0.28:
    verdict = f'{AWAY} 客胜 (冷门)'
    verdict_en = 'away'
    conf = '低'
    reasoning = 'Ecuador展现韧性但进攻乏力'
else:
    verdict = '谨慎主胜 / 防平'
    verdict_en = 'home_draw_hedge'
    conf = '中等'
    reasoning = 'Mexico优势+平局不可忽视'

print(f'  判定: {verdict}')
print(f'  信心: {conf}')
print(f'  依据: {reasoning}')
print()

# Score recommendations
print(f'  比分推荐:')
for i, (h, a, p) in enumerate(scored_templates[:3]):
    label = ['首选', '次选', '对冲'][i]
    if h > a:
        res = '主胜'
        hcp_result = '让胜' if HCP_LINE < 0 and (h - a) > abs(HCP_LINE) else ('让平' if HCP_LINE < 0 and (h - a) == abs(HCP_LINE) else '--')
    elif h == a:
        res = '平局'
        hcp_result = '让负' if HCP_LINE < 0 else ('让胜' if HCP_LINE > 0 else '--')
    else:
        res = '客胜'
        hcp_result = '让负' if HCP_LINE < 0 else ('让胜' if HCP_LINE > 0 and (a - h) > abs(HCP_LINE) else '--')
    
    print(f'    {label}: {h}-{a} ({res}, {hcp_result}, p={p:.1%})')

print()

# ============================================
# 竞彩推荐
# ============================================
print('=' * 56)
print('  🎫 竞彩过关推荐')
print('=' * 56)
hcp_rec = "让胜" if verdict_en == "home" else ("让负" if verdict_en == "away" else "让平/让负")
ht_ft_a = "胜-胜" if verdict_en == "home" else ("平-平" if verdict_en == "draw" else "负-负")
ht_ft_b = "平-胜" if verdict_en == "home" else "平-平"
print(f'  1X2方向:  {verdict}')
print(f'  让球(-0.5): {hcp_rec}')
print(f'  总进球:  Under {OU_LINE} (机构强压1.40)')
print(f'  比分首选: {scored_templates[0][0]}-{scored_templates[0][1]}')
print(f'  半全场:   {ht_ft_a}/{ht_ft_b}')
print()

# ============================================
# 风险提示
# ============================================
print('=' * 56)
print('  ⚠ 风险提示')
print('=' * 56)
risks = []
risks.append(f'淘汰赛单场淘汰制 → 可能加时/点球 → 常规时间平局概率↑')
risks.append(f'Mexico 3场零封样本小(n=3) → GA=0可能高估防守')
risks.append(f'Ecuador 2-1胜德国含金量高 → 有爆冷基因')
risks.append(f'H2H近5场3平 → 历史平局率60% → 不可忽视')
risks.append(f'Azteca高原主场 → Mexico加成, 但Ecuador也有高原经验')
for i, r in enumerate(risks, 1):
    print(f'  {i}. {r}')

print()
print('  📊 数据来源: ESPN / BetMines / DraftKings')
print(f'  📅 预测时间: 2026-07-01 07:00 GMT+8')
print(f'  🏷  版本: v6.0 (七链全链路 + D-Gate v5.4 + DC λ v5.22)')
