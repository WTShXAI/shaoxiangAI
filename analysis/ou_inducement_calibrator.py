"""
OU 不对称诱导校准器 v2 (2026-08-18 修正版)
===========================================
基于 deliverables/ou_inducement_calibration.json (v2, 宽分段+正确half-win结算)。

v1 甜点表作废(细桶过拟合 + half-win结算错误)。v2 双向校准:
  sweet (edge>0): 该方向概率上调 +edge pp
  trap  (edge<0): 该方向概率下调 edge pp (即 calib_pp 为负)

机制:
- 庄家在特定 OU 线+价位段系统性误定价(散户爱买大→低线 over 被高估→陷阱;
  低线 under 冷门被低估→甜点)。
- 查段命中 → 该方向概率 +calib_pp(正=甜点, 负=陷阱)。

铁律:
- 经验查表, 非 ML; 只在 |edge|>=5pp 且 n>=300 的段触发。
- 乐鱼单源数据; 赛前快照验证, 滚球中态待二次验证。
- 叠加在 market 去水锚之上。
"""
import os, json

_CALIB = None
_CALIB_PATH = os.path.join(os.path.dirname(__file__), '..', 'deliverables', 'ou_inducement_calibration.json')


def _load():
    global _CALIB
    if _CALIB is None:
        try:
            with open(_CALIB_PATH, encoding='utf-8') as f:
                _CALIB = json.load(f)
        except Exception:
            _CALIB = {'sweet': [], 'trap': []}
    return _CALIB


def get_calib(line, over_odds, under_odds):
    """查校准段(甜点+陷阱)。返回 (side, calib_pp, detail) 或 None。
    side='over' → over方向概率 +calib_pp; side='under' → under方向概率 +calib_pp。
    calib_pp 可正(甜点)/可负(陷阱)。命中多个段取 |edge| 最大者。"""
    tbl = _load()
    if over_odds is None or under_odds is None:
        return None
    best = None
    for s in tbl.get('sweet', []) + tbl.get('trap', []):
        if abs(s['line'] - line) > 0.001:
            continue
        if s['side'] == 'over' and s['odds_lo'] <= over_odds < s['odds_hi']:
            if best is None or abs(s['edge']) > abs(best['edge']):
                best = s
        elif s['side'] == 'under' and s['odds_lo'] <= under_odds < s['odds_hi']:
            if best is None or abs(s['edge']) > abs(best['edge']):
                best = s
    if best is None:
        return None
    return best['side'], best['calib_pp'], best


def reload():
    global _CALIB
    _CALIB = None
    return _load()
