"""
================================================================================
footballAI — 纯赔率破解核心引擎 v1.0 (从 SP/sp_core.py 移植)
================================================================================

大道至简。只做一件事：从赔率中读出庄家隐藏的真实意图。

四把钥匙 (可参数化):
  1. 反常指数 (AnomalyIndex) — 赔率偏离统计规律的程度 (0-100)
  2. 孙子兵法 (ArtOfWar) — 庄家操控手法的识别
  3. 历史验证 (HistoryValidator) — 类似赔率模式的真实胜率
  4. OTSM — 赔率时序状态机 (开盘→收盘漂移推断庄家确信度)

隐编码版本:
  v1 = 三钥匙版 (反常+兵法+历史) — 原始精简版
  v2 = +OTSM (LOCKED 状态增强置信度) — 默认版
  v3 = +OTSM+漂移方向增强 — 激进版
  v4 = 信噪分离版 — 高置信度信任赔率,低置信度才启用修正
  v5 = 纯赔率回归版 — 30 万场验证:信任赔率 (最准)

设计原则:
  1. 规则结构固定,数值阈值全部抽到 DEFAULT_CRACK_PARAMS
  2. 数据库路径可配置,默认 football_data.db
  3. 禁用亚洲盘口 (用户规则)
  4. 所有数值可通过 crack_param_optimizer.py 优化

移植日期: 2026-06-16
================================================================================
"""

import sqlite3
import numpy as np
from typing import Dict, List, Optional
import json
import os

# ================================================================================
# 破解参数配置 (可训练优化)
# ================================================================================

DEFAULT_CRACK_PARAMS = {
    # 反常指数权重
    'anomaly_draw_lowest': 40,
    'anomaly_away_hide': 30,
    'anomaly_da_close': 15,
    'anomaly_d_lt_3': 30,
    'anomaly_d_lt_3_3': 15,
    'anomaly_h_drift_up': 15,
    'anomaly_a_drift_up': 10,
    'anomaly_hd_close': 12,
    'anomaly_3way_balanced': 18,
    
    # 反常阈值
    'anomaly_da_close_threshold': 0.3,
    'anomaly_d_lt_3_threshold': 3.0,
    'anomaly_d_lt_3_3_threshold': 3.3,
    'anomaly_extreme_fav_threshold': 1.3,
    'anomaly_drift_threshold': 0.15,
    'anomaly_hd_close_threshold': 0.5,
    'anomaly_balanced_range': 1.5,
    'anomaly_balanced_min': 1.5,
    
    # OTSM 阈值 (5 万场回测拟合)
    'otsm_entropy_p20': 0.00032,
    'otsm_entropy_p50': 0.00969,
    'otsm_entropy_p80': 0.03735,
    'otsm_entropy_p90': 0.06247,
    'otsm_water_p20': -0.09059,
    'otsm_water_p80': 0.03212,
    
    # OTSM 置信度加成
    'otsm_locked_bonus_v4': 15,
    'otsm_locked_threshold': 0.6,
    'otsm_locked_strong_threshold': 0.7,
    
    # 概率调整 (v1/v2/v3)
    'adjust_d_lt_3_bonus': 0.06,
    'adjust_d_lt_2_8_bonus': 0.05,
    'adjust_anomaly_threshold': 25,
    'adjust_anomaly_bonus': 0.03,
    'adjust_otsm_v2_locked': 0.01,
    'adjust_otsm_v3_locked': 0.03,
    'adjust_otsm_active': 0.01,
    
    # v4 加成
    'v4_otsm_locked_strong': 0.08,
    'v4_otsm_locked_normal': 0.05,
    'v4_otsm_active': 0.02,
    'v4_high_confidence_threshold': 0.55,
    
    # 历史验证
    'history_min_samples': 20,
    'history_max_alpha': 0.35,
    'history_alpha_scale': 150000,
    'history_tolerance': 0.15,
    
    # 综合判定
    'tactic_high_confidence': 0.75,
    'tactic_bonus': 8,
    
    # 置信度上限
    'max_confidence': 99,
}

CRACK_PARAMS_PATH = os.path.join(os.path.dirname(__file__), 'crack_params.json')

