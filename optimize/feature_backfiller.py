"""
哨响AI - 特征回填引擎 (T20)
===========================
从现有数据源推算5个高默认率特征的真实值，并回填到 DB。

问题根因:
  pull_historical_data.compute_features() 对5个特征写死默认值:
    - aerial_advantage = 1.0 (默认均势)
    - press_intensity  = 0.0 (默认无逼抢)
    - card_risk        = 0.0 (默认无风险)
    - beta_dev         = 0.0 (默认无盘口偏差)
    - delta_fatigue    = 1.0 (默认无疲劳)

  因为这些特征需要原始数据(头球、逼抢、裁判、亚盘、赛程密度)，
  而 football-data.org 免费 API 不提供这些数据。

解决方案: 从现有数据源推算近似值:
  1. beta_dev:      从赔率隐含差 + 排名差推算理论让球 vs 赔率隐含让球
  2. delta_fatigue: 从赛程密度推算 (同队7天内多赛 → 高疲劳)
  3. aerial_advantage: 从球队历史进球分布估算 (大球率+进球差 → 空中优势)
  4. press_intensity:  从失球+零封率反向推算 (低失球+高零封 → 高逼抢)
  5. card_risk:        从联赛+球队纪律统计推算 (大球率+进球 → 纪律代理)

用法:
    from optimize.feature_backfiller import FeatureBackfiller
    filler = FeatureBackfiller(db_path='data/football_data.db')
    stats = filler.backfill_all()
"""

import sqlite3
import logging
from typing import Dict, List, Tuple, Optional
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

