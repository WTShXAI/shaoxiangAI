"""
三线联合平局识别 (WDL + AH + OU)  -- GQ.match_outcomes 真实同场
核心假设(用户方法论): 任一市场赔率不对称/异常 = 庄家在隐藏平局。
d1 = 1X2 去水平局概率 p_d
d2 = AH 平局倾向 = AH平衡度 * 盘口紧度
d3 = OU 平局倾向 = 去水 under 概率
另: odds_type 已是"比赛类型"预分类, 用于验证类型理论。
"""
import sqlite3, numpy as np

def dew(odds):
    inv = [1.0/x for x in odds if x and x > 1.0]
    if not inv: return None
    s = sum(inv); return [x/s for x in inv]

def auc(y, score):
    y = np.asarray(y, float); score = np.asarray(score, float)
    mask = ~np.isnan(score)
    y, score = y[mask], score[mask]
    pos = score[y == 1]; neg = score[y == 0]
    if len(pos) == 0 or len(neg) == 0: return float('nan')
    ranks = np.argsort(np.argsort(score)) + 1
    rp = ranks[y == 1]
    n1, n0 = len(pos), len(neg)
    return (np.sum(rp) - n1*(n1+1)/2) / (n1*n0)

def logistic_fit(X, y, iters=3000, lr=0.1):
    X = np.asarray(X, float); y = np.asarray(y, float)
    n, k = X.shape; w = np.zeros(k); b = 0.0
    for _ in range(iters):
        p = 1.0/(1.0+np.exp(-(X.dot(w)+b)))
        e = p - y
        w -= lr*(X.T.dot(e)/n); b -= lr*e.mean()
    return w, b

def feats(row):
    h,d,a, ahL,ahH,ahA, ouL,ouO,ouU, res, otype = row
    out = [np.nan, np.nan, np.nan]
    y = 1 if res == 'draw' else 0
    # d1
    if h and d and a and h>1 and d>1 and a>1:
        p = dew([h,d,a])
        if p: out[0] = p[1]
    # d2
    if ahL is not None and ahH and ahA and ahH>1 and ahA>1:
        p = dew([ahH, ahA])
        if p:
            bal = 1 - abs(p[0]-0.5)*2.0
            out[1] = max(0.0, bal) * (1.0/(1.0+abs(ahL)))
    # d3
    if ouL and ouO and ouU and ouL>1 and ouO>1 and ouU>1:
        p = dew([ouO, ouU])
        if p: out[2] = p[1]
    return out, y

