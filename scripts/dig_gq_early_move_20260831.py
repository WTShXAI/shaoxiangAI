# -*- coding: utf-8 -*-
"""
dig_gq_early_move_20260831.py — GQ 真实 tick 流, 无前视的早期移动 +EV 测试
用户 2026-08-31: "盘口 0.01-1.5 轮流变化, 深挖微观结构"

设计(严格无前视):
  对每场, 取 1X2 盘口的前 W 个 tick 变动 (captured_at 升序):
    - 方向 = 前 W tick 中净变动最负(被压低最多)的方 = 早期聪明钱方向
    - 下注价 = 该方在前 W tick 结束时的当前赔率 (可观测, 无前视)
    - 结算 = match_outcomes 真实 result (H/D/A)
  仅当早期有实质移动(max|net|>0.03)才下注, 否则无信号跳过。
对比:
    A) 早期(W=10)无前视下注
    B) 早期(W=20)无前视下注
    C) 全量移动(前视上界): 方向=全 tick 最压低方, 价=开盘价 -> 应复现 rb_matches PART C 的 +EV
诚实守卫: n>=300 且 ROI bootstrap CI[2.5%]>0 才认 +EV; 多窗口测试注意过拟合。
输出: scripts/dig_gq_early_move_out.json
"""
from __future__ import annotations
import sqlite3, json, numpy as np

DB = "data/GQ.db"
OUT = "scripts/dig_gq_early_move_out.json"
N_BOOT = 2000
SEL = {"home":0,"draw":1,"away":2}

con = sqlite3.connect(DB); cur = con.cursor()
# 结果: 主源 match_outcomes (可信, result 小写 home/draw/away), 兜底 matches(match_key 精确)
cur.execute("SELECT home, away, result FROM match_outcomes "
            "WHERE result IN ('home','draw','away') AND is_virtual=0 AND is_valid=1")
res_mo = {f"{h} vs {a}": SEL[r] for h,a,r in cur.fetchall()}
cur.execute("SELECT match_key, score_home, score_away FROM matches "
            "WHERE status='finished' AND score_home IS NOT NULL AND score_away IS NOT NULL")
res_mt = {}
for mk, sh, sa in cur.fetchall():
    if sh is None or sa is None: continue
    res_mt[mk] = 0 if sh > sa else (1 if sh == sa else 2)
res_map = dict(res_mt); res_map.update(res_mo)   # match_outcomes 优先
print(f"[results] match_outcomes={len(res_mo)}  matches兜底={len(res_mt)}  合并可用终果={len(res_map)} 场")

# 1X2 tick 流 (含 minute_at 区分临场/滚球)
cur.execute("SELECT match_key, selection, change, to_odds, from_odds, captured_at, minute_at "
            "FROM odds_changes WHERE market='1X2' ORDER BY match_key, captured_at")
rows = cur.fetchall()
con.close()
print(f"[ticks] 1X2 变动行: {len(rows)}")

# 分组
groups = {}
for mk, sel, chg, to_o, from_o, ts, mt in rows:
    groups.setdefault(mk, []).append((sel, chg, to_o, from_o, ts, mt))
print(f"[groups] 有 1X2 tick 的比赛: {len(groups)}")

