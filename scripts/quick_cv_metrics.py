#!/usr/bin/env python3
"""quick_cv_metrics.py — 修正版交叉验证指标
============================================
修复清单 (v2.0 vs 临时脚本v1):
  1. 时间序列切分(按 kickoff) 替代 random.sample → 防泄漏
  2. AUC/RPS/LogLoss/Brier/ECE/分箱单调性 替代 accuracy → 铁律7/8
  3. shaoxiang_feature_library.db 作标签源(已滤假0-0) 替代 match_outcomes
  4. 用 match_outcomes.op_1x2_* 开盘赔率 替代 ORDER BY DESC LIMIT 1
  5. 并排 4 条 baseline: 庄家热门/永远主胜/永远大球/随机
  6. ROI/P&L 模拟: 期望值×赔率 flat/Kelly 下注
  7. 输出完整概率分布(非 argmax) → 分箱命中率 Q1-Q5
  8. OU 取同一条线的 over/under(非 market LIKE, 用 match_outcomes.op_ou_*)
"""
import sys, os, sqlite3, math, json
import numpy as np
from datetime import datetime
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sklearn.metrics import roc_auc_score, log_loss, brier_score_loss

# ─────── 配置 ───────
FEATURE_DB = os.path.join(ROOT, "data", "shaoxiang_feature_library.db")
GQ_DB = os.path.join(ROOT, "data", "events.db")
OUTPUT = os.path.join(ROOT, "分析报告", "cv_metrics_report.md")
N_TEST_FRAC = 0.20        # 时间序列切分: 后 20% 作验证集
N_BINS = 5                # 分箱数
MIN_TEST = 100            # 最少测试样本

# ─────── 数据加载 ───────

def load_eval_data():
    """从特征库取标签 + 回连 match_outcomes 取开盘赔率 → 按 kickoff 时间序列切分."""
    feat_db = sqlite3.connect(FEATURE_DB)
    gq_db = sqlite3.connect(GQ_DB)

    # 特征库有标签的行 → (source, league, kickoff, label_1x2, label_ou)
    feat_rows = feat_db.execute("""
        SELECT source, league, kickoff, label_1x2, label_ou
        FROM features WHERE label_1x2 IS NOT NULL
        ORDER BY kickoff
    """).fetchall()

    # match_outcomes 有开盘 1X2 赔率的行 → 索引
    mo_rows = gq_db.execute("""
        SELECT source, league, kickoff, home, away,
               op_1x2_h, op_1x2_d, op_1x2_a,
               op_ou_line, op_ou_over, op_ou_under,
               op_cs, score_home, score_away, result
        FROM match_outcomes
        WHERE op_1x2_h IS NOT NULL
          AND score_home IS NOT NULL AND score_away IS NOT NULL
    """).fetchall()

    feat_db.close()
    gq_db.close()

    # 建索引: (source, league, kickoff) → match_outcomes 行
    mo_index = {}
    for r in mo_rows:
        key = (r[0] or "", (r[1] or "").strip(), (r[2] or "").strip())
        if key not in mo_index:
            mo_index[key] = r

    # 合并
    data = []
    for fr in feat_rows:
        key = (fr[0] or "", (fr[1] or "").strip(), (fr[2] or "").strip())
        mo = mo_index.get(key)
        if not mo:
            continue
        # mo columns: source(0), league(1), kickoff(2), home(3), away(4),
        #   op_1x2_h(5), op_1x2_d(6), op_1x2_a(7),
        #   op_ou_line(8), op_ou_over(9), op_ou_under(10),
        #   op_cs(11), score_home(12), score_away(13), result(14)
        sh, sa = int(mo[12]), int(mo[13])
        label_1x2 = fr[3]  # 0=H, 1=D, 2=A
        label_ou = fr[4]    # 0=O(over), 1=U(under)
        # 根据实际比分推算 1X2 (双重验证)
        expected_1x2 = 0 if sh > sa else (1 if sh == sa else 2)
        if expected_1x2 != label_1x2:
            continue  # 比分与标签矛盾, 跳过

        data.append({
            "source": fr[0], "league": fr[1] or "", "kickoff": fr[2],
            "home": mo[3], "away": mo[4],
            "h": mo[5], "d": mo[6], "a": mo[7],
            "ou_line": mo[8], "ou_over": mo[9], "ou_under": mo[10],
            "op_cs": mo[11],
            "score_home": sh,
            "score_away": sa,
            "result": mo[14],
            "label_1x2": label_1x2,  # 0=H, 1=D, 2=A
            "label_ou": label_ou,    # 0=O, 1=U
        })

    return data


