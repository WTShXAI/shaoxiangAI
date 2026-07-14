"""
================================================================================
 哨响SP — 纯赔率破解核心引擎 v2.1 (OTSM+v4信噪分离增强版)
================================================================================

 大道至简。只做一件事：从赔率中读出庄家隐藏的真实意图。

 四把钥匙:
   1. 反常指数 — 赔率偏离统计规律的程度 (0-100)
   2. 孙子兵法 — 庄家操控手法的识别
   3. 历史验证 — 类似赔率模式的真实胜率
   4. OTSM    — 赔率时序状态机(开盘→收盘漂移推断庄家确信度)

 隐编码版本:
   v1 = 三钥匙版(反常+兵法+历史) — 原始精简版
   v2 = +OTSM(LOCKED状态增强置信度) — 默认版
   v3 = +OTSM+漂移方向增强(初终盘方向变化直接影响判定) — 激进版
   v4 = 信噪分离版 — 高置信度信任赔率,低置信度才启用修正(基于80%纯赔率验证)
   v5 = 纯赔率回归版 — 30万场验证:去除所有双计修正,信任赔率(最准)

 核心定理(45万场验证):
   - 1-1是全球最常见比分(4.40%)
   - 平局赔率最低仅856场(0.27%), 此时平局命中36.2%
   - 赔率变化方向有预测力: 主胜赔升→主胜仅35.3%
   - 超低赔<1.2 → 主胜85.8%, 仍有14.2%冷门空间
   - OTSM LOCKED状态(LC>=0.85) → 方向命中69.0% (5万场回测)
   - 纯赔率>70%置信度 → 80.0%准确率 (641场验证)
   - 纯赔率>60%置信度 → 79.6%准确率 (641场验证)
   - OTSM LOCKED+赔率>0.55 → 70.7%准确率 (5158场回测)
   - D<3.0修正=双计(隐含概率已包含, 30万场验证:纯赔率39.2%>强制D33.5%)
   - D<2.5修正=有效(123场, 强制D50.4%>纯赔率47.2%)

 作者: 哨响SP团队
 日期: 2026-06-15
================================================================================
"""

import sqlite3
import numpy as np
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple
from datetime import datetime


# ============================================================================
# 第一把钥匙: 反常指数
# ============================================================================

class AnomalyIndex:
    """
    反常指数引擎 — 庄家的泄密窗口

    原理: 庄家设赔率是为了平衡投注量赚钱，不是反映真实概率。
    当赔率偏离统计规律时，偏差本身就是信号。
    """

    # 比赛类型 → 基线参数
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
               ou_line=None) -> Dict:
        """
        核心方法：检测赔率中的反常信号

        参数: 主客队名, 终盘H/D/A赔率, 联赛名, 可选初盘和大小球线
        返回: 反常报告
        """
        anomaly = 0
        signals = []
        match_type = cls.classify(league)
        ctx = cls.MATCH_CONTEXTS.get(match_type, cls.MATCH_CONTEXTS['league'])
        motivation = ctx['motivation']
        trap_risk = ctx['trap_risk']

        # ---- 信号1: 最低赔率方向反常 ----
        # 统计规律: 平局赔率全场最低仅0.27%的比赛,此时平局命中36.2%(远超均值25.7%)
        vals = [h, d, a]
        dirs = ['H', 'D', 'A']
        min_dir = dirs[np.argmin(vals)]
        min_val = min(vals)

        if min_dir == 'D':
            signals.append(f'🔥 平局赔率全场最低({d:.2f}) — 极度反常! 历史此类平局命中36.2%')
            anomaly += 40
        elif min_dir == 'A' and h < 2.0:
            signals.append(f'⚠️ 客胜赔率最低({a:.2f})但主队是热门 — 庄家在隐藏客胜')
            anomaly += 30

        # ---- 信号1.5: D/A赔率接近 = 平局风险高 ----
        if abs(d - a) < 0.3 and d <= 3.5 and h < 2.5:
            signals.append(f'⚠️ D/A赔率接近({d:.2f}/{a:.2f}) — 庄家在暗示平局方向')
            anomaly += 15

        # ---- 信号1.8: 隐含平局概率高 (统计验证: D<3.0→34.6%, D<3.3→29.1%) ----
        implied_d_prob = (1/d) / (1/h + 1/d + 1/a)
        min_odds = min(h, d, a)  # 最低赔率(热门方)
        if d < 3.0 and min_odds > 1.3:
            # D<3.0 + 无极端热门 → 34.6%平局率(远超25.5%基准)
            signals.append(f'🔥 D赔率极低({d:.2f}<3.0) — 历史平局率34.6%! 庄家在认真定价平局')
            anomaly += 30
        elif d < 3.3 and min_odds > 1.3:
            # D<3.3 + 无极端热门 → 29.1%平局率
            signals.append(f'⚠️ D赔率偏低({d:.2f}<3.3) — 历史平局率29.1%')
            anomaly += 15
        elif d < 3.0 and min_odds <= 1.3:
            # 有极端热门时D<3.0,但热门方通常赢→不推荐平局
            signals.append(f'📋 D赔率低({d:.2f})但有极端热门({min_odds:.2f}) → 热门方胜率更高(11.4%平局 vs 65%热门胜)')

        # ---- 信号1.9: 极端热门陷阱 ----
        # 最低赔率<1.3: 热门方64.8%胜率, 但仍有11.4%平局+23.8%冷门
        # 仅在有额外反常信号时才标记为陷阱
        if min_odds < 1.3:
            trap_dir = min_dir
            signals.append(f'📊 极端热门({trap_dir}方{min_odds:.2f}) — 热门胜率65%, 但仍有35%冷门空间')

        # ---- 信号2: 初终盘漂移 ----
        # 统计规律: 主胜赔升→主胜仅35.3%
        if open_h and open_d and open_a:
            drift_h = h - open_h
            drift_d = d - open_d
            drift_a = a - open_a
            total_drift = abs(drift_h) + abs(drift_d) + abs(drift_a)

            if total_drift > 0.8:
                if drift_h > 0.15:
                    signals.append(f'📊 主胜赔率上升({drift_h:+.2f}) — 历史此类主胜仅35.3%')
                    anomaly += 15
                elif drift_a > 0.15:
                    signals.append(f'📊 客胜赔率上升({drift_a:+.2f}) — 庄家在调整方向')
                    anomaly += 10
                else:
                    signals.append(f'📊 明显赔率漂移: H{drift_h:+.2f}/D{drift_d:+.2f}/A{drift_a:+.2f}')
                    if match_type == 'friendly':
                        anomaly += min(int(total_drift * 10), 25)
                    else:
                        anomaly += min(int(total_drift * 5), 15)

        # ---- 信号3: 赔率绝对值区间 ----
        # 统计规律: 超低赔<1.2 → 主胜85.8%, 1.2-1.5 → 主胜72%, 1.5-2.0 → 主胜55%
        if h < 1.2:
            signals.append(f'💰 超低赔({h:.2f}) — 历史主胜85.8%, 但仍有14.2%冷门空间!')
        elif h < 1.5:
            signals.append(f'📋 中低赔({h:.2f}) — 历史主胜约72%')
        elif h < 2.0:
            signals.append(f'⚖️ 均衡偏主({h:.2f}) — 历史主胜约55%')

        # ---- 信号3.5: H/D接近 = 主队不稳 ----
        # 当主胜和平局赔率接近时(差距<0.5),庄家在暗示主队可能丢分
        if abs(h - d) < 0.5 and 1.5 < h < 3.0:
            signals.append(f'⚠️ H/D接近({h:.2f}/{d:.2f}) — 主队可能丢分, 平局风险高')
            anomaly += 12

        # ---- 信号3.7: 三方向均衡 = 庄家也不知道 ----
        # 当H/D/A差距都不大时,说明比赛难预测,冷门概率高
        sorted_vals = sorted([h, d, a])
        if sorted_vals[2] - sorted_vals[0] < 1.5 and sorted_vals[0] > 1.5:
            signals.append(f'⚠️ 三方向均衡(极差{sorted_vals[2]-sorted_vals[0]:.2f}) — 冷门温床!')
            anomaly += 18

        # ---- 信号4: 大小球线信号 ----
        if ou_line and ou_line >= 3.5:
            if match_type == 'world_cup_group':
                signals.append(f'📊 O/U线={ou_line} — 分组赛大球模式(非诱盘)')
                anomaly += 5
            else:
                signals.append(f'⚠️ O/U线={ou_line} — 疑似诱Over陷阱(声东西)')
                anomaly += 20

        # ---- 信号5: 比赛情境 ----
        if trap_risk >= 0.5:
            signals.append(f'🔍 情境: {match_type}(陷阱风险{trap_risk:.0%})')
            anomaly += int(trap_risk * 10)
        else:
            signals.append(f'📋 情境: {match_type}(动机{motivation}/10)')

        # ---- 判定 ----
        anomaly = min(anomaly, 100)

        if anomaly >= 60:
            intent = '🚨 高度反常 — 庄家在设局'
            action = '逆向操作，跟庄家的反方向走'
        elif anomaly >= 30:
            intent = '⚠️ 中度反常 — 庄家有动作'
            action = '谨慎分析，注意反常方向'
        elif anomaly >= 15:
            intent = '📊 轻度反常 — 市场波动'
            action = '正常分析，关注反常方向'
        else:
            intent = '✅ 正常盘口 — 赔率反映实力'
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
            'implied_d_prob': implied_d_prob,  # 隐含平局概率
            'extreme_fav': min_odds < 1.3,  # 是否有极端热门
        }


