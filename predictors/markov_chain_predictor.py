"""
马尔可夫链状态转移预测器
========================
核心思想:
  - 每支球队的历史比赛结果序列 (W/D/L, 从该队视角) 构成一个马尔可夫链
  - 状态 = 最近 N 场结果序列 (e.g. "WWL" 表示最近3场赢赢输)
  - 转移矩阵: P(下一场结果 | 最近N场序列), 用 Laplace 平滑处理稀疏
  - 对一场比赛:
      主队状态 → P(主队 W/D/L)
      客队状态 → P(客队 W/D/L)
      融合: P(H) = α·P(主W)·P(客L) + (1-α)·0.5·[P(主W)+P(客L)]
            P(D) = β·sqrt(P(主D)·P(客D))            # 平局需要双方都"倾向平"
            P(A) = α·P(主L)·P(客W) + (1-α)·0.5·[P(主L)+P(客W)]
  - 归一化后输出 P(H)/P(D)/P(A)

优势:
  - 纯序列信号, 不依赖赔率 (独立于现有 stacking 的特征空间)
  - 可作为 stacking 的第 6 个基模型
  - 冷启动友好: 新球队用全局先验
"""
import os, sys, sqlite3, json, math
from collections import defaultdict, Counter
import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score, f1_score, confusion_matrix

ROOT = os.path.dirname(os.path.abspath(__file__))

