# -*- coding: utf-8 -*-
"""半场平局模型训练 (Half-Time Draw Model).

任务: 用「开赛 15 分钟」的滚动赔率快照, 预测上半场结束时是否平局 (HT 比分 home==away).

背景 (IR-15 已核实):
  - odds_snapshots.minute_at 粒度粗(0/45/90), 不可用于定位 15 分钟.
  - captured_at 为 UTC 秒, matches.kickoff 为本地(UTC+8)时间, 用 kickoff_ts+900s 定位"开赛15分钟".
  - match_outcomes.ht_score_home/away 提供半场赛果(标签).

特征 (全部为开赛 15min(±3min) 时点的真实赔率, 去水后):
  - p_ht_home / p_ht_draw / p_ht_away : 1X2_1H 三档去水概率 (半场结果分布, p_ht_draw 即半场平局概率)
  - ht_draw_odds_raw                  : 半场平局原始赔率
  - p_ou1h_over                       : OU_1H_1.0 线 over 去水概率 (半场进球期望; 低=半场平局概率高)
  - p_ft_draw                         : 全场 1X2 draw 去水概率 (全场平局先验, 弱辅助)

评估口径 (对齐哨响AI 基准 30×5 CV): AUC + 命中率(阈值0.5) + 分层校准, 对照 naive 基线.

诚实边界: 仅用真实赔率 devig; 样本不足的特征列可用性单独标注; 不做时序穿越(仅 15min 时点, 不用 HT 后信息).
"""
import os
import sqlite3
import datetime
import logging

import numpy as np
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.tree import DecisionTreeClassifier
from sklearn.model_selection import StratifiedKFold
from sklearn.metrics import roc_auc_score

logger = logging.getLogger("train_ht_draw_model")

GQ = "D:/Architecture/data/events.db"
MIN_ODDS, MAX_ODDS = 1.01, 1000.0
WINDOW_LO, WINDOW_HI = 720, 1080          # kickoff 后 12~18 分钟 (15±3min)
TARGET_MIN = 900                           # 15 分钟


def parse_kickoff_ts(s):
    """kickoff 字符串 -> UTC 秒. 支持 '2026-08-25 23:00'(本地) 与 '...Z'(UTC)."""
    if not s:
        return None
    s = str(s).strip()
    try:
        if s.endswith("Z"):
            dt = datetime.datetime.fromisoformat(s[:-1].replace("T", " "))
            return int(dt.replace(tzinfo=datetime.timezone.utc).timestamp())
        dt = datetime.datetime.fromisoformat(s.replace("T", " "))
        return int(dt.timestamp())
    except Exception:
        return None


def devig3(h, d, a):
    """三档比例去水."""
    try:
        inv = [1.0 / h, 1.0 / d, 1.0 / a]
        s = sum(inv)
        return [x / s for x in inv]
    except Exception:
        return None


def devig2(o, u):
    try:
        inv = [1.0 / o, 1.0 / u]
        s = sum(inv)
        return [x / s for x in inv]
    except Exception:
        return None


def _pick_nearest(rows, target):
    """从 [(selection, odds, captured_at)] 里取离 target 最近的 3 选."""
    d = {}
    for sel, odds, cap in rows:
        if sel not in d or abs(cap - target) < abs(d[sel][1] - target):
            d[sel] = (odds, cap)
    if len(d) == 3:
        return {k: d[k][0] for k in ("home", "draw", "away")}
    return None


def build_dataset():
    """返回 (X, y, meta). X 列: [p_ht_home, p_ht_draw, p_ht_away, ht_draw_odds_raw,
       p_ou1h_over, p_ft_draw]."""
    con = sqlite3.connect(GQ, timeout=30)
    cur = con.cursor()

    mo = cur.execute(
        """SELECT home, away, kickoff, ht_score_home, ht_score_away
           FROM match_outcomes
           WHERE ht_score_home IS NOT NULL AND ht_score_away IS NOT NULL
             AND score_home IS NOT NULL AND score_away IS NOT NULL
             -- HT 污染清洗(2026-08-27): 半场总进球须 < 全场总进球, 否则 ht 被回填为全场
             AND (ht_score_home + ht_score_away) < (score_home + score_away)"""
    ).fetchall()

    # 一次性读入 1X2_1H / OU_1H_1.0 / 1X2 快照
    def load(market, sels):
        d = {}
        q = ",".join("?" * len(sels))
        for mk, sel, odds, cap in cur.execute(
                f"""SELECT match_key, selection, odds, captured_at FROM odds_snapshots
                    WHERE market=? AND selection IN ({q}) AND odds>? AND odds<?""",
                (market, *sels, MIN_ODDS, MAX_ODDS)):
            d.setdefault(mk, []).append((sel, odds, cap))
        return d

    snap_1x2_1h = load("1X2_1H", ("home", "draw", "away"))
    snap_ou1h = load("OU_1H_1.0", ("over", "under"))
    snap_1x2 = load("1X2", ("home", "draw", "away"))

    X, y = [], []
    n_ou1h = n_ft = 0
    for home, away, ko, hsh, hsa in mo:
        mk = f"{home} vs {away}"
        ko_ts = parse_kickoff_ts(ko)
        if ko_ts is None:
            continue
        target = ko_ts + TARGET_MIN
        lo, hi = ko_ts + WINDOW_LO, ko_ts + WINDOW_HI

        # 核心: 1X2_1H 三档 (15min)
        rows = snap_1x2_1h.get(mk, [])
        in_win = [(s, o, c) for s, o, c in rows if lo <= c <= hi]
        three = _pick_nearest(in_win, target)
        if not three:
            continue
        p = devig3(three["home"], three["draw"], three["away"])
        if not p:
            continue

        feat = [p[0], p[1], p[2], float(three["draw"])]

        # OU_1H_1.0 over 概率 (15min)
        ou_rows = snap_ou1h.get(mk, [])
        ou_in = [(s, o, c) for s, o, c in ou_rows if lo <= c <= hi]
        ou_sel = {}
        for s, o, c in ou_in:
            if s not in ou_sel or abs(c - target) < abs(ou_sel[s][1] - target):
                ou_sel[s] = (o, c)
        if "over" in ou_sel and "under" in ou_sel:
            p2 = devig2(ou_sel["over"][0], ou_sel["under"][0])
            feat.append(p2[0] if p2 else np.nan)
            if p2:
                n_ou1h += 1
        else:
            feat.append(np.nan)

        # 全场 1X2 draw 去水 (15min)
        ft_rows = snap_1x2.get(mk, [])
        ft_in = [(s, o, c) for s, o, c in ft_rows if lo <= c <= hi]
        ft3 = _pick_nearest(ft_in, target)
        if ft3:
            pf = devig3(ft3["home"], ft3["draw"], ft3["away"])
            feat.append(pf[1] if pf else np.nan)
            if pf:
                n_ft += 1
        else:
            feat.append(np.nan)

        X.append(feat)
        y.append(1 if (hsh == hsa) else 0)

    con.close()
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=int)
    logger.info(f"[ht_draw] 样本 {len(y)} (平局 {int(y.sum())}) | OU_1H 覆盖 {n_ou1h} | 全场draw覆盖 {n_ft}")
    return X, y


