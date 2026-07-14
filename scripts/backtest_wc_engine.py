"""v7.1 引擎 · 世界杯历史诚实回测
对 wc_xlsx_matches (4届WC, 280场, 含真实赔率+比分) 跑 engine.predict,
对比 模型准确率 vs 市场argmax(赔率隐含)基线.
仅读取DB, 不写库. 输出准确率/平局指标/分届明细.
"""
import sys, time, sqlite3
sys.path.insert(0, r'D:\Architecture')
from pipeline.engine import create_engine
from pipeline.predictors.data_classes import MatchInput

DB = r'D:\Architecture\data\football_data.db'
ENGINE = create_engine("wc")

def actual_code(hg, ag):
    if hg > ag: return "H"
    if hg < ag: return "A"
    return "D"

def market_argmax(oh, od, oa):
    return "H" if (oh < od and oh < oa) else ("D" if (od < oh and od < oa) else "A")

con = sqlite3.connect(DB); cur = con.cursor()
cur.execute("SELECT edition, home, away, hg, ag, oh, od, oa, stage FROM wc_xlsx_matches")
rows = cur.fetchall()

total = 0; model_ok = 0; mkt_ok = 0
draw_total = 0; draw_pred = 0; draw_correct = 0
per_ed = {}
t0 = time.time()
fails = 0
for edition, home, away, hg, ag, oh, od, oa, stage in rows:
    if None in (hg, ag, oh, od, oa):
        continue
    actual = actual_code(hg, ag)
    mkt = market_argmax(oh, od, oa)
    try:
        m = MatchInput(
            home=home, away=away,
            odds_h=oh, odds_d=od, odds_a=oa,
            hcp=0.0, ou_line=2.5,
            over_water=1.90, under_water=1.92,
            matchday=3, r3_rotation=False,
            stage=("knockout" if any(k in (stage or "") for k in ["Final","Semi","Quarter","Round of 16"," knockout","Knockout"]) else "group"),
            home_formation="", away_formation="",
            home_full_strength=True, away_full_strength=True,
            home_missing_stars="", away_missing_stars="",
            sporttery_hcp=0.0,
        )
        res = ENGINE.predict(m)
        pred = res.prediction if hasattr(res, "prediction") else None
    except Exception as e:
        fails += 1
        if fails <= 3:
            print(f"  FAIL {home} vs {away}: {e}")
        pred = None

    total += 1
    if mkt == actual: mkt_ok += 1
    if pred == actual:
        model_ok += 1
    # 平局指标
    if actual == "D":
        draw_total += 1
        if pred == "D":
            draw_pred += 1
            draw_correct += 1
        elif mkt == "D":
            draw_pred += 1  # 市场也判平
    else:
        if pred == "D":
            draw_pred += 1  # 误判平局
    # 分届
    e = per_ed.setdefault(edition, dict(n=0, m=0, k=0, dt=0, dp=0, dc=0))
    e["n"] += 1
    if pred == actual: e["m"] += 1
    if mkt == actual: e["k"] += 1
    if actual == "D":
        e["dt"] += 1
        if pred == "D": e["dp"] += 1; e["dc"] += 1
    else:
        if pred == "D": e["dp"] += 1

elapsed = time.time() - t0
print(f"\n=== v7.1 引擎 WC 历史回测 (n={total}, 耗时{elapsed:.1f}s, predict失败={fails}) ===")
print(f"模型准确率 : {model_ok/total*100:.1f}%  ({model_ok}/{total})")
print(f"市场argmax : {mkt_ok/total*100:.1f}%  ({mkt_ok}/{total})")
print(f"平局真实数 : {draw_total} ({draw_total/total*100:.1f}%)")
if draw_total:
    print(f"平局召回   : {draw_correct/draw_total*100:.1f}%  (模型判平中实际平 {draw_correct}/{draw_total})")
if draw_pred:
    print(f"平局精确率 : {draw_correct/draw_pred*100:.1f}%  (模型判平 {draw_pred} 次, 中对 {draw_correct})")

print("\n=== 分届明细 ===")
print(f"{'届':<8}{'n':>4}{'模型%':>8}{'市场%':>8}{'平局召回%':>10}")
for ed, e in sorted(per_ed.items()):
    dr = (e['dc']/e['dt']*100) if e['dt'] else 0
    print(f"{ed:<8}{e['n']:>4}{e['m']/e['n']*100:>7.1f}%{e['k']/e['n']*100:>7.1f}%{dr:>9.1f}%")
