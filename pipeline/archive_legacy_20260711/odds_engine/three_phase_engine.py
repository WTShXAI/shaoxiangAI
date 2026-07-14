"""
================================================================================
三段操盘框架引擎 — 初盘 / 临场1h / 滚盘
================================================================================

把"机构操盘手三段工作法"翻译成可量化特征，接入 SPOddsCore(reverse_odds_engine)：

  阶段1 初盘(Opening)   : 立锚。看实力模型 + 造盘意图偏离度
  阶段2 临场1h(Close)   : 平衡筹码。看 drift / 跨机构同步 / RLM / CLV
  阶段3 滚盘(Live)      : 实时条件概率。看低水聪明边 / xG缺口 / 时间贬值

关键铁律 (涛哥 2026-07-10 确立, 已被两次证伪后修正):
  A. 单张 live 截图无开盘价 → "低水=庄家护的聪明边" 是优先铁律，
     绝不可反向把低水线说成"陷阱诱多"
  B. obscure 联赛(非五大/WC)领先后收缩防守假设不可靠 —
     Kabuscorp(0-1→2-1) 与 特尔纳瓦(76'1-2→2-2) 两次证伪 →
     单张深盘 live 不可据此判"不进球/不追平"
  C. 真陷阱判定必须有 开盘→收盘漂移(drift)证据；
     无漂移数据时，低水线只标"庄家倾向"，不升级为"陷阱"

输出:
  - enriched verdict (覆盖 ArtOfWar 的误判)
  - features_for_v6 : 扁平数值特征向量，直接喂 v6.0 模型

作者: 赵统筹 (接涛哥指令)
依赖: sp_core (AnomalyIndex / ArtOfWar / OTSM / crack), numpy
================================================================================
"""

import sys
import os
import json
import glob
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---- 接入 reverse_odds_engine ----
SP_DIR = Path(r'D:/Architecture/odds_engine')
if str(SP_DIR) not in sys.path:
    sys.path.insert(0, str(SP_DIR))

from sp_core import AnomalyIndex, ArtOfWar, OTSM, crack  # noqa: E402
import numpy as np  # noqa: E402

ODDS_DB = Path(r'D:/Architecture/odds_db')

# 主流/高可信联赛白名单 — 不在其中即视为 obscure
MAJOR_LEAGUES = (
    '世界杯', 'WC', 'world cup', '欧洲杯', '欧冠', '欧联',
    'premier league', '英超', 'la liga', '西甲', 'bundesliga', '德甲',
    'serie a', '意甲', 'ligue 1', '法甲', '五大联赛',
)


# ============================================================================
# 工具函数
# ============================================================================
def _overround(o: Dict[str, float]) -> float:
    """庄家抽水率 (overround / margin), 收 {home,draw,away}"""
    return 1.0 / o['home'] + 1.0 / o['draw'] + 1.0 / o['away'] - 1.0


def _implied(o: Dict[str, float]) -> np.ndarray:
    """赔率 → 归一化隐含概率 [H, D, A], 收 {home,draw,away}"""
    raw = np.array([1.0 / o['home'], 1.0 / o['draw'], 1.0 / o['away']])
    return raw / raw.sum()


def _fair_implied(o: Dict[str, float]) -> np.ndarray:
    """剔除抽水后的"公平"隐含概率 [H, D, A] (margin-stripped).

    比原始 implied 更接近真实概率 —— 用作偏离度/CLV 的基准线,
    避免两家机构 margin 厚度不同造成的伪漂移.
    """
    raw = np.array([1.0 / o['home'], 1.0 / o['draw'], 1.0 / o['away']])
    over = raw.sum()  # = 1 + overround
    return raw / over


def _is_obscure(league: str) -> bool:
    if not league:
        return True
    low = league.lower()
    return not any(k.lower() in low for k in MAJOR_LEAGUES)