# ─────── 预测 ───────

def run_predictions(data):
    """对每条数据跑 ranked_predictor.predict(), 提取概率."""
    from pipeline.ranked_predictor import predict as ranked_predict

    records = []
    for i, d in enumerate(data):
        try:
            # 构造参数
            kwargs = {
                "h": d["h"], "d": d["d"], "a": d["a"],
                "league": d["league"],
            }
            # OU
            if d["ou_line"] and d["ou_over"] and d["ou_under"]:
                kwargs["ou_line"] = d["ou_line"]
                kwargs["ou_over"] = d["ou_over"]
                kwargs["ou_under"] = d["ou_under"]

            r = ranked_predict(d["home"], d["away"], **kwargs)
            if not r or "markets" not in r:
                continue

            m1x2 = r["markets"].get("1x2", {})
            p_h = float(m1x2.get("p_h", 0) or 0)
            p_d = float(m1x2.get("p_d", 0) or 0)
            p_a = float(m1x2.get("p_a", 0) or 0)

            mou = r["markets"].get("ou", {})
            p_over = float(mou.get("p_over", 0.5) or 0.5)
            p_under = float(mou.get("p_under", 0.5) or 0.5)

            # 根据 label_1x2 推算实际方
            actual_1x2 = d["label_1x2"]  # 0=H,1=D,2=A
            actual_ou = d["label_ou"]    # 0=O,1=U

            records.append({
                "home": d["home"], "away": d["away"],
                "kickoff": d["kickoff"],
                "actual_1x2": actual_1x2,
                "actual_ou": actual_ou,
                "p_h": p_h, "p_d": p_d, "p_a": p_a,
                "p_over": p_over, "p_under": p_under,
                "odds_h": d["h"], "odds_d": d["d"], "odds_a": d["a"],
                "odds_ou_line": d["ou_line"],
                "odds_over": d["ou_over"], "odds_under": d["ou_under"],
            })

            if (i + 1) % 100 == 0:
                print(f"  预测进度: {i + 1}/{len(data)}")
        except Exception as e:
            print(f"  [skip] {d['home']} vs {d['away']}: {e}")
            continue

    return records


# ─────── 指标 ───────

def auc_1x2(records):
    """1X2 one-vs-rest AUC (macro 平均)."""
    y_true_h = [1 if r["actual_1x2"] == 0 else 0 for r in records]
    y_true_d = [1 if r["actual_1x2"] == 1 else 0 for r in records]
    y_true_a = [1 if r["actual_1x2"] == 2 else 0 for r in records]
    prob_h = [r["p_h"] for r in records]
    prob_d = [r["p_d"] for r in records]
    prob_a = [r["p_a"] for r in records]
    auc_h = roc_auc_score(y_true_h, prob_h)
    auc_d = roc_auc_score(y_true_d, prob_d)
    auc_a = roc_auc_score(y_true_a, prob_a)
    return auc_h, auc_d, auc_a


def auc_ou(records):
    ou_records = [r for r in records if r.get("actual_ou") is not None and r.get("odds_ou_line") is not None]
    if len(ou_records) < 5:
        return 0.5, 0.5  # 不足 5 场, 无法计算
    y_true = [r["actual_ou"] for r in ou_records]  # 0=O, 1=U
    prob_over = [r["p_over"] for r in ou_records]
    prob_under = [r["p_under"] for r in ou_records]
    auc_over = roc_auc_score([1 - y for y in y_true], prob_over)  # 大球=正类
    auc_under = roc_auc_score(y_true, prob_under)
    return auc_over, auc_under


