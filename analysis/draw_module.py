"""
滚球神器 v2 — 平局模块 (draw_module)
方法论(用户授权): 庄家初盘赔率编码结果; 平局用 初盘1X2去水p_d(校准) + 比赛类型识别。
不使用: 跨市场残差/多庄edge(已证伪 AUC<=0.53)。
输入: 初盘 1X2(h,d,a) + 可选 让球线ahL/盘口 + 大小球线ouL
输出: p_draw(校准) + match_type + verdict
校准来自 football_data 初盘1X2 全量 312K 实证分箱。
"""
import sqlite3, numpy as np

# —— 初盘1X2 p_d -> 实际平局率 校准 (football_data 312K 实证) ——
CALIB_EDGES = [0.0, 0.172, 0.315, 0.458, 1.0]
CALIB_RATE = [0.116, 0.262, 0.336, 0.467]  # 各箱实际平局率

# —— 类型 -> 平局率加成 (GQ odds_type 实证, 相对基线0.265) ——
TYPE_DRAW_RATE = {
    'low_draw': 0.402,
    'balanced': 0.320,
    'slight_home': 0.292, 'slight_away': 0.314,
    'home_fav': 0.277, 'away_fav': 0.273,
    'strong_home': 0.200, 'strong_away': 0.211,
}
BASELINE = 0.265

def dew(odds):
    inv = [1.0/x for x in odds if x and x > 1.0]
    if not inv: return None
    s = sum(inv); return [x/s for x in inv]

def classify_type(h, d, a, ahL=None, ouL=None):
    p = dew([h, d, a])
    if p is None: return 'unknown', None
    ph, pd, pa = p
    fav = 'home' if ph > pa else 'away'
    fav_m = abs(ph - pa)
    if pd > 0.30:
        t = 'low_draw'
    elif fav_m > 0.18:
        t = 'strong_' + fav
    elif fav_m > 0.08:
        t = 'slight_' + fav
    else:
        t = 'balanced'
    if ouL and ouL < 2.25:
        t = t + '|low_ou'
    return t, p

def calibrate_pd(pd):
    for i in range(len(CALIB_EDGES)-1):
        if CALIB_EDGES[i] <= pd < CALIB_EDGES[i+1]:
            return CALIB_RATE[i]
    return CALIB_RATE[-1]

def predict_draw(h, d, a, ahL=None, ouL=None, ahH=None, ahA=None, ouO=None, ouU=None):
    """返回 {p_draw, match_type, verdict, p_h, p_d, p_a}"""
    t, p = classify_type(h, d, a, ahL, ouL)
    if p is None:
        return {'p_draw': BASELINE, 'match_type': 'unknown', 'verdict': '数据不足',
                'p_h': None, 'p_d': None, 'p_a': None}
    ph, pd, pa = p
    # 基础校准
    p_draw = calibrate_pd(pd)
    # 类型加成: 用类型历史平局率与校准值的加权平均(类型样本较小时偏向校准)
    if t in TYPE_DRAW_RATE:
        type_rate = TYPE_DRAW_RATE[t]
        # 简单混合: 类型强信号时靠近类型历史
        p_draw = 0.6 * p_draw + 0.4 * type_rate
    # verdict
    if p_draw >= 0.34:
        verdict = '高平局倾向'
    elif p_draw >= 0.27:
        verdict = '偏平局'
    else:
        verdict = '低平局'
    return {'p_draw': round(p_draw, 3), 'match_type': t,
            'verdict': verdict, 'p_h': round(ph,3), 'p_d': round(pd,3), 'p_a': round(pa,3)}

def validate_on_gq():
    c = sqlite3.connect('data/events.db')
    rows = c.execute('''SELECT op_1x2_h,op_1x2_d,op_1x2_a,op_ah_line,op_ou_line,result
                        FROM match_outcomes
                        WHERE result IN ('home','draw','away')
                          AND op_1x2_h>1 AND op_1x2_d>1 AND op_1x2_a>1''').fetchall()
    ys, ps = [], []
    for h, d, a, ahL, ouL, res in rows:
        r = predict_draw(h, d, a, ahL, ouL)
        ys.append(1 if res == 'draw' else 0); ps.append(r['p_draw'])
    y = np.array(ys); s = np.array(ps)
    # AUC
    ranks = np.argsort(np.argsort(s)) + 1
    rp = ranks[y == 1]; n1 = (y==1).sum(); n0 = (y==0).sum()
    auc = (rp.sum() - n1*(n1+1)/2)/(n1*n0)
    print(f"[validate GQ] n={len(y)} 基线={y.mean():.3f}  draw_module AUC={auc:.4f}")
    # 分箱
    print("  p_draw分箱 -> 实际平局率:")
    for lo,hi in [(0,0.25),(0.25,0.30),(0.30,0.35),(0.35,1)]:
        m=(s>=lo)&(s<hi)
        if m.sum()>0: print(f"    [{lo},{hi}) n={m.sum():4d} 平局={y[m].mean():.3f}")

if __name__ == '__main__':
    print("=== 滚球神器v2 平局模块 自检 ===")
    # 示例
    print(predict_draw(2.10, 3.20, 3.40))   # 均衡
    print(predict_draw(1.50, 4.00, 6.50))   # 主强
    print(predict_draw(2.80, 2.90, 2.60))   # 平局味浓
    validate_on_gq()