def _snap_1x2(s: Dict) -> Optional[Dict[str, float]]:
    """从任意快照里抠出 1X2 (兼容 odds_1x2 / live_odds_1x2)"""
    for key in ('odds_1x2', 'live_odds_1x2', 'odds_1x2_live'):
        if isinstance(s.get(key), dict) and s[key].get('home'):
            o = s[key]
            return {'home': float(o['home']), 'draw': float(o['draw']), 'away': float(o['away'])}
    return None


# ============================================================================
# 阶段分割 — 从 odds_db 记录识别 初盘/临场/滚盘
# ============================================================================
def segment_phases(record: Dict) -> Dict:
    """
    返回:
      {
        'open' : {home,draw,away} | None,
        'close': {home,draw,away} | None,
        'live' : [ {odds:{home,draw,away}, minute, xg, score, is_primary}, ... ] | None,
        'meta' : {...}
      }
    """
    phases = {'open': None, 'close': None, 'live': None, 'meta': {}}

    # ---- 路径A: timeline 多快照 ----
    snaps = record.get('snapshots')
    if isinstance(snaps, list) and snaps:
        pre = [s for s in snaps
               if s.get('match_status') != 'live' and not s.get('match_minute')
               and _snap_1x2(s)]
        live = [s for s in snaps
                if (s.get('match_status') == 'live' or s.get('match_minute')) and _snap_1x2(s)]
        if pre:
            phases['open'] = _snap_1x2(pre[0])
            phases['close'] = _snap_1x2(pre[-1]) if len(pre) > 1 else phases['open']
        if live:
            phases['live'] = []
            for s in live:
                ls = {'odds': _snap_1x2(s), 'minute': s.get('match_minute') or s.get('note', '')}
                st = s.get('live_stats') or {}
                if isinstance(st, dict) and st.get('xg'):
                    ls['xg'] = st['xg']
                if s.get('score'):
                    ls['score'] = s['score']
                phases['live'].append(ls)
        phases['meta']['source'] = 'timeline'
        return phases

    # ---- 路径B: 单场 match JSON (常见: 一张 OCR 截图) ----
    o1x2 = record.get('odds_1x2')
    src = (record.get('source') or '').lower()
    is_live = ('live' in src) or bool(record.get('live_status')) \
        or any(k.startswith('live_snapshot') for k in record)

    # 内嵌更深滚盘快照 (live_snapshot_62min 等)
    live_snaps = []
    for k, v in record.items():
        if k.startswith('live_snapshot') and isinstance(v, dict) and v.get('odds_1x2_live'):
            live_snaps.append({
                'odds': {'home': float(v['odds_1x2_live']['home']),
                         'draw': float(v['odds_1x2_live']['draw']),
                         'away': float(v['odds_1x2_live']['away'])},
                'minute': k,
            })

    if live_snaps:
        phases['live'] = live_snaps

    if o1x2 and isinstance(o1x2, dict) and o1x2.get('home'):
        if is_live:
            # odds_1x2 本身就是一张 live 快照
            if phases.get('live') is None:
                phases['live'] = []
            phases['live'].insert(0, {
                'odds': {'home': float(o1x2['home']), 'draw': float(o1x2['draw']),
                         'away': float(o1x2['away'])},
                'minute': 'captured', 'is_primary': True,
            })
        else:
            phases['close'] = {'home': float(o1x2['home']), 'draw': float(o1x2['draw']),
                               'away': float(o1x2['away'])}

    # 终场比分 (用于 xg_gap / comeback 判定)
    ls_status = record.get('live_status') or {}
    if ls_status.get('score_home') is not None and ls_status.get('score_away') is not None:
        phases['meta']['final_score'] = (int(ls_status['score_home']), int(ls_status['score_away']))
    phases['meta']['source'] = 'match_json'
    phases['meta']['league'] = record.get('competition') or record.get('league') or ''
    return phases