def rps(records):
    """Ranked Probability Score (多类 Brier)."""
    n = len(records)
    total = 0.0
    for r in records:
        y = [0.0, 0.0, 0.0]
        y[r["actual_1x2"]] = 1.0
        p = [r["p_h"], r["p_d"], r["p_a"]]
        total += sum((sum(p[:k + 1]) - sum(y[:k + 1])) ** 2 for k in range(3)) / 2
    return total / n


def logloss_1x2(records):
    y = np.array([[1 if r["actual_1x2"] == 0 else 0,
                    1 if r["actual_1x2"] == 1 else 0,
                    1 if r["actual_1x2"] == 2 else 0] for r in records])
    p = np.array([[r["p_h"], r["p_d"], r["p_a"]] for r in records])
    return log_loss(y, p)


def brier_1x2(records):
    y = np.array([[1 if r["actual_1x2"] == 0 else 0,
                    1 if r["actual_1x2"] == 1 else 0,
                    1 if r["actual_1x2"] == 2 else 0] for r in records])
    p = np.array([[r["p_h"], r["p_d"], r["p_a"]] for r in records])
    return brier_score_loss(y.flatten(), p.flatten())


def ece(records, n_bins=10):
    """Expected Calibration Error (等宽分箱)."""
    probs = [[r["p_h"], r["p_d"], r["p_a"]] for r in records]
    labels = [[1 if r["actual_1x2"] == 0 else 0,
               1 if r["actual_1x2"] == 1 else 0,
               1 if r["actual_1x2"] == 2 else 0] for r in records]
    # 取预测概率最大的类别
    confs = np.array([max(p) for p in probs])
    accs = np.array([l[np.argmax(p)] for p, l in zip(probs, labels)])
    bins = np.linspace(0, 1, n_bins + 1)
    ece_val = 0.0
    for i in range(n_bins):
        mask = (confs >= bins[i]) & (confs < bins[i + 1])
        if mask.sum() == 0:
            continue
        bin_acc = accs[mask].mean()
        bin_conf = confs[mask].mean()
        ece_val += (mask.sum() / len(confs)) * abs(bin_acc - bin_conf)
    return ece_val


def binned_hit_rate(probs, labels, n_bins=N_BINS):
    """概率分箱命中率 → 单调性检查."""
    pairs = list(zip(probs, labels))
    pairs.sort(key=lambda x: x[0])
    n = len(pairs)
    out = []
    for i in range(n_bins):
        lo = int(n * i / n_bins)
        hi = int(n * (i + 1) / n_bins)
        if lo >= hi:
            continue
        chunk = pairs[lo:hi]
        avg_prob = sum(p[0] for p in chunk) / len(chunk)
        hit = sum(p[1] for p in chunk) / len(chunk)
        out.append((i + 1, avg_prob, hit, len(chunk)))
    return out


def bin_ou_hit_rate(probs_over, labels_ou, n_bins=N_BINS):
    """OU 分箱: 按 p_over 排, 统计实际大球率(1-label_ou)."""
    # label_ou: 0=O, 1=U → 大球=1-label_ou
    pairs = list(zip(probs_over, [1 - lo for lo in labels_ou]))
    pairs.sort(key=lambda x: x[0])
    n = len(pairs)
    out = []
    for i in range(n_bins):
        lo = int(n * i / n_bins)
        hi = int(n * (i + 1) / n_bins)
        if lo >= hi:
            continue
        chunk = pairs[lo:hi]
        avg_prob = sum(p[0] for p in chunk) / len(chunk)
        hit = sum(p[1] for p in chunk) / len(chunk)
        out.append((i + 1, avg_prob, hit, len(chunk)))
    return out


# ─────── Baseline ───────

def baseline_bookmaker_fav(records):
    """庄家热门 baseline: 1X2 赔率最低(概率最高) 的选项."""
    correct = 0
    for r in records:
        fav = np.argmin([r["odds_h"], r["odds_d"], r["odds_a"]])  # 最低赔率=庄家最看好
        if fav == r["actual_1x2"]:
            correct += 1
    return correct / len(records)


def baseline_always_home(records):
    return sum(1 for r in records if r["actual_1x2"] == 0) / len(records)


