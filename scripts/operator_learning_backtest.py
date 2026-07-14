# -*- coding: utf-8 -*-
"""
操盘手学习 / 逐场回测引擎
================================
把"让球研究结果"(AH=Margin非Winner / 分歧信1X2 / 深盘难穿 / 高平预警 / 高水风险)
反哺给操盘手模块，对 WC2026 全量比赛逐场回测验证，提炼判断思路 v2。

数据源:
  - 金标准: odds_db/handicap_db_matched.json (71场, 真实亚盘线+赛果)
  - 银标准: wc_all_matches edition='2026' (136场, 1X2+赛果, 部分无AH)
"""
import json, sqlite3
from collections import defaultdict, Counter

DB = 'data/football_data.db'
GOLD = 'odds_db/handicap_db_matched.json'
OUT_JSON = 'odds_db/operator_backtest_full.json'
OUT_REPORT = 'odds_db/operator_learning_report.md'

# ---------- 工具函数 ----------
def deoverround(oh, od, oa):
    inv = 1.0/oh + 1.0/od + 1.0/oa
    return (1.0/oh)/inv, (1.0/od)/inv, (1.0/oa)/inv

def argmax_dir(ph, pd, pa):
    probs = {'home': ph, 'draw': pd, 'away': pa}
    return max(probs, key=probs.get)

def norm_team(t):
    m = {'乌兹别克斯坦':'乌兹别克','佛得角共和国':'佛得角','沙特阿拉伯':'沙特',
         '民主刚果':'刚果(金)','刚果民主共和国':'刚果(金)','韩国共和国':'韩国'}
    if t in m: return m[t]
    return t.replace('共和国','').strip()

def norm_result(r):
    """统一赛果表示: H/D/A -> home/draw/away"""
    return {'H':'home','D':'draw','A':'away'}.get(r, r)

# ---------- 1. 载入金标准 ----------
gold = json.load(open(GOLD, encoding='utf-8'))['records']
gold_keys = {}
gold_recs = []
for r in gold:
    key = (norm_team(r['home']), norm_team(r['away']))
    rec = {
        'home': r['home'], 'away': r['away'], 'tier': 'gold',
        'oh': r['oh'], 'od': r['od'], 'oa': r['oa'],
        'hg': r['hg'], 'ag': r['ag'], 'actual': norm_result(r['actual']),
        'hcp_line': r.get('hcp_line'), 'hcp_ho': r.get('hcp_ho'), 'hcp_ao': r.get('hcp_ao'),
        'hcp_dir': r.get('hcp_dir'), 'x12_dir': r.get('x12_dir'),
        'hcp_depth': r.get('hcp_depth'),
    }
    gold_keys[key] = rec
    gold_recs.append(rec)

# ---------- 2. 载入银标准 (2026 全部, 去重金标准) ----------
con = sqlite3.connect(DB)
cur = con.cursor()
cur.execute("SELECT home,away,hg,ag,final_result,oh,od,oa,stage FROM wc_all_matches WHERE edition='2026'")
rows = cur.fetchall()
con.close()

silver_recs = []
for home, away, hg, ag, fr, oh, od, oa, stage in rows:
    key = (norm_team(home), norm_team(away))
    if key in gold_keys:
        continue  # 金标准已覆盖
    if oh is None or od is None or oa is None:
        continue  # 无赔率, 无法回测1X2
    silver_recs.append({
        'home': home, 'away': away, 'tier': 'silver', 'stage': stage,
        'oh': float(oh), 'od': float(od), 'oa': float(oa),
        'hg': hg, 'ag': ag, 'actual': norm_result(fr),
        'hcp_line': None, 'hcp_dir': None, 'x12_dir': None, 'hcp_depth': None,
    })

all_recs = gold_recs + silver_recs
print(f"金标准: {len(gold_recs)} 场 | 银标准: {len(silver_recs)} 场 | 合计: {len(all_recs)} 场")
print(f"[debug] 2026总场=136  金标准去重键={len(gold_keys)}  银标准={len(silver_recs)}")

# ---------- 3. 操盘手信号 + 手段分类 + 回测 ----------
DRAW_ALERT = 0.26
HIGH_VIG = 0.12

def classify_tactic(r, ph, pd, pa, fav_prob, draw_alert, overround):
    """庄家'手段'分类 (operator-observable bookmaker tactic)"""
    if draw_alert:
        return '高平预警'          # 庄家把平局赔率压低/抬高, 暗藏平局风险
    if r['tier'] == 'gold' and r['hcp_dir'] and r['x12_dir'] and r['hcp_dir'] != r['x12_dir']:
        return '分歧盘'            # 亚盘与1X2顶牛, 庄家制造方向混乱
    if r['tier'] == 'gold' and r['hcp_line'] is not None and abs(r['hcp_line']) >= 1.5:
        return '深盘阻上'          # 强队给深盘, 用盘口吓退上盘玩家
    if r['tier'] == 'gold' and r['hcp_line'] is not None and abs(r['hcp_line']) < 0.25:
        return '平手无观点'        # 庄家完全不表态
    if fav_prob >= 0.62:
        return '一边倒强队'        # 热门一边倒, 庄家顺势开浅/中盘
    if fav_prob <= 0.42:
        return '均势难分'          # 势均力敌, 庄家开平手/浅盘
    return '常规盘'