def evaluate(X, y):
    """30×5 分层 CV, 评估 AUC + 命中率. 对照 naive."""
    kf = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    cols = ["p_ht_home", "p_ht_draw", "p_ht_away", "ht_draw_odds_raw", "p_ou1h_over", "p_ft_draw"]

    # naive: 全押非平局 (多数类)
    naive_acc = 1.0 - y.mean()

    # 单一特征基线: p_ht_draw 直接做分数 (15min 去水平局概率)
    p_draw = X[:, 1]

    results = {"naive_acc": naive_acc, "p_draw_auc": None, "gbm": {}, "dt": {}}
    p_draw_aucs = []
    for tr, te in kf.split(X, y):
        if len(np.unique(y[te])) < 2:
            continue
        p_draw_aucs.append(roc_auc_score(y[te], p_draw[te]))
    results["p_draw_auc"] = float(np.mean(p_draw_aucs)) if p_draw_aucs else None

    # GBM (多特征, 含 NaN 用列中位数填充; 全 NaN 列填 0)
    def run_model(clf, name):
        aucs, accs = [], []
        for tr, te in kf.split(X, y):
            med = np.nanmedian(X[tr], axis=0)
            med = np.where(np.isnan(med), 0.0, med)
            Xtr = np.where(np.isnan(X[tr]), med, X[tr])
            Xte = np.where(np.isnan(X[te]), med, X[te])
            if len(np.unique(y[tr])) < 2:
                continue
            clf.fit(Xtr, y[tr])
            prob = clf.predict_proba(Xte)[:, 1]
            aucs.append(roc_auc_score(y[te], prob))
            accs.append((prob >= 0.5).astype(int) == y[te])
        results[name] = {
            "auc": float(np.mean(aucs)) if aucs else None,
            "auc_std": float(np.std(aucs)) if aucs else None,
            "acc": float(np.mean(np.concatenate(accs))) if accs else None,
        }

    run_model(GradientBoostingClassifier(n_estimators=200, max_depth=3,
                                         learning_rate=0.05, random_state=42), "gbm")
    run_model(DecisionTreeClassifier(max_depth=3, random_state=42), "dt")
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    X, y = build_dataset()
    if len(y) < 100:
        print(f"样本不足 {len(y)}, 无法训练")
        raise SystemExit(1)
    print(f"\n=== 半场平局模型 (开赛15min滚动赔率) ===")
    print(f"样本: {len(y)} (半场平局 {int(y.sum())} = {y.mean()*100:.1f}%)")
    print(f"特征列: p_ht_home/p_ht_draw/p_ht_away/ht_draw_odds_raw/p_ou1h_over/p_ft_draw")
    r = evaluate(X, y)
    print(f"\nnaive(全押非平局) 命中率: {r['naive_acc']*100:.2f}%")
    print(f"单特征 p_ht_draw(15min去水平局概率) AUC: {r['p_draw_auc']:.4f}" if r["p_draw_auc"] else "p_draw_auc: N/A")
    print(f"GBM  AUC: {r['gbm']['auc']:.4f} ± {r['gbm']['auc_std']:.4f} | acc: {r['gbm']['acc']*100:.2f}%" if r['gbm']['auc'] else "GBM: N/A")
    print(f"DT   AUC: {r['dt']['auc']:.4f} ± {r['dt']['auc_std']:.4f} | acc: {r['dt']['acc']*100:.2f}%" if r['dt']['auc'] else "DT: N/A")
