# -*- coding: utf-8 -*-
"""
滚球神器 v2.1 (重建) — 透明规则 + 分段校准模型
哲学: 庄家开盘赔率 = 比赛结果的编码。不考虑多庄/edge。
改进(vs v2.0): 平局识别从"仅p_d一维"升级为 (OU线桶 × p_d分箱) 二维校准
              + 专用"初盘大2.5/3.5 平局识别器"切片规则
"""
import sqlite3, json, math, random, pickle

DB = "data/rollball_training.db"
SEED = 20260821

def load():
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("SELECT * FROM rb_matches")]
    con.close(); return rows

def classify_type(r):
    oh, od, oa = r["op_h"], r["op_d"], r["op_a"]
    if not (oh and od and oa): return "unknown"
    p_h, p_d, p_a = r["p_h"], r["p_d"], r["p_a"]
    ou, ah = r["ou_line"], r["ah_line"]
    fav = min(oh, od, oa)
    if fav <= 1.50 or (ah is not None and abs(ah) >= 1.5 and fav <= 1.80):
        return "blowout"
    if p_d is not None and p_d >= 0.33:
        return "tacit"
    if ou is not None and ou >= 2.75:
        return "open"
    if ou is not None and ou <= 2.25:
        return "defensive"
    return "balanced"

def ou_bucket(ou):
    if ou is None: return "NA"
    if ou <= 2.0: return "u2.0"
    if ou < 2.5:  return "u2.25"
    if ou <= 2.75: return "u2.5"
    if ou <= 3.0: return "u2.75"
    if ou <= 3.25: return "u3.0"
    if ou <= 3.5: return "u3.25"
    return "u3.5p"

def split(rows):
    random.seed(SEED)
    fd = [r for r in rows if r["src"]=="fd"]; gq = [r for r in rows if r["src"]=="gq"]
    random.shuffle(fd); random.shuffle(gq)
    fk, gk = int(len(fd)*0.7), int(len(gq)*0.7)
    return fd[:fk], fd[fk:], gq[:gk], gq[gk:]

def build_2d(train, kb_fn, pk, lo, hi, step=0.01):
    """二维校准: key1(如ou桶) × p_d分箱 -> (n, draw_rate)"""
    tbl = {}
    for r in train:
        if r.get(pk) is None or r.get("is_draw") is None: continue
        p = r[pk]
        if p < lo or p > hi: continue
        b = round(math.floor((p-lo)/step)*step+lo, 4)
        k1 = kb_fn(r)
        d = tbl.setdefault(k1, {})
        e = d.get(b, [0,0.0]); e[0]+=1; e[1]+=(1 if r["is_draw"] else 0); d[b]=e
    out = {k1:{b:(n,s/n if n else 0.0) for b,(n,s) in d.items()} for k1,d in tbl.items()}
    return out

def pred_2d(tbl, r, kb_fn, pk, lo, hi, step=0.01, fb=0.27):
    p = r.get(pk)
    if p is None: return fb
    if p < lo: b = round(lo,4)
    elif p > hi: return fb
    else: b = round(math.floor((p-lo)/step)*step+lo, 4)
    v = tbl.get(kb_fn(r), {}).get(b)
    return v[1] if v else fb

def auc(probs, labels):
    pairs = sorted(zip(probs, labels), key=lambda x:-x[0])
    pos = sum(labels); neg = len(labels)-pos
    if pos==0 or neg==0: return float('nan')
    rank = sum(i+1 for i,(p,l) in enumerate(pairs) if l)
    return (rank - pos*(pos+1)/2)/(pos*neg)