# ============================================================================
# 阶段1 — 初盘特征
# ============================================================================
def phase1_open(open_o: Optional[Dict], close_o: Optional[Dict]) -> Dict:
    if not open_o:
        return {'available': False, 'reason': '无初盘 — 降级, 仅能结构性读盘'}

    open_margin = _overround(open_o)
    out = {
        'available': True,
        'open_margin_pct': round(open_margin * 100, 2),
        'open_odds': open_o,
        'margin_anomaly': open_margin < 0,
    }
    if open_margin < 0:
        out['margin_note'] = '负margin(套利/数据异常) — 单家sharp或OCR错误'
    # 初盘偏离度: 用剔除抽水后的公平概率做基准(比原始implied更干净)
    # open 公平线 vs close 公平线 之差 = 庄家"造盘意图"的净信号
    if close_o:
        open_fair = _fair_implied(open_o)
        close_fair = _fair_implied(close_o)
        dev = {k: round(float(open_fair[i] - close_fair[i]), 4)
               for i, k in enumerate(('H', 'D', 'A'))}
        out['open_vs_fair_dev'] = dev
        out['open_dev_magnitude'] = round(float(np.max(np.abs(list(dev.values())))), 4)
        # 庄家开盘时强行讲故事的方向 = 偏离最大的一侧
        out['open_narrative_dir'] = max(dev, key=lambda k: abs(dev[k]))
        out['open_narrative_strength'] = round(abs(dev[out['open_narrative_dir']]), 4)
        # 初盘抽水比终盘更厚 → 越敢造盘(配合高偏离度 = 强意图)
        close_margin = _overround(close_o)
        out['open_margin_thicker_than_close'] = bool(open_margin > close_margin)
    return out


# ============================================================================
# 阶段2 — 临场1h 特征 (平衡筹码 / drift / 跨机构同步 / RLM / CLV)
# ============================================================================
def phase2_close(close_o: Optional[Dict], open_o: Optional[Dict],
                 pre_snapshots: Optional[List[Dict]] = None,
                 volume_data: Optional[Dict] = None) -> Dict:
    if not close_o:
        return {'available': False, 'reason': '无临场(封盘)赔率'}

    close_margin = _overround(close_o)
    out = {
        'available': True,
        'close_margin_pct': round(close_margin * 100, 2),
        'close_odds': close_o,
        'margin_anomaly': close_margin < 0,
    }
    if close_margin < 0:
        out['margin_note'] = '负margin(套利/数据异常) — 单家sharp或OCR错误'

    # drift 开盘→收盘 — 用公平概率(剔除抽水)算, 不受 margin 厚度干扰
    if open_o:
        op = _fair_implied(open_o)
        cp = _fair_implied(close_o)
        drift = {k: round(float(cp[i] - op[i]), 4) for i, k in enumerate(('H', 'D', 'A'))}
        out['drift_open_to_close'] = drift
        fav_idx = int(np.argmax(op))
        fav_open = ('H', 'D', 'A')[fav_idx]
        # CLV: 初盘下注热门, 收盘公平概率是否更优(正数=beat封盘)
        out['clv_beat'] = round(float(cp[fav_idx] - op[fav_idx]), 4)
        # 跨机构同步: 必须有 >=2 个"可读赔率"的初盘源才算数
        # (快照多≠源多 — canada 84个初盘快照只有1个带赔率, 1/1=1.0 是假同步)
        favs = [int(np.argmax(_fair_implied(so))) for s in pre_snapshots
                if (so := _snap_1x2(s))]
        if len(favs) >= 2:
            out['n_books'] = len(favs)
            agree = max(favs.count(f) for f in set(favs)) / len(favs)
            out['cross_book_sync'] = round(agree, 3)
            out['cross_book_sync_note'] = '多源同向' if agree >= 0.66 else '多源分歧'
        else:
            out['cross_book_sync'] = None
            out['cross_book_sync_note'] = '单源/无多源赔率 — 无法判定同步(无意义)'
    else:
        out['drift_open_to_close'] = None
        out['clv_beat'] = None
        out['cross_book_sync'] = None

    # RLM / Steam: 需成交量或大众投注占比
    if volume_data and isinstance(volume_data.get('betting_pct'), dict):
        bp = volume_data['betting_pct']
        bp_fav = max(('H', 'D', 'A'), key=lambda k: float(bp.get(k, 0.0)))
        # 盘口热门移动方向 vs 大众投注方向 相反 = RLM(聪明钱在另一边)
        drift_dir = None
        if out.get('drift_open_to_close'):
            d = out['drift_open_to_close']
            drift_dir = max(('H', 'D', 'A'), key=lambda k: float(d.get(k, 0.0)))
        out['rlm_available'] = True
        out['rlm_signal'] = 'RLM(逆市)' if (drift_dir and drift_dir != bp_fav) else '一致(无逆市)'
        out['public_fav'] = bp_fav
    else:
        out['rlm_available'] = False
        out['rlm_signal'] = None
        out['rlm_note'] = 'odds_db 无成交量/大众占比 — RLM/steam 需实时feed接入'

    return out


