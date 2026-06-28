#!/usr/bin/env python3
"""
哨响AI — 统一分析报告生成器 v4.0（8章节固定协议版）
=====================================================
基于固定分析协议(analysis_protocol)，统一所有分析输出格式。

与v3相比的变化：
  - 从6模块改为8章节固定协议
  - 移除"置信度"/"confidence"输出
  - 新增"赔率中的真实信息是什么"和"博彩公司在掩盖什么真相"固定话术章节
  - 新增"赛前准备状态"和"首发阵容佐证"章节
  - 决策判断基于隐含概率而非置信度

固定话术（不可省略）:
  1. "赔率中的真实信息是什么"
  2. "博彩公司在掩盖什么真相"
  3. 直白显示推荐比分
  4. 始终从API获取实时赔率并比对图片中的赔率
  5. 有条件始终拉取双方球队首发阵容佐证

仅输出欧盘(1X2)分析。
"""

import os
import json
import sqlite3
import numpy as np
from datetime import datetime, timezone
from typing import Dict, Optional, List
from pathlib import Path

# 项目根目录
PROJECT_ROOT = Path(__file__).parent
DB_PATH = PROJECT_ROOT / "data" / "football_data.db"
OUTPUT_DIR = PROJECT_ROOT / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ══════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════

def implied_probs(home: float, draw: float, away: float) -> Dict:
    """从欧盘赔率计算隐含概率（去抽水）"""
    raw = np.array([1.0/home, 1.0/draw, 1.0/away])
    total = raw.sum()
    fair = raw / total
    overround = total - 1.0
    return {
        'home': float(fair[0]),
        'draw': float(fair[1]),
        'away': float(fair[2]),
        'overround': float(overround),
    }

def kelly_fraction(fair_prob: float, odds: float) -> float:
    """Kelly仓位 = (fair_prob * odds - 1) / (odds - 1)"""
    if odds <= 1.0:
        return 0.0
    ev = fair_prob * odds - 1.0
    if ev <= 0:
        return 0.0
    return ev / (odds - 1.0)

# ══════════════════════════════════════════════════
# 核心报告生成 — 遵循固定分析协议（8章节）
# ══════════════════════════════════════════════════