def main():
    rows = load()
    fd_tr, fd_te, gq_tr, gq_te = split(rows)
    for r in fd_tr+fd_te+gq_tr+gq_te: r["_type"]=classify_type(r); r["_oub"]=ou_bucket(r["ou_line"])

    # ===== 平局: 二维校准 (OU桶 × p_d) =====
    dtr = [r for r in fd_tr+gq_tr if r["p_d"] is not None]
    draw_2d = build_2d(dtr, lambda r:r["_oub"], "p_d", 0.18, 0.42)
    dg = sum(r["is_draw"] for r in dtr)/len(dtr)
    # 一维(p_d only) 做对照
    draw_1d = build_2d(dtr, lambda r:"ALL", "p_d", 0.18, 0.42)

    dte = [r for r in fd_te+gq_te if r["is_draw"] is not None and r["p_d"] is not None]
    p2 = [pred_2d(draw_2d, r, lambda x:x["_oub"], "p_d", 0.18, 0.42, fb=dg) for r in dte]
    p1 = [pred_2d(draw_1d, r, lambda x:"ALL", "p_d", 0.18, 0.42, fb=dg) for r in dte]
    labs = [r["is_draw"] for r in dte]
    print("="*72); print("滚球神器 v2.1 持有-out 回测"); print("="*72)
    print(f"[平局] n={len(dte)}")
    print(f"  一维(p_d):      AUC={auc(p1,labs):.4f}")
    print(f"  二维(OU×p_d):   AUC={auc(p2,labs):.4f}  <-- 加OU线规则后的提升")
    for name, pp in [("一维",p1),("二维",p2)]:
        pred=[1 if x>=0.5 else 0 for x in pp]
        tp=sum(1 for x,l in zip(pred,labs) if x==1 and l==1)
        fp=sum(1 for x,l in zip(pred,labs) if x==1 and l==0)
        fn=sum(1 for x,l in zip(pred,labs) if x==0 and l==1)
        prec=tp/(tp+fp) if tp+fp else 0; rec=tp/(tp+fn) if tp+fn else 0
        print(f"    {name} 阈值0.5: 精确率={prec:.4f}, 召回={rec:.4f}")

    # ===== 专用: 初盘大2.5/3.5 平局识别器 (用户#3优先级) =====
    print("\n[平局识别器] 初盘大2.5/3.5切片 (全量GQ, 有OU线):")
    for line in [2.5, 2.75, 3.0, 3.25, 3.5]:
        sub=[r for r in rows if r["src"]=="gq" and r["ou_line"] and abs(r["ou_line"]-line)<0.01 and r["p_d"] is not None and r["is_draw"] is not None]
        if not sub: continue
        n=len(sub); dr=sum(r["is_draw"] for r in sub)
        best=None
        for thr in [0.26,0.28,0.30,0.32,0.34]:
            fl=[r for r in sub if r["p_d"]>=thr]
            if not fl: continue
            fdt=sum(r["is_draw"] for r in fl); prec=fdt/len(fl); rec=fdt/dr if dr else 0
            if best is None or prec>best[1]: best=(thr,len(fl),fdt,prec,rec)
        print(f"  OU={line}: n={n}, 平局率={dr/n:.3f} | 最优规则 p_d>={best[0]}: 标记{best[1]}场, 平局{best[2]}, 精确={best[3]:.3f}, 召回={best[4]:.3f}")

    # ===== 大小球: edge只在极端 =====
    print("\n[大小球] 持有-out (仅GQ有OU):")
    ste=[r for r in gq_te if r["p_over"] is not None and r["over_ou"] is not None]
    for lo,hi in [(0.40,0.50),(0.50,0.55),(0.55,0.60),(0.60,0.66)]:
        b=[r for r in ste if lo<=r["p_over"]<hi]
        if not b: continue
        hr=sum(r["over_ou"] for r in b)/len(b)
        print(f"  p_over[{lo:.2f},{hi:.2f}): n={len(b):4d}, 实际大球={hr:.3f}  {'<-- 有edge' if (lo>=0.55 and hr>0.55) or (hi<=0.50 and hr<0.45) else ''}")

    # ===== 结果1X2 argmax =====
    print("\n[结果1X2] 持有-out:")
    for name,te in [("football_data",fd_te),("GQ",gq_te)]:
        sub=[r for r in te if r["p_h"] is not None and r["result"] in ("H","D","A")]
        if not sub: continue
        ok=sum(1 for r in sub if (r["p_h"]>=r["p_d"] and r["p_h"]>=r["p_a"] and r["result"]=="H") or
               (r["p_d"]>=r["p_h"] and r["p_d"]>=r["p_a"] and r["result"]=="D") or
               (r["p_a"]>=r["p_h"] and r["p_a"]>=r["p_d"] and r["result"]=="A"))
        print(f"  {name}: n={len(sub)}, 命中={ok/len(sub):.4f}")

    # ===== 保存 =====
    model={"version":"rollball_v2.1","built":"2026-08-21",
           "draw_2d":draw_2d,"draw_global":dg,
           "note":"平局=OU桶×p_d二维校准; 结果=庄家argmax; 大小=仅极端p_over有edge"}
    with open("analysis/rollball_v2_model.pkl","wb") as f: pickle.dump(model,f)
    print("\n[SAVE] analysis/rollball_v2_model.pkl  阶段2/4完成")

if __name__=="__main__":
    main()