# ============================================================================
# 阶段3 — 滚盘特征 (低水聪明边 / xG缺口 / 时间贬值 / 逆转风险)
# ============================================================================
def phase3_live(live_snaps: Optional[List[Dict]], league: str,
                final_score: Optional[Tuple[int, int]] = None) -> Dict:
    if not live_snaps:
        return {'available': False, 'reason': '无滚盘快照'}

    # 取最深一张(最后)滚盘快照做主读
    last = live_snaps[-1]
    odds = last['odds']
    cp = _implied(odds)
    fav_idx = int(np.argmax(cp))
    fav_dir = ('H', 'D', 'A')[fav_idx]
    fav_odds = (odds['home'], odds['draw'], odds['away'])[fav_idx]

    out = {
        'available': True,
        'live_lowwater_smartedge_dir': fav_dir,   # 铁律A: 低水=聪明边
        'live_fav_odds': round(fav_odds, 2),
        'live_minute': last.get('minute', ''),
        'live_snapshot_count': len(live_snaps),
    }

    # live drift: 首张→末张 热门方概率收紧程度
    if len(live_snaps) >= 2:
        first = live_snaps[0]['odds']
        fp = _implied(first)
        out['live_drift'] = round(float(cp[fav_idx] - fp[fav_idx]), 4)
    else:
        out['live_drift'] = None

    # xG 缺口: 累计 xg 差 - 实际比分差 → 暗示后续追平/逆转
    xg_gap = None
    if last.get('xg'):
        try:
            xs = str(last['xg']).replace(' ', '')
            # 形如 "CAN 0.31 / MAR 0.00" 或 "0.31/0.00"
            nums = [float(n) for n in xs.replace('/', ' ').split() if _looks_float(n)]
            if len(nums) >= 2:
                xg_gap = round(nums[0] - nums[1], 2)
        except Exception:
            xg_gap = None
    out['live_xg_gap'] = xg_gap

    # 铁律B: obscure 联赛领先方未必守得住
    obscure = _is_obscure(league)
    out['obscure_league'] = obscure
    leading_dir = None
    if final_score:
        diff = final_score[0] - final_score[1]
        leading_dir = 'H' if diff > 0 else ('A' if diff < 0 else 'D')
    out['final_leading_dir'] = leading_dir
    if obscure and leading_dir and leading_dir != 'D':
        out['comeback_risk'] = True
        out['comeback_note'] = ('obscure联赛领先方未必守得住 — '
                                '不可据深盘判"不进球/不追平" (Kabuscorp/特尔纳瓦实证)')
    else:
        out['comeback_risk'] = False

    # 时间贬值
    out['time_decay_note'] = '越接近终场, 概率越固化; 伤停补时为最后窗口'
    return out