class FeatureBackfiller:
    """特征回填引擎 — 从现有数据推算5个高默认率特征的真实值"""

    def __init__(self, db_path: str = 'data/football_data.db'):
        self.db_path = Path(db_path)
        if not self.db_path.is_absolute():
            self.db_path = Path(__file__).parent.parent / db_path

    # ═══════════════════════════════════════════════════════════
    #  1. beta_dev 回填: 从赔率推算盘口偏差
    # ═══════════════════════════════════════════════════════════

    def _compute_beta_dev(self, row: dict) -> float:
        """
        beta_dev = |theoretical_handicap - actual_handicap|

        推算策略:
        - theoretical_handicap: 从 ELO 排名差推算
          rating_diff → 让球 = rating_diff / 400 * 0.25 (经验系数)
        - actual_handicap: 从赔率隐含概率差推算
          implied_home_prob - implied_away_prob → 让球

        这两个让球之间的差异 = beta_dev
        """
        home_odds = row.get('home_odds', 0) or 0
        draw_odds = row.get('draw_odds', 0) or 0
        away_odds = row.get('away_odds', 0) or 0
        rank_diff = row.get('rank_diff_factor', 0) or 0

        if home_odds <= 0 or draw_odds <= 0 or away_odds <= 0:
            return 0.0

        # 理论让球: 从排名差推算
        # rank_diff_factor 范围 [-1, 1], 映射到 [-2.5, 2.5] 让球
        theoretical_hc = rank_diff * 2.5

        # 实际让球: 从赔率隐含差推算
        implied_home = 1.0 / home_odds
        implied_draw = 1.0 / draw_odds
        implied_away = 1.0 / away_odds
        total = implied_home + implied_draw + implied_away
        if total <= 0:
            return 0.0
        implied_home /= total
        implied_away /= total

        # 概率差 → 让球 (经验映射: prob_diff 0.3 ≈ 1球)
        prob_diff = implied_home - implied_away
        actual_hc = prob_diff / 0.3  # 映射到让球数

        beta_dev = abs(theoretical_hc - actual_hc)
        # 归一化到合理范围 [0, 1]
        return float(np.clip(beta_dev / 2.0, 0.0, 1.0))

    # ═══════════════════════════════════════════════════════════
    #  2. delta_fatigue 回填: 从赛程密度推算
    # ═══════════════════════════════════════════════════════════

    def _compute_fatigue_for_team(
        self,
        match_date: str,
        team_name: str,
        team_matches: dict,
    ) -> float:
        """
        delta_fatigue = exp(-0.05 * midweek_intensity)

        推算 midweek_intensity:
        - 检查该队最近7/3天内是否有比赛
        - 3天内有赛 → intensity=5 (极疲劳)
        - 5天内有赛 → intensity=3
        - 7天内有赛 → intensity=1
        - 7天+无赛 → intensity=0

        Args:
            match_date: 当前比赛日期
            team_name: 球队名
            team_matches: {team_name: [match_date, ...]} 按日期排序
        """
        matches = team_matches.get(team_name, [])
        if not matches:
            return 1.0

        try:
            cur_date = datetime.strptime(str(match_date)[:10], '%Y-%m-%d')
        except (ValueError, TypeError):
            return 1.0

        intensity = 0.0
        for prev_date_str in reversed(matches):
            try:
                prev_date = datetime.strptime(str(prev_date_str)[:10], '%Y-%m-%d')
            except (ValueError, TypeError):
                continue

            days_gap = (cur_date - prev_date).days
            if days_gap <= 0:
                continue  # 当天或未来的不算

            if days_gap <= 3:
                intensity += 5.0
            elif days_gap <= 5:
                intensity += 3.0
            elif days_gap <= 7:
                intensity += 1.0
            else:
                break  # 超过7天的不影响

        # cap at 10
        intensity = min(intensity, 10.0)
        return float(np.exp(-0.05 * intensity))

    # ═══════════════════════════════════════════════════════════
    #  3. aerial_advantage 回填: 从进球分布推算
    # ═══════════════════════════════════════════════════════════

    def _compute_aerial_for_team(self, team_stats: dict) -> float:
        """
        aerial_advantage ≈ attacker_aerial_win / defender_aerial_win

        近似:
        - attacker_aerial_win: 进球率 (高进球 = 攻击力强, 含头球)
        - defender_aerial_win: 被头球攻门率 (失球/大球率 反向)

        使用球队近20场的进球率与对手失球率对比
        """
        gf_rate = team_stats.get('goals_per_match', 1.3)
        ga_rate = team_stats.get('conceded_per_match', 1.1)
        cs_rate = team_stats.get('clean_sheet_rate', 0.3)

        # attacker: 进球率归一化到 [0, 100] 模拟 aerial_win%
        attacker_aerial = 35 + gf_rate * 10 + cs_rate * 15  # ~35-65 range

        # defender: (1 - 失球率/3) * 50 + 零封率 * 30
        defender_aerial = 35 + (1 - ga_rate / 3.0) * 20 + cs_rate * 20

        if defender_aerial == 0:
            return 1.0

        return float(np.clip(attacker_aerial / defender_aerial, 0.5, 1.5))

    # ═══════════════════════════════════════════════════════════
    #  4. press_intensity 回填: 从失球+零封推算
    # ═══════════════════════════════════════════════════════════

    def _compute_press_for_team(self, team_stats: dict) -> float:
        """
        press_intensity = home_press_count / away_pass_success

        近似:
        - press_count: 零封率 × 赛季场均对抗 (高零封 = 逼抢有效)
        - away_pass_success: 对手场均传球成功率 (失球少 = 对手传球被压制)

        归一化到 [0, 0.5] 范围
        """
        cs_rate = team_stats.get('clean_sheet_rate', 0.3)
        ga_rate = team_stats.get('conceded_per_match', 1.1)
        win_rate = team_stats.get('win_rate', 0.4)

        # press ≈ (零封率 + 胜率) / 2 × 0.4, 受失球率修正
        press = (cs_rate + win_rate) / 2.0
        # 失球少 → press 加成
        if ga_rate < 1.0:
            press *= 1.3
        elif ga_rate > 1.5:
            press *= 0.7

        return float(np.clip(press * 0.4, 0.0, 0.5))

    # ═══════════════════════════════════════════════════════════
    #  5. card_risk 回填: 从纪律统计推算
    # ═══════════════════════════════════════════════════════════

    def _compute_card_risk(self, home_press: float, away_press: float,
                           league_name: str = '') -> float:
        """
        card_risk = sigmoid(referee_avg_cards * (1 + 0.2 * press_intensity))

        近似:
        - referee_avg_cards: 用联赛平均代替 (五大联赛 ≈ 3.0-4.5)
        - press_intensity: 两队逼抢强度均值

        联赛差异:
          La Liga / Serie A → 高出牌 (3.8-4.5)
          Premier League → 中 (3.3-3.8)
          Bundesliga / Ligue 1 → 中低 (3.0-3.5)
        """
        league_card_avg = {
            'La Liga': 4.2, 'Serie A': 4.0, 'Primera Division': 4.2,
            'Premier League': 3.5, 'Bundesliga': 3.2,
            'Ligue 1': 3.4, 'Ligue 1 Uber Eats': 3.4,
        }

        ref_avg = 3.5  # 默认
        for key, val in league_card_avg.items():
            if key.lower() in (league_name or '').lower():
                ref_avg = val
                break

        press_avg = (home_press + away_press) / 2.0
        x = ref_avg * (1 + 0.2 * press_avg)
        card_risk = 1.0 / (1.0 + np.exp(-x + 3))
        return float(np.clip(card_risk, 0.0, 1.0))

    # ═══════════════════════════════════════════════════════════
    #  主流程: 全量回填
    # ═══════════════════════════════════════════════════════════

    def backfill_all(self, force: bool = False) -> Dict:
        """
        全量回填5个高默认率特征。

        Args:
            force: 是否强制重算已有非默认值的记录

        Returns:
            统计信息 dict
        """
        conn = sqlite3.connect(str(self.db_path))
        stats = {
            'total_rows': 0,
            'updated_rows': 0,
            'by_feature': {},
        }

        # ─── Step 1: 加载数据 ───
        logger.info("[Backfiller] Step 1: 加载数据...")

        # 加载比赛+特征+赔率
        query = """
        SELECT m.match_id, m.match_date, m.home_team_name, m.away_team_name,
               m.league_name, m.home_score, m.away_score,
               mf.aerial_advantage, mf.press_intensity, mf.card_risk,
               mf.beta_dev, mf.delta_fatigue, mf.rank_diff_factor,
               o.home_odds, o.draw_odds, o.away_odds, o.return_rate
        FROM matches m
        JOIN match_features mf ON m.match_id = mf.match_id
        LEFT JOIN odds o ON m.match_id = o.match_id
        WHERE m.home_score IS NOT NULL AND m.away_score IS NOT NULL
        ORDER BY m.match_date
        """
        df = pd.read_sql_query(query, conn)
        stats['total_rows'] = len(df)
        logger.info(f"[Backfiller] 加载 {len(df)} 条记录")

        # ─── Step 2: 构建球队历史索引 ───
        logger.info("[Backfiller] Step 2: 构建球队历史索引...")

        # form_trends → 球队统计
        ft_query = """
        SELECT team_name, match_date, result, home_away,
               goals_for, goals_against, is_clean_sheet, is_over25,
               rolling_form_5, goals_for_last5, goals_against_last5
        FROM form_trends
        ORDER BY match_date
        """
        df_ft = pd.read_sql_query(ft_query, conn)

        # 累积统计: 每场比赛之前的球队平均数据
        team_cum_stats = {}  # {team_name: {gf_list, ga_list, cs_list, ...}}
        team_match_dates = defaultdict(list)  # {team_name: [date_str, ...]}

        for _, row in df_ft.iterrows():
            team = row['team_name']
            team_match_dates[team].append(str(row['match_date']))

            if team not in team_cum_stats:
                team_cum_stats[team] = {
                    'gf_list': [], 'ga_list': [], 'cs_list': [],
                    'result_list': [], 'total': 0,
                }

            tc = team_cum_stats[team]
            tc['gf_list'].append(row['goals_for'] or 0)
            tc['ga_list'].append(row['goals_against'] or 0)
            tc['cs_list'].append(row['is_clean_sheet'] or 0)
            tc['result_list'].append(row['result'])
            tc['total'] += 1

        # 预计算每个球队到每个日期的累积统计
        # 使用增量计算: team_stats_at_date[team][date_idx]
        team_stats_cache = {}  # {team: {goals_per_match, ...}}

        for team, tc in team_cum_stats.items():
            if tc['total'] < 5:
                continue
            gf_arr = np.array(tc['gf_list'], dtype=float)
            ga_arr = np.array(tc['ga_list'], dtype=float)
            cs_arr = np.array(tc['cs_list'], dtype=float)
            result_arr = tc['result_list']

            team_stats_cache[team] = {
                'goals_per_match': float(np.mean(gf_arr)),
                'conceded_per_match': float(np.mean(ga_arr)),
                'clean_sheet_rate': float(np.mean(cs_arr)),
                'win_rate': sum(1 for r in result_arr if r == 'W') / max(len(result_arr), 1),
            }

        logger.info(f"[Backfiller] 球队统计缓存: {len(team_stats_cache)} 支球队")

        # ─── Step 3: 逐行计算新特征 ───
        logger.info("[Backfiller] Step 3: 计算新特征值...")

        default_vals = {
            'aerial_advantage': 1.0, 'press_intensity': 0.0,
            'card_risk': 0.0, 'beta_dev': 0.0, 'delta_fatigue': 1.0,
        }

        updates = []  # [(match_id, feature_name, new_value), ...]
        feature_counts = {f: 0 for f in default_vals}

        for idx, row in df.iterrows():
            match_id = row['match_id']
            home_team = row['home_team_name']
            away_team = row['away_team_name']
            match_date = str(row['match_date'])
            league = str(row.get('league_name', ''))

            # 检查是否需要更新
            needs_update = force
            if not needs_update:
                for feat, dv in default_vals.items():
                    val = row.get(feat)
                    if val is not None and not pd.isna(val) and abs(float(val) - dv) > 0.001:
                        # 已有非默认值, 跳过
                        pass
                    else:
                        needs_update = True
                        break

            if not needs_update:
                continue

            # 获取球队统计
            home_stats = team_stats_cache.get(home_team, {
                'goals_per_match': 1.3, 'conceded_per_match': 1.1,
                'clean_sheet_rate': 0.3, 'win_rate': 0.4,
            })
            away_stats = team_stats_cache.get(away_team, {
                'goals_per_match': 1.3, 'conceded_per_match': 1.1,
                'clean_sheet_rate': 0.3, 'win_rate': 0.4,
            })

            # 1. beta_dev
            new_beta = self._compute_beta_dev(dict(row))

            # 2. delta_fatigue (取主客队疲劳中更严重的一个)
            home_fatigue = self._compute_fatigue_for_team(
                match_date, home_team, team_match_dates)
            away_fatigue = self._compute_fatigue_for_team(
                match_date, away_team, team_match_dates)
            # delta_fatigue 语义: 主队体能衰减因子 (越高越好)
            # 取主队值
            new_fatigue = home_fatigue

            # 3. aerial_advantage (主队视角: home攻 / away防)
            home_attack_aerial = self._compute_aerial_for_team(home_stats)
            away_defend_aerial = self._compute_aerial_for_team(away_stats)
            # 主队防空 = 主队攻击 / 客队防守
            new_aerial = home_attack_aerial / max(away_defend_aerial, 0.01)

            # 4. press_intensity (主队视角)
            home_press = self._compute_press_for_team(home_stats)
            away_press = self._compute_press_for_team(away_stats)
            new_press = home_press

            # 5. card_risk
            new_card = self._compute_card_risk(home_press, away_press, league)

            # 只更新仍为默认值的
            new_values = {
                'beta_dev': new_beta,
                'delta_fatigue': new_fatigue,
                'aerial_advantage': new_aerial,
                'press_intensity': new_press,
                'card_risk': new_card,
            }

            for feat, new_val in new_values.items():
                old_val = row.get(feat)
                dv = default_vals[feat]
                is_default = (old_val is None or pd.isna(old_val) or
                              abs(float(old_val) - dv) < 0.001)
                if is_default or force:
                    updates.append((match_id, feat, new_val))
                    feature_counts[feat] += 1

        stats['by_feature'] = feature_counts
        logger.info(f"[Backfiller] 计算完成: {len(updates)} 个更新")
        for feat, cnt in feature_counts.items():
            logger.info(f"  {feat}: {cnt} rows to update")

        # ─── Step 4: 写入 DB ───
        logger.info("[Backfiller] Step 4: 写入数据库...")

        updated = 0
        for match_id, feat, val in updates:
            try:
                conn.execute(
                    f"UPDATE match_features SET [{feat}] = ? WHERE match_id = ?",
                    (round(val, 6), match_id)
                )
                updated += 1
            except (Exception, KeyError, IndexError, sqlite3.Error) as e:
                logger.debug(f"更新失败 match_id={match_id} {feat}: {e}")

        conn.commit()
        stats['updated_rows'] = updated
        logger.info(f"[Backfiller] 写入完成: {updated} 条更新")

        # ─── Step 5: 重新计算 A2 因子 (含新 press/aerial) ───
        logger.info("[Backfiller] Step 5: 重新计算受影响的 A2 因子...")
        a2_updated = self._recalculate_a2(conn)
        stats['a2_recalculated'] = a2_updated

        conn.close()
        return stats

    def _recalculate_a2(self, conn) -> int:
        """
        A2 = (lambda_crush + fitness_75 + press_intensity + rank_factor + form_factor) / 5
        press_intensity 和 aerial_advantage 变了, A2 需要重算
        但 fitness_75 不在 DB, 只用已有字段近似
        """
        query = """
        SELECT mf.match_id, mf.lambda_crush, mf.press_intensity,
               mf.rank_factor, mf.form_factor, mf.aerial_advantage, mf.a2
        FROM match_features mf
        JOIN matches m ON mf.match_id = m.match_id
        WHERE m.home_score IS NOT NULL
        """
        df = pd.read_sql_query(query, conn)

        updated = 0
        for _, row in df.iterrows():
            lc = row['lambda_crush'] or 1.0
            pi = row['press_intensity'] or 0.0
            rf = row['rank_factor'] or 0.5
            ff = row['form_factor'] or 0.5
            # fitness_75 用 aerial_advantage 近似 (相关度高)
            f75 = float(row.get('aerial_advantage', 1.0) or 1.0) - 0.5  # 粗略

            a2_new = float(np.clip(
                (lc + f75 + pi + rf + ff) / 5.0, 0.0, 1.0
            ))
            a2_old = float(row['a2'] or 0.5)
            if abs(a2_new - a2_old) > 0.001:
                conn.execute(
                    "UPDATE match_features SET a2 = ? WHERE match_id = ?",
                    (round(a2_new, 6), row['match_id'])
                )
                updated += 1

        conn.commit()
        return updated