def operator_pick(r, ph, pd, pa, draw_alert, overround):
    """操盘手判断思路 v2 的落子逻辑"""
    # 1X2 市场 argmax 为一级信号
    pick = argmax_dir(ph, pd, pa)
    # 分歧盘: 以1X2为准 (R1)
    if r['tier'] == 'gold' and r['hcp_dir'] and r['x12_dir'] and r['hcp_dir'] != r['x12_dir']:
        pick = r['x12_dir']   # 信1X2
    # 高平预警: 若平局概率最高且触发预警, 直接选平 (R4)
    if draw_alert and pd >= ph and pd >= pa:
        pick = 'draw'
    return pick

backtest = []
tactic_stats = defaultdict(lambda: {'n':0,'op_correct':0,'x12_correct':0,'hcp_correct':0})
disagree = []
for r in all_recs:
    ph, pd, pa = deoverround(r['oh'], r['od'], r['oa'])
    inv = 1.0/r['oh']+1.0/r['od']+1.0/r['oa']
    overround = inv - 1.0
    fav_prob = max(ph, pd, pa)
    draw_alert = pd >= DRAW_ALERT
    x12 = argmax_dir(ph, pd, pa)
    op_pick = operator_pick(r, ph, pd, pa, draw_alert, overround)
    op_correct = (op_pick == r['actual'])
    x12_correct = (x12 == r['actual'])
    # AH 作为WDL预测: 选hcp_dir那队赢 (不含平)
    hcp_correct = False
    if r['hcp_dir']:
        hcp_correct = (r['actual'] == r['hcp_dir'])
    tactic = classify_tactic(r, ph, pd, pa, fav_prob, draw_alert, overround)
    # 分歧盘单独收集
    if tactic == '分歧盘':
        disagree.append({
            'home': r['home'], 'away': r['away'], 'line': r['hcp_line'],
            'x12_dir': r['x12_dir'], 'hcp_dir': r['hcp_dir'],
            'score': f"{r['hg']}-{r['ag']}", 'actual': r['actual'],
            'op_pick': op_pick, 'op_correct': op_correct,
        })
    ts = tactic_stats[tactic]
    ts['n'] += 1
    if r['tier'] == 'gold':
        ts['hcp_n'] = ts.get('hcp_n', 0) + 1
    ts['op_correct'] += int(op_correct)
    ts['x12_correct'] += int(x12_correct)
    ts['hcp_correct'] += int(hcp_correct)
    backtest.append({
        'home': r['home'], 'away': r['away'], 'tier': r['tier'], 'tactic': tactic,
        'oh': r['oh'], 'od': r['od'], 'oa': r['oa'],
        'p_h': round(ph,3), 'p_d': round(pd,3), 'p_a': round(pa,3),
        'overround': round(overround,3), 'draw_alert': draw_alert,
        'x12_dir': x12, 'hcp_dir': r['hcp_dir'], 'hcp_line': r['hcp_line'],
        'operator_pick': op_pick, 'actual': r['actual'],
        'op_correct': op_correct, 'x12_correct': x12_correct,
    })

# ---------- 4. 汇总 ----------
total = len(backtest)
op_acc = sum(1 for b in backtest if b['op_correct'])/total
x12_acc = sum(1 for b in backtest if b['x12_correct'])/total
# 分歧盘 PK
dis_n = len(disagree)
dis_op = sum(1 for d in disagree if d['op_correct'])
# 深盘穿盘 (gold only)
deep = [b for b in backtest if b['tier']=='gold' and b['hcp_line'] is not None and abs(b['hcp_line'])>=1.5]
deep_cover = 0
for d in deep:
    # 用原始gold rec 计算穿盘
    pass

# 深盘穿盘率 (从 gold recs 重算)
deep_gold = [r for r in gold_recs if r['hcp_line'] is not None and abs(r['hcp_line'])>=1.5]
cover=push=lose=0
for r in deep_gold:
    line=r['hcp_line']; hg=r['hg']; ag=r['ag']
    # favorite = hcp_dir (lower odds side)
    if r['hcp_dir']=='home':
        diff = hg - ag - line
    else:
        diff = ag - hg + line
    if diff > 0: cover+=1
    elif diff == 0: push+=1
    else: lose+=1

print(f"\n=== 全量回测 ({total}场) ===")
print(f"操盘手(1X2为主+规则) WDL命中: {op_acc*100:.1f}%")
print(f"纯1X2 argmax WDL命中:        {x12_acc*100:.1f}%")
print(f"分歧盘: {dis_n}场 | 操盘手(信1X2)命中 {dis_op} ({dis_op/dis_n*100:.1f}%)")
print(f"深盘({len(deep_gold)}场): 穿{cover} 走{push} 输{lose} -> 穿盘率 {cover/(cover+lose)*100:.1f}%")

# 写入JSON
json.dump({'total':total,'operator_accuracy':round(op_acc,4),'x12_accuracy':round(x12_acc,4),
           'disagree':disagree,'tactic_stats':{k:dict(v) for k,v in tactic_stats.items()},
           'deep_cover':{'cover':cover,'push':push,'lose':lose,'n':len(deep_gold)},
           'matches':backtest}, open(OUT_JSON,'w',encoding='utf-8'), ensure_ascii=False, indent=1)
print(f"\n已写出: {OUT_JSON}")

# 打印手段分布
print("\n=== 各手段分布与命中 ===")
for t, s in sorted(tactic_stats.items(), key=lambda x:-x[1]['n']):
    hn = s.get('hcp_n', 0)
    hcp_str = f"{s['hcp_correct']/hn*100:5.1f}%" if hn else "  n/a"
    print(f"  {t:8s} n={s['n']:3d}  操盘手 {s['op_correct']/s['n']*100:5.1f}%  1X2 {s['x12_correct']/s['n']*100:5.1f}%  AH(仅金) {hcp_str}")