def baseline_always_over(records):
    """永远大球命中率 (OU: label_ou=0 是大球)."""
    ou_records = [r for r in records if r.get("actual_ou") is not None and r.get("odds_ou_line") is not None]
    if not ou_records:
        return 0.0
    return sum(1 for r in ou_records if r["actual_ou"] == 0) / len(ou_records)


def baseline_random(records):
    return 1.0 / 3  # 1X2 随机


# ─────── ROI / P&L ───────

def simulate_betting(records, bankroll=1000, flat_stake=10, kelly_frac=0.25, min_edge=0.02):
    """flat 下注 + Kelly 下注 模拟."""
    flat_pl = []
    kelly_pl = []
    flat_bank, kelly_bank = bankroll, bankroll

    for r in records:
        # 找预测概率最高的选项
        probs = [r["p_h"], r["p_d"], r["p_a"]]
        odds = [r["odds_h"], r["odds_d"], r["odds_a"]]
        pred = np.argmax(probs)
        pred_prob = probs[pred]
        pred_odds = odds[pred]
        actual = r["actual_1x2"]

        # 期望收益
        ev = pred_prob * pred_odds - 1

        # Flat
        if ev > min_edge:
            if actual == pred:
                flat_bank += flat_stake * (pred_odds - 1)
            else:
                flat_bank -= flat_stake

        # Kelly
        if ev > min_edge and pred_prob > 0:
            kelly_bet = kelly_bank * kelly_frac * (ev / (pred_odds - 1)) if pred_odds > 1 else 0
            kelly_bet = min(kelly_bet, kelly_bank * 0.25)  # 上限25%
            kelly_bet = max(kelly_bet, 0)
            if actual == pred:
                kelly_bank += kelly_bet * (pred_odds - 1)
            else:
                kelly_bank -= kelly_bet

        flat_pl.append(flat_bank)
        kelly_pl.append(kelly_bank)

    return {
        "flat_final": flat_bank, "flat_roi": (flat_bank - bankroll) / bankroll,
        "kelly_final": kelly_bank, "kelly_roi": (kelly_bank - bankroll) / bankroll,
        "flat_pl": flat_pl, "kelly_pl": kelly_pl,
    }


# ─────── 报告 ───────