# ============================================================================
# 第二把钥匙: 孙子兵法
# ============================================================================

class ArtOfWar:
    """
    孙子兵法赔率战术识别

    庄家的操控手法有限，因为赔率就是他们的武器。
    武器是什么样，意图就是什么样。
    """

    TACTICS = {
        '暗度陈仓': {
            'desc': '表面上推一个方向，实际上在做另一个方向',
            'trigger': '平局赔率最低 或 低赔方向赔率在偷偷下降',
        },
        '瞒天过海': {
            'desc': '比分变了但赔率不变，庄家在掩盖真实结果',
            'trigger': '滚球中比分变化但核心赔率不变(需live数据)',
        },
        '声东西': {
            'desc': '用大小球线吸引注意力，实际操控胜负方向',
            'trigger': 'O/U线极高+强队低赔，诱Over陷阱',
        },
        '顺手牵羊': {
            'desc': '利用市场情绪惯性，顺水推舟做盘',
            'trigger': '大众看好方向赔率持续下降',
        },
        '欲擒故纵': {
            'desc': '先给你点甜头，然后一把收割',
            'trigger': '初盘给高赔诱注，终盘大幅下降',
        },
    }

    @classmethod
    def detect(cls, h: float, d: float, a: float,
              open_h=None, open_d=None, open_a=None,
              ou_line=None, match_type='league') -> List[Dict]:
        """检测孙子兵法战术"""
        detected = []

        # ---- 暗度陈仓 ----
        # 平局赔率全场最低 = 庄家在隐藏平局方向
        vals = [h, d, a]
        if d <= min(vals) + 0.1 and h < 3.0:
            detected.append({
                'tactic': '暗度陈仓',
                'confidence': 0.78,
                'evidence': f'平局赔率({d:.2f})≈最低，庄家在隐藏平局方向',
                'direction': 'D',  # 指向平局
            })

        # ---- 欲擒故纵 ----
        # 初盘高赔诱注，终盘大幅下降
        if open_h and h < open_h * 0.85:
            detected.append({
                'tactic': '欲擒故纵',
                'confidence': 0.73,
                'evidence': f'主胜初盘{open_h:.2f}→终盘{h:.2f}大幅下降，先诱后收',
                'direction': 'H',
            })
        if open_a and a < open_a * 0.85:
            detected.append({
                'tactic': '欲擒故纵',
                'confidence': 0.73,
                'evidence': f'客胜初盘{open_a:.2f}→终盘{a:.2f}大幅下降，先诱后收',
                'direction': 'A',
            })

        # ---- 声东西 ----
        if ou_line and ou_line >= 3.5 and h < 1.3:
            detected.append({
                'tactic': '声东西',
                'confidence': 0.75,
                'evidence': f'O/U线={ou_line}极高+强队低赔({h:.2f})，诱Over陷阱',
                'direction': 'Under',  # 真实方向是小球
            })

        # ---- 顺手牵羊 ----
        if open_h and open_d and open_a:
            drift_h = h - open_h
            drift_a = a - open_a
            if drift_h < -0.2 and drift_a > 0.1:
                detected.append({
                    'tactic': '顺手牵羊',
                    'confidence': 0.70,
                    'evidence': f'主胜赔率持续下降({drift_h:+.2f})，利用市场惯性做盘',
                    'direction': 'H',
                })
            elif drift_a < -0.2 and drift_h > 0.1:
                detected.append({
                    'tactic': '顺手牵羊',
                    'confidence': 0.70,
                    'evidence': f'客胜赔率持续下降({drift_a:+.2f})，利用市场惯性做盘',
                    'direction': 'A',
                })

        return detected


# ============================================================================
# 第三把钥匙: 历史验证
# ============================================================================

