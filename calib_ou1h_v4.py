import sqlite3, math
from collections import defaultdict
gq = sqlite3.connect(r"D:\Architecture\data\GQ.db", timeout=60); gq.execute("PRAGMA busy_timeout=60000")
cur=gq.cursor()
def q(s,*a): cur.execute(s,a); return cur.fetchall()

# ---- clean ground truth: ht<ft only ----
gt = q("""SELECT match_key, ht_score_home+ht_score_away, score_home+score_away, league
          FROM matches WHERE ht_score_home IS NOT NULL AND ht_score_away IS NOT NULL
                         AND score_home IS NOT NULL AND score_away IS NOT NULL""")
clean={}
for mk,ht,ft,lg in gt:
    if ht<ft: clean[mk]=(ht, lg)
print(f"clean GT (ht<ft) matches: {len(clean)}")

# ---- settlement payoff fraction for Over line L given total goals X ----
def R(x, L):
    k=math.floor(L); frac=L-k
    if frac==0.5 or frac==0.0:
        return 1.0 if x > L else 0.0
    if frac==0.25:
        return (1.0 if x>=k+1 else 0.0) + 0.25*(1.0 if x==k else 0.0)
    if frac==0.75:
        return 0.5*(1.0 if x>=k+1 else 0.0) + 0.5*(1.0 if x>=k+2 else 0.0) + 0.25*(1.0 if x==k+1 else 0.0)
    return 1.0 if x > L else 0.0

# ---- all OU_1H pre-match observations ----
rows = q("""SELECT match_key, line, selection, odds FROM odds_snapshots
            WHERE market LIKE 'OU_1H%' AND minute_at=0""")
by=defaultdict(dict)  # (mk) -> line -> {over,under}
for mk,line,sel,odds in rows:
    by[mk].setdefault(round(line,2),{})[sel]=odds

obs=[]   # (implied_p, realized, line, league, over_odds)
for mk,d in by.items():
    if mk not in clean: continue
    ht,lg=clean[mk]
    for line,o in d.items():
        if 'over' not in o or 'under' not in o: continue
        po,pu=1/o['over'],1/o['under']; s=po+pu
        p=po/s
        obs.append((p, R(ht,line), line, lg, o['over']))

n=len(obs)
if n==0:
    print("no obs"); gq.close(); raise SystemExit
imp=sum(p for p,_,_,_,_ in obs)/n
real=sum(r for _,r,_,_,_ in obs)/n
# ROI: flat 1-unit stake on OVER at the quoted over decimal odds
# net per obs = R*(over_odds-1) - (1-R)*1   (R = payoff fraction, R=1 win, R=0 loss, R=0.5 half-push)
pnl=sum(r*(oo-1) - (1-r) for _,r,_,_,oo in obs)
print(f"\n=== OU_1H direct P(over) backtest (GQ-internal, clean GT ht<ft) ===")
print(f"observations (match,line,snapshot): {n}")
print(f"mean IMPLIED P(over) : {imp:.4f}")
print(f"mean REALIZED over   : {real:.4f}")
print(f"GAP (real-implied)   : {real-imp:+.4f}  ({'+'+format((real/imp-1)*100,'.1f')+'%' if imp else ''})")
print(f"FLAT-BET ROI (1u on OVER @ market odds): {pnl/n*100:+.2f}%  (total PnL {pnl:+.1f}u over {n} bets)")

# by line
bl=defaultdict(lambda:[0,0,0])
for p,r,L,lg,_o in obs:
    bl[L][0]+=p; bl[L][1]+=r; bl[L][2]+=1
print("\nby line:")
for L in sorted(bl):
    a,b,c=bl[L]; print(f"  OU_1H {L:>5}: n={c:4d}  implied={a/c:.3f}  realized={b/c:.3f}  gap={b/c-a/c:+.3f}")

# by league (top 8 by n)
bL=defaultdict(lambda:[0,0,0])
for p,r,L,lg,_o in obs:
    key=lg if lg else '(unknown)'
    bL[key][0]+=p; bL[key][1]+=r; bL[key][2]+=1
print("\nby league (top 10 by n):")
for lg in sorted(bL, key=lambda k:-bL[k][2])[:10]:
    a,b,c=bL[lg]; print(f"  {lg[:22]:22}: n={c:4d}  imp={a/c:.3f}  real={b/c:.3f}  gap={b/c-a/c:+.3f}")

# ---- per-match multi-line lambda fit (>=2 lines) ----
def poisson_pmf(x,lam):
    if x<0: return 0.0
    return math.exp(-lam)*lam**x/math.factorial(x)
def p_over_exp(lam,L):
    # E[R(X,L)] under Poisson(lam)
    s=0.0
    for x in range(0,30):
        s+=poisson_pmf(x,lam)*R(x,L)
    return s
def fit_lam(pairs):
    best=None;be=1e9
    for i in range(1,500):
        lam=i/100.0; e=sum((p_over_exp(lam,L)-p)**2 for L,p in pairs)
        if e<be: be=e;best=lam
    return best,be
fits=[]
for mk,d in by.items():
    if mk not in clean: continue
    ht,lg=clean[mk]
    pairs=[(L, (1/o['over'])/(1/o['over']+1/o['under'])) for L,o in d.items() if 'over' in o and 'under' in o]
    if len(pairs)>=2:
        lam,err=fit_lam(pairs)
        if lam: fits.append((lam,ht))
if fits:
    nf=len(fits)
    il=sum(l for l,_ in fits)/nf; ah=sum(h for _,h in fits)/nf
    print(f"\n=== per-match λ fit (>=2 lines) : {nf} matches ===")
    print(f"mean IMPLIED λ_1H : {il:.3f}")
    print(f"mean ACTUAL HT    : {ah:.3f}")
    print(f"ratio             : {il/ah:.3f}")
gq.close()