def generate_report(
    home_team: str,
    away_team: str,
    league: str,
    odds_t1: Dict[str, float],
    odds_t2: Optional[Dict[str, float]] = None,
    totals_line: float = 2.5,
    totals_over_t1: float = 0.0,
    totals_under_t1: float = 0.0,
    model_results: Optional[Dict] = None,
    backtest_stats: Optional[Dict] = None,
    # v4.0 协议参数
    api_odds: Optional[Dict] = None,
    image_odds: Optional[Dict] = None,
    odds_comparison: Optional[Dict] = None,
    lineups: Optional[Dict] = None,
    real_info: str = "",
    hidden_truth: str = "",
    recommended_score: str = "",
    anomalies: Optional[List[Dict]] = None,
    # 比分预测
    score_predictions: Optional[List] = None,
) -> str:
    """
    生成统一格式的欧盘分析报告（v4.0 固定8章节协议版）

    注意：移除了confidence参数，不输出任何置信度信息
    """
    # 赔率数据
    h1, d1, a1 = odds_t1['home'], odds_t1['draw'], odds_t1['away']
    imp1 = implied_probs(h1, d1, a1)

    if odds_t2:
        h2, d2, a2 = odds_t2['home'], odds_t2['draw'], odds_t2['away']
        imp2 = implied_probs(h2, d2, a2)
        has_t2 = True
    else:
        h2 = d2 = a2 = 0
        imp2 = {}
        has_t2 = False

    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')

    # ── 构建报告 ──
    lines = []
    sep = "=" * 70
    line_sep = "-" * 70

    lines.append(sep)
    lines.append(f"{home_team} vs {away_team} — 欧盘分析报告")
    lines.append(sep)
    lines.append(f"赛事: {league}")
    lines.append(f"分析时间: {now}")
    lines.append(f"协议版本: v4.0 固定分析协议（8章节）")
    lines.append("")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 一、赛前准备状态
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    lines.append(line_sep)
    lines.append("一、赛前准备状态")
    lines.append(line_sep)

    prep_items = [
        ("API实时赔率", api_odds is not None),
        ("图片赔率", image_odds is not None),
        ("赔率比对完成", odds_comparison is not None),
        ("首发阵容", lineups is not None),
        ("AORE四模型分析", model_results is not None),
        ("真实信息解读", bool(real_info)),
        ("庄家掩盖真相解读", bool(hidden_truth)),
        ("推荐比分", bool(recommended_score)),
    ]

    for item, ok in prep_items:
        icon = "✅" if ok else "❌"
        lines.append(f"  {icon} {item}")

    lines.append("")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 二、赔率数据（API实时 + 图片比对）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    lines.append(line_sep)
    lines.append("二、赔率数据（API实时 + 图片比对）")
    lines.append(line_sep)

    # API赔率
    if api_odds:
        lines.append(f"  API赔率来源: {api_odds.get('source', 'the-odds-api')}")
        lines.append(f"  API主胜: {api_odds.get('home', '?')}  平局: {api_odds.get('draw', '?')}  客胜: {api_odds.get('away', '?')}")
        if 'totals' in api_odds:
            for t in api_odds['totals'][:2]:
                lines.append(f"  API大小球: {t.get('point','?')} O={t.get('over','?')} U={t.get('under','?')}")
        lines.append("")

    # 图片赔率
    if image_odds:
        lines.append(f"  图片赔率来源: {image_odds.get('source', '截图')}")
        lines.append(f"  图片主胜: {image_odds.get('home', '?')}  平局: {image_odds.get('draw', '?')}  客胜: {image_odds.get('away', '?')}")
        lines.append("")

    # 比对结果
    if odds_comparison and odds_comparison.get('differences'):
        lines.append("  【API vs 图片赔率比对】")
        for d in odds_comparison['differences']:
            lines.append(f"    {d['direction']}: API={d['api_odds']} 图片={d['image_odds']} 差异={d['diff']:+.2f} ({d['diff_pct']}) → {d['interpretation']}")
        if odds_comparison.get('signal'):
            lines.append(f"    ⚠️ {odds_comparison['signal']}")
        lines.append("")

    # 基础赔率表
    lines.append(f"           | 主胜(H) | 平局(D) | 客胜(A)")
    lines.append(f"开盘(T1) |  {h1:.2f}   |  {d1:.2f}   |  {a1:.2f}")

    if has_t2:
        ch = h2 - h1
        cd = d2 - d1
        ca = a2 - a1
        lines.append(f"收盘(T2) |  {h2:.2f}   |  {d2:.2f}   |  {a2:.2f}")
        lines.append(f"变化      | {ch:+.2f}   | {cd:+.2f}   | {ca:+.2f}")
        lines.append("")
        lines.append(f"抽水率(T1): {imp1['overround']*100:.2f}%  →  抽水率(T2): {imp2['overround']*100:.2f}%")
    else:
        lines.append("")
        lines.append(f"抽水率: {imp1['overround']*100:.2f}%")

    # 隐含概率表（替代原来的置信度）
    lines.append("")
    lines.append(f"  隐含概率: 主胜 {imp1['home']*100:.1f}% | 平局 {imp1['draw']*100:.1f}% | 客胜 {imp1['away']*100:.1f}%")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 三、赔率中的真实信息是什么 ← 固定话术
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    lines.append("")
    lines.append(line_sep)
    lines.append("三、赔率中的真实信息是什么")
    lines.append(line_sep)

    if real_info:
        lines.append(real_info)
    else:
        lines.append("⚠️ 未填写（必须填写此项）")

    # 自动异常信号
    if anomalies:
        lines.append("")
        lines.append("异常信号:")
        for i, a in enumerate(anomalies, 1):
            lines.append(f"  {i}. [{a.get('severity','?')}] {a.get('type','')}: {a.get('desc','')}")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 四、博彩公司在掩盖什么真相 ← 固定话术
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    lines.append("")
    lines.append(line_sep)
    lines.append("四、博彩公司在掩盖什么真相")
    lines.append(line_sep)

    if hidden_truth:
        lines.append(hidden_truth)
    else:
        lines.append("⚠️ 未填写（必须填写此项）")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 五、AORE四模型分析（始终显示章节标题）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    lines.append("")
    lines.append(line_sep)
    lines.append("五、AORE四模型分析")
    lines.append(line_sep)

    if model_results:

        # OTSM
        if 'ts' in model_results:
            ts = model_results['ts']
            lines.append("  【TS 赔率时序状态机】")
            lines.append(f"    开盘熵: {ts.get('open_entropy', 0):.4f} bits")
            lines.append(f"    收盘熵: {ts.get('close_entropy', 0):.4f} bits")
            lines.append(f"    熵漂移: {ts.get('entropy_drift', 0):+.4f}")
            lines.append(f"    水位加速度: {ts.get('water_accel', 0):+.4f}")
            lines.append(f"    凯利涨落: {ts.get('kelly_fluct', 0):+.4f}")
            lines.append(f"    合成向量模: {ts.get('vector_mag', 0):.4f}")
            lines.append(f"    状态推断: {ts.get('state', 'UNKNOWN')}")
            lines.append(f"    锁定期确信度: {ts.get('lock_confidence', 0):.3f}")
            lines.append("")

        # 泊松
        if 'ps' in model_results:
            ps = model_results['ps']
            lines.append("  【PS 泊松分布模拟】")
            lines.append(f"    主队λ: {ps.get('lambda_home', 0):.2f}")
            lines.append(f"    客队λ: {ps.get('lambda_away', 0):.2f}")
            lines.append(f"    预期总进球: {ps.get('expected_total', 0):.2f}")
            if 'top_scores' in ps:
                lines.append("    最可能比分:")
                for sc in ps['top_scores'][:5]:
                    lines.append(f"      {sc['score']}: {sc['prob']:.1%}")
            lines.append("")

        # 冷门检测
        if 'ue' in model_results:
            ue = model_results['ue']
            lines.append("  【UE 冷门检测器】")
            lines.append(f"    市场过度自信: {ue.get('overconfidence', '未知')}")
            lines.append(f"    冷门信号: {ue.get('upset_signal', '无')}")
            lines.append("")

        # 收割防护墙
        if 'hg' in model_results:
            hg = model_results['hg']
            lines.append("  【HG 收割防护墙】")
            lines.append(f"    危险等级: {hg.get('danger_level', '未知')}")
            if 'signals' in hg:
                for sig in hg['signals']:
                    lines.append(f"    信号: {sig}")
            lines.append("")
    else:
        lines.append("  ⚡ AORE四模型分析未执行")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 六、首发阵容佐证（有条件时必填）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    lines.append("")
    lines.append(line_sep)
    lines.append("六、首发阵容佐证")
    lines.append(line_sep)

    if lineups:
        home_lu = lineups.get('home', {})
        away_lu = lineups.get('away', {})
        lines.append(f"  主队阵型: {home_lu.get('formation', '未知')}")
        for p in home_lu.get('players', []):
            lines.append(f"    {p}")
        lines.append("")
        lines.append(f"  客队阵型: {away_lu.get('formation', '未知')}")
        for p in away_lu.get('players', []):
            lines.append(f"    {p}")
    else:
        lines.append("  ⚡ 阵容未获取（比赛前1-2小时可获取时必须补充）")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 七、推荐比分 ← 必须直白显示
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    lines.append("")
    lines.append(line_sep)
    lines.append("七、推荐比分")
    lines.append(line_sep)

    if recommended_score:
        lines.append(f"  推荐比分: {recommended_score}")
        # 附加泊松概率支撑
        if model_results and 'ps' in model_results:
            ps = model_results['ps']
            if 'top_scores' in ps:
                top3 = [sc['score'] for sc in ps['top_scores'][:3]]
                lines.append(f"  泊松概率支撑: {', '.join(top3)}")
    elif score_predictions:
        # 兼容旧格式：从score_predictions提取
        if len(score_predictions) > 0:
            s1 = score_predictions[0]
            if len(s1) >= 3:
                lines.append(f"  推荐比分: {s1[0]}-{s1[1]}")
            if len(score_predictions) > 1:
                s2 = score_predictions[1]
                if len(s2) >= 3:
                    lines.append(f"  次选比分: {s2[0]}-{s2[1]}")

            # 比分概率表（不显示置信度）
            lines.append("")
            lines.append(f"  {'排名':>4s}  {'主':>3s}  {'客':>3s}  {'结果':>6s}  {'概率':>7s}")
            for i, s in enumerate(score_predictions[:6], 1):
                if len(s) >= 4:
                    gh, ga, res, prob = s[0], s[1], s[2], s[3]
                    lines.append(f"  {i:>3d}  {gh:>3d}  {ga:>3d}  {res:>6s}  {prob*100:>5.1f}%")
    else:
        lines.append("  ⚠️ 未填写（必须直白显示推荐比分）")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 八、投资建议（Kelly + 避坑）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    lines.append("")
    lines.append(line_sep)
    lines.append("八、投资建议（Kelly + 避坑）")
    lines.append(line_sep)

    # Kelly分析
    for label, prob_key, odds_val in [
        ('主胜', 'home', h1 if not has_t2 else h2),
        ('平局', 'draw', d1 if not has_t2 else d2),
        ('客胜', 'away', a1 if not has_t2 else a2),
    ]:
        fair_p = imp2.get(prob_key, imp1.get(prob_key, 0)) if has_t2 else imp1.get(prob_key, 0)
        kf = kelly_fraction(fair_p, odds_val)
        ev = fair_p * odds_val - 1.0
        lines.append(f"  {label} Kelly仓位: {kf*100:.1f}% (EV={ev*100:+.1f}%)")

    # 基于隐含概率自动排序
    fair_probs = {
        '主胜': (imp2 if has_t2 else imp1).get('home', 0),
        '平局': (imp2 if has_t2 else imp1).get('draw', 0),
        '客胜': (imp2 if has_t2 else imp1).get('away', 0),
    }
    sorted_dirs = sorted(fair_probs.items(), key=lambda x: -x[1])

    lines.append("")
    best_dir, best_prob = sorted_dirs[0]
    best_odds = {'主胜': h2 if has_t2 else h1, '平局': d2 if has_t2 else d1, '客胜': a2 if has_t2 else a1}[best_dir]
    lines.append(f"  首选: {best_dir} @{best_odds:.2f} (隐含概率{best_prob*100:.1f}%)")

    # 决策基于隐含概率而非置信度
    if best_prob >= 0.70:
        lines.append(f"  决策: ✅ INVEST — 隐含概率{best_prob*100:.1f}%>=70%门槛")
    elif best_prob >= 0.55:
        lines.append(f"  决策: 👀 WATCH — 隐含概率{best_prob*100:.1f}%在55%-70%之间，观望为主")
    else:
        lines.append(f"  决策: 🚫 PASS — 隐含概率{best_prob*100:.1f}%<55%，不建议下注")

    # 避坑
    lines.append("")
    lines.append("  避坑:")
    lines.append(f"    - 赔率最低的方向无价值（{sorted_dirs[0][0]}@{best_odds:.2f}可能过热）")
    lines.append(f"    - 逆庄家方向操作风险极高")

    # 尾部
    lines.append("")
    lines.append(sep)
    lines.append(f"生成时间: {now}")
    lines.append("模型: OTSM v4.0 + AORE 四模型融合 (纯欧盘)")
    lines.append("协议: 固定分析协议 v1.0（8章节）")
    lines.append("注意: 本分析仅使用欧盘1X2数据")

    return "\n".join(lines)