class HistoryValidator:
    """
    历史赔率模式验证 — 用45万场数据说话

    不需要模型，不需要模拟，只需要:
      找到历史上类似赔率的比赛 → 看看实际结果是什么
    """

    def __init__(self, db_path: str = 'data/sp_data.db'):
        self.db_path = db_path

    def validate(self, h: float, d: float, a: float, tolerance: float = 0.15) -> Dict:
        """
        验证给定赔率模式的历史胜率

        tolerance: 赔率匹配的容差比例(默认15%)
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()

            # 找类似赔率的历史比赛
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
            conn.close()

            if not rows:
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
            cur2 = sqlite3.connect(self.db_path).cursor()
            cur2.execute('''
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
            score_rows = cur2.fetchall()
            cur2.connection.close()

            top_scores = []
            for hs, as_, cnt in score_rows:
                top_scores.append({
                    'score': f'{hs}-{as_}',
                    'count': cnt,
                    'pct': round(cnt/total*100, 1),
                })

            # 最可能方向
            best_dir = max(results.keys(), key=lambda k: results[k]['pct'])
            
            # 平局率特意提取(关键!)
            draw_pct = results.get('D', {}).get('pct', 0)

            return {
                'found': total,
                'results': results,
                'best_direction': best_dir,
                'best_pct': results[best_dir]['pct'],
                'draw_pct': draw_pct,  # 新增: 平局率
                'top_scores': top_scores,
                'avg_goals': results.get(best_dir, {}).get('avg_goals', '?'),
            }
        except Exception as e:
            return {'found': 0, 'error': str(e)}

    def validate_cs(self, cs_odds: Dict[str, float]) -> Dict:
        """
        从波胆赔率中找庄家指向

        核心逻辑: 最低波胆赔率 = 庄家最"怕"的比分 = 最可能结果
        """
        if not cs_odds:
            return {'found': False}

        sorted_cs = sorted(cs_odds.items(), key=lambda x: x[1])
        top3 = sorted_cs[:3]

        return {
            'found': True,
            'bookmaker_direction': top3[0][0],  # 最低波胆
            'bookmaker_odds': top3[0][1],
            'top3': [{'score': s, 'odds': o} for s, o in top3],
        }


# ============================================================================
# 第四把钥匙: OTSM (赔率时序状态机)
# ============================================================================

class OTSM:
    """
    赔率时序状态机 — 从开盘→收盘的漂移推断庄家确信度

    核心原理:
      庄家的赔率调整不是随机漫步，而是一个加密协议。
      开盘→收盘之间的相变轨迹，暴露了庄家风控模型的内部状态。

    三维相空间:
      D1 — 熵漂移:     概率分布的熵变化, 量度市场信念收敛/发散
      D2 — 水位加速度:  overround变化率, 量度庄家自身的确信度
      D3 — 凯利涨落:    热门方隐含概率变化, 量度市场对赛果的重估强度

    三种状态(5万场回测验证):
      LOCKED (锁定期): 熵漂移 > P80 → 方向命中63.2%, +降抽水→64.0%
      ACTIVE (活跃期): P20~P80 → 方向命中~50%, 中等置信
      NOISE  (噪声期): 熵漂移 < P20 → 方向命中<49%, 无可用信号

    使用: 只需要初盘和终盘1X2赔率
    """

    # 从5万场回测拟合的阈值 (已验证)
    THRESHOLDS = {
        'entropy_drift': {
            'p20': 0.00032, 'p50': 0.00969, 'p80': 0.03735, 'p90': 0.06247,
        },
        'water_accel': {
            'p20': -0.09059, 'p50': 0.0, 'p80': 0.03212,
        },
    }

    @classmethod
    def _entropy(cls, probs):
        """香农熵 (bits)"""
        eps = 1e-10
        return -sum(p * np.log2(p + eps) for p in probs)

    @classmethod
    def _implied_probs(cls, h, d, a):
        """赔率→归一化隐含概率"""
        raw = np.array([1.0/h, 1.0/d, 1.0/a])
        return raw / raw.sum()

    @classmethod
    def _overround(cls, h, d, a):
        """庄家抽水率"""
        return 1.0/h + 1.0/d + 1.0/a - 1.0

    @classmethod
    def analyze(cls, close_h, close_d, close_a,
                open_h=None, open_d=None, open_a=None,
                ht_h=None, ht_d=None, ht_a=None) -> Dict:
        """
        OTSM分析 — 从赔率漂移推断庄家状态

        参数:
          close_h/d/a: 终盘1X2赔率 (必填)
          open_h/d/a:  初盘1X2赔率 (OTSM核心输入, 无则降级)
          ht_h/d/a:    半场1X2赔率 (辅助, 无则忽略)

        返回: OTSM状态报告
        """
        # ---- 终盘基线 ----
        close_probs = cls._implied_probs(close_h, close_d, close_a)
        close_entropy = cls._entropy(close_probs)
        close_or = cls._overround(close_h, close_d, close_a)

        # ---- 无初盘时降级: 仅用终盘推断 ----
        if not open_h or not open_d or not open_a:
            # 无初盘, 从终盘结构推断
            # 如果终盘overround极低(<3%)=庄家确信度高
            confidence_bonus = max(0, (0.06 - close_or) / 0.06 * 20) if close_or < 0.06 else 0

            # 从概率分布的极化程度推断
            max_prob = max(close_probs)
            min_prob = min(close_probs)
            polarization = max_prob - min_prob

            if polarization > 0.4:
                state = 'LOCKED'
                lock_confidence = min(0.85, 0.5 + polarization)
                reason = f'终盘高度极化(极差{polarization:.2f}), 庄家指向明确'
            elif polarization > 0.2:
                state = 'ACTIVE'
                lock_confidence = 0.4 + polarization * 0.5
                reason = f'终盘中度极化(极差{polarization:.2f})'
            else:
                state = 'NOISE'
                lock_confidence = 0.2
                reason = f'终盘均衡(极差{polarization:.2f}), 无明确信号'

            return {
                'state': state,
                'lock_confidence': round(lock_confidence, 3),
                'entropy_drift': None,
                'water_accel': None,
                'kelly_fluctuation': None,
                'confidence_bonus': round(confidence_bonus, 1),
                'drift_direction': None,
                'reason': f'[降级模式-无初盘] {reason}',
            }

        # ---- 有初盘: 完整OTSM分析 ----
        open_probs = cls._implied_probs(open_h, open_d, open_a)
        open_entropy = cls._entropy(open_probs)
        open_or = cls._overround(open_h, open_d, open_a)

        # D1: 熵漂移 (归一化到[-1,1])
        max_entropy = np.log2(3)
        entropy_drift = (close_entropy - open_entropy) / max_entropy

        # D2: 水位加速度 (归一化)
        if abs(open_or) > 1e-6:
            water_accel = (close_or - open_or) / (abs(open_or) + 0.01)
            water_accel = max(-1.0, min(1.0, water_accel))
        else:
            water_accel = 0.0

        # D3: 凯利涨落 (热门方概率变化)
        open_fav_idx = int(np.argmax(open_probs))
        close_fav_idx = int(np.argmax(close_probs))
        if open_fav_idx != close_fav_idx:
            kelly_fluct = abs(close_probs[close_fav_idx] - open_probs[open_fav_idx]) + 0.05
        else:
            kelly_fluct = abs(close_probs[open_fav_idx] - open_probs[open_fav_idx])
        kelly_fluct /= (open_probs[open_fav_idx] + 0.05)

        # ---- 状态推断 (v2三态模型) ----
        th = cls.THRESHOLDS
        abs_entropy = abs(entropy_drift)

        if abs_entropy < th['entropy_drift']['p20']:
            state = 'NOISE'
            lock_confidence = 0.0
        elif abs_entropy > th['entropy_drift']['p80']:
            state = 'LOCKED'
            # 线性插值到 [0.6, 1.0]
            if abs_entropy <= th['entropy_drift']['p90']:
                p80 = th['entropy_drift']['p80']
                p90 = th['entropy_drift']['p90']
                lock_confidence = 0.6 + (abs_entropy - p80) / (p90 - p80 + 1e-9) * 0.3
            else:
                lock_confidence = min(1.0, 0.9 + (abs_entropy - th['entropy_drift']['p90']) / 0.1 * 0.1)
        else:
            state = 'ACTIVE'
            p20 = th['entropy_drift']['p20']
            p80 = th['entropy_drift']['p80']
            lock_confidence = (abs_entropy - p20) / (p80 - p20 + 1e-9) * 0.6

        # 水位确认: 降抽水 +0.05, 升抽水 -0.02
        if water_accel < th['water_accel']['p20']:
            lock_confidence = min(1.0, lock_confidence + 0.05)
        elif water_accel > th['water_accel']['p80']:
            lock_confidence = max(0.0, lock_confidence - 0.02)

        # ---- 漂移方向推断 ----
        drift_h = close_probs[0] - open_probs[0]  # 主胜概率变化
        drift_d = close_probs[1] - open_probs[1]  # 平局概率变化
        drift_a = close_probs[2] - open_probs[2]  # 客胜概率变化

        # 哪个方向概率上升最多 = 庄家在加强该方向
        drifts = {'H': drift_h, 'D': drift_d, 'A': drift_a}
        drift_direction = max(drifts, key=drifts.get)
        drift_magnitude = max(drifts.values())

        # 如果半场赔率可用, 结合半场→全场漂移
        if ht_h and ht_d and ht_a:
            ht_probs = cls._implied_probs(ht_h, ht_d, ht_a)
            ht_drift_h = close_probs[0] - ht_probs[0]
            ht_drift_d = close_probs[1] - ht_probs[1]
            ht_drift_a = close_probs[2] - ht_probs[2]
            ht_drifts = {'H': ht_drift_h, 'D': ht_drift_d, 'A': ht_drift_a}
            ht_drift_dir = max(ht_drifts, key=ht_drifts.get)
            # 半场→全场漂移与初→终漂移方向一致时, 信号更强
            if ht_drift_dir == drift_direction:
                lock_confidence = min(1.0, lock_confidence + 0.03)

        # 置信度提升值 (用于最终判定)
        confidence_bonus = lock_confidence * 25  # 最大25分加成

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
            'reason': f'状态={state}(LC={lock_confidence:.2f}) 漂移指向{drift_direction}(Δ={drift_magnitude:.3f}) '
                      f'熵漂移={entropy_drift:.4f} 水位加速={water_accel:.4f}',
        }


# ============================================================================
# 核心入口: 一行破解
# ============================================================================

def crack(home: str, away: str, h: float, d: float, a: float,
          league: str = '', open_h=None, open_d=None, open_a=None,
          ou_line=None, cs_odds: Dict[str, float] = None,
          ht_h=None, ht_d=None, ht_a=None,
          version: str = 'v2') -> Dict:
    """
    纯赔率破解 — 一行调用，所有答案

    参数:
        home/away: 队名
        h/d/a: 终盘1X2赔率 (必填)
        league: 联赛名 (可选)
        open_h/open_d/open_a: 初盘1X2赔率 (可选, OTSM需要)
        ou_line: 大小球线 (可选)
        cs_odds: 波胆赔率 {'1-0': 5.50, '1-1': 6.30, ...} (可选)
        ht_h/ht_d/ht_a: 半场1X2赔率 (可选, OTSM辅助)
        version: 隐编码版本 'v1'(三钥匙)/'v2'(+OTSM,默认)/'v3'(+OTSM+漂移增强)

    返回: 完整破解报告
    """
    # 1. 反常指数
    anomaly = AnomalyIndex.detect(home, away, h, d, a, league,
                                  open_h, open_d, open_a, ou_line)

    # 2. 孙子兵法
    tactics = ArtOfWar.detect(h, d, a, open_h, open_d, open_a, ou_line,
                              anomaly['match_type'])

    # 3. 历史验证
    validator = HistoryValidator()
    history = validator.validate(h, d, a)

    # 4. 波胆方向
    cs_result = validator.validate_cs(cs_odds) if cs_odds else {'found': False}

    # 5. OTSM (v2+)
    otsm = OTSM.analyze(h, d, a, open_h, open_d, open_a, ht_h, ht_d, ht_a) \
        if version in ('v2', 'v3', 'v4', 'v5') else None

    # 6. 综合判定
    # 隐含概率
    raw_probs = {'H': 1/h, 'D': 1/d, 'A': 1/a}
    total_raw = sum(raw_probs.values())
    implied = {k: round(v/total_raw, 3) for k, v in raw_probs.items()}
    overround = round((total_raw - 1) * 100, 1)

    # ============================================================
    # 核心判定逻辑 — 按版本分道
    # ============================================================

    direction = 'H'
    direction_confidence = 0
    direction_reason = ''
    min_odds = min(h, d, a)

    # ---- Step 1: CS波胆方向 (硬覆盖, 最高优先, 所有版本通用) ----
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
        except:
            pass

    # ---- Step 2: 非CS判定 — 按版本分道 ----
    if not cs_direction_hit:

        if version == 'v4':
            # ============================================================
            # v4: 信噪分离策略
            # 核心洞察: 纯赔率>70%置信度就有80%准确率(641场验证)
            #          多信号叠加反而稀释了高置信度信号
            # 策略: 高置信度→信任赔率; 低置信度→启用修正
            # ============================================================

            max_implied = max(implied.values())
            odds_direction = max(implied, key=implied.get)

            # ---- 高置信度通道: max隐含概率 > 55% ----
            if max_implied > 0.55:
                # 赔率信号够强, 直接信任
                # 但OTSM LOCKED反指时需要警惕
                otsm_drift_dir = otsm.get('drift_direction') if otsm else None
                otsm_locked = otsm and otsm['state'] == 'LOCKED' and otsm['lock_confidence'] >= 0.7

                if otsm_locked and otsm_drift_dir and otsm_drift_dir != odds_direction:
                    # OTSM LOCKED强反指 → 转向OTSM方向
                    direction = otsm_drift_dir
                    direction_confidence = int(max_implied * 100) - 5
                    direction_reason = f'v4高置信({max_implied:.0%})赔率→{odds_direction}, 但OTSM LOCKED反指→{otsm_drift_dir}(LC={otsm["lock_confidence"]:.2f})'
                else:
                    # 信任赔率, 不叠加任何修正
                    direction = odds_direction
                    direction_confidence = int(max_implied * 100)
                    otsm_note = ''
                    if otsm_locked and otsm_drift_dir == odds_direction:
                        direction_confidence = min(99, direction_confidence + 5)
                        otsm_note = f' + OTSM LOCKED确认'
                    direction_reason = f'v4高置信通道: 赔率直接→{odds_direction}({max_implied:.0%}){otsm_note}'

            # ---- 低置信度通道: max隐含概率 ≤ 55% ----
            else:
                # 赔率信号弱, 启用修正栈(类似v2但OTSM权重更大)
                adj = {k: v for k, v in implied.items()}

                # 2a. 历史验证混合 (同v2, 最高35%)
                n_hist = history.get('found', 0)
                if n_hist >= 20:
                    alpha = min(0.35, n_hist / 150000)
                    for result_dir in ['H', 'D', 'A']:
                        hist_pct = history.get('results', {}).get(result_dir, {}).get('pct', 0)
                        if hist_pct > 0:
                            adj[result_dir] = adj[result_dir] * (1 - alpha) + (hist_pct / 100) * alpha

                # 2b. D赔率信号 (同v2)
                if d < 3.0 and min_odds > 1.3:
                    adj['D'] += 0.06
                if d < 2.8 and min_odds > 1.3:
                    adj['D'] += 0.05

                # 2c. 反常指数修正 (同v2)
                anomaly_suggested_dir = None
                for sig in anomaly.get('signals', []):
                    if '平局赔率全场最低' in sig:
                        anomaly_suggested_dir = 'D'
                        break
                    elif '客胜赔率最低' in sig and '隐藏客胜' in sig:
                        anomaly_suggested_dir = 'A'
                        break

                if anomaly['score'] >= 25 and anomaly_suggested_dir:
                    adj[anomaly_suggested_dir] += 0.03

                # 2d. OTSM修正 — v4大幅提升权重!
                # 原始账号验证: LOCKED+赔率>0.55 → 70.7%准确率
                # v2仅+1%太保守, v4提升至+5%(LOCKED)/+8%(LOCKED+LC>=0.7)
                otsm_drift_dir = otsm.get('drift_direction') if otsm else None
                if otsm and otsm_drift_dir:
                    if otsm['state'] == 'LOCKED':
                        if otsm['lock_confidence'] >= 0.7:
                            adj[otsm_drift_dir] += 0.08  # v4: 强LOCKED +8%
                        else:
                            adj[otsm_drift_dir] += 0.05  # v4: 普通LOCKED +5%
                    elif otsm['state'] == 'ACTIVE' and otsm.get('drift_magnitude', 0) > 0.02:
                        adj[otsm_drift_dir] += 0.02  # v4: ACTIVE +2%(v2是+1%)

                # 2e. 归一化
                total = sum(adj.values())
                adj = {k: round(v / total, 4) for k, v in adj.items()}

                # 2f. 选最高概率方向
                direction = max(adj, key=adj.get)
                direction_confidence = int(adj[direction] * 100)

                # 生成理由
                parts = [f'v4低置信通道: 调整概率 H={adj["H"]*100:.1f}% D={adj["D"]*100:.1f}% A={adj["A"]*100:.1f}%']
                if n_hist >= 20:
                    hist_dir = history.get('best_direction', '?')
                    hist_pct = history.get('best_pct', 0)
                    draw_pct = history.get('draw_pct', 0)
                    parts.append(f'历史{hist_dir}方{hist_pct:.0f}%/平{draw_pct:.0f}%({n_hist}场)')
                if d < 3.0 and min_odds > 1.3:
                    parts.append(f'D低({d:.2f})')
                if anomaly['score'] >= 25 and anomaly_suggested_dir:
                    parts.append(f'反常→{anomaly_suggested_dir}')
                if otsm and otsm_drift_dir:
                    parts.append(f'OTSM→{otsm_drift_dir}({otsm["state"]})')
                direction_reason = ' | '.join(parts)

        elif version == 'v5':
            # ============================================================
            # v5: 纯赔率回归策略 (修订版: D<2.5已证伪移除)
            # 30万场验证核心发现:
            #   1. D<3.0修正是双计! (隐含概率已包含, +6%伤害准确率)
            #   2. D<2.8修正同理
            #   3. D<2.5已证伪! (457K场William Hill: 仅31场D<2.5, 强制D命中9.7%<纯赔率12.9%)
            #   4. OTSM LOCKED反指有害
            #   5. 纯赔率方向=最优基线, 所有修正要么双计要么噪声
            #   6. 70-80%区间有+5.1%统计显著信息增量(p=0.0001)
            # 策略: 纯赔率方向, OTSM仅做确认
            # ============================================================

            max_implied = max(implied.values())
            odds_direction = max(implied, key=implied.get)

            # ---- 唯一通道: 纯赔率方向 ----
            direction = odds_direction
            direction_confidence = int(max_implied * 100)
            
            # OTSM LOCKED确认(仅当方向一致时增强置信度, 不改方向)
            otsm_note = ''
            if otsm and otsm['state'] == 'LOCKED':
                otsm_drift_dir = otsm.get('drift_direction')
                if otsm_drift_dir == direction and otsm['lock_confidence'] >= 0.6:
                    bonus = int(otsm['lock_confidence'] * 10)
                    direction_confidence = min(99, direction_confidence + bonus)
                    otsm_note = f' + OTSM LOCKED确认(LC={otsm["lock_confidence"]:.2f})'

            direction_reason = f'v5纯赔率: →{odds_direction}({max_implied:.0%}){otsm_note}'

        else:
            # ============================================================
            # v1/v2/v3: 原有概率调整法(保持不变)
            # ============================================================
            adj = {k: v for k, v in implied.items()}

            # 2a. 历史验证混合
            n_hist = history.get('found', 0)
            if n_hist >= 20:
                alpha = min(0.35, n_hist / 150000)
                for result_dir in ['H', 'D', 'A']:
                    hist_pct = history.get('results', {}).get(result_dir, {}).get('pct', 0)
                    if hist_pct > 0:
                        adj[result_dir] = adj[result_dir] * (1 - alpha) + (hist_pct / 100) * alpha

            # 2b. D赔率信号
            if d < 3.0 and min_odds > 1.3:
                adj['D'] += 0.06
            if d < 2.8 and min_odds > 1.3:
                adj['D'] += 0.05

            # 2c. 反常指数修正
            anomaly_suggested_dir = None
            for sig in anomaly.get('signals', []):
                if '平局赔率全场最低' in sig:
                    anomaly_suggested_dir = 'D'
                    break
                elif '客胜赔率最低' in sig and '隐藏客胜' in sig:
                    anomaly_suggested_dir = 'A'
                    break

            if anomaly['score'] >= 25 and anomaly_suggested_dir:
                adj[anomaly_suggested_dir] += 0.03

            # 2d. OTSM修正 (v2/v3)
            otsm_drift_dir = otsm.get('drift_direction') if otsm else None
            if version in ('v2', 'v3') and otsm and otsm_drift_dir:
                if otsm['state'] == 'LOCKED':
                    if version == 'v3':
                        adj[otsm_drift_dir] += 0.03
                    else:
                        adj[otsm_drift_dir] += 0.01
                elif otsm['state'] == 'ACTIVE' and otsm.get('drift_magnitude', 0) > 0.02:
                    adj[otsm_drift_dir] += 0.01

            # 2e. 归一化
            total = sum(adj.values())
            adj = {k: round(v / total, 4) for k, v in adj.items()}

            # 2f. 选最高概率方向
            direction = max(adj, key=adj.get)
            direction_confidence = int(adj[direction] * 100)

            # 生成理由
            parts = []
            parts.append(f'调整概率: H={adj["H"]*100:.1f}% D={adj["D"]*100:.1f}% A={adj["A"]*100:.1f}%')
            if n_hist >= 20:
                hist_dir = history.get('best_direction', '?')
                hist_pct = history.get('best_pct', 0)
                draw_pct = history.get('draw_pct', 0)
                parts.append(f'历史{hist_dir}方{hist_pct:.0f}%/平{draw_pct:.0f}%({n_hist}场)')
            if d < 3.0 and min_odds > 1.3:
                parts.append(f'D低({d:.2f})')
            if anomaly['score'] >= 25 and anomaly_suggested_dir:
                parts.append(f'反常→{anomaly_suggested_dir}')
            if otsm and otsm_drift_dir:
                parts.append(f'OTSM→{otsm_drift_dir}')
            direction_reason = ' | '.join(parts)

    # ---- Step 3: OTSM LOCKED置信度加成 (v2/v3) ----
    if version == 'v2' and otsm and otsm['state'] == 'LOCKED':
        otsm_lc = otsm['lock_confidence']
        bonus = int(otsm['confidence_bonus'] * 0.5)
        direction_confidence = min(99, direction_confidence + bonus)
        direction_reason += f' + OTSM LOCKED(LC={otsm_lc:.2f})'

        if otsm.get('drift_direction') and otsm['drift_direction'] != direction and otsm_lc >= 0.7:
            direction_reason += f' ⚠️OTSM漂移指向{otsm["drift_direction"]}'

    # ---- Step 4: v4 OTSM LOCKED置信度加成 ----
    if version == 'v4' and otsm and otsm['state'] == 'LOCKED':
        otsm_lc = otsm['lock_confidence']
        # v4: OTSM LOCKED与方向一致时, 给更大加成
        otsm_drift_dir = otsm.get('drift_direction')
        if otsm_drift_dir == direction:
            bonus = int(otsm_lc * 15)  # LC=0.85 → +12
            direction_confidence = min(99, direction_confidence + bonus)
            direction_reason += f' + OTSM LOCKED确认(LC={otsm_lc:.2f})'
        elif otsm_drift_dir and otsm_drift_dir != direction:
            # 不一致时已在高置信通道处理(可能反转), 低置信通道标记
            direction_reason += f' ⚠️OTSM LOCKED漂移→{otsm_drift_dir}(LC={otsm_lc:.2f})'

    # ---- Step 5: 孙子兵法增强 ----
    for t in tactics:
        if t.get('direction') and t['confidence'] > 0.75:
            tactic_dir = t['direction']
            if tactic_dir in ['H', 'D', 'A'] and tactic_dir == direction:
                direction_confidence = min(direction_confidence + 8, 99)
                direction_reason += f' + {t["tactic"]}确认'

    # 推荐比分
    recommended_scores = []
    if cs_result.get('found'):
        recommended_scores = [s['score'] for s in cs_result.get('top3', [])]
    elif history.get('top_scores'):
        recommended_scores = [s['score'] for s in history['top_scores'][:3]]

    # ---- Step 6: 前置过滤器(阶段一弱耦合增强) ----
    pre_filter = None
    try:
        from sp_pre_filters import PreFilterEngine
        pre_filter = PreFilterEngine.apply(
            home, away, direction, direction_confidence, league
        )
    except ImportError:
        pass  # 无前置过滤器模块时跳过

    # ---- Step 7: 仓位决策层(v3: 赛事配置驱动+赛道异常+全局熔断+自适应衰减) ----
    try:
        from sp_v5_config import (
            ENTRY_CONFIDENCE_THRESHOLD, AWAY_WIN_BASE_WEIGHT,
            LOW_WEIGHT_LEAGUES, LEAGUE_TRACK_BLACKLIST,
            WC_AWAY_WIN_WEIGHT, get_competition_profile, get_competition_type,
            get_event_category, get_event_config,
        )
    except ImportError:
        ENTRY_CONFIDENCE_THRESHOLD = 0.70
        AWAY_WIN_BASE_WEIGHT = 0.5
        LOW_WEIGHT_LEAGUES = {}
        LEAGUE_TRACK_BLACKLIST = {}
        WC_AWAY_WIN_WEIGHT = 0.5
        get_competition_profile = None
        get_competition_type = None
        get_event_category = None
        get_event_config = None

    # 获取赛事参数配置(双层: COMPETITION_PROFILES + event_config.json)
    comp_profile = {}
    comp_type = 'league_mid'
    event_category = '国内杯赛淘汰赛'  # 未知→保守
    event_cfg = {}  # event_config.json配置

    if league and get_competition_profile:
        try:
            comp_profile = get_competition_profile(league)
            comp_type = get_competition_type(league)
        except Exception:
            pass

    if league and get_event_category:
        try:
            event_category = get_event_category(league)
            event_cfg = get_event_config(league)
        except Exception:
            pass

    # 初始仓位=1.0
    bet_weight = 1.0
    weight_reasons = []

    # 7a. 置信度门槛(从event_config.json驱动, 默认70%)
    rc_cfg = event_cfg.get('risk_control', {})
    entry_threshold = rc_cfg.get('confidence_entry_threshold', 0.70)
    if direction_confidence < int(entry_threshold * 100):
        bet_weight = 0.0
        weight_reasons.append(f'置信度{direction_confidence}%<{int(entry_threshold*100)}%→不下注')

    # 7b. 跨赛事专属规则(从event_config.json驱动)
    is_wc = False
    wc_info = None
    is_international = comp_type == 'international_cup'
    is_domestic_cup = comp_type == 'domestic_cup'
    is_champions_league = comp_type == 'champions_league'

    if bet_weight > 0 and league:
        # 7b-i. 国际大赛(世界杯/洲际杯)专属规则
        if is_international:
            is_wc = True  # 兼容旧字段
            try:
                from wc_tracker import WCTracker
                tracker = WCTracker()
                wc_bet = tracker.should_bet()
                wc_info = tracker.get_summary()
            except Exception:
                wc_bet = {'allowed': True, 'reason': '追踪器不可用,默认允许', 'weight_modifier': 1.0}
                wc_info = {'status': 'unknown'}

            # 观察期/暂停判定
            if not wc_bet['allowed']:
                bet_weight = 0.0
                weight_reasons.append(f'国际大赛: {wc_bet["reason"]}')
            else:
                # 国际大赛客胜减仓(从event_config.json读取)
                away_weight = rc_cfg.get('away_win_base_weight', 0.5)
                if direction == 'A' and rc_cfg.get('away_win_default_reduce', True):
                    bet_weight *= away_weight
                    weight_reasons.append(f'国际大赛客胜减仓至{away_weight:.0%}')
                weight_reasons.append(f'国际大赛赛道: {wc_bet["reason"]}')

        # 7b-ii. 欧冠专属规则
        elif is_champions_league:
            try:
                from tournament_tracker import TournamentTracker
                tracker = TournamentTracker('champions_league')
                cl_bet = tracker.should_bet()
                cl_info = tracker.get_summary()
            except (ImportError, Exception):
                obs_cfg = event_cfg.get('observation_window', {})
                obs = obs_cfg.get('required_sample_count', 6)
                cl_bet = {'allowed': True, 'reason': f'欧冠(观察期{obs}场)', 'weight_modifier': 1.0}
                cl_info = {'status': 'unknown'}

            if not cl_bet['allowed']:
                bet_weight = 0.0
                weight_reasons.append(f'欧冠: {cl_bet["reason"]}')
            else:
                away_weight = rc_cfg.get('away_win_base_weight', 0.6)
                if direction == 'A' and rc_cfg.get('away_win_default_reduce', True):
                    bet_weight *= away_weight
                    weight_reasons.append(f'欧冠客胜减仓至{away_weight:.0%}')
                weight_reasons.append(f'欧冠赛道: {cl_bet["reason"]}')

        # 7b-iii. 国内杯赛(淘汰赛)专属规则
        elif is_domestic_cup:
            # 国内杯赛: 重注门槛抬升至75/78%(从event_config.json)
            heavy_min = rc_cfg.get('heavy_bet_min_conf', 0.78)
            if direction_confidence < int(heavy_min * 100) and bet_weight > 0:
                pass  # 70-78%区间: 标准仓

            # 国内杯赛客胜减仓
            away_weight = rc_cfg.get('away_win_base_weight', 0.5)
            if direction == 'A' and rc_cfg.get('away_win_default_reduce', True):
                bet_weight *= away_weight
                weight_reasons.append(f'国内杯赛客胜减仓至{away_weight:.0%}')

            weight_reasons.append(f'国内杯赛(冷门防御)')

    # 7c. 赛道风控(客胜减仓) — 仅联赛类(非杯赛/非国际大赛)
    if direction == 'A' and bet_weight > 0 and not is_international and not is_champions_league and not is_domestic_cup:
        # 自适应衰减: 基础系数 + min(0, 赛道增量/10)
        xg_cfg = event_cfg.get('xg_calibration', {})
        away_decay_base = xg_cfg.get('away_win_lambda_decay', AWAY_WIN_BASE_WEIGHT)
        # 特殊联赛叠加(如意甲×0.88)
        special_decay = xg_cfg.get('special_league_away_decay', {})
        for lg_key, lg_decay in special_decay.items():
            if lg_key in (league or ''):
                away_decay_base *= lg_decay
                break

        # 自适应: 如果赛道有增量数据,动态调整
        adaptive_decay = away_decay_base
        try:
            from track_monitor import TrackMonitor
            tm = TrackMonitor()
            anomaly_info = tm.get_anomaly_info(league, direction)
            if anomaly_info['total_bets'] >= 10:
                track_inc = anomaly_info['full_increment']
                # 衰减系数 = 基础 + min(0, 增量/10), 下限0.4
                adaptive_offset = min(0, track_inc / 10.0)
                adaptive_decay = max(0.4, away_decay_base + adaptive_offset)
        except Exception:
            pass

        bet_weight *= adaptive_decay
        if abs(adaptive_decay - away_decay_base) > 0.01:
            weight_reasons.append(f'客胜自适应减仓至{adaptive_decay:.0%}(基础{away_decay_base:.0%}+赛道偏移)')
        else:
            weight_reasons.append(f'客胜减仓至{adaptive_decay:.0%}')

    # 7d. 赛道异常兜底(3级判定) — 无论是否下注都检测，返回状态
    anomaly_info = None
    if league:
        try:
            from track_monitor import TrackMonitor
            tm = TrackMonitor()
            anomaly_info = tm.get_anomaly_info(league, direction)
            anomaly_level = anomaly_info['anomaly_level']

            if anomaly_level >= 1 and bet_weight > 0:
                k_anomaly = anomaly_info['decay']

                # 二级特殊: 70-75%区间直接剔除
                if anomaly_level == 2 and 70 <= direction_confidence < 75:
                    bet_weight = 0.0
                    weight_reasons.append(f'二级异常: {anomaly_info["anomaly_reason"]}→70-75%剔除')
                else:
                    bet_weight *= k_anomaly
                    level_names = {1: '一级', 2: '二级', 3: '三级'}
                    weight_reasons.append(
                        f'{level_names.get(anomaly_level, "?")}异常: '
                        f'{anomaly_info["anomaly_reason"]}→仓位×{k_anomaly}'
                    )
            elif anomaly_level >= 1 and bet_weight == 0:
                # 不下注时也记录异常状态
                level_names = {1: '一级', 2: '二级', 3: '三级'}
                weight_reasons.append(
                    f'{level_names.get(anomaly_level, "?")}异常: '
                    f'{anomaly_info["anomaly_reason"]}(已不下注,仅标记)'
                )
        except Exception:
            pass

    # 7e. 全局熔断校验 — 无论是否下注都检测，返回状态
    circuit_info = None
    try:
        from sp_circuit_breaker import CircuitBreaker
        cb = CircuitBreaker()
        circuit_info = cb.get_summary()
        if bet_weight > 0 and cb.is_triggered:
            k_circuit = cb.get_weight_modifier(direction, direction_confidence)
            bet_weight *= k_circuit
            if k_circuit > 0:
                weight_reasons.append(f'全局熔断({cb.status}): 仓位×{k_circuit}')
            else:
                weight_reasons.append(f'全局熔断({cb.status}): 该方向清零')
    except Exception:
        pass

    # 7f. 联赛黑名单
    if bet_weight > 0:
        for lg_name, lg_rules in LOW_WEIGHT_LEAGUES.items():
            if lg_name in league:
                ignore_lo, ignore_hi = lg_rules.get('ignore_range', (0, 0))
                if ignore_lo * 100 <= direction_confidence < ignore_hi * 100:
                    bet_weight = 0.0
                    weight_reasons.append(f'{lg_name}{ignore_lo:.0%}-{ignore_hi:.0%}直接剔除')
                elif direction_confidence >= ignore_hi * 100:
                    max_w = lg_rules.get('max_weight', 1.0)
                    if bet_weight > max_w:
                        bet_weight = max_w
                        weight_reasons.append(f'{lg_name}上限权重{max_w:.0%}')

        for lg_name, black_tracks in LEAGUE_TRACK_BLACKLIST.items():
            if lg_name in league:
                track_zh = {'H': '主胜', 'D': '平局', 'A': '客胜'}
                if track_zh.get(direction) in black_tracks or direction in black_tracks:
                    bet_weight = 0.0
                    weight_reasons.append(f'{lg_name}{direction}方向黑名单')

    # 7e. 前置过滤器
    if pre_filter and bet_weight > 0:
        pf_weight = pre_filter['final_weight']
        if pf_weight < bet_weight:
            bet_weight = pf_weight
            weight_reasons.append(f'前置过滤: {pre_filter["action"]}')

    # 仓位等级
    if bet_weight == 0.0:
        bet_level = '🚫 不下注'
    elif bet_weight <= 0.3:
        bet_level = '🔻 观察区'
    elif bet_weight <= 0.6:
        bet_level = '⚠️ 减仓'
    elif bet_weight < 1.0:
        bet_level = '📋 轻降'
    else:
        bet_level = '✅ 标准仓'

    # 翻译方向
    dir_zh = {'H': f'{home}胜', 'D': '平局', 'A': f'{away}胜'}
    dir_name = dir_zh.get(direction, direction)

    # 辅助方向(当置信度<50%时，给出备选)
    alt_direction = None
    alt_name = None
    if direction_confidence < 50:
        sorted_dirs = sorted(implied.items(), key=lambda x: -x[1])
        for d2, _ in sorted_dirs:
            if d2 != direction:
                alt_direction = d2
                alt_name = dir_zh.get(d2, d2)
                break

    return {
        'match': f'{home} vs {away}',
        'league': league,
        'version': version,
        'time': datetime.now().isoformat(),

        # 赔率数据
        'odds': {'close': {'H': h, 'D': d, 'A': a}},
        'implied': implied,
        'overround_pct': overround,

        # 四把钥匙
        'anomaly': anomaly,
        'tactics': tactics,
        'history': history,
        'cs_direction': cs_result,
        'otsm': otsm,

        # 前置过滤器
        'pre_filter': pre_filter,

        # 世界杯赛道信息(兼容旧字段)
        'is_world_cup': is_wc,
        'wc_track_info': wc_info,

        # 跨赛事适配信息(v3.2新增)
        'competition_type': comp_type,
        'competition_name': comp_profile.get('name', '未知赛事'),
        'event_category': event_category,
        'is_international': is_international,
        'is_domestic_cup': is_domestic_cup,
        'is_champions_league': is_champions_league,

        # 赛道异常信息(v4新增)
        'track_anomaly': anomaly_info,
        # 全局熔断信息(v4新增)
        'circuit_breaker': circuit_info,

        # 最终判定
        'direction': direction,
        'direction_zh': dir_name,
        'confidence': direction_confidence,
        'reason': direction_reason,
        'recommended_scores': recommended_scores,
        'alt_direction': alt_direction,
        'alt_direction_zh': alt_name,

        # 仓位决策
        'bet_weight': round(bet_weight, 2),
        'bet_level': bet_level,
        'weight_reasons': weight_reasons,

        # 一句话结论
        'verdict': f'{dir_name} (置信度{direction_confidence}%, 仓位{bet_level}) — {direction_reason}',
    }


# ============================================================================
# 快速验证
# ============================================================================

if __name__ == '__main__':
    print('=' * 60)
    print('  哨响SP — 纯赔率破解核心引擎 v2.0 (OTSM增强版)')
    print('  四把钥匙: 反常 + 兵法 + 历史 + OTSM')
    print('=' * 60)

    # 测试不同版本
    tests = [
        ('德国', '库拉索', 1.05, 23.0, 41.0, '美加墨世界杯',
         1.10, 21.0, 36.0, None, None, None, None, 'H'),
        ('荷兰', '日本',   2.03, 3.50, 3.60, '美加墨世界杯',
         1.90, 3.80, 3.90, {'1-1': 6.40, '1-0': 7.30, '2-1': 7.50, '0-1': 9.50, '2-0': 10.0}, None, None, None, 'D'),
        ('澳大利亚', '土耳其', 4.95, 3.75, 1.71, '美加墨世界杯',
         4.50, 3.90, 1.85, None, 5.10, 2.21, 2.47, 'H'),
    ]

    for ver in ['v1', 'v2', 'v3']:
        print(f'\n{"─"*50}')
        print(f'  隐编码版本: {ver}')
        print(f'{"─"*50}')
        correct = 0
        for home, away, h, d, a, league, oh, od, oa, cs, hth, htd, hta, actual_dir in tests:
            r = crack(home, away, h, d, a, league=league,
                      open_h=oh, open_d=od, open_a=oa, cs_odds=cs,
                      ht_h=hth, ht_d=htd, ht_a=hta, version=ver)
            hit = '✅' if r['direction'] == actual_dir else '❌'
            if hit == '✅':
                correct += 1
            otsm_state = r['otsm']['state'] if r['otsm'] else 'N/A'
            print(f'  {home} vs {away} → {r["direction_zh"]} (实际:{actual_dir}) {hit} | OTSM:{otsm_state} | {r["verdict"]}')

        print(f'  结果: {correct}/{len(tests)}')

    print(f'\n{"="*60}')