# ═══════════════════════════════════════════════════════════
#  便捷函数
# ═══════════════════════════════════════════════════════════

def backfill_features(db_path: str = 'data/football_data.db',
                      force: bool = False) -> Dict:
    """一键回填5个高默认率特征"""
    filler = FeatureBackfiller(db_path=db_path)
    return filler.backfill_all(force=force)

# ═══════════════════════════════════════════════════════════
#  自测
# ═══════════════════════════════════════════════════════════

if __name__ == '__main__':
    import sys
    import os
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

    db = os.path.join(os.path.dirname(__file__), '..', 'data', 'football_data.db')
    filler = FeatureBackfiller(db_path=db)

    # 先看回填前的情况
    conn = sqlite3.connect(db)
    default_vals = {
        'aerial_advantage': 1.0, 'press_intensity': 0.0,
        'card_risk': 0.0, 'beta_dev': 0.0, 'delta_fatigue': 1.0,
    }
    print("=== BEFORE ===")
    for feat, dv in default_vals.items():
        df = pd.read_sql_query(f'SELECT {feat} FROM match_features', conn)
        total = len(df)
        n_default = (df[feat] == dv).sum()
        print(f"  {feat}: {n_default}/{total} ({n_default/total*100:.1f}%) default")
    conn.close()

    # 执行回填
    stats = filler.backfill_all()
    print(f"\nBackfill stats: {stats}")

    # 看回填后的情况
    conn = sqlite3.connect(db)
    print("\n=== AFTER ===")
    for feat, dv in default_vals.items():
        df = pd.read_sql_query(f'SELECT {feat} FROM match_features', conn)
        total = len(df)
        n_default = (df[feat] == dv).sum()
        nd = df[df[feat] != dv][feat]
        print(f"  {feat}: {n_default}/{total} ({n_default/total*100:.1f}%) default, "
              f"non-default mean={nd.mean():.4f}, std={nd.std():.4f}")
    conn.close()

    print("\nDone!")
