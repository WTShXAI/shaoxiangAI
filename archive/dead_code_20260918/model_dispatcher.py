# ╔══════════════════════════════════════════════════════════════════════╗
# ║  ⚠ DEPRECATED — 2026-08-05 模型收敛 (M1-M7)                          ║
# ║  死链: 全项目仅 tmp/ 临时脚本引用, 线上零调用                      ║
# ║  替代: pipeline/ranked_predictor.py (编排 SSoT)                      ║
# ║  单一真相源: pipeline/model_catalog.py                                ║
# ║  本文件保留仅为历史可追溯, 禁止在新代码中引用.                        ║
# ╚══════════════════════════════════════════════════════════════════════╝
"""
模型集成路由器 — v2.0

路由规则:
  赔率差 > 6       → 规则引擎 (30万场统计)
  赔率差 2.5-6     → 规则+模型混合 (4:6权重)
  赔率差 < 2.5     → 纯LightGBM模型
  有live比分       → 叠加条件波胆
  有leisu跨庄      → 叠加multibook_consensus
"""

"""
模型调度器 v2.0 — 基于对标优化 (2026-07-29)
基准: 规则引擎 50.78% vs Pinnacle 52.00% (差1.2pp)
策略: 规则引擎主导 + LGBM辅助 + stacking兜底
"""
import numpy as np
from typing import Optional, Tuple, Dict
from pathlib import Path

def _build_features(oh, od, oa, ch=None, cd=None, ca=None):
    ch,cd,ca = (ch or oh),(cd or od),(ca or oa)
    dh=(ch-oh)/oh if oh>0 else 0;dd=(cd-od)/od if od>0 else 0;da=(ca-oa)/oa if oa>0 else 0
    io=1/oh+1/od+1/oa;po=[(1/oh)/io,(1/od)/io,(1/oa)/io]
    ic=1/ch+1/cd+1/ca;pc=[(1/ch)/ic,(1/cd)/ic,(1/ca)/ic]
    so=max(oh,od,oa)-min(oh,od,oa);sc=max(ch,cd,ca)-min(ch,cd,ca)
    of=np.argmin([oh,od,oa]);cf=np.argmin([ch,cd,ca]);sh=[pc[i]-po[i] for i in range(3)]
    return np.array([[po[0],po[1],po[2],pc[0],pc[1],pc[2],dh,dd,da,sh[0],sh[1],sh[2],so,sc,0.0,float(of==cf),abs(dh)+abs(dd)+abs(da),float(np.argmin([dh,dd,da]))]],dtype=np.float32)

