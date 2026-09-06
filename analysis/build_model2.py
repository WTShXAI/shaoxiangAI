import sqlite3, json
import numpy as np

# ---------- 1) Calibrate fair P(0-0) from historical_matches (open 1X2 + results) ----------
f = sqlite3.connect("D:/Architecture/data/football_data.db")
rows = f.execute("""
  SELECT open_home_odds, open_draw_odds, open_away_odds, home_score, away_score
  FROM historical_matches
  WHERE open_home_odds>1.01 AND open_draw_odds>1.01 AND open_away_odds>1.01
    AND home_score IS NOT NULL AND away_score IS NOT NULL
""").fetchall()
print(f"[calib] historical matches: {len(rows)}")

def margin_strip(h,d,a):
    s=1/h+1/d+1/a
    return 1/h/s, 1/d/s, 1/a/s

xs=[]; ys=[]
for h,d,a,hs,as_ in rows:
    _,dp,_=margin_strip(h,d,a)
    xs.append(dp); ys.append(1 if hs==0 and as_==0 else 0)
xs=np.array(xs); ys=np.array(ys)
print(f"[calib] overall 0-0 rate: {ys.mean():.4f}  (n={len(xs)})")

edges=np.linspace(xs.min(), xs.max(), 12)
bins=np.digitize(xs, edges)
calib={}
for b in range(1,len(edges)+1):
    m=bins==b
    if m.sum()>30:
        calib[float(edges[b-1])]=float(ys[m].mean())
print("[calib] P(0-0 | draw_prob):")
for k in sorted(calib): print(f"   drawP={k:.3f}: 0-0={calib[k]:.4f}")
kx=np.array(sorted(calib)); vy=np.array([calib[k] for k in sorted(calib)])
def fair_p00(dp):
    if dp<=kx[0]: return vy[0]
    if dp>=kx[-1]: return vy[-1]
    return float(np.interp(dp,kx,vy))

# ---------- 2) Backtest on GQ finished matches ----------
g=sqlite3.connect("D:/Architecture/data/events.db")
mrows=g.execute("SELECT home,away,score_home,score_away FROM matches WHERE score_home IS NOT NULL AND score_away IS NOT NULL AND status='finished'").fetchall()
def snap(mk,market,sel,earliest=True):
    o="ASC" if earliest else "DESC"
    r=g.execute(f"SELECT odds FROM odds_snapshots WHERE match_key=? AND market=? AND selection=? ORDER BY captured_at {o} LIMIT 1",(mk,market,sel)).fetchone()
    return float(r[0]) if r else None

CAP=60.0
naive_flags=naive_wins=0; naive_pnl=0.0
ev_flags=ev_wins=0; ev_pnl=0.0
THRESH=0.03
tested=0; odds_list=[]
for home,away,sh,sa in mrows:
    mk=f"{home} vs {away}"
    dO=snap(mk,'1X2','draw'); hO=snap(mk,'1X2','home'); aO=snap(mk,'1X2','away'); cO=snap(mk,'CS','0-0')
    if not(dO and hO and aO and cO): continue
    if cO>CAP: continue
    _,dp,_=margin_strip(hO,dO,aO)
    fair=fair_p00(dp); impl=1/cO
    tested+=1; is00=1 if sh==0 and sa==0 else 0
    odds_list.append(cO)
    # naive: always bet 0-0 pre-match
    naive_flags+=1
    if is00: naive_wins+=1; naive_pnl+=cO-1
    else: naive_pnl-=1
    # +EV filter
    if fair-impl>=THRESH:
        ev_flags+=1
        if is00: ev_wins+=1; ev_pnl+=cO-1
        else: ev_pnl-=1

print(f"\n[backtest] matches w/ pre-match 1X2+CS0-0 (odds<{CAP}): {tested}")
print(f"[naive]  bet 0-0 always : flags={naive_flags} wins={naive_wins} hit={naive_wins/naive_flags*100:.1f}% ROI={naive_pnl/naive_flags*100:.1f}%")
if ev_flags:
    print(f"[+EV]    fair-impl>={THRESH}: flags={ev_flags} wins={ev_wins} hit={ev_wins/ev_flags*100:.1f}% ROI={ev_pnl/ev_flags*100:.1f}%")
print(f"[info] avg CS0-0 odds used: {np.mean(odds_list):.2f}  median: {np.median(odds_list):.2f}")

json.dump({"calib_keys":kx.tolist(),"calib_vals":vy.tolist(),"overall_00":float(ys.mean()),
           "thresh":THRESH,"naive_roi":naive_pnl/naive_flags,"ev_roi":(ev_pnl/ev_flags if ev_flags else None),
           "naive_hit":naive_wins/naive_flags,"ev_hit":(ev_wins/ev_flags if ev_flags else None)},
          open("cs_mispricing_model.json","w"), ensure_ascii=False)
print("saved cs_mispricing_model.json")