def save_report(report_text: str, filename: str = None) -> str:
    """保存报告到output目录"""
    if not filename:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"report_{timestamp}.md"

    filepath = OUTPUT_DIR / filename
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(report_text)

    return str(filepath)

# ══════════════════════════════════════════════════
# 从报告数据（report_data）生成报告
# 兼容footballAI后端report_generator的输入格式
# ══════════════════════════════════════════════════

def generate_from_report_data(report_data: Dict) -> str:
    """
    从report_data格式（后端服务输出）生成8章节报告

    report_data 格式:
    {
        'meta': { 'home': str, 'away': str, 'league': str, 'kickoff': str, ... },
        'odds': { 'full': { 'H': float, 'D': float, 'A': float }, 'ou': { 'line': float } },
        'pred': { 'p_h': float, 'p_d': float, 'p_a': float, 'scores': [...], ... },
        'intent': [str, ...],
        'decision': str,  # 不再使用confidence，基于隐含概率
    }
    """
    rd = report_data
    meta = rd.get('meta', {})
    odds = rd.get('odds', {})
    pred = rd.get('pred', {})
    intent = rd.get('intent', [])

    # 提取赔率
    full_odds = odds.get('full', {})
    h, d, a = full_odds.get('H', 1.0), full_odds.get('D', 1.0), full_odds.get('A', 1.0)

    # 提取比分预测
    scores = pred.get('scores', [])
    score_predictions = []
    for s in scores:
        if isinstance(s, (list, tuple)) and len(s) >= 4:
            score_predictions.append(s)
        elif isinstance(s, dict):
            score_str = s.get('score', '0-0')
            try:
                gh, ga = map(int, score_str.split('-'))
            except (ValueError, TypeError):
                gh, ga = 0, 0
            outcome_map = {'home': '主胜', 'away': '客胜', 'draw': '平局'}
            res = outcome_map.get(s.get('outcome', ''), s.get('outcome', ''))
            prob = float(s.get('probability', 0.05))
            score_predictions.append((gh, ga, res, prob))

    # 计算隐含概率
    imp = implied_probs(h, d, a)

    # 构建真实信息和掩盖真相（从intent自动提取）
    real_info_lines = []
    hidden_truth_lines = []

    for line in intent:
        if '⚠' in line or '警告' in line or '风险' in line or '警惕' in line:
            hidden_truth_lines.append(line)
        else:
            real_info_lines.append(line)

    # 如果没有intent，基于赔率自动生成
    if not real_info_lines:
        best_dir = '主胜' if imp['home'] > imp['away'] else '客胜'
        best_prob = max(imp['home'], imp['away'])
        real_info_lines.append(f"市场定价指向{best_dir}，隐含概率{best_prob*100:.1f}%")
        if imp['overround'] > 0.06:
            real_info_lines.append(f"抽水率{imp['overround']*100:.1f}%偏高，庄家在收取风险溢价")

    if not hidden_truth_lines:
        if imp['draw'] > 0.30:
            hidden_truth_lines.append(f"平局隐含概率{imp['draw']*100:.1f}%偏高，庄家在引导投注远离平局")
        if h < 1.15:
            hidden_truth_lines.append(f"主胜赔率{h}超低=引流诱饵，热门方真实价值被严重压缩")

    # 推荐比分
    recommended_score = ""
    if score_predictions:
        s1 = score_predictions[0]
        recommended_score = f"{s1[0]}-{s1[1]}"

    return generate_report(
        home_team=meta.get('home', '主队'),
        away_team=meta.get('away', '客队'),
        league=meta.get('league', ''),
        odds_t1={'home': h, 'draw': d, 'away': a},
        model_results=None,
        api_odds={'home': h, 'draw': d, 'away': a, 'source': '市场赔率'},
        real_info="\n".join(real_info_lines),
        hidden_truth="\n".join(hidden_truth_lines),
        recommended_score=recommended_score,
        score_predictions=score_predictions,
        anomalies=[],
    )

