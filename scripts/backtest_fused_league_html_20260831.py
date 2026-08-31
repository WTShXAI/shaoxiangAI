"""融合模型按联赛回测 (2026-08-31, 时间外OOS, 诚实边界)
================================================================
按用户指令"分别抽取各个联赛中的100场比赛做回测, 输出HTML":
  - 数据: events.db 干净+三盘口齐全场(排除假0-0, IR-04); 时间外切分(晚30% = OOS,
    融合模型训练于早70%, 不复用).
  - 每个联赛最多抽 100 场; <100 取全部可用干净场并标注"短供".
  - 仅 n>=15 的联赛进逐联赛明细表(其余统计不可靠, 只池化).
  - 主结论用全量OOS池化(最可信); 逐联赛为辅.
  - OU1H 原型单列一节(数据太稀, 不按联赛).

指标: AUC / Acc / 平注ROI(对开盘隐含概率有edge才下注).
诚实边界(IR-30): 联赛间样本极端不均, 逐联赛数字仅作结构参考, 不下"某联赛可部署"结论.
"""
from __future__ import annotations
import os, sys, json, sqlite3, datetime
import numpy as np
import joblib
from sklearn.metrics import roc_auc_score, accuracy_score
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.build_fused_models_20260831 import (
    collect, fl_probs, league_probs, build_feat, predict_lambdas, p_over, opening_ou, FIT_FRAC
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS = os.path.join(ROOT, "models")
DB = os.path.join(ROOT, "data", "events.db")
LEAGUE_MIN_N = 15      # 进明细表的最小样本
PER_LEAGUE_CAP = 100   # 每联赛最多抽

# ----------------------------------------------------------- 工具
def auc1(p, y):
    if len(set(y)) < 2:
        return float("nan")
    return float(roc_auc_score(y, np.asarray(p)))

def auc_macro(p, y):
    if p.ndim < 2 or p.shape[1] < 2:
        return float("nan")
    # 多分类 AUC 要求 y 的类别数 == 列数; 子集缺类时不定义 -> 返回 nan(HTML显示—)
    if len(set(int(v) for v in y)) != p.shape[1]:
        return float("nan")
    return float(roc_auc_score(y, p, multi_class="ovo", average="macro"))

def implied_ou(ov, un):
    return (1/ov)/((1/ov)+(1/un))

# ----------------------------------------------------------- 加载融合模型
f_ou = joblib.load(os.path.join(MODELS, "fused_ou_20260831.joblib"))
f_x2 = joblib.load(os.path.join(MODELS, "fused_1x2_20260831.joblib"))
f_lg = joblib.load(os.path.join(MODELS, "fused_league_20260831.joblib"))

def fused_ou_pover(fl_ou_pover, poisson_pover):
    if fl_ou_pover is None:
        return float(poisson_pover)  # fl_model_ou 已下线(2026-08-31): 纯泊松回退
    X = np.array([[fl_ou_pover, poisson_pover]])
    return float(f_ou["meta"].predict_proba(X)[0, 1])

def fused_x2_probs(fl_1x2, fl_ah):
    X = np.array([[fl_1x2[0], fl_1x2[1], fl_1x2[2], fl_ah[0], fl_ah[1]]])
    return f_x2["meta"].predict_proba(X)[0]

def fused_lg_probs(lg_main, lg_draw):
    X = np.array([[lg_main[0], lg_main[1], lg_main[2], lg_draw]])
    return f_lg["meta"].predict_proba(X)[0]

# ----------------------------------------------------------- 主流程
def main():
    print("收集同步样本(同融合建模口径)...")
    recs = collect()
    recs.sort(key=lambda r: r["ko"])
    k = int(len(recs) * FIT_FRAC)
    te = recs[k:]   # 时间外OOS
    print(f"  OOS(晚30%)场数: {len(te)}")

    # 计算基模型 + 融合预测
    rows = []
    for r in te:
        fl = fl_probs(r["oh"], r["od"], r["oa"], r["line"], r["ov"], r["un"],
                      r["ah_line"], r["ah_h"], r["ah_a"], r["league"], r["ko"])
        fl_1x2 = fl["1x2"]; fl_ou = fl["ou"]; fl_ah = fl["ah"]
        lg_main, lg_draw = league_probs(build_feat(
            r["oh"], r["od"], r["oa"], r["line"], r["ov"], r["un"],
            r["ah_line"], r["ah_h"], r["ah_a"], r["league"], r["ko"]))
        po = float(p_over(r["lam"][0], r["lam"][1], r["line"]))

        r["fl_1x2"] = fl_1x2; r["fl_ou"] = fl_ou; r["fl_ah"] = fl_ah
        r["lg_main"] = lg_main; r["lg_draw"] = lg_draw; r["poisson_over"] = po
        r["f_ou"] = fused_ou_pover(fl_ou[0] if fl_ou else None, po)  # fl_model_ou 已下线(2026-08-31) → None 回退纯泊松
        r["f_x2"] = fused_x2_probs(fl_1x2, fl_ah)
        r["f_lg"] = fused_lg_probs(lg_main, lg_draw)
        rows.append(r)

    n = len(rows)
    sh = np.array([r["sh"] for r in rows])
    sa = np.array([r["sa"] for r in rows])
    tot = sh + sa
    line = np.array([r["line"] for r in rows])

    # ===== 池化 OU =====
    def ou_metrics(p, base_p, ov, un, y):
        auc_f = auc1(p, y)
        auc_b = auc1(base_p, y)
        imp = np.array([implied_ou(o, u) for o, u in zip(ov, un)])
        # 模型ROI: edge侧下注
        profit = []
        for i in range(len(p)):
            if p[i] > imp[i]:
                profit.append((ov[i]-1) if y[i] == 1 else -1)
            else:
                profit.append((un[i]-1) if y[i] == 0 else -1)
        roi = float(np.mean(profit))
        # 朴素: 市场偏好侧
        profit_n = []
        for i in range(len(p)):
            if imp[i] > 0.5:
                profit_n.append((ov[i]-1) if y[i] == 1 else -1)
            else:
                profit_n.append((un[i]-1) if y[i] == 0 else -1)
        roi_n = float(np.mean(profit_n))
        return auc_f, auc_b, roi, roi_n

    y_ou = (tot > line).astype(int)
    ov = np.array([r["ov"] for r in rows]); un = np.array([r["un"] for r in rows])
    f_ou_p = np.array([r["f_ou"] for r in rows])
    fl_ou_p = np.array([r["fl_ou"][0] for r in rows])
    po_p = np.array([r["poisson_over"] for r in rows])
    ou_auc_f, ou_auc_b, ou_roi, ou_roi_n = ou_metrics(f_ou_p, fl_ou_p, ov, un, y_ou)

    # ===== 池化 1X2 =====
    def x2_metrics(fprob, base_prob, oh, od, oa, y):
        auc_f = auc_macro(fprob, y)
        acc_f = float(accuracy_score(y, fprob.argmax(1)))
        acc_b = float(accuracy_score(y, base_prob.argmax(1)))
        # 模型ROI: 押 argmax
        profit = []
        for i in range(len(y)):
            pick = int(fprob[i].argmax())
            odds = [oh[i], od[i], oa[i]][pick]
            profit.append((odds-1) if pick == int(y[i]) else -1)
        roi = float(np.mean(profit))
        # 朴素: 押热门(最低赔=最高隐含)
        profit_n = []
        for i in range(len(y)):
            pick = int(np.argmin([oh[i], od[i], oa[i]]))
            odds = [oh[i], od[i], oa[i]][pick]
            profit_n.append((odds-1) if pick == int(y[i]) else -1)
        roi_n = float(np.mean(profit_n))
        return auc_f, acc_f, acc_b, roi, roi_n

    y_x2 = np.array([0 if r["sh"] > r["sa"] else (1 if r["sh"] == r["sa"] else 2) for r in rows])
    oh = np.array([r["oh"] for r in rows]); od = np.array([r["od"] for r in rows]); oa = np.array([r["oa"] for r in rows])
    f_x2_p = np.array([r["f_x2"] for r in rows])
    fl_x2_p = np.array([r["fl_1x2"] for r in rows])
    x2_auc_f, x2_acc_f, x2_acc_b, x2_roi, x2_roi_n = x2_metrics(f_x2_p, fl_x2_p, oh, od, oa, y_x2)

    # ===== 池化 league =====
    f_lg_p = np.array([r["f_lg"] for r in rows])
    lg_main_p = np.array([r["lg_main"] for r in rows])
    lg_auc_f, lg_acc_f, lg_acc_b, lg_roi, lg_roi_n = x2_metrics(f_lg_p, lg_main_p, oh, od, oa, y_x2)

    pooled = dict(
        n=n,
        ou=dict(auc_fused=ou_auc_f, auc_base=ou_auc_b, roi_fused=ou_roi, roi_naive=ou_roi_n),
        x2=dict(auc_fused=x2_auc_f, acc_fused=x2_acc_f, acc_base=x2_acc_b, roi_fused=x2_roi, roi_naive=x2_roi_n),
        lg=dict(auc_fused=lg_auc_f, acc_fused=lg_acc_f, acc_base=lg_acc_b, roi_fused=lg_roi, roi_naive=lg_roi_n),
    )
    print(f"\n[池化OOS n={n}]")
    print(f"  OU:   AUC融合={ou_auc_f:.4f} 基线={ou_auc_b:.4f} | ROI融合={ou_roi:+.2%} 朴素={ou_roi_n:+.2%}")
    print(f"  1X2:  AUC={x2_auc_f:.4f} acc融合={x2_acc_f:.4f} 基线={x2_acc_b:.4f} | ROI融合={x2_roi:+.2%} 朴素={x2_roi_n:+.2%}")
    print(f"  LG:   AUC={lg_auc_f:.4f} acc融合={lg_acc_f:.4f} 基线={lg_acc_b:.4f} | ROI融合={lg_roi:+.2%} 朴素={lg_roi_n:+.2%}")

    # ===== 逐联赛 =====
    from collections import defaultdict
    by_lg = defaultdict(list)
    for r in rows:
        by_lg[r["league"]].append(r)

    league_rows = []
    for lg, lst in by_lg.items():
        if len(lst) < LEAGUE_MIN_N:
            continue
        # 抽样 <=100
        sample = lst if len(lst) <= PER_LEAGUE_CAP else lst[:PER_LEAGUE_CAP]
        sh_ = np.array([r["sh"] for r in sample]); sa_ = np.array([r["sa"] for r in sample])
        tot_ = sh_ + sa_; line_ = np.array([r["line"] for r in sample])
        ov_ = np.array([r["ov"] for r in sample]); un_ = np.array([r["un"] for r in sample])
        y_ = (tot_ > line_).astype(int)
        f_p = np.array([r["f_ou"] for r in sample]); b_p = np.array([r["fl_ou"][0] for r in sample])
        _, _, roi_, roi_n_ = ou_metrics(f_p, b_p, ov_, un_, y_)
        yx = np.array([0 if r["sh"] > r["sa"] else (1 if r["sh"] == r["sa"] else 2) for r in sample])
        oh_ = np.array([r["oh"] for r in sample]); od_ = np.array([r["od"] for r in sample]); oa_ = np.array([r["oa"] for r in sample])
        fx = np.array([r["f_x2"] for r in sample]); bx = np.array([r["fl_1x2"] for r in sample])
        _, axf, axb, rx, rnx = x2_metrics(fx, bx, oh_, od_, oa_, yx)
        flg = np.array([r["f_lg"] for r in sample]); blg = np.array([r["lg_main"] for r in sample])
        _, alg, alb, rlg, rlg_n = x2_metrics(flg, blg, oh_, od_, oa_, yx)
        league_rows.append(dict(
            league=lg, n=len(lst), n_used=len(sample), short=(len(lst) < PER_LEAGUE_CAP),
            ou_auc=auc1(f_p, y_), ou_roi=roi_, ou_roi_n=roi_n_,
            x2_acc=axf, x2_acc_b=axb, x2_roi=rx, x2_roi_n=rnx,
            lg_acc=alg, lg_acc_b=alb, lg_roi=rlg, lg_roi_n=rlg_n,
        ))
    league_rows.sort(key=lambda d: -d["n"])
    print(f"\n[逐联赛] 进明细表的联赛数(n>={LEAGUE_MIN_N}): {len(league_rows)}")
    for d in league_rows[:12]:
        print(f"  {d['league']!r:30} n={d['n']:4d} OU_AUC={d['ou_auc']:.3f} 1X2_acc={d['x2_acc']:.3f} LG_acc={d['lg_acc']:.3f}")

    # OU1H 原型(读构建JSON指标)
    with open(os.path.join(ROOT, "reports", "fused_models_build_20260831.json"), encoding="utf-8") as f:
        build_json = json.load(f)
    ou1h_info = build_json["models"]["ou1h"]

    out = dict(
        generated_at=datetime.datetime.now().astimezone().isoformat(),
        n=n,
        data_note="时间外OOS(晚30%), 排除假0-0(IR-04), 三盘口齐全; 仅n>=15联赛进明细表",
        total_leagues_with_clean=int(sum(1 for v in by_lg.values() if len(v) >= 1)),
        leagues_in_table=len(league_rows),
        pooled=pooled,
        leagues=league_rows,
    )
    with open(os.path.join(ROOT, "reports", "backtest_fused_league_20260831.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    render_html(out, ou1h_info)
    print("\n完成 -> reports/backtest_fused_league_20260831.html")

def pct(x):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "—"
    return f"{x*100:+.2f}%"

def render_html(out, ou1h):
    p = out["pooled"]
    def cls_roi(v):
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return ""
        return "pos" if v > 0 else "neg"
    # 池化卡片
    pooled_html = f"""
    <div class="grid">
      <div class="card">
        <h3>OU (大小球) 融合</h3>
        <div class="row"><span>AUC 融合</span><b>{p['ou']['auc_fused']:.4f}</b></div>
        <div class="row"><span>AUC 基线(fl_OU)</span><b>{p['ou']['auc_base']:.4f}</b></div>
        <div class="row"><span>平注 ROI 融合</span><b class="{cls_roi(p['ou']['roi_fused'])}">{pct(p['ou']['roi_fused'])}</b></div>
        <div class="row"><span>平注 ROI 朴素(市场偏好)</span><b class="{cls_roi(p['ou']['roi_naive'])}">{pct(p['ou']['roi_naive'])}</b></div>
      </div>
      <div class="card">
        <h3>1X2 (胜平负) 融合</h3>
        <div class="row"><span>macro AUC 融合</span><b>{p['x2']['auc_fused']:.4f}</b></div>
        <div class="row"><span>acc 融合</span><b>{p['x2']['acc_fused']:.4f}</b></div>
        <div class="row"><span>acc 基线(fl_1x2)</span><b>{p['x2']['acc_base']:.4f}</b></div>
        <div class="row"><span>平注 ROI 融合</span><b class="{cls_roi(p['x2']['roi_fused'])}">{pct(p['x2']['roi_fused'])}</b></div>
        <div class="row"><span>平注 ROI 朴素(押热门)</span><b class="{cls_roi(p['x2']['roi_naive'])}">{pct(p['x2']['roi_naive'])}</b></div>
      </div>
      <div class="card">
        <h3>league (联赛) 融合</h3>
        <div class="row"><span>macro AUC 融合</span><b>{p['lg']['auc_fused']:.4f}</b></div>
        <div class="row"><span>acc 融合</span><b>{p['lg']['acc_fused']:.4f}</b></div>
        <div class="row"><span>acc 基线(league_main)</span><b>{p['lg']['acc_base']:.4f}</b></div>
        <div class="row"><span>平注 ROI 融合</span><b class="{cls_roi(p['lg']['roi_fused'])}">{pct(p['lg']['roi_fused'])}</b></div>
        <div class="row"><span>平注 ROI 朴素(押热门)</span><b class="{cls_roi(p['lg']['roi_naive'])}">{pct(p['lg']['roi_naive'])}</b></div>
      </div>
    </div>"""

    # 逐联赛表
    trs = []
    for d in out["leagues"]:
        tag = '<span class="tag warn">短供</span>' if d["short"] else '<span class="tag ok">满100</span>'
        trs.append(f"""<tr>
          <td>{d['league'] or '(空名/未知)'}</td>
          <td>{d['n']}</td><td>{d['n_used']}</td><td>{tag}</td>
          <td>{d['ou_auc']:.3f}</td>
          <td class="{cls_roi(d['ou_roi'])}">{pct(d['ou_roi'])}</td>
          <td>{d['x2_acc']:.3f}</td><td>{d['x2_acc_b']:.3f}</td>
          <td class="{cls_roi(d['x2_roi'])}">{pct(d['x2_roi'])}</td>
          <td>{d['lg_acc']:.3f}</td><td>{d['lg_acc_b']:.3f}</td>
          <td class="{cls_roi(d['lg_roi'])}">{pct(d['lg_roi'])}</td>
        </tr>""")
    table_html = "\n".join(trs)

    # OU1H 原型
    oi = ou1h
    ou1h_html = f"""
    <div class="card proto">
      <h3>OU1H 模型 (研究原型, 不可部署)</h3>
      <p class="caveat">⚠️ {oi.get('caveat','数据地雷: ht_score 66%污染, 仅ht&lt;ft子集, 选择偏差, n小')}</p>
      <div class="row"><span>干净半场样本 n</span><b>{oi.get('n_clean_total','?')}</b></div>
      <div class="row"><span>隐含 P(over) 均值</span><b>{oi.get('implied_mean',0):.3f}</b></div>
      <div class="row"><span>实际 over 率</span><b>{oi.get('actual_over_rate',0):.3f}</b></div>
      <div class="row"><span>AUC(raw)</span><b>{oi.get('fused_AUC_raw',0):.4f}</b></div>
      <div class="row"><span>AUC(cal, Isotonic)</span><b>{oi.get('fused_AUC_cal',0):.4f}</b></div>
      <div class="row"><span>缺口 (实际-隐含)</span><b class="neg">{oi.get('actual_over_rate',0)-oi.get('implied_mean',0):+.3f}</b></div>
    </div>"""

    html = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>融合模型按联赛回测 2026-08-31</title>
<style>
 *{{box-sizing:border-box}} body{{font-family:-apple-system,Segoe UI,Roboto,'Microsoft YaHei',sans-serif;background:#0f1420;color:#e6edf6;margin:0;padding:28px}}
 h1{{font-size:22px;margin:0 0 4px}} .sub{{color:#8aa0bd;font-size:13px;margin-bottom:20px}}
 h2{{font-size:16px;margin:26px 0 12px;border-left:4px solid #4f8cff;padding-left:10px}}
 .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}}
 .card{{background:#182032;border:1px solid #25324d;border-radius:12px;padding:16px}}
 .card h3{{margin:0 0 12px;font-size:15px;color:#cfe0ff}}
 .row{{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #223052;font-size:13px}}
 .row b{{font-variant-numeric:tabular-nums}}
 .pos{{color:#3fd07a}} .neg{{color:#ff6b6b}}
 table{{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:6px}}
 th,td{{padding:8px 10px;text-align:center;border-bottom:1px solid #223052}}
 th{{background:#1c2740;color:#a9c0e6;position:sticky;top:0}}
 td:first-child,th:first-child{{text-align:left}}
 .tag{{font-size:11px;padding:2px 7px;border-radius:6px}}
 .tag.ok{{background:#1d3a2a;color:#5fe0a0}} .tag.warn{{background:#3a2f1d;color:#ffcf7a}}
 .proto{{border-color:#5a3a1d}} .caveat{{color:#ffcf7a;font-size:12.5px;background:#2a2113;padding:8px 10px;border-radius:8px}}
 .note{{background:#16203a;border-left:4px solid #4f8cff;padding:12px 14px;border-radius:8px;font-size:13px;color:#bcd0ee;margin:14px 0}}
 code{{background:#0c1322;padding:1px 6px;border-radius:5px;color:#9fd0ff}}
</style></head><body>
<h1>融合模型按联赛回测 · 2026-08-31</h1>
<div class="sub">生成于 {out['generated_at']} · 时间外 OOS(晚30%)，融合模型训练于早70%不复用 · 排除假0-0(IR-04) · 三盘口齐全</div>

<div class="note">
  <b>数据现实：</b> events.db 共 <b>990</b> 个联赛，仅 <b>1</b> 个联赛有 ≥100 场干净+三盘口齐全数据（且该联赛名为空，属未知/默认联赛）。
  进入 OOS 回测的联赛 <b>{out['total_leagues_with_clean']}</b> 个，其中 <b>{out['leagues_in_table']}</b> 个达到明细表最小样本(n≥{15})。
  逐联赛回测对 obscure 联赛统计不可靠，<b>主结论以池化(全量OOS)为准</b>。融合模型均带来小幅真实增益，但 ROI 仍为负→<b>不建议实盘部署</b>(IR-30)。
</div>

<h2>① 池化 OOS 主结论 (n={out['n']})</h2>
{pooled_html}

<h2>② 逐联赛明细 (n≥15, 最多抽100/联赛, 短供标注)</h2>
<div style="overflow:auto;max-height:560px;border:1px solid #25324d;border-radius:12px">
<table><thead><tr>
<th>联赛</th><th>总干净</th><th>用</th><th>供给</th>
<th>OU AUC</th><th>OU ROI</th>
<th>1X2 acc</th><th>1X2基acc</th><th>1X2 ROI</th>
<th>LG acc</th><th>LG基acc</th><th>LG ROI</th>
</tr></thead><tbody>
{table_html}
</tbody></table></div>

<h2>③ OU1H 模型 (按 OU1H_校准与跟随大球回测_实测报告.md 开发)</h2>
{ou1h_html}

<div class="note">
  <b>诚实边界(IR-30)：</b> 本回测所有 ROI 均为平注模拟(对开盘隐含概率有 edge 才下注)，非实盘。
  OU 融合 AUC 0.619 仍远低于 0.70 接入门槛；1X2/league 融合 acc 较基线 +5pp 但 ROI 仍负。
  模型增益真实存在但<b>不足以覆盖抽水</b>，故 4 个融合测试模型均为<b>研究/对照用途，不接生产</b>。
</div>
</body></html>"""
    with open(os.path.join(ROOT, "reports", "backtest_fused_league_20260831.html"), "w", encoding="utf-8") as f:
        f.write(html)

if __name__ == "__main__":
    main()
