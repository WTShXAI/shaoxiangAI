# -*- coding: utf-8 -*-
"""
统一滚球(in-play)回测 · 双系统准确率对决
=========================================
数据源 : D:/Architecture/data/events.db
         odds_snapshots (in-play 1X2 三元组) + match_outcomes (终场 result 真值)
真值   : match_outcomes.result ∈ {home,draw,away} (经 home||' vs '||away = match_key 关联)

重要诚实声明:
  events.db 的 in-play 采集集中在 2026-07-30 ~ 2026-08-27 一个约 4 周窗口,
  无更长历史跨度可做"训练窗 < 测试窗"的时间切分。故本回测为 *held-out in-play 评估*:
  每个预测点只使用"该时刻可得信息"(当场 live 1X2 赔率 + 当前比分 + 当前分钟),
  不存在未来泄露; 两参赛方均为闭式/规则型(无 in-play 训练泄露)。

参赛方:
  A. 本系统 (D:/Architecture)  — analysis/live_goal_probe + analysis/inplay_calibration
     真实生产入口的 live 概率层:
       去水隐含 1X2 → _reverse_poisson_total 反推静态 λ
       → (比分已知时) dynamic_team_lambda 以当前比分+剩余时间做贝叶斯收缩后验 λ(t)
       → simulate_inplay_1x2 终场 1X2 分布 → isotonic_calibrate_1x2(按联赛等渗校准)
     这正是"live 特征层"(当前比分+分钟), 即与 GitHub 冻结仓拉开差距的维度。
  B. GitHub shaoxiangAI 仓     — agents/heuristic_predictor.py :: HeuristicPredictor
     纯 numpy 规则型 odds→probs (feature_weight=0.30 + odds_weight=0.70, SP R1/R6 规则),
     冻结仓唯一可无头运行入口。**其 live 特征层(比分/分钟)在冻结态不可参赛**——
     HeuristicPredictor 不接受 score/minute, 故在任意 in-play 状态都只用 live 赔率。
基线         : 当场 live 1X2 去水隐含概率 (naive implied)。

指标: 宏平均 AUC(OvR) / LogLoss / 多类 Brier / Top-1 准确率 / 每类 AUC。
"""
import os, sys, json, sqlite3, random, math
from collections import Counter, defaultdict
import numpy as np

ROOT = r"D:\Architecture"
GH   = r"C:\Users\ShXAI\Documents\GitHub\shaoxiangAI"
DB   = os.path.join(ROOT, "data", "events.db")
SAMPLE_N = 6000          # 从全部 in-play 状态抽样上限(控时); 全量若更少则全用
SEED = 20260827

# ---- 复用本系统真实 live 概率层 ----
sys.path.insert(0, ROOT)
from analysis.live_goal_probe import _dewater_1x2, _reverse_poisson_total
from analysis.inplay_calibration import (
    dynamic_team_lambda, simulate_inplay_1x2, isotonic_calibrate_1x2,
    _match_probs as _ic_match_probs,
)

# ---- GitHub 预测器 (纯 numpy, 无头) ----
sys.path.insert(0, os.path.join(GH, "agents"))
from heuristic_predictor import HeuristicPredictor

LABELS = ["H", "D", "A"]
LIDX   = {"home": 0, "draw": 1, "away": 2}

# ── 记忆化: _reverse_poisson_total 的 116×16 网格搜索很贵, 按去水三元组缓存 ──
_RP_CACHE = {}
def reverse_poisson_cached(ph, pd_, pa):
    key = (round(ph, 3), round(pd_, 3), round(pa, 3))
    v = _RP_CACHE.get(key)
    if v is not None:
        return v
    r = _reverse_poisson_total(ph, pd_, pa)
    _RP_CACHE[key] = r
    return r

_MP_CACHE = {}
def match_probs_cached(lh, la):
    key = (round(lh, 3), round(la, 3))
    v = _MP_CACHE.get(key)
    if v is not None:
        return v
    r = _lg_match_probs(lh, la)
    _MP_CACHE[key] = r
    return r

def system_live_probs(h, d, a, sh, sa, minute, league):
    """本系统 live 概率层(复刻 predict_fulltime_outcome 内部管线)。返回 [ph,pd,pa] 或 None。"""
    x2 = _dewater_1x2(h, d, a)
    if x2 is None:
        return None
    rev = reverse_poisson_cached(x2[0], x2[1], x2[2])
    if rev is None:
        return None
    _, static_lh, static_la = rev
    if (sh + sa) > 0:
        _hp, _ap, _rem = dynamic_team_lambda(static_lh, static_la, sh, sa, minute)
        _ph, _pd, _pa = simulate_inplay_1x2(_hp, _ap, sh, sa, minute)
        if max(_ph, _pd, _pa) < 0.9:
            _ph, _pd, _pa, _ = isotonic_calibrate_1x2(_ph, _pd, _pa, league)
        return [float(_ph), float(_pd), float(_pa)]
    else:
        # 0-0: 网格搜索拟合 x2 (与系统 0-0 路径一致, 本质≈去水隐含)
        best = None
        T_remain = 2.5
        for ri in range(10, 91):
            r = ri / 100.0
            lh, la = T_remain * r, T_remain * (1 - r)
            mp = _ic_match_probs(lh, la)   # 归一化版本 (inplay_calibration)
            if mp is None:
                continue
            dd = (mp[0] - x2[0]) ** 2 + (mp[1] - x2[1]) ** 2 + (mp[2] - x2[2]) ** 2
            if best is None or dd < best[0]:
                best = (dd, mp[0], mp[1], mp[2])
        if best is None:
            return [x2[0], x2[1], x2[2]]
        return [float(best[1]), float(best[2]), float(best[3])]

