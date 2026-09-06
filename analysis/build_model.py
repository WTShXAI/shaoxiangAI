import sqlite3, json, re
import numpy as np

# ---------- 1) Calibrate fair P(0-0) from football_data.db historical 1X2 + results ----------
f = sqlite3.connect("D:/Architecture/data/football_data.db")
# upset_matches has home_score, away_score, home_odds, draw_odds, away_odds
rows = f.execute("""
  SELECT home_odds, draw_odds, away_odds, home_score, away_score
  FROM upset_matches
  WHERE home_odds>1.01 AND draw_odds>1.01 AND away_odds>1.01
    AND home_score IS NOT NULL AND away_score IS NOT NULL
""").fetchall()
print(f"[calib] historical matches w/ odds+result: {len(rows)}")

def fair_probs(h,d,a):
    s = 1/h+1/d+1/a
    return 1/h/s, 1/d/s, 1/a/s   # margin-stripped

xs=[]; ys=[]
for h,d,a,hs,as_ in rows:
    hp,dp,ap = fair_probs(h,d,a)
    xs.append(dp)                 # draw prob ~ low-scoring signal
    ys.append(1 if (hs==0 and as_==0) else 0)

xs=np.array(xs); ys=np.array(ys)
overall = ys.mean()
print(f"[calib] overall 0-0 rate: {overall:.4f}")
# bin by draw prob
edges=np.linspace(xs.min(), xs.max(), 12)
bins=np.digitize(xs, edges)
calib={}
for b in range(1, len(edges)+1):
    m=bins==b
    if m.sum()>20:
        calib[round(float(edges[b-1]),3)]=float(ys[m].mean())
print("[calib] P(0-0 | draw_prob bucket):")
for k,v in calib.items(): print(f"   drawP={k}: 0-0rate={v:.4f}  n={int((bins==list(calib.keys()).index(k)+1).sum())}")

# smooth calibration into a function
kx=np.array(sorted(calib.keys())); vy=np.array([calib[k] for k in sorted(calib.keys())])
def fair_p00(drawp):
    if drawp<=kx[0]: return vy[0]
    if drawp>=kx[-1]: return vy[-1]
    return float(np.interp(drawp, kx, vy))

# ---------- 2) Backtest +EV CS(0-0) on events.db finished matches ----------
g = sqlite3.connect("D:/Architecture/data/events.db")
# matches with outcomes
mrows = g.execute("""
  SELECT mid, home, away, score_home, score_away FROM matches
  WHERE score_home IS NOT NULL AND score_away IS NOT NULL AND status='finished'
""").fetchall()
print(f"\n[backtest] finished matches: {len(mrows)}")

def get_snap(cur, match_key, market, selection, earliest=True):
    order = "ASC" if earliest else "DESC"
    r = cur.execute(f"""
      SELECT odds FROM odds_snapshots
      WHERE match_key=? AND market=? AND selection=?
      ORDER BY captured_at {order} LIMIT 1
    """, (match_key, market, selection)).fetchone()
    return float(r[0]) if r else None

THRESH = 0.04   # +EV trigger: fair - implied >= 4%
unit = 1.0
flags=0; wins=0; pnl=0.0
naive_pnl=0.0; naive_flags=0; naive_wins=0
tested=0
examples=[]
for mid,home,away,sh,sa in mrows:
    mk = f"{home} vs {away}"
    draw_o = get_snap(g, mk, '1X2', 'draw', True)
    cs00_o = get_snap(g, mk, 'CS', '0-0', True)
    if not draw_o or not cs00_o: 
        continue
    hp,dp,ap = fair_probs(*[1.0,1.0,1.0])  # placeholder
    # recompute draw prob from this match's 1X2
    h_o=get_snap(g,mk,'1X2','home',True); a_o=get_snap(g,mk,'1X2','away',True)
    if not h_o or not a_o: continue
    _,dp,_ = fair_probs(h_o, draw_o, a_o)
    fair = fair_p00(dp)
    implied = 1.0/cs00_o
    tested+=1
    is00 = 1 if (sh==0 and sa==0) else 0
    # naive: always bet 0-0 at its odds
    naive_flags+=1
    if is00: naive_wins+=1; naive_pnl += (cs00_o-1)
    else: naive_pnl -= 1
    # +EV filtered
    if fair - implied >= THRESH:
        flags+=1
        if is00: wins+=1; pnl += (cs00_o-1)
        else: pnl -= 1
        if len(examples)<8:
            examples.append(dict(mk=mk, drawO=round(draw_o,2), cs00O=round(cs00_o,2),
                                 fair=round(fair,4), implied=round(implied,4),
                                 is00=is00))

print(f"[backtest] matches w/ 1X2+CS pre-match odds: {tested}")
print(f"[naive]   bet 0-0 always: flags={naive_flags} wins={naive_wins} ROI={naive_pnl/naive_flags*100:.1f}%")
print(f"[+EV]     fair-implied>={THRESH}: flags={flags} wins={wins} ROI={pnl/flags*100:.1f}%  hit%={wins/flags*100:.1f}" if flags else "[+EV] no flags")
print("\n[examples of +EV-flagged 0-0 bets]:")
for e in examples:
    print(f"   {e['mk']}: drawO={e['drawO']} cs00O={e['cs00O']} fairP={e['fair']} implP={e['implied']} actual0-0={e['is00']}")

# persist model artifacts
json.dump({"calib_keys":kx.tolist(),"calib_vals":vy.tolist(),"overall_00":overall,
           "thresh":THRESH}, open("cs_mispricing_model.json","w"))
print("\nsaved cs_mispricing_model.json")