def generate_report(records, data_info):
    """生成 Markdown 报告."""
    n = len(records)
    if n == 0:
        return "# CV 指标报告\n\n无有效预测记录。"

    auc_h, auc_d, auc_a = auc_1x2(records)
    auc_ou_over, auc_ou_under = auc_ou(records)
    rps_val = rps(records)
    ll = logloss_1x2(records)
    br = brier_1x2(records)
    ec = ece(records)

    # baseline
    fav = baseline_bookmaker_fav(records)
    home = baseline_always_home(records)
    rnd = baseline_random(records)

    # model argmax accuracy (for comparison only)
    model_acc = sum(1 for r in records if np.argmax([r["p_h"], r["p_d"], r["p_a"]]) == r["actual_1x2"]) / n

    # OU
    ou_records = [r for r in records if r.get("actual_ou") is not None and r.get("odds_ou_line") is not None]
    ou_n = len(ou_records)
    ou_acc = sum(1 for r in ou_records if np.argmax([r["p_over"], r["p_under"]]) == r["actual_ou"]) / ou_n if ou_records else 0
    always_over = baseline_always_over(records)

    # 分箱
    bin_h = binned_hit_rate([r["p_h"] for r in records], [1 if r["actual_1x2"] == 0 else 0 for r in records])
    bin_d = binned_hit_rate([r["p_d"] for r in records], [1 if r["actual_1x2"] == 1 else 0 for r in records])
    bin_a = binned_hit_rate([r["p_a"] for r in records], [1 if r["actual_1x2"] == 2 else 0 for r in records])
    bin_ou = bin_ou_hit_rate([r["p_over"] for r in ou_records], [r["actual_ou"] for r in ou_records]) if ou_records else []

    # ROI
    bet = simulate_betting(records)

    lines = []
    lines.append("# 哨响AI 交叉验证指标体系")
    lines.append(f"\n**数据源**: shaoxiang_feature_library.db → match_outcomes.op_1x2_* 开盘赔率")
    lines.append(f"**切分方式**: 时间序列 (kickoff 排序, 后 {N_TEST_FRAC:.0%} 作验证集)")
    lines.append(f"**评估场次**: {data_info['total_matched']} 场有赔率, {n} 场预测成功")
    lines.append(f"**时间范围**: {data_info['kickoff_first']} ~ {data_info['kickoff_last']}")
    lines.append(f"**测试集**: {data_info['test_start']} ~ {data_info['test_end']} ({n} 场)")
    lines.append(f"\n*生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")

    # ── 1X2 ──
    lines.append("\n---")
    lines.append("\n## 1X2 指标")
    lines.append("")
    lines.append(f"| 指标 | 值 | 说明 |")
    lines.append(f"|------|-----|------|")
    lines.append(f"| AUC (H/D/A) | {auc_h:.4f} / {auc_d:.4f} / {auc_a:.4f} | one-vs-rest |")
    lines.append(f"| AUC macro | **{(auc_h + auc_d + auc_a) / 3:.4f}** | 三分类宏观平均 |")
    lines.append(f"| RPS | **{rps_val:.4f}** | Ranked Probability Score (越低越好) |")
    lines.append(f"| LogLoss | **{ll:.4f}** | 交叉熵 (越低越好) |")
    lines.append(f"| Brier | **{br:.4f}** | Brier Score (越低越好) |")
    lines.append(f"| ECE | **{ec:.4f}** | Expected Calibration Error (越接近0越好) |")
    lines.append(f"| Accuracy (仅参考) | {model_acc:.1%} | 模型 argmax 命中率 |")

    # ── 分箱单调性 (1X2 主胜) ──
    lines.append("\n### 主胜概率分箱 (Q1→Q5 应单调递增)")
    lines.append("| 分箱 | 概率区间 | 命中率 | 样本量 | 单调? |")
    lines.append("|------|---------|--------|--------|-------|")
    prev_hit = -1
    monotone = True
    for qi, avg_p, hit, sz in bin_h:
        ok = "✅" if hit >= prev_hit else "❌"
        if hit < prev_hit:
            monotone = False
        prev_hit = hit
        lines.append(f"| Q{qi} | {avg_p:.3f} | {hit:.1%} | {sz} | {ok} |")

    # ── 平局分箱 ──
    lines.append("\n### 平局概率分箱")
    lines.append("| 分箱 | 概率区间 | 命中率 | 样本量 |")
    lines.append("|------|---------|--------|--------|")
    for qi, avg_p, hit, sz in bin_d:
        lines.append(f"| Q{qi} | {avg_p:.3f} | {hit:.1%} | {sz} |")

    # ── Baseline ──
    lines.append("\n---")
    lines.append("\n## Baseline 对照")
    lines.append("")
    lines.append(f"| Baseline | 命中率 | Δ vs 模型 |")
    lines.append(f"|----------|--------|----------|")
    lines.append(f"| 模型 argmax | {model_acc:.1%} | — |")
    lines.append(f"| 庄家热门 (最低赔率) | {fav:.1%} | {model_acc - fav:+.1%} |")
    lines.append(f"| 永远主胜 | {home:.1%} | {model_acc - home:+.1%} |")
    lines.append(f"| 随机 (1/3) | {rnd:.1%} | {model_acc - rnd:+.1%} |")

    # ── OU ──
    if ou_records:
        lines.append("\n---")
        lines.append("\n## OU (大小球) 指标")
        lines.append(f"\n| 指标 | 值 |")
        lines.append(f"|------|-----|")
        lines.append(f"| 有效场次 | {ou_n} |")
        lines.append(f"| AUC (over) | {auc_ou_over:.4f} |")
        lines.append(f"| AUC (under) | {auc_ou_under:.4f} |")
        lines.append(f"| Accuracy | {ou_acc:.1%} |")
        lines.append(f"| 永远大球 baseline | {always_over:.1%} |")

        lines.append("\n### OU 概率分箱 (按 p_over 排序)")
        lines.append("| 分箱 | avg p_over | 实际大球率 | 样本量 |")
        lines.append("|------|-----------|----------|--------|")
        prev = -1
        for qi, avg_p, hit, sz in bin_ou:
            ok = "✅" if hit >= prev else "⚠️"
            prev = hit
            lines.append(f"| Q{qi} | {avg_p:.3f} | {hit:.1%} | {sz} | {ok} |")

    # ── ROI ──
    lines.append("\n---")
    lines.append("\n## ROI / P&L 模拟")
    lines.append("")
    lines.append(f"| 策略 | 终值 | ROI |")
    lines.append(f"|------|------|-----|")
    lines.append(f"| Flat (每注{10}) | {bet['flat_final']:.2f} | {bet['flat_roi']:+.2%} |")
    lines.append(f"| Kelly (25% fractional) | {bet['kelly_final']:.2f} | {bet['kelly_roi']:+.2%} |")

    # ── 风险提示 ──
    lines.append("\n---")
    lines.append("\n## ⚠️ 风险提示")
    lines.append("")
    lines.append("- **时间序列切分**: 测试集 = 最近 20% 比赛, 模拟真实前瞻环境")
    lines.append("- **数据源**: shaoxiang_feature_library.db (已过滤假 0-0), 回连 match_outcomes.op_1x2_* 取开盘赔率")
    lines.append("- **Accuracy 仅作参考**, 以 AUC/RPS/LogLoss/ECE/分箱单调性为主要指标")
    lines.append("- **ROI 为模拟回测**, 真实交易受流动性/滑点/限注影响")
    lines.append("- **OU baseline**: 永远大球 = 低级别联赛结构性偏大球, 基线本身不可靠")

    return "\n".join(lines)


