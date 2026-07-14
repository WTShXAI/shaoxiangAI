#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
WC2026 全源合并回测 (最大规模, 真实赔率+真实赛果)
==================================================
数据源:
  A. 3个脚本源 (去重后 37 场, 6.11~6.21): validate_wc2026 / wc2026_backtest_final / worldcup2026_backtest
  B. 桌面截图 OCR 源 (37 场, 6.22~7.2) -> 其中 36 场在 football_data.db 命中真实赛果
赛果: 全部取自 football_data.db (matches 表中文行 final_result), 禁止任何虚拟/模拟数据
引擎: wc_engine v7.1 (rule 模式 = 6步流水线; optimized 模式 = 同规则 + ML融合层)
     注: WC match_features 表实际含 ~198 场世界杯行 (77维特征), 模型文件存在 ->
         optimized 在队名命中DB时真实调用 wc_main_v1+DrawExpert 融合, 一般 ≠ rule。
赛果: 全部取自 football_data.db (matches 表 final_result) 为唯一权威; 脚本源自带 res 仅作交叉校验,
      若与 DB 不符以 DB 为准 (禁止任何虚拟/编造/源内错误数据进入评分)。
去重: 按 DB 规范队名对 (home,away) 去重, 解决 佛得角/佛得角共和国 等同场别名被计两次的问题。
输出: deliverables/wc2026_full_backtest.json
"""
import sys, os, json, importlib.util, sqlite3
from pathlib import Path
from collections import defaultdict

ARCH = Path(r"D:/Architecture")
PIPE = ARCH / "pipeline"
DB = ARCH / "data" / "football_data.db"
sys.path.insert(0, str(PIPE))
sys.path.insert(0, str(PIPE / "archive"))

import numpy as np
import wc_engine as W

def load_mod(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

vwc = load_mod("validate_wc2026", PIPE / "archive" / "validate_wc2026.py")
bf  = load_mod("wc2026_backtest_final", PIPE / "wc2026_backtest_final.py")
wb  = load_mod("worldcup2026_backtest", PIPE / "worldcup2026_backtest.py")

# ═══ 1. 汇总三脚本源原始比赛 ═══
raw_script = []
for m in bf.COMPLETED:
    home, away, oh, od, oa, hcp, ou, res, sc, date = m
    raw_script.append(dict(src="bf", home=home, away=away, oh=oh, od=od, oa=oa,
                           hcp=hcp, ou=ou, res=res, sc=sc, date=date))
for m in wb.MATCHES:
    home, away, oh, od, oa, hcp, ou, res, sc, date = m
    raw_script.append(dict(src="wb", home=home, away=away, oh=oh, od=od, oa=oa,
                           hcp=hcp, ou=ou, res=res, sc=sc, date=date))
for (date, home, away, hs, aws, result, ho, do, ao) in vwc.WC2026:
    raw_script.append(dict(src="vwc", home=home, away=away, oh=ho, od=do, oa=ao,
                           hcp=0.0, ou=2.5, res=result, sc=f"{hs}-{aws}", date=date))
print(f"[脚本源] 原始 {len(raw_script)} 条 (bf/wb/vwc)")

# ═══ 2. OCR 源 ═══
ocr = json.load(open(str(ARCH / "data" / "wc2026_screenshot_odds_full.json"), encoding="utf-8"))
print(f"[OCR源] {len(ocr)} 场 (6.22~7.2)")

# ═══ 3. DB 真实赛果 ═══
con = sqlite3.connect(str(DB)); cur = con.cursor()
cur.execute("""SELECT match_date, home_team_name, away_team_name, home_score, away_score,
                      final_result, matchday
               FROM matches WHERE league_name='世界杯' AND final_result IS NOT NULL
               AND home_team_name IS NOT NULL AND away_team_name IS NOT NULL""")
db_rows = cur.fetchall()
print(f"[DB] 有赛果WC行 {len(db_rows)}")

def find_db(h, a):
    for r in db_rows:
        if r[1] == h and r[2] == a: return r
        if r[1] == a and r[2] == h: return r
    for r in db_rows:
        if (h in r[1] or r[1] in h) and (a in r[2] or r[2] in a): return r
        if (h in r[2] or r[2] in h) and (a in r[1] or r[1] in a): return r
    return None

# ═══ 4. 合并 + 规范化 + 去重 ═══
# 赛果一律以 DB final_result 为准 (脚本源自带 res 仅交叉校验, 不一致则记 mismatch 并采用 DB)。
# 去重键 = DB 规范队名对 (home,away), 解决 佛得角/佛得角共和国 等同场别名被计两次。
# 任何无法在 DB 命中真实赛果的比赛一律排除 (禁止虚拟/编造数据进入评分)。
final = []
seen = {}
stat = {'added_script': 0, 'added_ocr': 0, 'dup': 0, 'skip_script': 0, 'skip_ocr': 0,
        'res_mismatch': []}

def add_match(home, away, oh, od, oa, hcp, ou, src, raw_res, raw_sc, raw_date):
    db = find_db(home, away)
    if not db:
        return 'skip'                       # 无真实赛果 -> 排除
    ch, ca = db[1], db[2]                   # 规范队名
    key = (ch, ca)
    if key in seen:
        return 'dup'                        # 已计入 (如 佛得角 == 佛得角共和国)
    seen[key] = True
    match = dict(home=ch, away=ca, oh=oh, od=od, oa=oa, hcp=hcp, ou=ou,
                 res=db[5], sc=f"{db[3]}-{db[4]}",
                 date=str(db[0]), matchday=db[6], src=src)
    if raw_res is not None and raw_res != db[5]:
        match['raw_res'] = raw_res
        match['res_match'] = False
        stat['res_mismatch'].append(dict(src=src, home=ch, away=ca,
                                         raw=raw_res, db=db[5],
                                         db_score=f"{db[3]}-{db[4]}"))
    final.append(match)
    return 'added'

for m in raw_script:
    rc = add_match(m['home'], m['away'], m['oh'], m['od'], m['oa'], m['hcp'], m['ou'],
                   "script/"+m['src'], m['res'], m['sc'], m['date'])
    if rc == 'added': stat['added_script'] += 1
    elif rc == 'dup': stat['dup'] += 1
    elif rc == 'skip': stat['skip_script'] += 1
for m in ocr:
    rc = add_match(m['home'], m['away'], m['oh'], m['od'], m['oa'], m['hcp'], m['ou'],
                   "ocr", None, None, m['date'])
    if rc == 'added': stat['added_ocr'] += 1
    elif rc == 'dup': stat['dup'] += 1
    elif rc == 'skip': stat['skip_ocr'] += 1

print(f"[合并] 脚本 {stat['added_script']} + OCR {stat['added_ocr']} = {len(final)} 场 "
      f"(去重 {stat['dup']}, 脚本未命中DB {stat['skip_script']}, OCR未命中 {stat['skip_ocr']})")
if stat['res_mismatch']:
    print(f"[赛果校验] {len(stat['res_mismatch'])} 场脚本源 res 与 DB 不符, 已采用 DB:")
    for x in stat['res_mismatch']:
        print(f"   {x['src']} {x['home']} vs {x['away']}: 源={x['raw']} DB={x['db']} ({x['db_score']})")

# ═══ 5. 跑引擎 ═══
def argmax_pred(oh, od, oa):
    t = 1/oh + 1/od + 1/oa
    ph, pd, pa = 1/oh/t, 1/od/t, 1/oa/t
    return max(('H', ph), ('D', pd), ('A', pa), key=lambda x: x[1])[0]

details = []
skipped = 0
for m in final:
    mi = W.MatchInput(home=m['home'], away=m['away'], odds_h=m['oh'], odds_d=m['od'],
                      odds_a=m['oa'], hcp=m['hcp'], ou_line=m['ou'], stage="group",
                      matchday=m['matchday'], r3_rotation=False)
    try:
        r_rule = W.predict(mi, mode="rule")
        r_opt = W.predict(mi, mode="optimized")
    except Exception as e:
        skipped += 1
        print(f"  !! 引擎异常 {m['home']} vs {m['away']}: {e}")
        continue
    am = argmax_pred(m['oh'], m['od'], m['oa'])
    details.append(dict(
        date=m['date'], home=m['home'], away=m['away'], src=m['src'],
        res=m['res'], sc=m['sc'], oh=m['oh'], od=m['od'], oa=m['oa'],
        rule=r_rule.prediction, rule_conf=round(r_rule.confidence, 3),
        opt=r_opt.prediction, opt_conf=round(r_opt.confidence, 3),
        argmax=am,
        rule_ok=(r_rule.prediction == m['res']),
        opt_ok=(r_opt.prediction == m['res']),
        am_ok=(am == m['res']),
        opt_eq_rule=(r_opt.prediction == r_rule.prediction),
    ))

# ═══ 6. 指标 ═══
def metrics(preds, actuals):
    n = len(preds)
    correct = sum(1 for p, a in zip(preds, actuals) if p == a)
    acc = correct / n if n else 0
    out = {'n': n, 'acc': acc, 'correct': correct}
    for cls in ('H', 'D', 'A'):
        tp = sum(1 for p, a in zip(preds, actuals) if p == cls and a == cls)
        fp = sum(1 for p, a in zip(preds, actuals) if p == cls and a != cls)
        fn = sum(1 for p, a in zip(preds, actuals) if p != cls and a == cls)
        prec = tp/(tp+fp) if (tp+fp) else 0
        rec = tp/(tp+fn) if (tp+fn) else 0
        f1 = 2*prec*rec/(prec+rec) if (prec+rec) else 0
        out[f'{cls}_tp'] = tp; out[f'{cls}_fp'] = fp; out[f'{cls}_fn'] = fn
        out[f'{cls}_prec'] = prec; out[f'{cls}_rec'] = rec; out[f'{cls}_f1'] = f1
    return out

actuals = [d['res'] for d in details]
rule_preds = [d['rule'] for d in details]
opt_preds = [d['opt'] for d in details]
am_preds = [d['argmax'] for d in details]

m_rule = metrics(rule_preds, actuals)
m_opt = metrics(opt_preds, actuals)
m_am = metrics(am_preds, actuals)

opt_eq_rule_all = all(d['opt_eq_rule'] for d in details)

# ═══ 模型警示: optimized 是否在训练集上评估(数据泄漏) ═══
# wc_main_v1 自报 CV 准确率≈74.1%, 本回测却得≈98.6%, 巨大落差说明回测样本与
# 训练样本高度重叠 -> optimized 准确率属 IN-SAMPLE, 不能作为 live 预报性能代表。
model_caveat = None
try:
    reg_path = ARCH / "saved_models" / "model_registry.json"
    if reg_path.exists():
        reg = json.load(open(str(reg_path), encoding="utf-8"))
        cur = reg.get("current", {})
        wc_samples = cur.get("wc_samples")
        cv_acc = (cur.get("metrics") or {}).get("main_acc")
        if wc_samples:
            model_caveat = (f"optimized 使用 wc_main_v1 (训练于 {wc_samples} 场真实WC比赛, "
                            f"模型自报CV准确率 {cv_acc:.1%}); 本回测样本取自同一WC赛果集, "
                            f"与训练集高度重叠, 故 optimized 准确率属 IN-SAMPLE(训练集内), "
                            f"非真实外推预报准确率, 不可作为 live 预测性能代表。")
except Exception:
    model_caveat = None

# 每日
daily = defaultdict(lambda: {'n':0,'rule_ok':0,'opt_ok':0,'am_ok':0,'d_total':0,'d_rule':0})
for d in details:
    b = daily[d['date']]
    b['n'] += 1
    b['rule_ok'] += int(d['rule_ok']); b['opt_ok'] += int(d['opt_ok']); b['am_ok'] += int(d['am_ok'])
    if d['res'] == 'D':
        b['d_total'] += 1
        b['d_rule'] += int(d['rule_ok'])

# 按源
by_src = defaultdict(lambda: {'n':0,'rule_ok':0,'opt_ok':0,'am_ok':0})
for d in details:
    s = d['src'].split('/')[0]
    by_src[s]['n'] += 1
    by_src[s]['rule_ok'] += int(d['rule_ok'])
    by_src[s]['opt_ok'] += int(d['opt_ok'])
    by_src[s]['am_ok'] += int(d['am_ok'])

summary = {
    'n_total': len(final),
    'n_scored': len(details),
    'n_skipped': skipped,
    'actual_dist': {c: sum(1 for a in actuals if a==c) for c in ('H','D','A')},
    'rule': {k: m_rule[k] for k in ('n','acc','correct','D_f1','D_rec','D_prec','H_f1','A_f1')},
    'optimized': {k: m_opt[k] for k in ('n','acc','correct','D_f1','D_rec','D_prec','H_f1','A_f1')},
    'argmax_baseline': {k: m_am[k] for k in ('n','acc','correct','D_f1','D_rec','D_prec','H_f1','A_f1')},
    'optimized_eq_rule_for_all': opt_eq_rule_all,
    'by_src': {k: by_src[k] for k in by_src},
    'data_integrity': {
        'n_raw_script': len(raw_script),
        'n_raw_ocr': len(ocr),
        'dedup_removed': stat['dup'],
        'script_no_db': stat['skip_script'],
        'ocr_no_db': stat['skip_ocr'],
        'res_mismatch_count': len(stat['res_mismatch']),
        'res_mismatch': stat['res_mismatch'],
        'res_source': 'football_data.db matches.final_result (脚本源 res 仅作交叉校验)',
    },
    'model_caveat': model_caveat,
    'note': ('optimized==rule: 全部比赛两模式判决一致'
             if opt_eq_rule_all else 'optimized 与 rule 存在分歧 (optimized 在队名命中DB时调用ML融合层)'),
}

out = {'summary': summary, 'daily': {k: daily[k] for k in sorted(daily)}, 'details': details}
(ARCH / "deliverables").mkdir(exist_ok=True)
with open(str(ARCH / "deliverables" / "wc2026_full_backtest.json"), "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)

# ═══ 7. 打印 ═══
print("\n" + "="*64)
print("WC2026 全源合并回测 (真实赔率 + 真实赛果)")
print("="*64)
ad = summary['actual_dist']
dn = ad['D']; tot = summary['n_scored']
print(f"样本: {tot} 场 (脚本 {by_src.get('script',{}).get('n',0)} + OCR {by_src.get('ocr',{}).get('n',0)}) | 跳过 {skipped}")
print(f"实际分布: H={ad['H']} D={dn} A={ad['A']} (平局率 {dn/tot:.1%})")
print(f"\n{'指标':<14}{'Argmax':>10}{'Rule':>10}{'Opt':>10}")
print(f"{'准确率':<14}{m_am['acc']:>9.1%}{m_rule['acc']:>10.1%}{m_opt['acc']:>10.1%}")
print(f"{'D-F1':<14}{m_am['D_f1']:>10.3f}{m_rule['D_f1']:>10.3f}{m_opt['D_f1']:>10.3f}")
print(f"{'D召回':<14}{m_am['D_rec']:>9.1%}{m_rule['D_rec']:>10.1%}{m_opt['D_rec']:>10.1%}")
print(f"{'D精确':<14}{m_am['D_prec']:>9.1%}{m_rule['D_prec']:>10.1%}{m_opt['D_prec']:>10.1%}")
print(f"\noptimized==rule 全场一致: {opt_eq_rule_all}")
di = summary['data_integrity']
print(f"[数据完整性] 原始脚本 {di['n_raw_script']} + OCR {di['n_raw_ocr']} -> 去重移除 {di['dedup_removed']}, "
      f"脚本未命中DB {di['script_no_db']}, OCR未命中 {di['ocr_no_db']}, 源/DB赛果不符 {di['res_mismatch_count']}")
print("\n按源:")
for s, v in by_src.items():
    ro = v['rule_ok']/v['n'] if v['n'] else 0
    oo = v['opt_ok']/v['n'] if v['n'] else 0
    ao = v['am_ok']/v['n'] if v['n'] else 0
    print(f"  {s:<8} n={v['n']:<3} rule {v['rule_ok']}/{v['n']} ({ro:.0%}) | opt {v['opt_ok']}/{v['n']} ({oo:.0%}) | argmax {v['am_ok']}/{v['n']} ({ao:.0%})")
print("\n每日 (rule / argmax):")
for dt in sorted(daily):
    b = daily[dt]
    print(f"  {dt}: {b['rule_ok']}/{b['n']} ({b['rule_ok']/b['n']:.0%}) | am {b['am_ok']}/{b['n']} (平局 {b['d_rule']}/{b['d_total']})")
print(f"\nJSON -> deliverables/wc2026_full_backtest.json")
if summary.get('model_caveat'):
    print("\n⚠️ 模型警示:", summary['model_caveat'])