def load_crack_params() -> Dict:
    """加载破解参数 (优先从文件,否则用默认)"""
    if os.path.exists(CRACK_PARAMS_PATH):
        with open(CRACK_PARAMS_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    return DEFAULT_CRACK_PARAMS.copy()

def save_crack_params(params: Dict):
    """保存破解参数到文件"""
    os.makedirs(os.path.dirname(CRACK_PARAMS_PATH), exist_ok=True)
    with open(CRACK_PARAMS_PATH, 'w', encoding='utf-8') as f:
        json.dump(params, f, ensure_ascii=False, indent=2)

# ================================================================================
# 第一把钥匙: 反常指数
# ================================================================================

class AnomalyIndex:
    """反常指数引擎 — 庄家的泄密窗口"""

    MATCH_CONTEXTS = {
        'world_cup_group':    {'motivation': 8, 'trap_risk': 0.2, 'notes': '净胜球驱动'},
        'world_cup_knockout': {'motivation': 3, 'trap_risk': 0.4, 'notes': '晋级优先'},
        'friendly':           {'motivation': 4, 'trap_risk': 0.6, 'notes': '可控制'},
        'league':             {'motivation': 5, 'trap_risk': 0.3, 'notes': '视排名'},
    }

    LEAGUE_MAP = {
        '美加墨世界杯': 'world_cup_group', '世界杯': 'world_cup_group',
        '欧洲杯': 'world_cup_knockout', '欧冠': 'world_cup_knockout',
        '国际友谊': 'friendly', '球会友谊': 'friendly',
    }

    @classmethod
    def classify(cls, league: str) -> str:
        for k, v in cls.LEAGUE_MAP.items():
            if k in (league or ''):
                return v
        return 'league'

    @classmethod
    def detect(cls, home: str, away: str, h: float, d: float, a: float,
               league: str = '', open_h=None, open_d=None, open_a=None,
               ou_line=None, params: Dict = None) -> Dict:
        """检测赔率中的反常信号 (使用可学习参数)"""
        if params is None:
            params = load_crack_params()
        
        anomaly = 0
        signals = []
        match_type = cls.classify(league)
        ctx = cls.MATCH_CONTEXTS.get(match_type, cls.MATCH_CONTEXTS['league'])
        motivation = ctx['motivation']
        trap_risk = ctx['trap_risk']

        # ---- 信号1: 最低赔率方向反常 ----
        vals = [h, d, a]
        dirs = ['H', 'D', 'A']
        min_dir = dirs[np.argmin(vals)]
        min_val = min(vals)

        if min_dir == 'D':
            signals.append(f'平局赔率全场最低({d:.2f}) — 极度反常! 历史此类平局命中36.2%')
            anomaly += params['anomaly_draw_lowest']
        elif min_dir == 'A' and h < 2.0:
            signals.append(f'客胜赔率最低({a:.2f})但主队是热门 — 庄家在隐藏客胜')
            anomaly += params['anomaly_away_hide']

        # ---- 信号1.5: D/A 赔率接近 ----
        if abs(d - a) < params['anomaly_da_close_threshold'] and d <= 3.5 and h < 2.5:
            signals.append(f'D/A 赔率接近({d:.2f}/{a:.2f}) — 庄家在暗示平局方向')
            anomaly += params['anomaly_da_close']

        # ---- 信号1.8: 隐含平局概率高 ----
        implied_d_prob = (1/d) / (1/h + 1/d + 1/a)
        min_odds = min(h, d, a)
        if d < params['anomaly_d_lt_3_threshold'] and min_odds > params['anomaly_extreme_fav_threshold']:
            signals.append(f'D 赔率极低({d:.2f}<3.0) — 历史平局率34.6%!')
            anomaly += params['anomaly_d_lt_3']
        elif d < params['anomaly_d_lt_3_3_threshold'] and min_odds > params['anomaly_extreme_fav_threshold']:
            signals.append(f'D 赔率偏低({d:.2f}<3.3) — 历史平局率29.1%')
            anomaly += params['anomaly_d_lt_3_3']

        # ---- 信号2: 初终盘漂移 ----
        if open_h and open_d and open_a:
            drift_h = h - open_h
            drift_a = a - open_a
            total_drift = abs(drift_h) + abs(d - open_d) + abs(drift_a)

            if total_drift > 0.8:
                if drift_h > params['anomaly_drift_threshold']:
                    signals.append(f'主胜赔率上升({drift_h:+.2f}) — 历史此类主胜仅35.3%')
                    anomaly += params['anomaly_h_drift_up']
                elif drift_a > params['anomaly_drift_threshold']:
                    signals.append(f'客胜赔率上升({drift_a:+.2f}) — 庄家在调整方向')
                    anomaly += params['anomaly_a_drift_up']

        # ---- 信号3: 赔率绝对值区间 ----
        if h < 1.2:
            signals.append(f'超低赔({h:.2f}) — 历史主胜85.8%, 但仍有14.2%冷门空间')
        elif h < 1.5:
            signals.append(f'中低赔({h:.2f}) — 历史主胜约72%')
        elif h < 2.0:
            signals.append(f'均衡偏主({h:.2f}) — 历史主胜约55%')

        # ---- 信号3.5: H/D 接近 ----
        if abs(h - d) < params['anomaly_hd_close_threshold'] and 1.5 < h < 3.0:
            signals.append(f'H/D 接近({h:.2f}/{d:.2f}) — 主队可能丢分')
            anomaly += params['anomaly_hd_close']

        # ---- 信号3.7: 三方向均衡 ----
        sorted_vals = sorted([h, d, a])
        if sorted_vals[2] - sorted_vals[0] < params['anomaly_balanced_range'] and sorted_vals[0] > params['anomaly_balanced_min']:
            signals.append(f'三方向均衡(极差{sorted_vals[2]-sorted_vals[0]:.2f}) — 冷门温床!')
            anomaly += params['anomaly_3way_balanced']

        # ---- 信号4: 大小球线信号 ----
        if ou_line and ou_line >= 3.5:
            if match_type == 'world_cup_group':
                signals.append(f'O/U线={ou_line} — 分组赛大球模式')
                anomaly += 5
            else:
                signals.append(f'O/U线={ou_line} — 疑似诱Over陷阱(声东击西)')
                anomaly += 20

        # ---- 判定 ----
        anomaly = min(anomaly, 100)

        if anomaly >= 60:
            intent = '高度反常 — 庄家在设局'
            action = '逆向操作,跟庄家的反方向走'
        elif anomaly >= 30:
            intent = '中度反常 — 庄家有动作'
            action = '谨慎分析'
        elif anomaly >= 15:
            intent = '轻度反常 — 市场波动'
            action = '正常分析'
        else:
            intent = '正常盘口 — 赔率反映实力'
            action = '按赔率指向操作'

        return {
            'score': float(round(anomaly, 1)),
            'intent': intent,
            'signals': signals,
            'action': action,
            'match_type': match_type,
            'motivation': motivation,
            'lowest_dir': min_dir,
            'lowest_val': min_val,
            'implied_d_prob': implied_d_prob,
            'extreme_fav': min_odds < params['anomaly_extreme_fav_threshold'],
        }

# ================================================================================
# 第二把钥匙: 孙子兵法
# ================================================================================

class ArtOfWar:
    """孙子兵法赔率战术识别"""

    @classmethod
    def detect(cls, h: float, d: float, a: float,
               open_h=None, open_d=None, open_a=None,
               ou_line=None, match_type='league',
               params: Dict = None) -> List[Dict]:
        """检测孙子兵法战术"""
        detected = []

        # 暗度陈仓: 平局赔率最低
        vals = [h, d, a]
        if d <= min(vals) + 0.1 and h < 3.0:
            detected.append({
                'tactic': '暗度陈仓',
                'confidence': 0.78,
                'evidence': f'平局赔率({d:.2f})≈最低,庄家在隐藏平局方向',
                'direction': 'D',
            })

        # 欲擒故纵: 初盘高赔诱注
        if open_h and h < open_h * 0.85:
            detected.append({
                'tactic': '欲擒故纵',
                'confidence': 0.73,
                'evidence': f'主胜初盘{open_h:.2f}→终盘{h:.2f}大幅下降',
                'direction': 'H',
            })
        if open_a and a < open_a * 0.85:
            detected.append({
                'tactic': '欲擒故纵',
                'confidence': 0.73,
                'evidence': f'客胜初盘{open_a:.2f}→终盘{a:.2f}大幅下降',
                'direction': 'A',
            })

        # 声东击西: O/U 极高 + 强队低赔
        if ou_line and ou_line >= 3.5 and h < 1.3:
            detected.append({
                'tactic': '声东击西',
                'confidence': 0.75,
                'evidence': f'O/U线={ou_line}极高+强队低赔({h:.2f})',
                'direction': 'Under',
            })

        # 顺手牵羊: 主胜赔率持续下降
        if open_h and open_d and open_a:
            drift_h = h - open_h
            drift_a = a - open_a
            if drift_h < -0.2 and drift_a > 0.1:
                detected.append({
                    'tactic': '顺手牵羊',
                    'confidence': 0.70,
                    'evidence': f'主胜赔率持续下降({drift_h:+.2f})',
                    'direction': 'H',
                })
            elif drift_a < -0.2 and drift_h > 0.1:
                detected.append({
                    'tactic': '顺手牵羊',
                    'confidence': 0.70,
                    'evidence': f'客胜赔率持续下降({drift_a:+.2f})',
                    'direction': 'A',
                })

        return detected

# ================================================================================
# 第三把钥匙: 历史验证
# ================================================================================

class HistoryValidator:
    """历史赔率模式验证"""

    def __init__(self, db_path: str = None, params: Dict = None):
        if db_path is None:
            db_path_candidates = [
                os.path.join(os.path.dirname(__file__), '..', 'data', 'football_data.db'),
                'data/football_data.db',
            ]
            for p in db_path_candidates:
                if os.path.exists(p):
                    db_path = p
                    break
            else:
                db_path = db_path_candidates[0]
        self.db_path = db_path
        self.params = params or load_crack_params()

    def validate(self, h: float, d: float, a: float, tolerance: float = None) -> Dict:
        """验证给定赔率模式的历史胜率"""
        if tolerance is None:
            tolerance = self.params.get('history_tolerance', 0.15)
        
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()

            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='historical_matches'")
            if not cur.fetchone():
                conn.close()
                return {'found': 0, 'message': 'historical_matches 表不存在'}

            cur.execute('''
                SELECT final_result, COUNT(*) as cnt,
                       AVG(home_score) as avg_hs,
                       AVG(away_score) as avg_as,
                       AVG(total_goals) as avg_tg
                FROM historical_matches
                WHERE close_home_odds BETWEEN ? AND ?
                  AND close_draw_odds BETWEEN ? AND ?
                  AND close_away_odds BETWEEN ? AND ?
                  AND final_result IN ('H', 'D', 'A')
                GROUP BY final_result
                ORDER BY cnt DESC
            ''', (h*(1-tolerance), h*(1+tolerance),
                  d*(1-tolerance), d*(1+tolerance),
                  a*(1-tolerance), a*(1+tolerance)))

            rows = cur.fetchall()
            
            if not rows:
                conn.close()
                return {'found': 0, 'message': '无类似赔率历史'}

            total = sum(r[1] for r in rows)
            results = {}
            for result, cnt, avg_hs, avg_as, avg_tg in rows:
                pct = cnt / total * 100
                results[result] = {
                    'count': cnt,
                    'pct': round(pct, 1),
                    'avg_score': f'{avg_hs:.1f}-{avg_as:.1f}' if avg_hs else '?',
                    'avg_goals': round(avg_tg, 1) if avg_tg else '?',
                }

            # 最可能比分
            cur.execute('''
                SELECT home_score, away_score, COUNT(*) as cnt
                FROM historical_matches
                WHERE close_home_odds BETWEEN ? AND ?
                  AND close_draw_odds BETWEEN ? AND ?
                  AND close_away_odds BETWEEN ? AND ?
                  AND home_score IS NOT NULL
                GROUP BY home_score, away_score
                ORDER BY cnt DESC
                LIMIT 5
            ''', (h*(1-tolerance), h*(1+tolerance),
                  d*(1-tolerance), d*(1+tolerance),
                  a*(1-tolerance), a*(1+tolerance)))
            score_rows = cur.fetchall()
            conn.close()

            top_scores = []
            for hs, as_, cnt in score_rows:
                top_scores.append({
                    'score': f'{hs}-{as_}',
                    'count': cnt,
                    'pct': round(cnt/total*100, 1),
                })

            best_dir = max(results.keys(), key=lambda k: results[k]['pct'])
            draw_pct = results.get('D', {}).get('pct', 0)

            return {
                'found': total,
                'results': results,
                'best_direction': best_dir,
                'best_pct': results[best_dir]['pct'],
                'draw_pct': draw_pct,
                'top_scores': top_scores,
                'avg_goals': results.get(best_dir, {}).get('avg_goals', '?'),
            }
        except Exception as e:
            return {'found': 0, 'error': str(e)}

    def validate_cs(self, cs_odds: Dict[str, float]) -> Dict:
        """从波胆赔率中找庄家指向"""
        if not cs_odds:
            return {'found': False}

        sorted_cs = sorted(cs_odds.items(), key=lambda x: x[1])
        top3 = sorted_cs[:3]

        return {
            'found': True,
            'bookmaker_direction': top3[0][0],
            'bookmaker_odds': top3[0][1],
            'top3': [{'score': s, 'odds': o} for s, o in top3],
        }

# ================================================================================
# 第四把钥匙: OTSM (赔率时序状态机)
# ================================================================================

class OTSM:
    """赔率时序状态机 — 从开盘→收盘的漂移推断庄家确信度"""

    @classmethod
    def _entropy(cls, probs):
        eps = 1e-10
        return -sum(p * np.log2(p + eps) for p in probs)

    @classmethod
    def _implied_probs(cls, h, d, a):
        raw = np.array([1.0/h, 1.0/d, 1.0/a])
        return raw / raw.sum()

    @classmethod
    def _overround(cls, h, d, a):
        return 1.0/h + 1.0/d + 1.0/a - 1.0

    @classmethod
    def analyze(cls, close_h, close_d, close_a,
                open_h=None, open_d=None, open_a=None,
                ht_h=None, ht_d=None, ht_a=None,
                params: Dict = None) -> Dict:
        """OTSM 分析"""
        if params is None:
            params = load_crack_params()
        
        close_probs = cls._implied_probs(close_h, close_d, close_a)
        close_entropy = cls._entropy(close_probs)
        close_or = cls._overround(close_h, close_d, close_a)

        # 无初盘时降级
        if not open_h or not open_d or not open_a:
            confidence_bonus = max(0, (0.06 - close_or) / 0.06 * 20) if close_or < 0.06 else 0
            max_prob = max(close_probs)
            min_prob = min(close_probs)
            polarization = max_prob - min_prob

            if polarization > 0.4:
                state = 'LOCKED'
                lock_confidence = min(0.85, 0.5 + polarization)
                reason = f'终盘高度极化(极差{polarization:.2f})'
            elif polarization > 0.2:
                state = 'ACTIVE'
                lock_confidence = 0.4 + polarization * 0.5
                reason = f'终盘中度极化(极差{polarization:.2f})'
            else:
                state = 'NOISE'
                lock_confidence = 0.2
                reason = f'终盘均衡(极差{polarization:.2f})'

            return {
                'state': state,
                'lock_confidence': round(lock_confidence, 3),
                'entropy_drift': None,
                'water_accel': None,
                'kelly_fluctuation': None,
                'confidence_bonus': round(confidence_bonus, 1),
                'drift_direction': None,
                'reason': f'[降级模式] {reason}',
            }

        # 有初盘: 完整 OTSM
        open_probs = cls._implied_probs(open_h, open_d, open_a)
        open_entropy = cls._entropy(open_probs)
        open_or = cls._overround(open_h, open_d, open_a)

        max_entropy = np.log2(3)
        entropy_drift = (close_entropy - open_entropy) / max_entropy

        if abs(open_or) > 1e-6:
            water_accel = (close_or - open_or) / (abs(open_or) + 0.01)
            water_accel = max(-1.0, min(1.0, water_accel))
        else:
            water_accel = 0.0

        open_fav_idx = int(np.argmax(open_probs))
        close_fav_idx = int(np.argmax(close_probs))
        if open_fav_idx != close_fav_idx:
            kelly_fluct = abs(close_probs[close_fav_idx] - open_probs[open_fav_idx]) + 0.05
        else:
            kelly_fluct = abs(close_probs[open_fav_idx] - open_probs[open_fav_idx])
        kelly_fluct /= (open_probs[open_fav_idx] + 0.05)

        # 状态推断
        abs_entropy = abs(entropy_drift)
        p20 = params['otsm_entropy_p20']
        p80 = params['otsm_entropy_p80']
        p90 = params['otsm_entropy_p90']

        if abs_entropy < p20:
            state = 'NOISE'
            lock_confidence = 0.0
        elif abs_entropy > p80:
            state = 'LOCKED'
            if abs_entropy <= p90:
                lock_confidence = 0.6 + (abs_entropy - p80) / (p90 - p80 + 1e-9) * 0.3
            else:
                lock_confidence = min(1.0, 0.9 + (abs_entropy - p90) / 0.1 * 0.1)
        else:
            state = 'ACTIVE'
            lock_confidence = (abs_entropy - p20) / (p80 - p20 + 1e-9) * 0.6

        if water_accel < params['otsm_water_p20']:
            lock_confidence = min(1.0, lock_confidence + 0.05)
        elif water_accel > params['otsm_water_p80']:
            lock_confidence = max(0.0, lock_confidence - 0.02)

        drift_h = close_probs[0] - open_probs[0]
        drift_d = close_probs[1] - open_probs[1]
        drift_a = close_probs[2] - open_probs[2]
        drifts = {'H': drift_h, 'D': drift_d, 'A': drift_a}
        drift_direction = max(drifts, key=drifts.get)
        drift_magnitude = max(drifts.values())

        if ht_h and ht_d and ht_a:
            ht_probs = cls._implied_probs(ht_h, ht_d, ht_a)
            ht_drifts = {
                'H': close_probs[0] - ht_probs[0],
                'D': close_probs[1] - ht_probs[1],
                'A': close_probs[2] - ht_probs[2],
            }
            ht_drift_dir = max(ht_drifts, key=ht_drifts.get)
            if ht_drift_dir == drift_direction:
                lock_confidence = min(1.0, lock_confidence + 0.03)

        confidence_bonus = lock_confidence * 25

        return {
            'state': state,
            'lock_confidence': round(lock_confidence, 3),
            'entropy_drift': round(entropy_drift, 4),
            'water_accel': round(water_accel, 4),
            'kelly_fluctuation': round(kelly_fluct, 4),
            'confidence_bonus': round(confidence_bonus, 1),
            'drift_direction': drift_direction,
            'drift_magnitude': round(drift_magnitude, 4),
            'drifts': {k: round(v, 4) for k, v in drifts.items()},
            'reason': f'状态={state}(LC={lock_confidence:.2f}) 漂移指向{drift_direction}(Δ={drift_magnitude:.3f})',
        }

# ================================================================================
# 核心入口: 一行破解
# ================================================================================

def crack(home: str, away: str, h: float, d: float, a: float,
          league: str = '', open_h=None, open_d=None, open_a=None,
          ou_line=None, cs_odds: Dict[str, float] = None,
          ht_h=None, ht_d=None, ht_a=None,
          version: str = 'v2', db_path: str = None,
          params: Dict = None) -> Dict:
    """
    纯赔率破解 — 一行调用,所有答案
    """
    if params is None:
        params = load_crack_params()
    
    # 1. 反常指数
    anomaly = AnomalyIndex.detect(home, away, h, d, a, league,
                                  open_h, open_d, open_a, ou_line, params)

    # 2. 孙子兵法
    tactics = ArtOfWar.detect(h, d, a, open_h, open_d, open_a, ou_line,
                              anomaly['match_type'], params)

    # 3. 历史验证
    validator = HistoryValidator(db_path=db_path, params=params)
    history = validator.validate(h, d, a)

    # 4. 波胆方向
    cs_result = validator.validate_cs(cs_odds) if cs_odds else {'found': False}

    # 5. OTSM
    otsm = OTSM.analyze(h, d, a, open_h, open_d, open_a, ht_h, ht_d, ht_a, params) \
        if version in ('v2', 'v3', 'v4', 'v5') else None

    # 6. 综合判定
    raw_probs = {'H': 1/h, 'D': 1/d, 'A': 1/a}
    total_raw = sum(raw_probs.values())
    implied = {k: round(v/total_raw, 3) for k, v in raw_probs.items()}
    overround = round((total_raw - 1) * 100, 1)

    direction = 'H'
    direction_confidence = 0
    direction_reason = ''
    min_odds = min(h, d, a)

    # CS 波胆方向 (硬覆盖)
    cs_direction_hit = False
    if cs_result.get('found') and cs_result.get('bookmaker_direction'):
        cs_score = cs_result['bookmaker_direction']
        cs_odds_val = cs_result['bookmaker_odds']
        try:
            parts = cs_score.split('-')
            hs, as_ = int(parts[0]), int(parts[1])
            if hs > as_:
                cs_dir = 'H'
            elif hs == as_:
                cs_dir = 'D'
            else:
                cs_dir = 'A'

            if cs_odds_val and cs_odds_val < 10.0:
                direction = cs_dir
                cs_confidence = max(30, int((10.0 - cs_odds_val) * 10))
                direction_confidence = cs_confidence
                direction_reason = f'波胆最低{cs_score}@{cs_odds_val:.2f} → {cs_dir}方'
                cs_direction_hit = True
        except (ValueError, TypeError, IndexError):
            pass

    if not cs_direction_hit:
        if version == 'v5':
            # 纯赔率回归
            max_implied = max(implied.values())
            odds_direction = max(implied, key=implied.get)
            direction = odds_direction
            direction_confidence = int(max_implied * 100)
            
            otsm_note = ''
            if otsm and otsm['state'] == 'LOCKED':
                otsm_drift_dir = otsm.get('drift_direction')
                if otsm_drift_dir == direction and otsm['lock_confidence'] >= params['otsm_locked_threshold']:
                    bonus = int(otsm['lock_confidence'] * 10)
                    direction_confidence = min(params['max_confidence'], direction_confidence + bonus)
                    otsm_note = f' + OTSM LOCKED确认(LC={otsm["lock_confidence"]:.2f})'
            
            direction_reason = f'v5纯赔率: →{odds_direction}({max_implied:.0%}){otsm_note}'
        
        elif version == 'v4':
            # 信噪分离
            max_implied = max(implied.values())
            odds_direction = max(implied, key=implied.get)
            
            if max_implied > params['v4_high_confidence_threshold']:
                # 高置信通道
                otsm_drift_dir = otsm.get('drift_direction') if otsm else None
                otsm_locked = otsm and otsm['state'] == 'LOCKED' and otsm['lock_confidence'] >= params['otsm_locked_strong_threshold']
                
                if otsm_locked and otsm_drift_dir and otsm_drift_dir != odds_direction:
                    direction = otsm_drift_dir
                    direction_confidence = int(max_implied * 100) - 5
                    direction_reason = f'v4高置信({max_implied:.0%})赔率→{odds_direction}, 但OTSM LOCKED反指→{otsm_drift_dir}'
                else:
                    direction = odds_direction
                    direction_confidence = int(max_implied * 100)
                    otsm_note = ''
                    if otsm_locked and otsm_drift_dir == odds_direction:
                        direction_confidence = min(params['max_confidence'], direction_confidence + 5)
                        otsm_note = f' + OTSM LOCKED确认'
                    direction_reason = f'v4高置信通道: →{odds_direction}({max_implied:.0%}){otsm_note}'
            else:
                # 低置信通道
                adj = {k: v for k, v in implied.items()}
                
                n_hist = history.get('found', 0)
                if n_hist >= params['history_min_samples']:
                    alpha = min(params['history_max_alpha'], n_hist / params['history_alpha_scale'])
                    for result_dir in ['H', 'D', 'A']:
                        hist_pct = history.get('results', {}).get(result_dir, {}).get('pct', 0)
                        if hist_pct > 0:
                            adj[result_dir] = adj[result_dir] * (1 - alpha) + (hist_pct / 100) * alpha
                
                if d < params['anomaly_d_lt_3_threshold'] and min_odds > params['anomaly_extreme_fav_threshold']:
                    adj['D'] += params['adjust_d_lt_3_bonus']
                
                anomaly_suggested_dir = None
                for sig in anomaly.get('signals', []):
                    if '平局赔率全场最低' in sig:
                        anomaly_suggested_dir = 'D'
                        break
                    elif '隐藏客胜' in sig:
                        anomaly_suggested_dir = 'A'
                        break
                
                if anomaly['score'] >= params['adjust_anomaly_threshold'] and anomaly_suggested_dir:
                    adj[anomaly_suggested_dir] += params['adjust_anomaly_bonus']
                
                otsm_drift_dir = otsm.get('drift_direction') if otsm else None
                if otsm and otsm_drift_dir:
                    if otsm['state'] == 'LOCKED':
                        if otsm['lock_confidence'] >= params['otsm_locked_strong_threshold']:
                            adj[otsm_drift_dir] += params['v4_otsm_locked_strong']
                        else:
                            adj[otsm_drift_dir] += params['v4_otsm_locked_normal']
                    elif otsm['state'] == 'ACTIVE' and otsm.get('drift_magnitude', 0) > 0.02:
                        adj[otsm_drift_dir] += params['v4_otsm_active']
                
                total = sum(adj.values())
                adj = {k: round(v / total, 4) for k, v in adj.items()}
                direction = max(adj, key=adj.get)
                direction_confidence = int(adj[direction] * 100)
                
                parts = [f'v4低置信: 调整概率 H={adj["H"]*100:.1f}% D={adj["D"]*100:.1f}% A={adj["A"]*100:.1f}%']
                if otsm and otsm_drift_dir:
                    parts.append(f'OTSM→{otsm_drift_dir}')
                direction_reason = ' | '.join(parts)
        else:
            # v1/v2/v3
            adj = {k: v for k, v in implied.items()}
            
            n_hist = history.get('found', 0)
            if n_hist >= params['history_min_samples']:
                alpha = min(params['history_max_alpha'], n_hist / params['history_alpha_scale'])
                for result_dir in ['H', 'D', 'A']:
                    hist_pct = history.get('results', {}).get(result_dir, {}).get('pct', 0)
                    if hist_pct > 0:
                        adj[result_dir] = adj[result_dir] * (1 - alpha) + (hist_pct / 100) * alpha
            
            if d < params['anomaly_d_lt_3_threshold'] and min_odds > params['anomaly_extreme_fav_threshold']:
                adj['D'] += params['adjust_d_lt_3_bonus']
            
            anomaly_suggested_dir = None
            for sig in anomaly.get('signals', []):
                if '平局赔率全场最低' in sig:
                    anomaly_suggested_dir = 'D'
                    break
                elif '隐藏客胜' in sig:
                    anomaly_suggested_dir = 'A'
                    break
            
            if anomaly['score'] >= params['adjust_anomaly_threshold'] and anomaly_suggested_dir:
                adj[anomaly_suggested_dir] += params['adjust_anomaly_bonus']
            
            otsm_drift_dir = otsm.get('drift_direction') if otsm else None
            if version in ('v2', 'v3') and otsm and otsm_drift_dir:
                if otsm['state'] == 'LOCKED':
                    if version == 'v3':
                        adj[otsm_drift_dir] += params['adjust_otsm_v3_locked']
                    else:
                        adj[otsm_drift_dir] += params['adjust_otsm_v2_locked']
                elif otsm['state'] == 'ACTIVE' and otsm.get('drift_magnitude', 0) > 0.02:
                    adj[otsm_drift_dir] += params['adjust_otsm_active']
            
            total = sum(adj.values())
            adj = {k: round(v / total, 4) for k, v in adj.items()}
            direction = max(adj, key=adj.get)
            direction_confidence = int(adj[direction] * 100)
            
            parts = [f'调整概率: H={adj["H"]*100:.1f}% D={adj["D"]*100:.1f}% A={adj["A"]*100:.1f}%']
            if otsm and otsm_drift_dir:
                parts.append(f'OTSM→{otsm_drift_dir}')
            direction_reason = ' | '.join(parts)

    # OTSM LOCKED 置信度加成 (v2)
    if version == 'v2' and otsm and otsm['state'] == 'LOCKED':
        otsm_lc = otsm['lock_confidence']
        bonus = int(otsm['confidence_bonus'] * 0.5)
        direction_confidence = min(params['max_confidence'], direction_confidence + bonus)
        direction_reason += f' + OTSM LOCKED(LC={otsm_lc:.2f})'

    # v4 OTSM LOCKED 加成
    if version == 'v4' and otsm and otsm['state'] == 'LOCKED':
        otsm_lc = otsm['lock_confidence']
        otsm_drift_dir = otsm.get('drift_direction')
        if otsm_drift_dir == direction:
            bonus = int(otsm_lc * params['otsm_locked_bonus_v4'])
            direction_confidence = min(params['max_confidence'], direction_confidence + bonus)
            direction_reason += f' + OTSM LOCKED确认(LC={otsm_lc:.2f})'

    # 孙子兵法增强
    for t in tactics:
        if t.get('direction') and t['confidence'] > params['tactic_high_confidence']:
            tactic_dir = t['direction']
            if tactic_dir in ['H', 'D', 'A'] and tactic_dir == direction:
                direction_confidence = min(direction_confidence + params['tactic_bonus'], params['max_confidence'])
                direction_reason += f' + {t["tactic"]}确认'

    # 推荐比分
    recommended_scores = []
    if cs_result.get('found'):
        recommended_scores = [s['score'] for s in cs_result.get('top3', [])]
    elif history.get('top_scores'):
        recommended_scores = [s['score'] for s in history['top_scores'][:3]]

    return {
        'version': version,
        'home': home,
        'away': away,
        'league': league,
        'implied_probs': implied,
        'overround': overround,
        'direction': direction,
        'direction_confidence': direction_confidence,
        'direction_reason': direction_reason,
        'anomaly': anomaly,
        'tactics': tactics,
        'otsm': otsm,
        'history': history,
        'cs_result': cs_result,
        'recommended_scores': recommended_scores,
    }

# ================================================================================
# 自检
# ================================================================================

if __name__ == '__main__':
    print('=' * 65)
    print('  footballAI 纯赔率破解核心 v1.0 — 自检')
    print('=' * 65)
    
    params = load_crack_params()
    print(f'\n参数加载: {len(params)} 个')
    
    # 测试: 卡塔尔 vs 瑞士
    result = crack(
        home='卡塔尔', away='瑞士',
        h=13.0, d=6.70, a=1.21,
        league='世界杯',
        open_h=12.0, open_d=7.00, open_a=1.18,
        ou_line=2.5,
        version='v2',
    )
    
    print(f'\n测试比赛: 卡塔尔 vs 瑞士 (世界杯)')
    print(f'  隐含概率: H={result["implied_probs"]["H"]:.1%} D={result["implied_probs"]["D"]:.1%} A={result["implied_probs"]["A"]:.1%}')
    print(f'  抽水率: {result["overround"]}%')
    print(f'\n反常指数: {result["anomaly"]["score"]} ({result["anomaly"]["intent"]})')
    print(f'  活跃信号: {len(result["anomaly"]["signals"])} 条')
    for s in result["anomaly"]["signals"][:3]:
        print(f'    - {s}')
    
    print(f'\n孙子兵法: {len(result["tactics"])} 计')
    for t in result["tactics"]:
        print(f'    - {t["tactic"]} ({t["confidence"]:.0%}) → {t["direction"]}')
    
    if result["otsm"]:
        print(f'\nOTSM 状态: {result["otsm"]["state"]} (LC={result["otsm"]["lock_confidence"]:.2f})')
        if result["otsm"]["drift_direction"]:
            print(f'  漂移指向: {result["otsm"]["drift_direction"]}')
    
    print(f'\n历史验证: {result["history"]["found"]} 场类似赔率')
    if result["history"]["found"] > 0:
        print(f'  最可能方向: {result["history"]["best_direction"]} ({result["history"]["best_pct"]:.1f}%)')
        print(f'  平局率: {result["history"].get("draw_pct", 0):.1f}%')
    
    print(f'\n最终判定: {result["direction"]} (置信度 {result["direction_confidence"]}%)')
    print(f'  理由: {result["direction_reason"]}')
    if result["recommended_scores"]:
        print(f'  推荐比分: {result["recommended_scores"]}')
    
    print(f'\n✅ 核心引擎就绪')