def run(W, use_full=False, bet_open=False, prematch_only=False):
    wins=[]; prices=[]; n_used=0; n_skip=0
    for mk, ch in groups.items():
        if mk not in res_map: continue
        if len(ch) < max(W, 3): n_skip+=1; continue
        pool = [x for x in ch if (x[5]==0)] if prematch_only else ch
        if len(pool) < max(W, 3): n_skip+=1; continue
        early = pool if use_full else pool[:W]
        net = {"home":0.0,"draw":0.0,"away":0.0}
        price_after = {}
        open_price = {}
        for sel, c, to_o, from_o, ts, mt in early:
            net[sel] += c
            price_after[sel] = to_o
            if sel not in open_price: open_price[sel] = from_o
        # 未在早期出现的方: 价=其整体首 tick 的 to_odds(=开盘, 未变)
        for s in ("home","draw","away"):
            if s not in price_after:
                fc = next((x for x in ch if x[0]==s), None)
                price_after[s] = fc[2] if fc else None
                if s not in open_price and fc: open_price[s] = fc[3]
        if max(abs(v) for v in net.values()) < 0.03:  # 无实质早期移动
            n_skip+=1; continue
        direction = min(net, key=net.get)  # 最压低
        price = open_price.get(direction) if bet_open else price_after.get(direction)
        if price is None or price <= 1.01: n_skip+=1; continue
        win = (res_map[mk] == SEL[direction])
        wins.append(1.0 if win else 0.0); prices.append(price); n_used+=1
    wins=np.array(wins); prices=np.array(prices)
    if n_used==0: return {"n_used":0,"n_skip":n_skip}
    wr=float(wins.mean())
    roi=float(np.where(wins, prices-1.0, -1.0).mean())
    # 隐含(开盘去水近似): 用 1/price 作单边隐含
    implied = float((1.0/prices).mean())
    rng=np.random.default_rng(7)
    b=rng.integers(0,n_used,size=(N_BOOT,n_used))
    rois=np.where(wins[b], prices[b]-1.0, -1.0).mean(axis=1)
    lo,hi=float(np.percentile(rois,2.5)),float(np.percentile(rois,97.5))
    return {"n_used":n_used,"n_skip":n_skip,"win_rate":round(100*wr,2),
            "implied_single":round(100*implied,2),
            "roi":round(100*roi,2),"roi_CI":[round(100*lo,2),round(100*hi,2)],
            "pos_ev": bool(roi>0 and lo>0)}

print("\n=== 无前视早期移动测试 ===")
res_A = run(10);  print(f"[A] 早期 W=10 无前视(含滚球): {res_A}")
res_B = run(20);  print(f"[B] 早期 W=20 无前视(含滚球): {res_B}")
res_D = run(10, prematch_only=True); print(f"[D] 临场前 W=10 无前视(仅minute_at=0): {res_D}")
res_C = run(0, use_full=True, bet_open=True); print(f"[C] 全量移动+开盘价(前视上界): {res_C}")

# 对照: 跟早期反向(押被拉高方) 应亏损
def run_reverse(W):
    wins=[];prices=[]
    for mk, ch in groups.items():
        if mk not in res_map or len(ch)<W: continue
        early=ch[:W]
        net={"home":0.0,"draw":0.0,"away":0.0}; pa={}
        for sel,c,to_o,from_o,ts,mt in early:
            net[sel]+=c; pa[sel]=to_o
        if max(abs(v) for v in net.values())<0.03: continue
        direction=max(net,key=net.get)  # 最拉高
        price=pa.get(direction)
        if price is None or price<=1.01: continue
        wins.append(1.0 if res_map[mk]==SEL[direction] else 0.0); prices.append(price)
    if not wins: return {"n_used":0}
    w=np.array(wins);p=np.array(prices)
    roi=float(np.where(w,p-1.0,-1.0).mean())
    return {"n_used":len(w),"win_rate":round(100*w.mean(),2),"roi":round(100*roi,2)}
res_R=run_reverse(10); print(f"[R] 早期反向(押被拉高方)对照: {res_R}")

pos=[k for k,v in [("A",res_A),("B",res_B),("D",res_D),("C",res_C)] if isinstance(v,dict) and v.get("pos_ev")]
out={"meta":{"db":DB,"n_results":len(res_map),"n_tick_matches":len(groups)},
     "A_early_W10":res_A,"B_early_W20":res_B,"D_prematch_W10":res_D,
     "C_full_open_lookahead":res_C, "R_reverse":res_R,"pos_ev":pos}
with open(OUT,"w",encoding="utf-8") as f: json.dump(out,f,ensure_ascii=False,indent=2)
print(f"\n[done] 写出 {OUT}")
print("\n[诚实判定] 若 A/B 显著+EV(无前视) => 早期tick真含可交易信息(用户假设成立);")
print("           若仅 C +EV => 价值只在'已知收盘'的前视窗口, 不可交易; R 应亏损(反向无效).")
