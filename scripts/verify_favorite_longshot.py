"""检验 favorite-longshot bias (庄家对冷门高估/热门低估) 在 IW 14万场是否成立.
文献一致结论: 庄家抽水在赔率轴上分布不均 -> 长赔率(冷门)被系统性高估(ROI更负),
短赔率(热门)被低估(ROI更接近0甚至正). 这是单庄内少数被证实的偏差.
若成立, 可转化为 unified_predictor 的 '价值倾斜' 信号(非覆盖, 仅当模型/偏差共振时轻倾斜).
"""
import sqlite3, numpy as np

DB = "data/football_data.db"
con = sqlite3.connect(DB)
rows = con.execute("""
    SELECT close_home_odds,close_draw_odds,close_away_odds, final_result
    FROM interwetten_odds
    WHERE close_home_odds>0 AND close_draw_odds>0 AND close_away_odds>0
      AND final_result IN ('H','D','A')
""").fetchall()
print(f"样本: {len(rows)}")

def devig(h,d,a):
    inv = 1/h+1/d+1/a
    return np.array([(1/h)/inv,(1/d)/inv,(1/a)/inv])

close = np.array([(r[0],r[1],r[2]) for r in rows], float)   # (n,3)
y = np.array([{"H":0,"D":1,"A":2}[r[3]] for r in rows])
imp = np.stack([devig(*close[i]) for i in range(len(rows))])  # (n,3) 去水隐含概率

# 每个 selection 的 ROI: 若押注该方, ROI = win_rate*(odds-1) - (1-win_rate)
# 全局抽水检查
margin = (1/close).sum(1).mean() - 1
print(f"平均庄家抽水(margin): {margin*100:.2f}%")

# ---- 分桶: 按该方赔率 ----
bins = [(1.0,1.5),(1.5,2.0),(2.0,2.5),(2.5,3.0),(3.0,4.0),(4.0,6.0),(6.0,10.0),(10.0,1e9)]
print(f"\n{'赔率桶':<14}{'sel':>4}{'n':>8}{'win%':>8}{'impl%':>8}{'ROI%':>9}{'edge_pp':>9}")
for lo,hi in bins:
    for k in range(3):
        mk = (close[:,k]>=lo)&(close[:,k]<hi)
        cnt = int(mk.sum())
        if cnt < 50: continue
        win = float((y[mk]==k).mean())           # 实际打出率
        impb = float(imp[mk,k].mean())           # 该桶平均隐含概率
        roi = win*(close[mk,k].mean()-1) - (1-win)
        edge = (win-impb)*100
        print(f"[{lo:.1f}-{hi:.1f}) {k}  {cnt:>7}{win*100:>7.1f}{impb*100:>7.1f}{roi*100:>8.1f}{edge:>+8.1f}")

# ---- 聚合: 热门(最短赔率方) vs 冷门(最长赔率方) ROI ----
fav = close.argmin(1)      # 市场热门(最短赔率)
dog = close.argmax(1)      # 冷门(最长赔率方)
draw = np.array([1])  # placeholder

def bucket_roi(sel_idx):
    # sel_idx: 每场选择哪个方(0/1/2)
    odds = close[np.arange(len(rows)), sel_idx]
    win = (y==sel_idx)
    wr = win.mean()
    roi = wr*(odds.mean()-1) - (1-wr)   # 粗略(用均值赔率近似)
    return wr, odds.mean(), roi

for name, idx in [("热门(最短赔率)", fav), ("冷门(最长赔率)", dog)]:
    w,o,r = bucket_roi(idx)
    print(f"\n[{name}] n={idx.sum()} 平均赔率={o:.2f} 打出率={w*100:.1f}% ROI={r*100:+.2f}%")

# ---- 精细 ROI: 用每场实际赔率(非均值) ----
def roi_exact(sel_idx):
    odds = close[np.arange(len(rows)), sel_idx]
    win = (y==sel_idx)
    # 每场 ROI = win*(odds-1) - (1-win); 平均
    per = np.where(win, odds-1, -1.0)
    return per.mean()
print(f"\n[精细ROI] 热门(最短赔率) = {roi_exact(fav)*100:+.2f}%")
print(f"[精细ROI] 冷门(最长赔率) = {roi_exact(dog)*100:+.2f}%")

# ---- 经典 FLB 检验: 按赔率大小排序, 看 ROI 是否随赔率单调下降 ----
print("\n--- 按 selection 赔率排序的 ROI(经典 FLB 检验) ---")
all_odds = close.ravel()
all_win  = (y[:,None]==np.arange(3)).ravel()
# 按赔率分10分位
order = np.argsort(all_odds)
quant = np.array_split(order, 10)
print(f"{'赔率分位':<10}{'赔率中值':>10}{'n':>9}{'win%':>8}{'ROI%':>9}")
for qi,part in enumerate(quant):
    o = all_odds[part]; w = all_win[part]
    roi = w*(o-1) - (1-w)
    print(f"Q{qi+1:<9}{np.median(o):>10.2f}{len(part):>9}{w.mean()*100:>7.1f}{roi.mean()*100:>8.2f}")