def main():
    # ===== A. football_data OPENING 1X2 全量 (312K) — 大样本开盘平局信号 =====
    c = sqlite3.connect('data/football_data.db')
    rows = c.execute('''SELECT open_home_odds,open_draw_odds,open_away_odds,final_result
                        FROM historical_matches
                        WHERE open_home_odds>1 AND open_draw_odds>1 AND open_away_odds>1
                          AND final_result IN ('H','D','A')''').fetchall()
    yA=[]; d1A=[]
    for h,d,a,res in rows:
        p = dew([h,d,a])
        if p:
            d1A.append(p[1]); yA.append(1 if res=='D' else 0)
    YA = np.array(yA); D1A=np.array(d1A)
    print(f">>> football_data 初盘1X2 全量 n={len(YA)}  平局基线={YA.mean():.3f}")
    print(f"  [全量开盘] d1 初盘 p_d   AUC={auc(YA,D1A):.4f}")
    # 分箱校准
    print("  初盘 p_d 分箱 -> 实际平局率:")
    edges=np.linspace(D1A.min(),D1A.max(),6)
    for i in range(5):
        m=(D1A>=edges[i])&(D1A<edges[i+1])
        if m.sum()>0: print(f"    p_d[{edges[i]:.3f},{edges[i+1]:.3f}) n={m.sum():5d} 平局={YA[m].mean():.3f}")

    # ===== B. GQ 三线同场 (开盘) =====
    c = sqlite3.connect('data/events.db')
    rows = c.execute('''SELECT op_1x2_h,op_1x2_d,op_1x2_a,op_ah_line,op_ah_home,op_ah_away,
                               op_ou_line,op_ou_over,op_ou_under,result,odds_type
                        FROM match_outcomes
                        WHERE result IN ('home','draw','away')''').fetchall()
    D1,D2,D3,Y,TYPE,ASYM_AH,ASYM_OU = [],[],[],[],[],[],[]
    for r in rows:
        h,d,a, ahL,ahH,ahA, ouL,ouO,ouU, res, otype = r
        f, y = feats(r)
        D1.append(f[0]); D2.append(f[1]); D3.append(f[2]); Y.append(y); TYPE.append(otype)
        # 不对称特征: 1X2强让盘 但 AH线偏浅 / OU线偏低
        asym_ah = np.nan; asym_ou = np.nan
        if h and d and a and h>1 and d>1 and a>1 and ahL is not None:
            p = dew([h,d,a]); fav_m = abs(p[0]-p[2])
            ah_signed = -ahL if p[0]>p[2] else ahL  # 让球方给球的盘口深度(正)
            # 期望盘口深度 ~ fav_margin*4; 实际浅于期望 => 异常(藏平局)
            asym_ah = fav_m - ah_signed*0.25
        if h and d and a and h>1 and d>1 and a>1 and ouL and ouL>1:
            p = dew([h,d,a]); fav_m = abs(p[0]-p[2])
            # 强让盘方通常更高总球; OU线偏低 => 异常
            asym_ou = fav_m - (ouL-2.5)*0.15
        ASYM_AH.append(asym_ah); ASYM_OU.append(asym_ou)
    D1,D2,D3,Y = map(np.array,[D1,D2,D3,Y])
    ASYM_AH,ASYM_OU = map(np.array,[ASYM_AH,ASYM_OU])
    base = Y.mean()
    print(f"\n>>> GQ 三线测试  n={len(Y)}  平局基线={base:.3f}")
    print(f"  1X2 可用 {np.isfinite(D1).sum()} | AH 可用 {np.isfinite(D2).sum()} | OU 可用 {np.isfinite(D3).sum()}")

    print("\n--- 单市场 AUC (GQ 开盘) ---")
    print(f"  d1 1X2 p_d        AUC={auc(Y,D1):.4f}")
    print(f"  d2 AH平局倾向     AUC={auc(Y,D2):.4f}")
    print(f"  d3 OU under概率   AUC={auc(Y,D3):.4f}")
    print(f"  asym 1X2-vs-AH    AUC={auc(Y,ASYM_AH):.4f}   <- 强让盘但AH浅=藏平局")
    print(f"  asym 1X2-vs-OU    AUC={auc(Y,ASYM_OU):.4f}   <- 强让盘但OU低=藏平局")

    all3 = np.isfinite(D1) & np.isfinite(D2) & np.isfinite(D3)
    n3 = all3.sum()
    if n3 > 50:
        y3 = Y[all3]; d1,d2,d3 = D1[all3],D2[all3],D3[all3]
        mx = np.maximum(np.maximum(d1,d2),d3)
        rng = np.max(np.vstack([d1,d2,d3]),0) - np.min(np.vstack([d1,d2,d3]),0)
        print(f"\n--- 三线齐备子集 n={n3} ---")
        print(f"  max(d1,d2,d3)       AUC={auc(y3,mx):.4f}")
        print(f"  range(三线分歧)     AUC={auc(y3,rng):.4f}")
        M = np.vstack([d1,d2,d3]).T
        Mu = (M - M.mean(0))/M.std(0)
        w,b = logistic_fit(Mu, y3)
        p = 1.0/(1.0+np.exp(-(Mu.dot(w)+b)))
        print(f"  logistic(d1,d2,d3)  AUC={auc(y3,p):.4f}  w={np.round(w,3)}")

    print("\n--- 平局率 by odds_type (类型理论验证) ---")
    from collections import defaultdict
    dd = defaultdict(lambda:[0,0])
    for t,y in zip(TYPE,Y):
        dd[t][0]+=1; dd[t][1]+=y
    for t,(n,dy) in sorted(dd.items(), key=lambda kv:-kv[1][1]/kv[1][0]):
        if n>=20:
            print(f"  {str(t):32s} n={n:4d} 平局率={dy/n:.3f}")

if __name__ == '__main__':
    main()