class MarkovChainPredictor:
    """球队结果序列马尔可夫链预测器"""

    def __init__(self, order=3, laplace_alpha=1.0, league_prior_weight=5.0,
                 fusion_alpha=0.6, draw_beta=1.2):
        """
        Args:
            order: 马尔可夫链阶数 (最近N场作为状态)
            laplace_alpha: 拉普拉斯平滑强度
            league_prior_weight: 联赛先验权重 (等效样本数)
            fusion_alpha: 主客队融合时乘积项的权重
            draw_beta: 平局融合的指数 (放大平局信号)
        """
        self.order = order
        self.laplace_alpha = laplace_alpha
        self.league_prior_weight = league_prior_weight
        self.fusion_alpha = fusion_alpha
        self.draw_beta = draw_beta

        # 转移计数: {state: {next_result: count}}
        self.transitions = defaultdict(lambda: defaultdict(float))
        # 联赛级先验: {league: {W: p, D: p, L: p}}
        self.league_prior = {}
        self.global_prior = {'W': 1/3, 'D': 1/3, 'L': 1/3}
        # 球队历史序列缓存
        self.team_history = defaultdict(list)  # {team: [(date, result, league)]}

    def _result_from_team_perspective(self, home_team, away_team, home_score, away_score):
        """从主队视角返回结果; 客队视角取反"""
        if home_score > away_score:
            return 'W', 'L'  # 主队赢, 客队输
        elif home_score == away_score:
            return 'D', 'D'
        else:
            return 'L', 'W'

    def _state_to_idx(self, results):
        """结果列表 → 状态字符串"""
        return ''.join(results[-self.order:]) if len(results) >= 1 else ''

    def fit(self, df):
        """
        从历史数据学习转移矩阵
        df 需要列: home_team_name, away_team_name, match_date, league_name, home_score, away_score
        """
        df = df.sort_values('match_date').reset_index(drop=True)

        # 第一步: 构建每支球队的历史序列 (按时间)
        team_seq = defaultdict(list)  # {team: [(date, result, league)]}
        for _, row in df.iterrows():
            if pd.isna(row['home_team_name']) or pd.isna(row['away_team_name']):
                continue
            home_r, away_r = self._result_from_team_perspective(
                row['home_team_name'], row['away_team_name'],
                row['home_score'], row['away_score'])
            team_seq[row['home_team_name']].append((row['match_date'], home_r, row['league_name']))
            team_seq[row['away_team_name']].append((row['match_date'], away_r, row['league_name']))

        self.team_history = team_seq

        # 第二步: 学习转移矩阵
        # 对每支球队, 用滑动窗口: state = 最近order场, next = 第order+1场
        league_counts = defaultdict(lambda: defaultdict(float))
        global_counts = defaultdict(float)

        for team, seq in team_seq.items():
            results = [s[1] for s in seq]
            leagues = [s[2] for s in seq]
            for i in range(self.order, len(results)):
                state = ''.join(results[i-self.order:i])
                next_r = results[i]
                self.transitions[state][next_r] += 1.0
                league_counts[leagues[i]][next_r] += 1.0
                global_counts[next_r] += 1.0

        # 第三步: 联赛先验 & 全局先验
        total_global = sum(global_counts.values())
        if total_global > 0:
            self.global_prior = {k: v / total_global for k, v in global_counts.items()}

        for league, counts in league_counts.items():
            total = sum(counts.values())
            if total > 0:
                # 与全局先验混合, 避免小联赛极端
                mixed = {}
                for r in ['W', 'D', 'L']:
                    obs = counts[r] / total
                    mixed[r] = (counts[r] + self.league_prior_weight * self.global_prior[r]) / \
                               (total + self.league_prior_weight)
                self.league_prior[league] = mixed

        n_states = len(self.transitions)
        total_trans = sum(sum(c.values()) for c in self.transitions.values())
        print(f"  [Markov] 阶数={self.order}, 状态数={n_states}, 总转移={total_trans:.0f}")
        print(f"  [Markov] 全局先验: W={self.global_prior['W']:.3f} D={self.global_prior['D']:.3f} L={self.global_prior['L']:.3f}")
        print(f"  [Markov] 联赛先验数: {len(self.league_prior)}")

        return self

    def _predict_team_probs(self, team, league, date):
        """
        给定球队、联赛、日期, 返回该队下一场结果的 P(W)/P(D)/P(L)
        用该队 date 之前的最近 order 场作为状态
        """
        seq = self.team_history.get(team, [])
        # 只取 date 之前的
        past = [s[1] for s in seq if s[0] < date]

        if len(past) < self.order:
            # 冷启动: 用联赛先验 (若有) 或全局先验
            prior = self.league_prior.get(league, self.global_prior)
            return dict(prior)

        state = ''.join(past[-self.order:])
        counts = self.transitions.get(state, None)

        if counts is None or sum(counts.values()) == 0:
            prior = self.league_prior.get(league, self.global_prior)
            return dict(prior)

        total = sum(counts.values())
        # Laplace 平滑 + 联赛先验回退
        prior = self.league_prior.get(league, self.global_prior)
        probs = {}
        smooth_total = total + 3 * self.laplace_alpha
        for r in ['W', 'D', 'L']:
            probs[r] = (counts[r] + self.laplace_alpha * prior[r]) / smooth_total

        return probs

    def predict_proba(self, home_team, away_team, league, date):
        """
        预测一场比赛的 P(H)/P(D)/P(A)
        返回 [P_H, P_D, P_A]

        融合逻辑 (v2):
          - 实力差 diff = (主W-主L) - (客W-客L)
          - diff>0 主强 → H; diff<0 客强 → A; diff≈0 势均 → D
          - 平局需要: 双方D倾向高 + 实力差小
        """
        p_home = self._predict_team_probs(home_team, league, date)  # 主队 W/D/L
        p_away = self._predict_team_probs(away_team, league, date)  # 客队 W/D/L

        # 净胜倾向 (实力指标)
        s_home = p_home['W'] - p_home['L']
        s_away = p_away['W'] - p_away['L']
        diff = s_home - s_away  # >0 主强, <0 客强

        # 双方平局倾向
        d_home = p_home['D']
        d_away = p_away['D']

        # 势均力敌度 [0, 1], diff=0 时为 1
        balance = 1.0 - min(abs(diff), 1.0)

        # H/A: 实力差驱动 + 乘积项
        p_H = max(diff, 0) * 0.5 + p_home['W'] * p_away['L'] * 2.0
        p_A = max(-diff, 0) * 0.5 + p_home['L'] * p_away['W'] * 2.0
        # D: 双方D倾向 × 势均力敌度 (关键: 不用乘积收缩, 用平均放大)
        p_D = 0.5 * (d_home + d_away) * (0.4 + 1.2 * balance)

        # 归一化
        total = p_H + p_D + p_A
        if total > 0:
            p_H /= total
            p_D /= total
            p_A /= total
        else:
            p_H = p_D = p_A = 1/3

        return np.array([p_H, p_D, p_A])

    def predict(self, home_team, away_team, league, date):
        """返回 Top-1 预测: 0=H, 1=D, 2=A"""
        return int(np.argmax(self.predict_proba(home_team, away_team, league, date)))

def load_data():
    """加载全量数据"""
    conn = sqlite3.connect(os.path.join(ROOT, 'data', 'football_data.db'))
    df = pd.read_sql_query('''
        SELECT match_id, home_team_name, away_team_name, match_date,
               league_name, home_score, away_score
        FROM matches
        WHERE home_score IS NOT NULL AND away_score IS NOT NULL
          AND home_team_name IS NOT NULL AND away_team_name IS NOT NULL
        ORDER BY match_date
    ''', conn)
    conn.close()
    return df

def label_from_score(home_score, away_score):
    """0=H, 1=D, 2=A"""
    if home_score > away_score:
        return 0
    elif home_score == away_score:
        return 1
    else:
        return 2

