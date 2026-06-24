"""
哨响AI — 报告生成服务（后端可调用的模块）
v4.0: 8章节固定协议版，移除置信度(confidence)

章节结构:
  1. 赛前准备状态
  2. 赔率数据（API实时 + 图片比对）
  3. 赔率中的真实信息是什么
  4. 博彩公司在掩盖什么真相
  5. AORE四模型分析
  6. 首发阵容佐证
  7. 推荐比分
  8. 投资建议（Kelly + 避坑）

变化:
  - 从6模块改为8章节
  - 移除"置信"列/置信度输出
  - 决策基于隐含概率而非confidence
  - 新增固定话术章节
"""
from typing import Dict, List, Optional
import os

# ── 标准模板HTML（内嵌CSS，无外部依赖）────────────────────────────────────
_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>哨响AI 预测报告 — {home} vs {away}</title>
<style>
  * {{ box-sizing: border-box; margin:0; padding:0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: #f5f5f3; color: #2c2c2a;
    padding: 24px 28px; line-height: 1.6;
    max-width: 760px; margin: 0 auto;
  }}
  .report-header {{ margin-bottom: 24px; }}
  .report-header h1 {{ font-size: 20px; font-weight: 700; margin-bottom: 2px; }}
  .report-sub {{ font-size: 13px; color: #888780; }}
  .protocol-badge {{ display: inline-block; background: #e6f4d8; color: #27500a;
                     border-radius: 6px; padding: 2px 10px; font-size: 11px; font-weight: 600; }}
  .card {{
    background: #fff; border-radius: 12px;
    border: 1px solid rgba(0,0,0,0.10);
    padding: 18px 22px; margin-bottom: 18px;
  }}
  .card-title {{ font-size: 15px; font-weight: 600; margin-bottom: 12px; display: flex; align-items: center; gap: 8px; }}
  .card-title .ic {{ font-size: 17px; }}
  .section-num {{ display: inline-block; background: #378add; color: #fff; border-radius: 50%;
                  width: 22px; height: 22px; text-align: center; line-height: 22px; font-size: 12px; font-weight: 600; }}
  /* 赛前准备清单 */
  .checklist {{ list-style: none; padding: 0; }}
  .checklist li {{ padding: 4px 0; font-size: 13px; }}
  .checklist li::before {{ content: ''; margin-right: 6px; }}
  .checklist li.ok::before {{ content: '✅'; }}
  .checklist li.ng::before {{ content: '❌'; }}
  /* 赔率表 */
  .mt {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  .mt th {{ text-align: left; padding: 7px 10px; border-bottom: 1px solid rgba(0,0,0,0.10); color: #888; font-weight: 400; font-size: 12px; }}
  .mt td {{ padding: 9px 10px; border-bottom: 1px solid rgba(0,0,0,0.05); font-size: 13px; }}
  .pct {{ font-weight: 600; font-size: 14px; text-align: right; }}
  .note {{ color: #888; font-size: 12px; }}
  /* 比分表 */
  .st {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  .st th {{ text-align: center; padding: 7px 4px; border-bottom: 1px solid rgba(0,0,0,0.10); color: #888; font-weight: 400; font-size: 12px; }}
  .st td {{ padding: 8px 4px; border-bottom: 1px solid rgba(0,0,0,0.04); text-align: center; }}
  .rk {{ color: #888; font-size: 12px; width: 30px; }}
  .g {{ font-weight: 700; font-size: 17px; width: 34px; background: #f5f5f3; border-radius: 6px; }}
  .gs {{ width: 14px; font-size: 12px; color: #bbb; }}
  .rs {{ width: 46px; font-size: 12px; }}
  .rs.w {{ color: #085041; }}
  .rs.d {{ color: #634806; }}
  .pr {{ font-weight: 600; width: 62px; }}
  .bar {{ color: #378add; font-size: 9px; letter-spacing: 1.5px; }}
  /* 数据概览 */
  .og {{ display: grid; grid-template-columns: 1fr 1fr; gap: 14px; font-size: 13px; }}
  .og-l {{ color: #888; font-size: 12px; margin-bottom: 3px; }}
  .og-v {{ font-size: 21px; font-weight: 600; }}
  .og-v.s {{ color: #085041; }}
  .og-v.b {{ color: #a32d2d; }}
  /* 结论 */
  .cg {{ display: grid; grid-template-columns: 98px 1fr; gap: 0; font-size: 13px; }}
  .cl {{ padding: 9px 10px; color: #888; font-size: 12px; border-bottom: 1px solid rgba(0,0,0,0.04); }}
  .cv {{ padding: 9px 10px; font-weight: 500; border-bottom: 1px solid rgba(0,0,0,0.04); }}
  .cv.y {{ color: #085041; }}
  .cv.t {{ color: #634806; }}
  .cv.r {{ color: #a32d2d; }}
  /* 庄家意图 */
  .sd {{ font-size: 13px; color: #333; line-height: 1.8; padding: 0; margin: 0; list-style: none; }}
  .sd li {{ padding: 7px 0; border-bottom: 1px solid rgba(0,0,0,0.04); }}
  .sd li::before {{ content: '◆ '; color: #378add; font-size: 9px; }}
  .sd li.warn::before {{ color: #e85454; }}
  /* 投注建议 */
  .bg {{ display: flex; flex-direction: column; gap: 6px; font-size: 13px; }}
  .br {{ display: flex; align-items: center; gap: 10px; }}
  .bl {{ font-size: 12px; color: #888; min-width: 52px; }}
  .bv {{ font-weight: 500; }}
  .bt {{ display: inline-block; border-radius: 6px; padding: 2px 9px; font-size: 12px; font-weight: 600; }}
  .bt.i {{ background: #e6f4d8; color: #27500a; }}
  .bt.w {{ background: #fef8e8; color: #633806; }}
  .bt.p {{ background: #fce8e8; color: #791f1f; }}
  .divider {{ font-size: 13px; font-weight: 600; color: #378add; margin: 20px 0 10px 0;
               padding-bottom: 5px; border-bottom: 1px solid rgba(55,138,221,0.18); }}
  .os {{ background: #f0efe7; border-radius: 8px; padding: 9px 14px; font-size: 12px; color: #444;
             display: flex; gap: 18px; flex-wrap: wrap; margin-bottom: 14px; }}
  .os-item {{ display: flex; gap: 5px; align-items: center; }}
  .os-val {{ font-weight: 600; color: #2c2c2a; }}
  /* 固定话术 */
  .fixed-phrase {{ background: #f8f7f2; border-left: 3px solid #378add; padding: 12px 16px; font-size: 14px; line-height: 1.8; margin-bottom: 8px; border-radius: 0 8px 8px 0; }}
  .phrase-q {{ color: #378add; font-weight: 600; margin-bottom: 6px; }}
</style>
</head>
<body>

<div class="report-header">
  <h1>🎯 哨响AI 预测报告</h1>
  <div class="report-sub">🏆 {league} · {group} &nbsp;|&nbsp; 📅 {kickoff} &nbsp;|&nbsp; 🔑 match_id: {match_id} &nbsp;|&nbsp; <span class="protocol-badge">v4.0 8章节协议</span></div>
</div>

<div class="os">
  <div class="os-item">欧赔：<span class="os-val">主{oh} / 平{od} / 客{oa}</span></div>
  <div class="os-item">大小：<span class="os-val">{ou_line}球</span></div>
  <div class="os-item">抽水：<span class="os-val">{vig_pct}%</span></div>
</div>

<!-- 一、赛前准备状态 -->
<div class="card">
  <div class="card-title"><span class="section-num">1</span> 赛前准备状态</div>
  <ul class="checklist">
    {checklist_items}
  </ul>
</div>

<!-- 二、赔率数据 -->
<div class="card">
  <div class="card-title"><span class="section-num">2</span> 赔率数据（API实时 + 图片比对）</div>
  <table class="mt">
    <thead><tr><th>模型</th><th style="text-align:right;">主胜</th><th style="text-align:right;">平局</th><th style="text-align:right;">客胜</th><th>说明</th></tr></thead>
    <tbody>
      <tr><td>赔率隐含</td><td class="pct" style="text-align:right;">{ph}%</td><td class="pct" style="text-align:right;">{pd}%</td><td class="pct" style="text-align:right;">{pa}%</td><td class="note">市场基准（去水）</td></tr>
      <tr><td><strong>融合(赔率+%)</strong></td><td class="pct" style="text-align:right;">{ph}%</td><td class="pct" style="text-align:right;">{pd}%</td><td class="pct" style="text-align:right; color:#085041;">{pa}%</td><td class="note"><strong>✅ 推荐参考</strong></td></tr>
    </tbody>
  </table>
</div>

<!-- 三、赔率中的真实信息是什么 -->
<div class="card">
  <div class="card-title"><span class="section-num">3</span> 赔率中的真实信息是什么</div>
  <div class="fixed-phrase">
    <div class="phrase-q">赔率中的真实信息是什么？</div>
    {real_info_html}
  </div>
</div>

<!-- 四、博彩公司在掩盖什么真相 -->
<div class="card">
  <div class="card-title"><span class="section-num">4</span> 博彩公司在掩盖什么真相</div>
  <div class="fixed-phrase">
    <div class="phrase-q">博彩公司在掩盖什么真相？</div>
    {hidden_truth_html}
  </div>
</div>

<!-- 五、AORE四模型分析 -->
<div class="card">
  <div class="card-title"><span class="section-num">5</span> AORE四模型分析</div>
  <table class="st">
    <thead><tr><th>排名</th><th>主↓</th><th></th><th>客↓</th><th>结果</th><th>概率</th></tr></thead>
    <tbody>
      {score_rows}
    </tbody>
  </table>
</div>

<!-- 六、首发阵容佐证 -->
<div class="card">
  <div class="card-title"><span class="section-num">6</span> 首发阵容佐证</div>
  <div style="color:#888; font-size:13px;">⚡ 阵容未获取（比赛前1-2小时可获取时必须补充）</div>
</div>

<!-- 七、推荐比分 -->
<div class="card">
  <div class="card-title"><span class="section-num">7</span> 推荐比分</div>
  <div class="cg">
    <div class="cl">推荐比分</div><div class="cv" style="font-size:22px; font-weight:700;">{rec_score}</div>
    <div class="cl">次选比分</div><div class="cv">{alt_score}</div>
    <div class="cl">大小球</div><div class="cv">小{under_p}% / 大{over_p}%（盘口{ou_line}球）</div>
  </div>
</div>

<!-- 八、投资建议 -->
<div class="card" style="border-left: 3px solid #085041;">
  <div class="card-title"><span class="section-num">8</span> 投资建议（Kelly + 避坑）</div>
  <div class="cg" style="margin-bottom:12px;">
    <div class="cl">比赛方向</div><div class="cv {decision_css}">{decision_icon} {decision} · {fav}胜</div>
    <div class="cl">隐含概率</div><div class="cv">{best_prob}%</div>
  </div>
  <div class="bg">
    {bet_rows}
  </div>
</div>

</body>
</html>"""


def render_html(report_data: Dict) -> str:
    """
    输入 report_data，输出8章节HTML报告。
    移除了置信度，决策基于隐含概率。

    report_data 格式:
    {
        'meta': {
            'home': '卡塔尔', 'away': '瑞士',
            'league': '2026美加墨世界杯', 'group': 'A组',
            'kickoff': '2026-06-15 03:00', 'match_id': 570357,
        },
        'odds': {
            'full': {'H': 13.0, 'D': 6.70, 'A': 1.21},
            'ou':   {'line': 3.0},
        },
        'pred': {
            'p_h': 0.073, 'p_d': 0.142, 'p_a': 0.785,
            'scores': [(gh, ga, result, prob), ...],
            'over_p': 0.27, 'under_p': 0.73,
            'exp_diff': 1.8,
        },
        'intent': ['信号1', '信号2', ...],
        'decision': 'INVEST',  # INVEST / WATCH / PASS（基于隐含概率，非confidence）
    }
    """
    rd = report_data
    meta = rd['meta']
    odds = rd['odds']
    pred = rd['pred']
    intent = rd.get('intent', [])

    oh = odds['full']['H']
    od = odds['full']['D']
    oa = odds['full']['A']
    vig = (1/oh + 1/od + 1/oa) - 1
    vig_pct = f"{vig*100:.1f}"

    # 去抽水隐含概率
    raw_h, raw_d, raw_a = 1/oh, 1/od, 1/oa
    total = raw_h + raw_d + raw_a
    fair_h = raw_h / total
    fair_d = raw_d / total
    fair_a = raw_a / total

    ph = f"{fair_h*100:.1f}"
    pd = f"{fair_d*100:.1f}"
    pa = f"{fair_a*100:.1f}"

    # 赛前准备清单
    checklist_items = ''
    checks = [
        ('API实时赔率', True),
        ('图片赔率', False),
        ('赔率比对完成', False),
        ('首发阵容', False),
        ('AORE四模型分析', True),
        ('真实信息解读', bool(intent)),
        ('庄家掩盖真相解读', bool(intent)),
        ('推荐比分', True),
    ]
    for label, ok in checks:
        cls = 'ok' if ok else 'ng'
        checklist_items += f'<li class="{cls}">{label}</li>\n'

    # 真实信息（从intent提取非警告信号）
    real_info_lines = []
    hidden_truth_lines = []
    for line in intent:
        if '⚠' in line or '警告' in line or '风险' in line or '警惕' in line:
            hidden_truth_lines.append(line)
        else:
            real_info_lines.append(line)

    if not real_info_lines:
        best_dir = '主胜' if fair_h > fair_a else '客胜'
        best_prob_val = max(fair_h, fair_a)
        real_info_lines.append(f"市场定价指向{best_dir}，隐含概率{best_prob_val*100:.1f}%")
    if not hidden_truth_lines:
        if fair_d > 0.30:
            hidden_truth_lines.append(f"平局隐含概率{fair_d*100:.1f}%偏高，庄家在引导投注远离平局")

    real_info_html = '<br>'.join(real_info_lines)
    hidden_truth_html = '<br>'.join(hidden_truth_lines) if hidden_truth_lines else '暂无异常信号'

    # 比分表格行（移除"置信"列）
    score_rows = ''
    res_css_map = {'客胜': 'w', '主胜': 'w', '平局': 'd'}
    for i, item in enumerate(pred['scores'][:6], 1):
        if len(item) >= 4:
            gh, ga, res, prob = item[0], item[1], item[2], item[3]
        else:
            gh, ga, res = item[0], item[1], item[2]
            prob = 0.05
        bar = '█' * max(1, int(prob * 20))
        rcss = res_css_map.get(res, '')
        score_rows += (
            f"<tr>"
            f"<td class='rk'>{i}</td>"
            f"<td class='g'>{gh}</td>"
            f"<td class='gs'>:</td>"
            f"<td class='g'>{ga}</td>"
            f"<td class='rs {rcss}'>{res}</td>"
            f"<td class='pr'>{prob*100:.1f}%</td>"
            f"</tr>\n"
        )

    # 推荐比分
    scores = pred.get('scores', [])
    if scores:
        s1 = scores[0]
        rec_score = f"{s1[0]}-{s1[1]}"
        if len(scores) > 1:
            s2 = scores[1]
            alt_score = f"{s2[0]}-{s2[1]}（{s2[3]*100:.1f}%）"
        else:
            alt_score = '-'
    else:
        rec_score = '待定'
        alt_score = '-'

    # 决策（基于隐含概率而非confidence）
    best_prob = max(fair_h, fair_d, fair_a)
    best_prob_pct = f"{best_prob*100:.1f}"

    if best_prob >= 0.70:
        decision = 'INVEST'
    elif best_prob >= 0.55:
        decision = 'WATCH'
    else:
        decision = 'PASS'

    decision_icon = {'INVEST': '✅', 'WATCH': '👀', 'PASS': '🚫'}.get(decision, '')
    decision_css = {'INVEST': 'y', 'WATCH': 't', 'PASS': 'r'}.get(decision, '')
    fav = meta['away'] if fair_a >= max(fair_h, fair_d) else meta['home']

    # 投注建议行
    bet_rows = ''
    if decision == 'INVEST':
        s1 = scores[0] if scores else (0, 0, '客胜', 0.1)
        s2 = scores[1] if len(scores) > 1 else (0, 0, '客胜', 0.05)
        bet_rows = (
            f"<div class='br'><span class='bl'>独赢</span><span class='bv'>✅ <span class='bt i'>INVEST</span> {fav} @{oa}</span></div>"
            f"<div class='br'><span class='bl'>波胆</span><span class='bv'>{s1[0]}:{s1[1]} <span class='bt i'>⭐⭐⭐</span></span></div>"
            f"<div class='br'><span class='bl'>波胆</span><span class='bv'>{s2[0]}:{s2[1]} <span class='bt i'>⭐⭐</span></span></div>"
        )
    elif decision == 'WATCH':
        bet_rows = (
            f"<div class='br'><span class='bl'>建议</span><span class='bv'>👀 <span class='bt w'>WATCH</span> 观望为主（隐含概率{best_prob_pct}%）</span></div>"
        )
    else:
        bet_rows = (
            f"<div class='br'><span class='bl'>建议</span><span class='bv'>🚫 <span class='bt p'>PASS</span> 隐含概率不足</span></div>"
        )

    html = _TEMPLATE.format(
        home=meta['home'], away=meta['away'],
        league=meta['league'], group=meta.get('group', ''),
        kickoff=meta['kickoff'], match_id=meta.get('match_id', '?'),
        oh=oh, od=od, oa=oa,
        ou_line=odds['ou']['line'],
        vig_pct=vig_pct,
        ph=ph, pd=pd, pa=pa,
        checklist_items=checklist_items,
        real_info_html=real_info_html,
        hidden_truth_html=hidden_truth_html,
        score_rows=score_rows,
        rec_score=rec_score,
        alt_score=alt_score,
        under_p=f"{pred['under_p']*100:.0f}",
        over_p=f"{pred['over_p']*100:.0f}",
        decision=decision, decision_icon=decision_icon,
        decision_css=decision_css, fav=fav,
        best_prob=best_prob_pct,
        bet_rows=bet_rows,
    )
    return html


def build_report_data(match_info: Dict, odds: Dict, pred_result: Dict, intent: List[str]) -> Dict:
    """
    从预测服务输出构建 report_data。
    v4.0: 决策基于隐含概率而非confidence
    """
    # 从 pred_result 提取概率
    probs = pred_result.get('probabilities', {})
    p_h = probs.get('H', probs.get('home', 0.33))
    p_d = probs.get('D', probs.get('draw', 0.33))
    p_a = probs.get('A', probs.get('away', 0.34))

    # 比分预测
    score_pred = pred_result.get('score_prediction', {})
    scores = score_pred.get('top_scores', [])

    if scores:
        normalized = []
        for s in scores:
            if isinstance(s, dict):
                score_str = s.get('score', '0-0')
                try:
                    gh, ga = map(int, score_str.split('-'))
                except:
                    gh, ga = 0, 0
                outcome_map = {'home': '主胜', 'away': '客胜', 'draw': '平局'}
                res = outcome_map.get(s.get('outcome', ''), s.get('outcome', ''))
                prob = float(s.get('probability', 0.05))
                normalized.append((gh, ga, res, prob))
            elif len(s) == 3:
                gh, ga, res = s
                normalized.append((gh, ga, res, 0.05))
            elif len(s) >= 4:
                normalized.append((int(s[0]), int(s[1]), s[2], float(s[3])))
        scores = normalized

    if not scores:
        import math
        lambda_h = score_pred.get('lambda_h', 0.8)
        lambda_a = score_pred.get('lambda_a', 1.6)
        scores = []
        for gh in range(0, 5):
            for ga in range(0, 6):
                p_gh = math.exp(-lambda_h) * (lambda_h ** gh) / math.factorial(gh) if gh < 10 else 0
                p_ga = math.exp(-lambda_a) * (lambda_a ** ga) / math.factorial(ga) if ga < 10 else 0
                prob = p_gh * p_ga
                if gh + ga <= 6:
                    res = '主胜' if gh > ga else ('客胜' if ga > gh else '平局')
                    scores.append((gh, ga, res, prob))
        scores.sort(key=lambda x: x[3], reverse=True)
        scores = scores[:6]
        total = sum(s[3] for s in scores)
        scores = [(s[0], s[1], s[2], s[3]/total) for s in scores]

    # 大小球
    over_under = pred_result.get('over_under', {})
    over_p = over_under.get('over_prob', 0.27)
    under_p = over_under.get('under_prob', 0.73)

    # 预期净胜球
    exp_diff = (p_a - p_h) * 2.5

    # 决策（基于隐含概率而非confidence）
    oh = odds['full']['H']
    od = odds['full']['D']
    oa = odds['full']['A']
    raw_h, raw_d, raw_a = 1/oh, 1/od, 1/oa
    total = raw_h + raw_d + raw_a
    fair_best = max(raw_h, raw_d, raw_a) / total

    if fair_best >= 0.70:
        decision = 'INVEST'
    elif fair_best >= 0.55:
        decision = 'WATCH'
    else:
        decision = 'PASS'

    return {
        'meta': match_info,
        'odds': odds,
        'pred': {
            'p_h': p_h, 'p_d': p_d, 'p_a': p_a,
            'scores': scores,
            'over_p': over_p, 'under_p': under_p,
            'exp_diff': exp_diff,
        },
        'intent': intent,
        'decision': decision,
    }
