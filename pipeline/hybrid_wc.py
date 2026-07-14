# -*- coding: utf-8 -*-
"""世界杯混合预测模块: 模型2分类(只判H/A) + 操盘手平局.

架构(用户2026-07-08裁定): 删除模型平局模块, 平局完全由操盘手判定.
回测验证(280场WC Walk-Forward):
  3类模型 53.6% < 混合 57.9%(T=0.33) < 市场argmax 58.8%
  -> 混合优于原3类模型(+4.3pp); 操盘手argmax判平(=市场)为准确率最优
平局规则: 操盘手P(平)为其三概率之最大 -> 判平(argmax版, 准确率最优);
          或 P(平)>=T 阈值版(抓平局更激进, T=0.27甜点lift1.40).
模型H/A为赔率缺失时的fallback; 有赔率时直接用操盘手H/A更准(已证).
"""
from __future__ import annotations
import sqlite3
import numpy as np
import pandas as pd
import lightgbm as lgb

DB = r"D:\Architecture\data\football_data.db"
FEATS = ["p_h","p_d","p_a","log_oh","log_od","log_oa","oh_minus_oa"]


def demargin(oh, od, oa):
    inv = 1.0/oh + 1.0/od + 1.0/oa
    return (1.0/oh)/inv, (1.0/od)/inv, (1.0/oa)/inv


def _feats(oh, od, oa):
    ph, pd_, pa = demargin(oh, od, oa)
    return dict(p_h=ph, p_d=pd_, p_a=pa, log_oh=np.log(oh),
                log_od=np.log(od), log_oa=np.log(oa), oh_minus_oa=oh-oa)


def train_ha_model():
    """训练2分类H/A模型(仅用非平局场, 删除平局模块). 返回LightGBM."""
    con = sqlite3.connect(DB)
    df = pd.read_sql_query(
        "SELECT oh,od,oa,hg,ag,edition FROM wc_xlsx_matches "
        "WHERE oh>1.01 AND od>1.01 AND oa>1.01 AND hg IS NOT NULL AND ag IS NOT NULL", con)
    con.close()
    df["y"] = np.where(df["hg"]>df["ag"],0, np.where(df["hg"]<df["ag"],2,1))
    for c in ["oh","od","oa"]:
        df[c]=df[c].astype(float)
    f = df.apply(lambda r: _feats(r["oh"],r["od"],r["oa"]), axis=1, result_type="expand")
    df = pd.concat([df, f], axis=1)
    tr = df[df["y"]!=1].copy()  # 删平局
    tr["y2"] = (tr["y"]==2).astype(int)
    m = lgb.LGBMClassifier(objective="binary", n_estimators=150, learning_rate=0.05,
        num_leaves=10, min_child_samples=10, reg_lambda=2.0, random_state=42, n_jobs=1, verbose=-1)
    m.fit(tr[FEATS].values, tr["y2"].values)
    return m


def hybrid_predict(oh, od, oa, ha_model=None, draw_mode="argmax", T=0.33,
                   home_norm=None, away_norm=None, match_date=None):
    """混合预测.

    oh,od,oa: 赔率
    ha_model: 2分类H/A模型(可None, 此时H/A用操盘手argmax)
    draw_mode: 'argmax'(P平为三概率最大才判平, 准确率最优) | 'threshold'(P平>=T判平, 抓平更激进)
    T: threshold模式阈值
    home_norm/away_norm/match_date: 跨庄家共识booster(双庄强共识直接判平, 无IW自动回退)
    返回 dict: prediction('H'/'D'/'A'), market_probs, draw_signal
    """
    try:
        from pipeline.draw_signal import consensus_draw_signal
    except ModuleNotFoundError:
        from draw_signal import consensus_draw_signal
    ph, pd_, pa = demargin(oh, od, oa)

    # 跨庄家共识booster(用户批准): 双庄独立确认高平局 -> 直接判平
    cons = None
    if home_norm and away_norm:
        cons = consensus_draw_signal(home_norm, away_norm, oh, od, oa, match_date)
        if cons.get("strong"):
            return dict(prediction="D", market_probs=dict(H=round(ph,3),D=round(pd_,3),A=round(pa,3)),
                        draw_signal=dict(consensus=round(cons["consensus"],3), strong=True,
                                         source="cross_bookmaker"))

    # 操盘手平局裁决
    if draw_mode == "argmax":
        is_draw = (pd_ >= ph and pd_ >= pa)
    else:
        is_draw = (pd_ >= T)

    if is_draw:
        pred = 1  # D
    else:
        # H/A: 用模型或操盘手argmax的H/A
        if ha_model is not None:
            f = _feats(oh, od, oa)
            x = pd.DataFrame([[f[k] for k in FEATS]], columns=FEATS)
            ha = int(ha_model.predict(x)[0])  # 0=H,1=A
            pred = 0 if ha == 0 else 2
        else:
            pred = 0 if (ph >= pa) else 2  # 操盘手H/A
    label = {0:"H",1:"D",2:"A"}[pred]
    ds = dict(consensus=(round(cons["consensus"],3) if cons else None),
              strong=(cons.get("strong") if cons else False),
              source=("market_only" if not cons or not cons.get("available") else "cross_bookmaker"))
    return dict(prediction=label, market_probs=dict(H=round(ph,3),D=round(pd_,3),A=round(pa,3)),
                draw_signal=ds, draw_mode=draw_mode, T=T)


# ── 自测 ──
if __name__ == "__main__":
    m = train_ha_model()
    # 阿根廷vs埃及 (1.32/5.0/10.0) -> 主胜
    print("阿根廷vs埃及 1.32/5.0/10.0:", hybrid_predict(1.32,5.0,10.0,m))
    # 瑞士vs哥伦比亚 (3.45/3.0/2.29) -> 客胜/平
    print("瑞士vs哥伦比亚 3.45/3.0/2.29:", hybrid_predict(3.45,3.0,2.29,m))
    print("hybrid_wc module OK")