def _looks_float(s: str) -> bool:
    try:
        float(s)
        return True
    except Exception:
        return False


# ============================================================================
# 铁律覆盖 — 修正 ArtOfWar 的误判
# ============================================================================
def apply_trap_rules(engine_intent: str, engine_signals: List[str],
                     phases: Dict, p1: Dict, p2: Dict, p3: Dict) -> Dict:
    notes = []
    adjusted_intent = engine_intent
    has_open = p1.get('available') and p1.get('open_odds')
    has_drift = bool(p2.get('drift_open_to_close'))

    # 铁律A: 单张 live 截图无开盘价 → 禁止把低水说成陷阱诱多
    if p3.get('available') and not has_open:
        smart = p3['live_lowwater_smartedge_dir']
        # 若引擎因低水给出陷阱/设局意图, 强制降级为"庄家倾向"
        if ('设局' in (engine_intent or '') or '陷阱' in (engine_intent or '')) and '低水' in ' '.join(engine_signals or []):
            adjusted_intent = '📊 结构性读盘(样本外) — 低水线=庄家倾向(聪明边), 非陷阱'
            notes.append(f'铁律A触发: 无开盘价, 低水方{smart}判为聪明边, 已阻断"陷阱诱多"误判')
        else:
            notes.append(f'铁律A: 低水方{smart}=庄家护的聪明边(无开盘价, 不升级陷阱)')

    # 铁律C: 无 drift 证据 → 只标倾向, 不升级陷阱
    if not has_drift and ('陷阱' in engine_intent or '设局' in engine_intent):
        adjusted_intent = engine_intent.replace('设局', '倾向').replace('陷阱', '倾向')
        notes.append('铁律C触发: 无开盘→收盘漂移证据, 陷阱判定降级为"庄家倾向"')

    # 铁律B: obscure 联赛领先逆转风险
    if p3.get('comeback_risk'):
        notes.append(p3['comeback_note'])

    # 跨机构同步增强
    if p2.get('cross_book_sync') is not None:
        if p2['cross_book_sync'] >= 0.66:
            notes.append('跨机构同向异动 → 真信号(非平衡动作)')
        else:
            notes.append('跨机构分歧 → 谨慎, 可能是单家平衡动作')

    return {'adjusted_intent': adjusted_intent, 'notes': notes}


# ============================================================================
# 主入口 — 一场比赛的三段破解
# ============================================================================
def analyze_match(match_id: str, record: Optional[Dict] = None,
                  league: str = '', volume_data: Optional[Dict] = None) -> Dict:
    if record is None:
        record = _load_record(match_id)
    if record is None:
        return {'error': f'match {match_id} not found in odds_db'}

    phases = segment_phases(record)
    lg = league or phases['meta'].get('league', '')
    final_score = phases['meta'].get('final_score')

    p1 = phase1_open(phases['open'], phases['close'])
    # 跨机构同步只比较"初盘段"快照, 排除滚盘
    pre_snaps = [s for s in record.get('snapshots', [])
                 if s.get('match_status') != 'live' and not s.get('match_minute')]
    p2 = phase2_close(phases['close'], phases['open'],
                      pre_snapshots=pre_snaps, volume_data=volume_data)
    p3 = phase3_live(phases['live'], lg, final_score)

    # 引擎调用 (reverse_odds_engine)
    close = phases['close'] or (phases['live'][0]['odds'] if phases['live'] else None)
    open_o = phases['open']
    home_name = record.get('home') or (record.get('teams') or {}).get('home') or '主'
    away_name = record.get('away') or (record.get('teams') or {}).get('away') or '客'
    engine = {}
    otsm = None
    if close:
        try:
            eng = crack(home=home_name, away=away_name,
                        h=close['home'], d=close['draw'], a=close['away'],
                        league=lg,
                        open_h=open_o['home'] if open_o else None,
                        open_d=open_o['draw'] if open_o else None,
                        open_a=open_o['away'] if open_o else None,
                        version='v2')
            anom = eng.get('anomaly') or {}
            engine = {
                'anomaly_score': anom.get('score'),
                'intent': anom.get('intent') or '✅ 正常盘口 — 赔率反映实力',
                'direction': eng.get('direction'),
                'direction_confidence': eng.get('confidence'),
                'verdict': eng.get('verdict'),
                'signals': anom.get('signals', []),
            }
            otsm = eng.get('otsm')
        except Exception as e:
            engine = {'error': str(e)}
    else:
        engine = {'intent': '✅ 正常盘口 — 赔率反映实力', 'signals': []}

    # 扁平特征向量 (喂 v6.0)
    feats = _build_features(p1, p2, p3, engine, otsm, phases)

    # 铁律覆盖
    rules = apply_trap_rules(
        engine.get('intent', '✅ 正常盘口 — 赔率反映实力'),
        engine.get('signals', []), phases, p1, p2, p3)

    return {
        'match_id': match_id,
        'phases_present': [k for k in ('open', 'close', 'live') if phases.get(k)],
        'phase1_open': p1,
        'phase2_close': p2,
        'phase3_live': p3,
        'engine': engine,
        'otsm': otsm,
        'trap_rules': rules,
        'features_for_v6': feats,
        'feature_vector': feature_vector(feats),
        'verdict': _verdict_string(phases, p1, p2, p3, engine, rules),
    }