def dispatch(oh: float, od: float, oa: float, ch=None, cd=None, ca=None, live_score:Optional[Tuple[int,int,int]]=None, has_leisu:bool=False, home:str="", away:str="") -> Dict:
    spread=max(oh,od,oa)-min(oh,od,oa);route=[];signals={}

    # Layer0: 规则引擎
    from pipeline.pattern_matcher import classify_verbose
    base=classify_verbose(oh,od,oa);route.append("pattern");signals["pattern"]=base

    # Layer1: 主模型路由
    from pipeline.model_registry import get
    try:model=get("outcome_3class_full");feats=_build_features(oh,od,oa,ch,cd,ca);proba=model.predict_proba(feats)[0];mh,md,ma=float(proba[0]),float(proba[1]),float(proba[2])
    except Exception:mh,md,ma=base["HDA"]

    rh,rd,ra=base["HDA"]
    # v2.0 路由: 规则引擎主导 (对标证明规则50.78% > LGBM 46.49%)
    if spread>6:
        # 深盘 → 纯粹规则 (历史100%命中)
        hda,src=(rh,rd,ra),"rules_70_deep";route.append("rule_deep")
    elif spread>3:
        # 中等盘 → 规则为主 + LGBM轻度辅助 (0.7/0.3)
        hda=(rh*0.7+mh*0.3,rd*0.7+md*0.3,ra*0.7+ma*0.3)
        src="blend(rule0.7+lgbm0.3)";route.append("rule_primary")
    elif spread>1.5:
        # 窄差 → 规则为主 + LGBM微量
        hda=(rh*0.85+mh*0.15,rd*0.85+md*0.15,ra*0.85+ma*0.15)
        src="blend(rule0.85+lgbm0.15)";route.append("rule_narrow_lite")
    else:
        # 极端窄差 → 纯规则 + 均匀平滑
        hda,src=(rh+0.02,rd+0.02,ra+0.02),"rules_70_flat";route.append("rule_flat")

    # Layer2: 逆转检测
    try:
        rm=get("operator_reversal");signals["reversal_risk"]=round(float(rm.predict_proba(feats)[0,1]),3);route.append("reversal")
    except Exception:signals["reversal_risk"]=None

    # Layer2.5: 操盘手信号(仅展示, 不介入主预测)
    from pipeline.operator_signals import operator_signal
    try:
        op=operator_signal(oh,od,oa,ch or oh,cd or od,ca or oa)
        signals["operator"]=op;route.append("operator")
    except Exception:pass

    # Layer3: 条件波胆
    if live_score:
        hg,ag,elapsed=live_score
        from scipy.stats import poisson
        ch2,cd2,ca2=(ch or oh),(cd or od),(ca or oa)
        ic=1/ch2+1/cd2+1/ca2;pc=[(1/ch2)/ic,(1/cd2)/ic,(1/ca2)/ic]
        lam=max(0.05,1.2+(pc[0]-0.33)*2.5);mu=max(0.05,1.2+(pc[2]-0.33)*2.5)
        rem=max(90-elapsed,1)/90.0;lr=lam*rem;mr=mu*rem
        maxg=6;scores=[]
        for ih in range(hg,maxg+1):
            for ia in range(ag,maxg+1):
                p=poisson.pmf(ih-hg,lr)*poisson.pmf(ia-ag,mr)
                if p>0.001:scores.append((ih,ia,round(float(p),4)))
        scores.sort(key=lambda x:-x[2])
        signals["conditional_top5"]=scores[:5];route.append("conditional")

    # 团队实力增强: 窄差盘口(<2.5)用历史战绩补充
    if spread < 2.5 and home and away:
        try:
            from pipeline.team_strength import get_strength
            ts = get_strength(home, away)
            if ts:
                signals["team_strength"] = ts
                route.append("team_strength")
                # 实力信号调权: pts_diff越大越信
                if ts["signal"] == "H" and hda[0] < 0.45:
                    hda = (hda[0]+0.08, hda[1]-0.04, hda[2]-0.04)
                elif ts["signal"] == "A" and hda[2] < 0.45:
                    hda = (hda[0]-0.04, hda[1]-0.04, hda[2]+0.08)
        except Exception:
            pass

    # 盘口背离增强: 让球方向与大小球方向矛盾 → 平局概率升
    # 源于 long/ 419张截图特征发现, 哈尔姆斯塔德 v 赫根 divergence=1 增强分 0.572
    ah_line = signals.get("pattern", {}).get("ah_line")
    ou_line = signals.get("pattern", {}).get("ou_line")
    if ah_line and ou_line and spread < 5:
        try:
            is_home_fav = float(ah_line) < 0  # 负=主让
            is_under_fav = float(ou_line) <= 2.75  # 大小球线偏低=小球预期
            if is_home_fav and is_under_fav:
                # 主场让球 + 小球预期 = 操盘手矛盾 → 平局概率+0.08
                hda = (hda[0]-0.04, hda[1]+0.08, hda[2]-0.04)
                route.append("divergence_draw_boost")
                signals["handicap_ou_divergence"] = True
        except Exception:
            pass

    # 置信度
    rev=signals.get("reversal_risk")
    # 置信度 (对标数据: 规则引擎OOF准确率≈52%, 窄差≈47%)
    rev=signals.get("reversal_risk")
    if spread>6: conf=0.85
    elif spread>3: conf=0.55
    elif spread>1.5: conf=0.40
    else: conf=0.30
    # 窄差赔率惩罚(滚动赔率倒挂 → 不可信)
    if spread<0.5: conf=min(conf,0.30)
    elif spread<1.0: conf=min(conf,0.45)

    # 修(2026-07-30 体检): HDA 归一化 — 加权/调权后分量和不恒为1, 下游价值层需概率和=1
    _s = sum(hda)
    if _s > 0:
        hda = tuple(max(0.0, x) / _s for x in hda)
    return {"HDA":hda,"source":src,"route":route,"confidence":round(conf,3),
            "signals":{k:v for k,v in signals.items() if v is not None},"blocked":False}

# 缓存已加载模型
_meta_model = None