def github_probs(h, d, a):
    """GitHub: HeuristicPredictor 无头 odds→probs (冻结仓唯一入口, 无 live 特征)。"""
    hp = HeuristicPredictor()
    X = np.zeros((1, 1))
    p = hp.predict_proba(X, feature_names=[], odds_data={"home": h, "draw": d, "away": a}, league_name="")
    return [float(p[0][0]), float(p[0][1]), float(p[0][2])]

def naive_probs(h, d, a):
    x2 = _dewater_1x2(h, d, a)
    return [x2[0], x2[1], x2[2]] if x2 else [1/3, 1/3, 1/3]

def fetch_inplay():
    """取 in-play 1X2 完整三元组(按 match_key+minute_at+score_at 聚合), 关联终场真值。"""
    con = sqlite3.connect(DB)
    con.execute("PRAGMA busy_timeout=30000")
    cur = con.cursor()
    # match_outcomes: home/away/league/result
    cur.execute("SELECT home||' vs '||away, league, result FROM match_outcomes WHERE result IN ('home','draw','away')")
    mo = {}
    for k, lg, res in cur.fetchall():
        mo[k] = (lg, res)
    q = """
        SELECT match_key, minute_at, score_at,
               AVG(CASE WHEN selection='home' THEN odds END) AS h,
               AVG(CASE WHEN selection='draw' THEN odds END) AS d,
               AVG(CASE WHEN selection='away' THEN odds END) AS a
        FROM odds_snapshots
        WHERE market='1X2' AND minute_at>0 AND selection IN ('home','draw','away')
        GROUP BY match_key, minute_at, score_at
        HAVING h IS NOT NULL AND d IS NOT NULL AND a IS NOT NULL
    """
    rows = cur.execute(q).fetchall()
    con.close()
    out = []
    for mk, minute, score_at, h, d, a in rows:
        if mk not in mo:
            continue
        if not (1 <= minute <= 95):
            continue
        if not (h > 1.01 and d > 1.01 and a > 1.01):
            continue
        sh = sa = 0
        if score_at and '-' in score_at:
            try:
                sh, sa = (int(x) for x in score_at.split('-'))
            except Exception:
                sh = sa = 0
        lg, res = mo[mk]
        out.append(dict(mk=mk, minute=int(minute), sh=sh, sa=sa, h=h, d=d, a=a,
                        league=lg, y=LIDX[res]))
    return out

def _normalize_rows(M):
    """防御性归一化: 任何非有限/非和为1的行 → 拉回合法概率分布, 并返回需修正的行数(透明)。"""
    M = np.asarray(M, dtype=float)
    M = np.where(np.isfinite(M), M, 0.0)
    s = M.sum(axis=1, keepdims=True)
    bad = int(np.sum((~np.isfinite(s)) | (s <= 0) | (np.abs(s - 1.0) > 1e-6)))
    M = np.where(s > 0, M / s, np.full_like(M, 1.0/3))
    return M, bad

def metrics(y_true, y_score):
    from sklearn.metrics import roc_auc_score, log_loss
    y_score = _normalize_rows(y_score)[0]
    auc = roc_auc_score(y_true, y_score, multi_class="ovr", average="macro", labels=[0,1,2])
    ll  = log_loss(y_true, y_score, labels=[0,1,2])
    onehot = np.zeros_like(y_score); onehot[np.arange(len(y_true)), y_true] = 1.0
    brier = ((onehot - y_score) ** 2).sum(axis=1).mean()
    acc = (y_score.argmax(axis=1) == y_true).mean()
    per = {}
    for i, lab in enumerate(LABELS):
        try:
            per[lab] = float(roc_auc_score((y_true == i).astype(int), y_score[:, i]))
        except Exception:
            per[lab] = float("nan")
    return dict(auc=float(auc), logloss=float(ll), brier=float(brier), acc=float(acc), per_class_auc=per)

def run_subset(data, tag):
    y = np.array([x["y"] for x in data])
    sys_p, gh_p, nv_p = [], [], []
    for x in data:
        sp = system_live_probs(x["h"], x["d"], x["a"], x["sh"], x["sa"], x["minute"], x["league"])
        sys_p.append(sp if sp else [1/3,1/3,1/3])
        gh_p.append(github_probs(x["h"], x["d"], x["a"]))
        nv_p.append(naive_probs(x["h"], x["d"], x["a"]))
    S_arr, bad_s = _normalize_rows(sys_p)
    G_arr, bad_g = _normalize_rows(gh_p)
    N_arr, bad_n = _normalize_rows(nv_p)
    S, G, N = metrics(y, S_arr), metrics(y, G_arr), metrics(y, N_arr)
    maj = Counter(x["y"] for x in data).most_common(1)[0][1] / len(data)
    return tag, len(data), maj, bad_s, bad_g, S, G, N, S_arr, G_arr, N_arr, y