def _build_features(p1, p2, p3, engine, otsm, phases) -> Dict:
    DIR_ENC = {'H': 0, 'D': 1, 'A': 2, None: -1}
    STATE_ENC = {'LOCKED': 2, 'ACTIVE': 1, 'NOISE': 0, None: -1}

    f = {}
    f['open_margin_pct'] = p1.get('open_margin_pct', -1)
    f['open_margin_anomaly'] = 1 if p1.get('margin_anomaly') else 0
    f['close_margin_anomaly'] = 1 if p2.get('margin_anomaly') else 0
    f['open_dev_magnitude'] = p1.get('open_dev_magnitude', -1)
    f['close_margin_pct'] = p2.get('close_margin_pct', -1)
    drift = p2.get('drift_open_to_close') or {}
    f['drift_H'] = drift.get('H', 0.0)
    f['drift_D'] = drift.get('D', 0.0)
    f['drift_A'] = drift.get('A', 0.0)
    f['clv_beat'] = p2.get('clv_beat') if p2.get('clv_beat') is not None else -1
    f['cross_book_sync'] = p2.get('cross_book_sync') if p2.get('cross_book_sync') is not None else -1
    f['rlm_available'] = 1 if p2.get('rlm_available') else 0
    f['live_lowwater_smartedge_dir'] = DIR_ENC.get(p3.get('live_lowwater_smartedge_dir'), -1)
    f['live_drift'] = p3.get('live_drift') if p3.get('live_drift') is not None else 0.0
    f['live_xg_gap'] = p3.get('live_xg_gap') if p3.get('live_xg_gap') is not None else 0.0
    f['obscure_league'] = 1 if p3.get('obscure_league') else 0
    f['comeback_risk'] = 1 if p3.get('comeback_risk') else 0
    f['engine_anomaly_score'] = engine.get('anomaly_score') or 0.0
    f['engine_lock_confidence'] = float(otsm.get('lock_confidence', -1)) if otsm else -1
    f['engine_state'] = STATE_ENC.get(otsm.get('state') if otsm else None, -1)
    f['engine_drift_direction'] = DIR_ENC.get(otsm.get('drift_direction') if otsm else None, -1)
    f['has_open'] = 1 if p1.get('available') else 0
    f['has_live'] = 1 if p3.get('available') else 0
    f['n_books'] = p2.get('n_books', 0)
    f['open_margin_thicker'] = 1 if p1.get('open_margin_thicker_than_close') else 0
    return f


