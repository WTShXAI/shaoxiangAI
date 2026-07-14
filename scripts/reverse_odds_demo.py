import sys, os, sqlite3, json, numpy as np
from collections import defaultdict
sys.path.insert(0, os.getcwd())
from pipeline.reverse_odds_engine import ReverseOddsEngine, OddsInput, Intent

eng = ReverseOddsEngine()

con = sqlite3.connect('data/football_data.db')
cur = con.cursor()
cur.execute("SELECT DISTINCT league FROM odds_features")
all_lg = [r[0] for r in cur.fetchall()]
# 仅用干净的纯联赛名（剔除 15/16 等历史赛季分段与巴西甲以保证五大联赛纯度）
clean_five = [l for l in all_lg if l in ('法甲', '西甲', '英超', '德甲', '意甲')]
rows = cur.execute(
    "SELECT home_team, away_team, league, open_h, open_d, open_a, close_h, close_d, close_a, outcome "
    "FROM odds_features WHERE league IN (%s) AND open_h>0 AND close_h>0" % ','.join('?' * len(clean_five)),
    clean_five
).fetchall()
con.close()

def sane(oi):
    vals = [oi.open_h, oi.open_d, oi.open_a, oi.close_h, oi.close_d, oi.close_a]
    if any(v < 1.05 or v > 18 for v in vals):
        return False
    if any(abs(d) > 0.5 for d in [oi.drift_h, oi.drift_d, oi.drift_a]):
        return False
    if oi.overround > 0.18:
        return False
    return True

samples = []
for r in rows:
    ht, at, lg, oh, od, oa, ch, cd, ca, out = r
    oi = OddsInput(open_h=oh, open_d=od, open_a=oa, close_h=ch, close_d=cd, close_a=ca)
    if not sane(oi):
        continue
    intent, conf, pat = eng.classify_intent(oi)
    if intent in (Intent.FAKE_DEF_H, Intent.FAKE_DEF_A, Intent.HONEST_DEF_H, Intent.HONEST_DEF_A):
        dm = max(abs(oi.drift_h), abs(oi.drift_d), abs(oi.drift_a))
        samples.append({'dm': dm, 'ht': ht, 'at': at, 'lg': lg, 'oi': oi,
                        'intent': intent, 'conf': conf, 'pat': pat, 'out': out})

print("干净样本(含诱盘/诚实防):", len(samples))

stat = defaultdict(lambda: {'n': 0, 'hit': 0})
for s in samples:
    target = eng.INTENT_TARGET[s['intent']]
    if target is None:
        continue
    stat[s['intent'].value]['n'] += 1
    if s['out'] == target:
        stat[s['intent'].value]['hit'] += 1
print("\n=== 意图判定历史命中率(五大联赛, 过滤脏数据后) ===")
for k, v in stat.items():
    rate = v['hit'] / v['n'] if v['n'] else 0
    print("  %s: n=%d 命中=%d 命中率=%.1f%%" % (k, v['n'], v['hit'], rate * 100))

def pick_rep(intent_set):
    cands = [s for s in samples if s['intent'] in intent_set]
    cands.sort(key=lambda x: -x['dm'])
    hit_cands = [s for s in cands if eng.INTENT_TARGET[s['intent']] == s['out']]
    return hit_cands[0] if hit_cands else cands[0]

fake = pick_rep({Intent.FAKE_DEF_H, Intent.FAKE_DEF_A})
honest = pick_rep({Intent.HONEST_DEF_H, Intent.HONEST_DEF_A})

def analyze_block(s):
    oi = s['oi']
    res = eng.analyze(oi)
    target = eng.INTENT_TARGET[s['intent']]
    return {
        'ht': s['ht'], 'at': s['at'], 'lg': s['lg'],
        'open': [oi.open_h, oi.open_d, oi.open_a],
        'close': [oi.close_h, oi.close_d, oi.close_a],
        'drift': [oi.drift_h, oi.drift_d, oi.drift_a],
        'pattern': s['pat'], 'intent': s['intent'].value, 'conf': s['conf'],
        'implied': [round(p, 4) for p in res.implied_probs],
        'true_probs': [round(p, 4) for p in res.true_probs],
        'hit_prob': round(res.argmax_hit_prob, 4), 'edge': round(res.expected_edge, 4),
        'mp': round(res.mispricing_score, 3), 'kelly': round(res.kelly_fraction, 4),
        'bet': res.recommended_bet, 'verdict': res.verdict,
        'outcome': s['out'], 'target': target, 'verdict_hit': (s['out'] == target),
    }

fake_b = analyze_block(fake)
honest_b = analyze_block(honest)

for b, lbl in [(fake_b, '诱盘 FAKE_DEF'), (honest_b, '诚实防 HONEST_DEF')]:
    print("\n" + "=" * 60)
    print("【%s】 %s vs %s [%s]" % (lbl, b['ht'], b['at'], b['lg']))
    print("  开盘 H%.2f D%.2f A%.2f -> 收盘 H%.2f D%.2f A%.2f" %
          (b['open'][0], b['open'][1], b['open'][2], b['close'][0], b['close'][1], b['close'][2]))
    print("  drift H%+.1f%% D%+.1f%% A%+.1f%%  形态%s 意图%s conf%.0f%%" %
          (b['drift'][0] * 100, b['drift'][1] * 100, b['drift'][2] * 100, b['pattern'], b['intent'], b['conf'] * 100))
    print("  收盘隐含 H%.1f%% D%.1f%% A%.1f%%" %
          (b['implied'][0] * 100, b['implied'][1] * 100, b['implied'][2] * 100))
    print("  argmax命中估计 %.1f%% (edge %+.1f%%) 误定价 %.3f 凯利 %+.1f%% -> 下注%s" %
          (b['hit_prob'] * 100, b['edge'] * 100, b['mp'], b['kelly'] * 100, b['bet']))
    print("  真实概率估计 H%.1f%% D%.1f%% A%.1f%%" %
          (b['true_probs'][0] * 100, b['true_probs'][1] * 100, b['true_probs'][2] * 100))
    print("  结论: %s" % b['verdict'])
    print("  实际赛果=%s | 意图指向=%s | 判定%s" %
          (b['outcome'], b['target'], "✅正确" if b['verdict_hit'] else "❌错误"))

out = {'stat': dict(stat), 'fake': fake_b, 'honest': honest_b,
       'clean_samples': len(samples), 'raw_samples': len(rows)}
with open('deliverables/reverse_odds_demo_20260710.json', 'w', encoding='utf-8') as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print("\nJSON saved: deliverables/reverse_odds_demo_20260710.json")
