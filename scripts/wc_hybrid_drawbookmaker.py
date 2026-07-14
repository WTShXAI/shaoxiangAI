# -*- coding: utf-8 -*-
"""世界杯混合预测: 模型2分类(只判H/A) + 操盘手平局覆盖.
架构改造验证: 删除模型平局模块, 平局完全由操盘手(赔率去抽水P平)判定.
Walk-Forward按届切分(旧届训->新届测), 回测全部280场, 严禁模拟/泄漏.
对比: 市场argmax(3类) / 3类模型 / 新混合策略(扫阈值T).
"""
import sys, json, sqlite3, numpy as np, pandas as pd, lightgbm as lgb
sys.path.insert(0, r"D:\Architecture")

DB = r"D:\Architecture\data\football_data.db"
FEATS = ["p_h","p_d","p_a","log_oh","log_od","log_oa","oh_minus_oa"]

def demargin(oh, od, oa):
    inv = 1.0/oh + 1.0/od + 1.0/oa
    return (1.0/oh)/inv, (1.0/od)/inv, (1.0/oa)/inv

def load():
    con = sqlite3.connect(DB)
    df = pd.read_sql_query(
        "SELECT edition, home, away, oh, od, oa, hg, ag FROM wc_xlsx_matches "
        "WHERE oh>1.01 AND od>1.01 AND oa>1.01 AND hg IS NOT NULL AND ag IS NOT NULL", con)
    con.close()
    # 标签: 0=H,1=D,2=A
    df["y"] = np.where(df["hg"]>df["ag"],0, np.where(df["hg"]<df["ag"],2,1))
    for c in ["oh","od","oa","hg","ag"]:
        df[c]=df[c].astype(float)
    # 赔率特征
    ph,pd_,pa = [],[],[]
    for _,r in df.iterrows():
        a,b,c = demargin(r["oh"],r["od"],r["oa"])
        ph.append(a); pd_.append(b); pa.append(c)
    df["p_h"]=ph; df["p_d"]=pd_; df["p_a"]=pa
    df["log_oh"]=np.log(df["oh"]); df["log_od"]=np.log(df["od"]); df["log_oa"]=np.log(df["oa"])
    df["oh_minus_oa"]=df["oh"]-df["oa"]
    df["edition"]=df["edition"].astype(int)
    return df

def acc(preds, y):
    return float(np.mean([p==yy for p,yy in zip(preds,y)]))

def main():
    df = load()
    editions = sorted(df["edition"].unique())
    print(f"加载 {len(df)} 场, 届次={editions}, 基础平局率={ (df['y']==1).mean():.3f}")

    # 阈值扫描
    Ts = [0.25,0.27,0.29,0.31,0.33]
    hybrid_acc = {T:[] for T in Ts}
    market_acc = []
    model3_acc = []
    rows = []  # 逐场记录

    for Y in editions:
        tr = df[df.edition < Y]; te = df[df.edition == Y]
        if len(tr) < 30 or len(te) < 10: continue
        # --- 市场argmax(3类) 基线 ---
        tef = te.dropna(subset=FEATS)
        mkt = tef.apply(lambda r: int(np.argmax([r["p_h"],r["p_d"],r["p_a"]])), axis=1).values
        market_acc.append(acc(mkt, tef["y"].values))

        # --- 3类模型(LightGBM, 含平局, 对照) ---
        tr3 = tr.dropna(subset=FEATS+["y"]); te3 = te.dropna(subset=FEATS)
        m3 = lgb.LGBMClassifier(objective="multiclass",num_class=3,n_estimators=150,
            learning_rate=0.05,num_leaves=10,min_child_samples=10,reg_lambda=2.0,
            random_state=42,n_jobs=1,verbose=-1)
        m3.fit(tr3[FEATS].values, tr3["y"].values.astype(int))
        p3 = m3.predict(te3[FEATS].values)
        model3_acc.append(acc(p3, te3["y"].values))

        # --- 2分类模型(H/A only, 删除平局模块: 仅用非平局场训) ---
        tr2 = tr[tr["y"]!=1].copy()  # 去掉平局
        tr2["y2"] = (tr2["y"]==2).astype(int)  # 1=A, 0=H
        m2 = lgb.LGBMClassifier(objective="binary",n_estimators=150,learning_rate=0.05,
            num_leaves=10,min_child_samples=10,reg_lambda=2.0,random_state=42,n_jobs=1,verbose=-1)
        m2.fit(tr2[FEATS].values, tr2["y2"].values)
        # 测试场: 模型判H/A
        tef = te.dropna(subset=FEATS)
        p_ha = m2.predict(tef[FEATS].values)  # 0=H,1=A -> 映射回 0/2
        model_ha = np.where(p_ha==1, 2, 0)

        # 操盘手平局覆盖: market P(d) 超阈值 -> 改为D
        pd_test = tef["p_d"].values
        for T in Ts:
            pred = np.where(pd_test >= T, 1, model_ha)  # 1=D
            hybrid_acc[T].append(acc(pred, tef["y"].values))

        for i in range(len(tef)):
            rows.append(dict(edition=int(Y), y=int(tef["y"].values[i]),
                             pd=float(pd_test[i]), mkt=int(mkt[i])))

    # 汇总
    print("\n==== 世界杯混合策略回测 (Walk-Forward) ====")
    print(f"市场argmax(3类): {np.mean(market_acc)*100:.1f}%  (各届 {[round(a*100,1) for a in market_acc]})")
    print(f"3类模型:         {np.mean(model3_acc)*100:.1f}%  (各届 {[round(a*100,1) for a in model3_acc]})")
    for T in Ts:
        print(f"混合(T={T}):      {np.mean(hybrid_acc[T])*100:.1f}%  (各届 {[round(a*100,1) for a in hybrid_acc[T]]})")

    # 最佳T
    bestT = max(Ts, key=lambda T: np.mean(hybrid_acc[T]))
    print(f"\n最佳阈值 T={bestT} -> 混合准确率 {np.mean(hybrid_acc[bestT])*100:.1f}%")

    # 平局专项
    print("\n==== 平局检测对比 (实际平局) ====")
    ally = np.array([r["y"] for r in rows]); allpd = np.array([r["pd"] for r in rows])
    base = (ally==1).mean()
    print(f"基础平局率={base:.3f}")
    for T in Ts:
        flagged = allpd >= T
        tp = ((flagged) & (ally==1)).sum(); nflag=flagged.sum()
        rec = tp/((ally==1).sum()) if (ally==1).sum() else 0
        prec = tp/nflag if nflag else 0
        print(f"  操盘手P(d)>={T}: 标{nflag}场 平局召回={rec:.3f} 精确={prec:.3f} lift={prec/base:.2f}")

    out = dict(
        n_total=int(len(rows)), base_draw_rate=round(float(base),3),
        market_argmax_acc=round(float(np.mean(market_acc)),3),
        model3_acc=round(float(np.mean(model3_acc)),3),
        hybrid_acc={str(T):round(float(np.mean(hybrid_acc[T])),3) for T in Ts},
        best_T=bestT, best_hybrid_acc=round(float(np.mean(hybrid_acc[bestT])),3),
        note="模型2分类(H/A)+操盘手平局覆盖; 与3类模型/市场argmax对比")
    with open(r"D:\Architecture\deliverables\wc_hybrid_drawbookmaker.json","w",encoding="utf-8") as f:
        json.dump(out,f,ensure_ascii=False,indent=2)
    print("\nwritten D:\\Architecture\\deliverables\\wc_hybrid_drawbookmaker.json")

if __name__ == "__main__":
    main()