# 固定顺序特征名 — 与 feature_vector() / to_feature_matrix() 严格对应
FEATURE_ORDER = [
    'open_margin_pct', 'open_margin_anomaly', 'close_margin_anomaly',
    'open_dev_magnitude', 'open_margin_thicker', 'close_margin_pct',
    'drift_H', 'drift_D', 'drift_A', 'clv_beat', 'cross_book_sync',
    'n_books', 'rlm_available',
    'live_lowwater_smartedge_dir', 'live_drift', 'live_xg_gap',
    'obscure_league', 'comeback_risk',
    'engine_anomaly_score', 'engine_lock_confidence', 'engine_state',
    'engine_drift_direction', 'has_open', 'has_live',
]


def feature_vector(features: Dict) -> np.ndarray:
    """固定顺序数值向量 (len==len(FEATURE_ORDER)), 直接喂 v6.0 训练/推理.

    None(字段存在但无值) → 0.0 兜底, 保留 -1 这类"无数据"哨兵.
    """
    return np.array([float(features.get(k)) if features.get(k) is not None else 0.0
                     for k in FEATURE_ORDER], dtype=float)


def to_feature_matrix(match_ids: Optional[List[str]] = None) -> Tuple[np.ndarray, List[str]]:
    """全库(或指定)比赛 → (N, D) 特征矩阵 + id 列表, 直接喂 v6.0.

    跳过无赔率的数据文件; 单场异常不影响整体.
    """
    recs = load_all_records()
    if match_ids:
        want = set(match_ids)
        recs = [(m, r) for m, r in recs if m in want]
    mat, ids = [], []
    for mid, rec in recs:
        if not (rec.get('odds_1x2') or rec.get('snapshots')):
            continue
        try:
            r = analyze_match(mid, rec)
            mat.append(feature_vector(r['features_for_v6']))
            ids.append(mid)
        except Exception:
            continue
    return np.array(mat, dtype=float), ids


def _verdict_string(phases, p1, p2, p3, engine, rules) -> str:
    parts = []
    if p1.get('available'):
        parts.append(f"初盘margin={p1['open_margin_pct']}%")
    if p2.get('available'):
        parts.append(f"临场margin={p2['close_margin_pct']}%")
        if p2.get('drift_open_to_close'):
            parts.append(f"drift={p2['drift_open_to_close']}")
        if p2.get('cross_book_sync') is not None:
            parts.append(f"跨机构同步={p2['cross_book_sync']}")
    if p3.get('available'):
        parts.append(f"滚盘低水聪明边={p3['live_lowwater_smartedge_dir']}@{p3.get('live_fav_odds')}")
        if p3.get('live_xg_gap') is not None:
            parts.append(f"xG缺口={p3['live_xg_gap']}")
    base = f"引擎intent={engine.get('intent','?')} | " + " | ".join(parts)
    base += f"\n→ 终判: {rules['adjusted_intent']}"
    for n in rules['notes']:
        base += f"\n  · {n}"
    return base


# ============================================================================
# odds_db 加载
# ============================================================================
def _load_record(match_id: str) -> Optional[Dict]:
    # 优先 timeline(多快照, 三段数据最全); 无 timeline 才用单场 match json
    candidates = [
        ODDS_DB / 'timeline' / f'{match_id}.json',
        ODDS_DB / f'{match_id}.json',
    ]
    best = None
    for c in candidates:
        if c.exists():
            try:
                rec = json.loads(c.read_text(encoding='utf-8'))
            except Exception:
                continue
            if not isinstance(rec, dict):
                continue  # 跳过数组型文件
            # timeline 含 snapshots → 直接选; 否则保留作候选
            if isinstance(rec.get('snapshots'), list) and rec['snapshots']:
                return rec
            if best is None:
                best = rec
    return best