def run_backtest():
    """OOF 2023+ 回测"""
    print("=" * 70)
    print("马尔可夫链状态转移预测器 — OOF 2023+ 回测")
    print("=" * 70)

    df = load_data()
    print(f"全量数据: {len(df)} 条")

    # 时间切分: pre-2023 训练, 2023+ 测试
    train_mask = df['match_date'] < '2023-01-01'
    test_mask = df['match_date'] >= '2023-01-01'
    df_train = df[train_mask].copy()
    df_test = df[test_mask].copy()
    print(f"训练集 (pre-2023): {len(df_train)} 条")
    print(f"测试集 (2023+):    {len(df_test)} 条")

    # 尝试不同阶数
    results_all = {}
    for order in [1, 2, 3, 4]:
        print(f"\n{'='*50}")
        print(f"阶数 order={order}")
        print(f"{'='*50}")

        mc = MarkovChainPredictor(order=order, laplace_alpha=1.0,
                                   league_prior_weight=5.0,
                                   fusion_alpha=0.6, draw_beta=1.2)
        mc.fit(df_train)

        # 在测试集上预测
        y_true = []
        y_pred = []
        y_proba = []

        for _, row in df_test.iterrows():
            true_label = label_from_score(row['home_score'], row['away_score'])
            proba = mc.predict_proba(row['home_team_name'], row['away_team_name'],
                                      row['league_name'], row['match_date'])
            pred = int(np.argmax(proba))
            y_true.append(true_label)
            y_pred.append(pred)
            y_proba.append(proba)

        y_true = np.array(y_true)
        y_pred = np.array(y_pred)
        y_proba = np.array(y_proba)

        acc = accuracy_score(y_true, y_pred)
        macro_f1 = f1_score(y_true, y_pred, average='macro', zero_division=0)
        f1_h, f1_d, f1_a = f1_score(y_true, y_pred, average=None, zero_division=0)
        cm = confusion_matrix(y_true, y_pred)

        # 逐类 recall
        recalls = cm.diagonal() / cm.sum(axis=1)
        precisions = []
        for i in range(3):
            col_sum = cm[:, i].sum()
            precisions.append(cm[i, i] / col_sum if col_sum > 0 else 0)

        target_names = ['H(主胜)', 'D(平局)', 'A(客胜)']
        print(f"\nConfusion Matrix (行=实际, 列=预测):")
        print(f"            预测H    预测D    预测A")
        for i, name in enumerate(target_names):
            print(f"  实际{name}  {cm[i][0]:6d}  {cm[i][1]:6d}  {cm[i][2]:6d}")

        print(f"\n  Acc={acc:.4f}  Macro-F1={macro_f1:.4f}")
        print(f"  {'类':>8} {'Precision':>10} {'Recall':>10} {'F1':>10} {'Support':>10}")
        for i, name in enumerate(target_names):
            print(f"  {name:>8} {precisions[i]:>10.4f} {recalls[i]:>10.4f} {f1_score(y_true, y_pred, labels=[i], average='micro', zero_division=0):>10.4f} {int(cm[i].sum()):>10d}")

        # AUC
        try:
            from sklearn.metrics import roc_auc_score
            auc = roc_auc_score(y_true, y_proba, multi_class='ovr', average='macro')
            print(f"  AUC (OvR macro) = {auc:.4f}")
        except Exception:
            auc = 0.0

        results_all[order] = {
            'acc': acc, 'macro_f1': macro_f1, 'auc': auc,
            'd_recall': float(recalls[1]), 'd_precision': float(precisions[1]), 'd_f1': float(f1_d),
            'cm': cm.tolist()
        }

    # 汇总
    print("\n" + "=" * 70)
    print("阶数对比汇总")
    print("=" * 70)
    print(f"  {'阶数':>4} {'Acc':>8} {'Macro-F1':>10} {'AUC':>8} {'D_Recall':>10} {'D_Prec':>8} {'D_F1':>8}")
    for order, r in results_all.items():
        print(f"  {order:>4} {r['acc']:>8.4f} {r['macro_f1']:>10.4f} {r['auc']:>8.4f} "
              f"{r['d_recall']:>10.4f} {r['d_precision']:>8.4f} {r['d_f1']:>8.4f}")

    # 保存最优模型结果
    best_order = max(results_all, key=lambda k: results_all[k]['macro_f1'])
    print(f"\n  最优阶数: {best_order} (Macro-F1={results_all[best_order]['macro_f1']:.4f})")

    # 保存
    output = {
        'model': 'MarkovChainPredictor',
        'best_order': best_order,
        'results': {str(k): {kk: (vv if not isinstance(vv, list) else vv) for kk, vv in v.items()}
                    for k, v in results_all.items()}
    }
    out_path = os.path.join(ROOT, 'output', 'markov_chain_backtest.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"\n结果已保存: {out_path}")

    return results_all

if __name__ == '__main__':
    run_backtest()