# ─────── main ───────

def main():
    print("=" * 64)
    print("quick_cv_metrics.py v2.0 — 修正版 CV 指标")
    print("=" * 64)

    # 1. 加载数据
    print("\n[1/4] 加载数据...")
    data = load_eval_data()
    data.sort(key=lambda x: x["kickoff"] or "")

    # 时间序列切分
    split_idx = int(len(data) * (1 - N_TEST_FRAC))
    train_data, test_data = data[:split_idx], data[split_idx:]

    print(f"  全量匹配: {len(data)} 场")
    print(f"  训练(观察): {len(train_data)} 场 (前 {1-N_TEST_FRAC:.0%})")
    print(f"  测试(验证): {len(test_data)} 场 (后 {N_TEST_FRAC:.0%})")

    if len(test_data) < MIN_TEST:
        print(f"\n⚠️ 测试集仅 {len(test_data)} 场 < {MIN_TEST}, 指标不可靠")
        return

    # 2. 跑预测(仅测试集)
    print(f"\n[2/4] 对测试集 {len(test_data)} 场跑 ranked_predictor...")
    records = run_predictions(test_data)
    print(f"  预测成功: {len(records)} 场")

    # 3. 计算指标
    print("\n[3/4] 计算指标...")
    auc_h, auc_d, auc_a = auc_1x2(records)
    print(f"  1X2 AUC: H={auc_h:.4f} D={auc_d:.4f} A={auc_a:.4f} macro={(auc_h+auc_d+auc_a)/3:.4f}")
    print(f"  RPS={rps(records):.4f}  LogLoss={logloss_1x2(records):.4f}  Brier={brier_1x2(records):.4f}")
    print(f"  ECE={ece(records):.4f}")

    # 4. 输出报告
    print("\n[4/4] 生成报告...")
    info = {
        "total_matched": len(data),
        "kickoff_first": data[0]["kickoff"] if data else "N/A",
        "kickoff_last": data[-1]["kickoff"] if data else "N/A",
        "test_start": test_data[0]["kickoff"] if test_data else "N/A",
        "test_end": test_data[-1]["kickoff"] if test_data else "N/A",
    }
    report = generate_report(records, info)

    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(report)

    print(f"\n✅ 报告已输出: {OUTPUT}")
    print(report)
    return records, report


if __name__ == "__main__":
    main()