if __name__ == "__main__":
    # 使用示例数据
    print("生成示例分析报告（v4.0 8章节固定协议版）...")

    sample_report = generate_report(
        home_team="卡塔尔",
        away_team="瑞士",
        league="2026美加墨世界杯",
        odds_t1={'home': 13.0, 'draw': 6.70, 'away': 1.21},
        api_odds={'home': 13.0, 'draw': 6.70, 'away': 1.21, 'source': 'the-odds-api (Pinnacle)'},
        real_info="市场定价指向客胜，隐含概率78.5%；大小球开3.0（世界杯罕见高线）→庄家预期总进球≥3",
        hidden_truth="抽水率5.3%（正常6-8%）→庄家对结果极有信心，未收取风险溢价；参照2022阿根廷@1.25爆冷1-2沙特",
        recommended_score="0-3",
        score_predictions=[
            (0, 3, '客胜', 0.168),
            (0, 2, '客胜', 0.152),
            (1, 3, '客胜', 0.139),
            (0, 4, '客胜', 0.112),
            (0, 1, '客胜', 0.098),
            (1, 2, '客胜', 0.087),
        ],
    )

    filepath = save_report(sample_report, "sample_report_v4.md")
    print(f"报告已保存: {filepath}")
    print("\n" + sample_report)