def load_all_records() -> List[Tuple[str, Dict]]:
    best = {}
    for f in glob.glob(str(ODDS_DB / '*.json')) + glob.glob(str(ODDS_DB / 'timeline' / '*.json')):
        if f.endswith('schema.json') or f.endswith('index.json'):
            continue
        try:
            rec = json.loads(Path(f).read_text(encoding='utf-8'))
        except Exception:
            continue
        if not isinstance(rec, dict):
            continue  # 跳过数组型数据文件(如 trap_detector_dataset)
        mid = rec.get('match_id') or Path(f).stem
        # 去重: 优先保留含 snapshots 的 timeline(三段数据最全)
        if mid not in best or (isinstance(rec.get('snapshots'), list) and rec['snapshots']):
            best[mid] = (mid, rec)
    return list(best.values())


# ============================================================================
# 自测
# ============================================================================
def run_self_test(limit: Optional[int] = None) -> Dict:
    recs = load_all_records()
    if limit:
        recs = recs[:limit]
    results = []
    for mid, rec in recs:
        try:
            r = analyze_match(mid, rec)
            results.append(r)
        except Exception as e:
            results.append({'match_id': mid, 'error': str(e)})
    return {'count': len(results), 'results': results}


def generate_report(out_dir: str = None) -> Dict:
    """批量跑 odds_db, 产出 report(JSON+MD). np类型用 default=str 兜底."""
    import json as _json
    recs = load_all_records()
    rows, errors = [], []
    for mid, rec in recs:
        if not (rec.get('odds_1x2') or rec.get('snapshots')):
            continue  # 跳过非比赛数据文件
        try:
            r = analyze_match(mid, rec)
            rows.append({
                'match_id': mid,
                'phases': r.get('phases_present'),
                'verdict': r.get('verdict'),
                'features': r.get('features_for_v6'),
                'trap_notes': r.get('trap_rules', {}).get('notes', []),
            })
        except Exception as e:
            errors.append({'match_id': mid, 'error': str(e)})
    report = {'total_scanned': len(recs), 'matched': len(rows),
              'errors': errors, 'rows': rows}
    if out_dir:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / 'three_phase_report.json').write_text(
            _json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
        md = [f'# 三段操盘框架 — odds_db 批量报告',
              f'扫描 {len(recs)} 个文件, 命中比赛 {len(rows)} 场, 失败 {len(errors)} 场\n']
        for row in rows:
            md.append(f"## {row['match_id']}  ·  阶段={row['phases']}")
            md.append(row['verdict'])
            for n in row['trap_notes']:
                md.append(f"  - {n}")
            md.append('')
        (out / 'three_phase_report.md').write_text('\n'.join(md), encoding='utf-8')
    return report


if __name__ == '__main__':
    import pprint
    # 优先跑三场代表性比赛: 多快照(canada) + 单live(ashdod) + 单live(kabuscorp)
    for mid in ('canada_vs_morocco_20260705', 'ashdod_vs_modiin_20260710',
                'kabuscorp_vs_interdeluanda_20260710'):
        print('=' * 70)
        print('MATCH:', mid)
        r = analyze_match(mid)
        print('phases_present:', r.get('phases_present'))
        print('--- verdict ---')
        print(r.get('verdict'))
        print('--- features_for_v6 ---')
        pprint.pprint(r.get('features_for_v6'))
    print('=' * 70)
    # 批量报告 → workspace
    ws = r'C:\Users\ShXAI\WorkBuddy\2026-07-10-19-37-10'
    rep = generate_report(ws)
    print(f'self_test OK | 批量报告: 命中 {rep["matched"]} 场, 失败 {len(rep["errors"])} 场')
    if rep['errors']:
        print('ERRORS:', rep['errors'][:5])
    # 验证新特征矩阵接口
    mat, ids = to_feature_matrix()
    print(f'feature_matrix: shape={mat.shape}, 样本数={len(ids)}, 维度={len(FEATURE_ORDER)}')
    assert mat.shape[1] == len(FEATURE_ORDER), '特征维度与 FEATURE_ORDER 不一致'