def main():
    random.seed(SEED)
    allrows = fetch_inplay()
    print(f"[data] in-play 状态总数(可关联真值+合规): {len(allrows)}")
    if len(allrows) > SAMPLE_N:
        rows = random.sample(allrows, SAMPLE_N)
        print(f"[sample] 抽样至 {SAMPLE_N} (seed={SEED})")
    else:
        rows = allrows
    # 主评估
    tag, n, maj, sok, gok, S, G, N, S_arr, G_arr, N_arr, y_arr = run_subset(rows, "ALL in-play")
    # 子集: 比分已知(>0-0)
    score_known = [x for x in rows if (x["sh"] + x["sa"]) > 0]
    _, n2, _, _, _, S2, G2, N2, _, _, _, _ = run_subset(score_known, "score-known(>0-0)")
    # 子集: 0-0
    zerozero = [x for x in rows if (x["sh"] + x["sa"]) == 0]
    _, n3, _, _, _, S3, G3, N3, _, _, _, _ = run_subset(zerozero, "0-0")
    # 子集: 早段(1-45')
    early = [x for x in rows if 1 <= x["minute"] <= 45]
    _, n4, _, _, _, S4, G4, N4, _, _, _, _ = run_subset(early, "early(1-45')")

    # ── 导出逐场概率向量(供集成扫描复用, 避免重跑昂贵网格搜索) ──
    per_sample = []
    for i, x in enumerate(rows):
        per_sample.append(dict(
            y=int(y_arr[i]),
            sys=[float(v) for v in S_arr[i]],
            gh=[float(v) for v in G_arr[i]],
            nv=[float(v) for v in N_arr[i]],
            mk=x["mk"], minute=x["minute"], sh=x["sh"], sa=x["sa"], league=x["league"],
        ))
    ps_path = r"D:\Architecture\deliverables\inplay_per_sample.json"
    os.makedirs(os.path.dirname(ps_path), exist_ok=True)
    with open(ps_path, "w", encoding="utf-8") as f:
        json.dump(per_sample, f, ensure_ascii=False)
    print(f"\n[dump] 逐场概率向量已存 {ps_path} (n={len(per_sample)})")

    def show(tag, n, maj, S, G, N):
        print(f"\n===== {tag}  (n={n}, 多数类基线 acc={maj:.3f}) =====")
        hdr = f"{'系统':<30}{'AUC':>8}{'LogLoss':>10}{'Brier':>9}{'Acc':>8}"
        print(hdr); print("-"*len(hdr))
        for name, m in [("本系统(live 概率层)", S), ("GitHub(HeuristicPredictor)", G), ("基线(live去水隐含)", N)]:
            print(f"{name:<28}{m['auc']:>8.4f}{m['logloss']:>10.4f}{m['brier']:>9.4f}{m['acc']:>8.3f}")
        pcS, pcG, pcN = S['per_class_auc'], G['per_class_auc'], N['per_class_auc']
        print(f"  每类AUC 本系统 H={pcS['H']:.3f}/D={pcS['D']:.3f}/A={pcS['A']:.3f} | GitHub H={pcG['H']:.3f}/D={pcG['D']:.3f}/A={pcG['A']:.3f} | 基线 H={pcN['H']:.3f}/D={pcN['D']:.3f}/A={pcN['A']:.3f}")

    show("ALL in-play", n, maj, S, G, N)
    show(f"score-known(>0-0) n={n2}", n2, maj, S2, G2, N2)
    show(f"0-0 n={n3}", n3, maj, S3, G3, N3)
    show(f"early(1-45') n={n4}", n4, maj, S4, G4, N4)
    print(f"\n[diag] 主评估中需修正的非归一化行数: 本系统={sok}  GitHub={gok}")

    result = {
        "data_source": "events.db (odds_snapshots in-play 1X2 + match_outcomes.result)",
        "window": "2026-07-30 ~ 2026-08-27 (single ~4-week collection window; held-out in-play eval, no temporal OOS possible)",
        "n_total_inplay_states": len(allrows),
        "n_evaluated": n, "seed": SEED, "majority_baseline_acc": round(maj, 4),
        "all_inplay": {"system": S, "github": G, "naive": N},
        "score_known": {"n": n2, "system": S2, "github": G2, "naive": N2},
        "zero_zero": {"n": n3, "system": S3, "github": G3, "naive": N3},
        "early_1_45": {"n": n4, "system": S4, "github": G4, "naive": N4},
        "system_bad_rows_corrected": sok, "github_bad_rows_corrected": gok,
    }
    outp = r"D:\Architecture\deliverables\unified_inplay_duel_result.json"
    os.makedirs(os.path.dirname(outp), exist_ok=True)
    with open(outp, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n[ok] 结果已存 {outp}")

if __name__ == "__main__":
    main()
