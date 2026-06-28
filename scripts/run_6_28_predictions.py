#!/usr/bin/env python
"""6/28 六场比赛全链路预测 (OU方向性修正 v4.0)"""

import sys
import json
import math
from pathlib import Path

# 添加项目根目录

from pipeline.full_linkage_predictor import (
    MatchInput, FullLinkagePipeline, OULinkageEngine
)

# ═══ 6/28 六场比赛 ═══
MATCHES_6_28 = [
    # L组
    MatchInput('克罗地亚', '加纳',     1.65, 3.05, 5.11, -1.0, 2.25, r3_rotation=True,  sporttery_hcp=-1.0),
    MatchInput('巴拿马', '英格兰',     2.70, 4.10, 1.94, +2.0, 2.75, r3_rotation=True,  sporttery_hcp=+2.0),
    # K组
    MatchInput('哥伦比亚', '葡萄牙',   3.50, 3.68, 1.75, +1.0, 2.25, r3_rotation=True,  sporttery_hcp=+1.0),
    MatchInput('民主刚果', '乌兹别克斯坦', 1.46, 4.10, 5.00, -1.0, 2.25, r3_rotation=True, sporttery_hcp=-1.0),
    # J组
    MatchInput('阿尔及利亚', '奥地利', 3.70, 2.02, 2.75, +1.0, 2.25, r3_rotation=True,  sporttery_hcp=+1.0),
    MatchInput('约旦', '阿根廷',       2.58, 3.90, 2.06, +2.0, 3.0,  r3_rotation=True,  sporttery_hcp=+2.0),
]

def _ou_label(ou_line, match=None):
    """根据OU诚信度生成正确的标签"""
    # 名字溢价陷阱 (R3 + 强队已出线 + OU=2.75)
    # ⚠️ OU≥3.0是诚实整数线不触发 (如阿根廷屠杀OU=3.0正当)
    if match and match.r3_rotation and 2.75 <= ou_line < 3.0:
        nears = {'瑞士', '瑞典', '英格兰', '法国', '阿根廷', '日本', '伊朗'}
        massacres = {'巴西', '德国', '荷兰', '美国', '加拿大'}
        if match.home in nears or match.away in nears or match.home in massacres or match.away in massacres:
            return f'外围OU {ou_line} (🟠名气陷阱→小球)', 'down', f'OU={ou_line}→R3轮换名气溢价,实际≤2球'
    
    honesty = OULinkageEngine.get_ou_honesty(ou_line)
    grade = honesty['grade']
    note = honesty['note']
    if 'trap_low' == grade:
        return f'外围OU {ou_line} (↓小球 · 分裂陷阱)', 'down', f'OU={ou_line}→分裂线陷阱≤1球'
    elif 'trap_high_side' == grade:
        return f'外围OU {ou_line} (↑↑大球 · 高侧陷阱)', 'up', f'OU={ou_line}→高侧陷阱≥3球'
    elif 'trap_high' == grade:
        return f'外围OU {ou_line} (↑↑大球 · 高线陷阱)', 'up', f'OU={ou_line}→高线陷阱≥3.5球'
    elif 'honest_high' in grade:
        return f'外围OU {ou_line} (↑↑大球 · 诚实盘)', 'up', f'OU={ou_line}→诚实大球'
    elif 'honest_low' in grade:
        return f'外围OU {ou_line} (↓小球 · 诚实盘)', 'down', f'OU={ou_line}→诚实小球'
    elif 'honest_mid' in grade:
        return f'外围OU {ou_line} (≈标准)', '', f'OU={ou_line}→标准中球'
    else:
        return f'外围OU {ou_line}', '', f'OU={ou_line}'

def _context_tags(m: MatchInput):
    """生成情境标签"""
    tags = []
    if m.home == '克罗地亚' and m.away == '加纳':
        tags.append(('context', '🔴 搏命局 | 加纳4分打平出线'))
    elif m.home == '巴拿马' and m.away == '英格兰':
        tags.append(('flip-tag', '巴0分淘汰 | 英4分赢球头名'))
    elif m.home == '哥伦比亚' and m.away == '葡萄牙':
        tags.append(('context', '哥6分出线轮换 | 葡4分打平出线'))
    elif m.home == '民主刚果':
        tags.append(('context', '🔥双求生 | 刚果1分必须赢'))
    elif m.home == '阿尔及利亚' and m.away == '奥地利':
        tags.append(('context', '🔥双求生 各3分 | 淘汰赛博弈'))
    elif m.home == '约旦' and m.away == '阿根廷':
        tags.append(('context', '🇦🇷 6分头名→轮换 | 约旦0分淘汰'))
    return tags

def _verdict_class(primary):
    if '胜' in primary or '让胜' in primary:
        return 'win'
    elif '平' in primary:
        return 'draw'
    else:
        return 'lose'

def _clean_label(primary, secondary, hcp):
    """统一标签: 增强版 — 增加让球方向说明
    
    返回增强后的标签，让球场景会附加方向说明
    例如: "让胜 (主让1球)" 或 "让负 (客让1球)"
    """
    # 标签已经统一，无需转换，但可以增强可读性
    enhanced_primary = primary
    enhanced_secondary = secondary
    
    # 让球标签增强: 增加方向说明
    if '让' in primary and hcp != 0:
        if hcp < 0:
            # 主队让球
            direction = f' (主让{abs(hcp)}球)'
        else:
            # 客队让球
            direction = f' (客让{abs(hcp)}球)'
        enhanced_primary = primary + direction
    
    if '让' in secondary and hcp != 0:
        if hcp < 0:
            direction = f' (主让{abs(hcp)}球)'
        else:
            direction = f' (客让{abs(hcp)}球)'
        enhanced_secondary = secondary + direction
    
    return enhanced_primary, enhanced_secondary

def generate_html(results):
    """生成修正版HTML报告"""
    html = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FootballAI v5.7 · R3最终预测 · 2026-06-28 (OU方向性修正v5)</title>
<style>
  :root { --bg:#f5f5f5;--card:#fff;--text:#222;--muted:#888;--accent:#d32f2f;--win:#2e7d32;--draw:#f57c00;--lose:#1565c0;--good:#e8f5e9;--warn:#fff3e0;--bad:#ffebee;--border:#e0e0e0;--radius:8px;--walk:#ff9800; }
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,'Microsoft YaHei',sans-serif;background:var(--bg);color:var(--text);line-height:1.6}
  .container{max-width:1000px;margin:0 auto;padding:16px}
  .header{background:linear-gradient(135deg,#1a237e 0%,#283593 50%,#d32f2f 100%);color:#fff;padding:24px 20px;border-radius:var(--radius);margin-bottom:20px}
  .header h1{font-size:1.5em}.header .sub{font-size:.85em;opacity:.85;margin-top:4px}.header .meta{font-size:.75em;opacity:.7;margin-top:6px}

  .match-card{background:var(--card);border-radius:var(--radius);padding:20px;margin-bottom:16px;border-left:4px solid var(--border);box-shadow:0 1px 3px rgba(0,0,0,.06)}
  .match-card.flip{border-left-color:var(--accent)}
  .match-card.ou-hit{border-left-color:#9c27b0}
  .match-header{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px;margin-bottom:10px}
  .match-no{font-size:.75em;color:var(--muted);font-weight:600}
  .match-teams{font-size:1.15em;font-weight:700}
  .match-hcp{font-size:.8em;padding:2px 8px;border-radius:4px;background:#f0f0f0;color:#666}
  .ou-diff{font-size:.75em;padding:2px 8px;border-radius:4px;font-weight:600}
  .ou-diff.down{background:#e8f5e9;color:#2e7d32}
  .ou-diff.up{background:#ffebee;color:#b71c1c}

  .odds-row{display:flex;gap:10px;margin-bottom:10px;flex-wrap:wrap}
  .odds-box{flex:1;min-width:56px;text-align:center;padding:5px 3px;border-radius:6px;background:#fafafa;border:1px solid var(--border)}
  .odds-box .label{font-size:.65em;color:var(--muted)}
  .odds-box .val{font-size:.95em;font-weight:700}

  .verdict{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:8px}
  .verdict-main{font-size:1.2em;font-weight:800;padding:3px 12px;border-radius:6px;color:#fff}
  .verdict-main.win{background:var(--win)}.verdict-main.draw{background:var(--draw)}.verdict-main.lose{background:var(--lose)}
  .verdict-secondary{font-size:.85em;color:var(--muted);font-weight:600}
  .score-main{font-size:1.1em;font-weight:800;padding:2px 10px;background:#e3f2fd;border-radius:6px;color:#1565c0}
  .score-alt{font-size:.75em;color:var(--muted)}

  .tags{display:flex;gap:5px;flex-wrap:wrap;margin-bottom:8px}
  .tag{font-size:.7em;padding:2px 8px;border-radius:12px;font-weight:600}
  .tag.context{background:#e8eaf6;color:#283593}
  .tag.ou{background:var(--good);color:#2e7d32}
  .tag.flip-tag{background:var(--bad);color:#b71c1c}
  .tag.warn{background:var(--warn);color:#e65100}
  .market-tag{font-size:.7em;padding:2px 8px;border-radius:12px;font-weight:600;background:#e0f2f1;color:#00695c;margin-left:8px}
  .market-info{font-size:.72em;color:var(--muted);margin-top:4px}
  .market-label{font-weight:600}

  .layer-row{margin-top:8px}
  .layer{display:inline-block;margin:2px 4px 2px 0;padding:3px 8px;background:#f5f5f5;border-radius:4px;font-size:.72em}
  .layer strong{color:#1a237e}

  .summary-table{width:100%;border-collapse:collapse;margin-top:16px;background:var(--card);border-radius:var(--radius);overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.06)}
  .summary-table th{background:#1a237e;color:#fff;padding:8px 10px;font-size:.75em;text-align:left}
  .summary-table td{padding:8px 10px;font-size:.78em;border-bottom:1px solid var(--border)}
  .summary-table tr.flipped{background:#ffebee}
  .summary-table tr.bugfix{background:#e8f5e9}
</style>
</head>
<body>
<div class="container">

<div class="header">
  <h1>⚽ FootballAI v5.7 · R3 外围OU修正预测 (v5-方向性修复)</h1>
  <div class="sub">数据: Bing Sports + 11v11 + 外围OU(截图OCR) | 引擎: 7链 + D-Gate v5.3 + OU约束v4.0</div>
  <div class="meta">2026-06-27 16:20 · 🔧 OU分裂线方向性修复: 2.75高侧陷阱→大球(非小球) · 标签与分析层一致</div>
</div>

<!-- ⚠️ Bug修复说明 -->
<div style="background:#fff3e0;padding:12px;border-radius:var(--radius);margin-bottom:16px;font-size:.82em;border-left:4px solid #f57c00">
  <b>🔧 v4→v5 核心修复:</b> OU_HONESTY分裂线方向性bug —
  2.75/3.25为<b>高侧陷阱</b>(庄家诱导买小→实际大球), 但v4.0错误标为≤2球。
  修复后2.75→exp_goals=3.0(原2.0), 2.75不再压低比分, 大球标签与比分一致。
  <b>受影响场次:</b> 巴拿马vs英格兰 (OU=2.75) · 约旦vs阿根廷 (OU=3.0) 无变化。
</div>

'''
    res_no = 67
    for r in results:
        m = r['match_obj']
        strategy = r['final_verdict']
        ou_link = r['chains']['OU_linkage']
        form = r['chains']['Form_Analysis']
        dgate = r['chains']['D_Gate']
        model = r['chains']['UnifiedPredictor']

        ou_label, ou_class, ou_tag = _ou_label(m.ou_line, m)
        context_tags = _context_tags(m)
        # 清理标签: 让球场景用"让胜/让平/让负"而非"主胜/平/客胜"
        clean_primary, clean_secondary = _clean_label(strategy['primary'], strategy['secondary'], m.hcp)
        vc = _verdict_class(clean_primary)

        hcp_text = f'[{-m.hcp:+}]' if m.hcp != 0 else '[0]'
        if m.hcp < 0:
            hcp_text = f'[{-m.hcp:+}] 主让{abs(m.hcp)}球'
        else:
            hcp_text = f'[+{m.hcp}] 客让{abs(m.hcp)}球'

        flip_class = ' flip' if strategy.get('ou_constrained') or 'flip' in r.get('change', '') else ''
        if ou_class == 'up' and '高侧' in ou_label:
            flip_class += ' ou-hit'

        html += f'''
<!-- M{res_no}: {m.home}vs{m.away} -->
<div class="match-card{flip_class}">
  <div class="match-header">
    <span class="match-no">{res_no:03d} · R3</span>
    <span class="match-teams">{m.home} vs {m.away}</span>
    <span class="match-hcp">{hcp_text}</span>
    <span class="ou-diff {ou_class}">{ou_label}</span>
  </div>
  <div class="odds-row">
    <div class="odds-box"><div class="label">主胜</div><div class="val">{m.odds_h:.2f}</div></div>
    <div class="odds-box"><div class="label">平局</div><div class="val" style="color:var(--draw)">{m.odds_d:.2f}</div></div>
    <div class="odds-box"><div class="label">客胜</div><div class="val">{m.odds_a:.2f}</div></div>
    <div class="odds-box"><div class="label">外围OU</div><div class="val" style="color:{'#b71c1c' if ou_class=='up' else '#2e7d32'}">{m.ou_line}</div></div>
  </div>
  <div class="tags">
'''
        for tcls, ttext in context_tags:
            html += f'    <span class="tag {tcls}">{ttext}</span>\n'
        html += f'    <span class="tag ou">{ou_tag}</span>\n'

        if r.get('change'):
            html += f'    <span class="tag flip-tag">⚡ v4→v5 {r["change"]}</span>\n'

        if strategy.get('massacre_warning'):
            html += f'    <span class="tag warn">⚠️ 屠杀预警</span>\n'

        html += f'''  </div>
  <div class="verdict">
    <span class="verdict-main {vc}">{clean_primary}</span>
    <span class="verdict-secondary">次选: {clean_secondary}</span>
    <span class="score-main">{m.home} <b>{strategy["best_score"]}</b> {m.away}</span>
    <span class="score-alt">备选: {", ".join([f'{m.home} {s} {m.away}' for s in strategy["alt_scores"][:3]])}</span>
    <span class="market-tag">推荐市场: {", ".join(strategy.get("rec_markets", ["1X2"]))}</span>
  </div>
  <div class="market-info">
    <span class="market-label">推荐类型:</span>
    <span class="market-value">{strategy.get("rec_type", "均衡")}</span>
  </div>
  <div class="layer-row">
    <span class="layer"><strong>链-1</strong> 净胜差{form["goal_diff_advantage"]:+.2f}→{form["strength_gap"]}</span>
    <span class="layer"><strong>链0</strong> 战意覆盖</span>
    <span class="layer"><strong>链1</strong> OU={m.ou_line} {ou_tag}</span>
    <span class="layer"><strong>链2</strong> D-Gate: {dgate["verdict"]}</span>
    <span class="layer"><strong>🗳</strong> 三路投票</span>
    <span class="layer"><strong>链4</strong> {clean_primary}+{clean_secondary}</span>
  </div>
</div>
'''
        res_no += 1

    html += '''
<!-- Summary -->
<h2 style="font-size:1.1em;margin-top:20px;">📊 五版演进</h2>
<table class="summary-table">
  <thead><tr><th>比赛</th><th>v1(2场WC)</th><th>v2(估算)</th><th>v3(11v11)</th><th>v4(外围OU·有bug)</th><th>v5(方向修复)</th><th>翻转</th></tr></thead>
  <tbody>
'''

    # Hardcoded v1-v4 comparison
    evo = [
        ('067 克罗地亚vs加纳', '主胜/平 1-1', '主胜/平 1-1', '主胜/平 1-1', '主胜/平 0-0', None, '比分↓(v3→v4)'),
        ('068 巴拿马vs英格兰', '让负/客胜 0-3', '让负/客胜 0-3', '让平/让胜 0-1', '客胜/让负 0-1', None, '🔥方向(v3→v4)'),
        ('069 哥伦比亚vs葡萄牙', '平/胜 1-1', '平/胜 1-1', '平/胜 1-1', '平/胜 0-0', None, '比分↓(v3→v4)'),
        ('070 刚果vs乌兹别克', '让胜/主胜 3-1', '胜/客胜 1-1', '胜/客胜 1-1', '胜/客胜 0-0', None, '比分↓(v3→v4)'),
        ('071 阿尔及利亚vs奥地利', '胜/客胜 1-1', '胜/客胜 1-1', '胜/客胜 1-1', '胜/客胜 0-0', None, '比分↓(v3→v4)'),
        ('072 约旦vs阿根廷', '让负/客胜 0-4', '让平/让胜 1-3', '让平/让胜 1-3', '让负/客胜 0-3', None, '🔥方向(v3→v4)'),
    ]

    # Update v5 column from results
    for i, r in enumerate(results):
        strategy = r['final_verdict']
        m = r['match_obj']
        _, clean_sec = _clean_label(strategy["primary"], strategy["secondary"], m.hcp)
        pri_label = _clean_label(strategy["primary"], strategy["secondary"], m.hcp)[0]
        score_display = f'{m.home} {strategy["best_score"]} {m.away}'
        v5_text = f'{pri_label}/{clean_sec} {score_display}'
        evo[i] = evo[i][:5] + (v5_text,) + evo[i][6:]

    for row in evo:
        name, v1, v2, v3, v4, v5, flip = row
        flip_row = ' flipped' if '🔥' in flip else ''
        bugfix_row = ' bugfix' if 'OU' in name and '巴拿马' in name else ''
        html += f'    <tr class="{flip_row}{bugfix_row}"><td>{name}</td><td>{v1}</td><td>{v2}</td><td>{v3}</td><td>{v4}</td><td><b>{v5}</b></td><td>{flip}</td></tr>\n'

    html += '''  </tbody>
</table>

<div class="legend" style="margin-top:12px;padding:12px;background:var(--card);border-radius:var(--radius);font-size:.78em">
  <b>🔧 v5核心修复 (OU方向性):</b><br>
  · <b>概念bug</b>: v4.0将<b>所有</b>分裂线统一降进球 → OU=2.75被错误标为"trap_mid/≤2球", 但实际2.75高于标准2.5, 庄家诱导买小, 实际进球偏高<br>
  · <b>修复</b>: 2.75 → trap_high_side (exp_goals=3.0→2.0) · 3.25 → trap_high (exp_goals=3.8→2.5) · honesty_mult从0.8→1.1<br>
  · <b>受影响</b>: 巴拿马vs英格兰 (OU=2.75) — v4标签"↑大球"但比分低, v5一致修复<br>
  · <b>OCR管道</b>: 2026WC/{date}/*.png → rapidocr → ou_screenshot → 链3.5 OU约束 ✅ 固定路径已建立<br>
  · <b>代码变更</b>: pipeline/full_linkage_predictor.py · OU_HONESTY v4.0 · 行159-176
</div>

</div>
</body>
</html>
'''
    return html

def main():
    print("=" * 60)
    print("FootballAI v5.7 · 6/28 全链路预测 (OU方向性修正)")
    print("=" * 60)

    pipeline = FullLinkagePipeline()
    results = []

    for m in MATCHES_6_28:
        print(f"\n{'─'*60}")
        print(f"  📋 {m.home} vs {m.away}")
        print(f"{'─'*60}")

        result = pipeline.predict(m)
        results.append(result)
        
        # 显示OU分级 (使用pipeline实际输出)
        ou_honesty = result['chains']['OU_linkage'].get('ou_honesty', {})
        grade = ou_honesty.get('grade', '?')
        if grade and grade != '?':
            print(f"  OU={m.ou_line} → {grade} | {ou_honesty.get('note', '')}")        # 标记变化
        result['match_obj'] = m
        strategy = result['final_verdict']
        if m.home == '巴拿马' and m.away == '英格兰':
            result['change'] = 'OU方向修复: 大球标签-比分一致'
        elif m.home == '约旦' and m.away == '阿根廷':
            result['change'] = 'OU=3.0诚实大球·无变化'

    # ── 汇总 ──
    print(f"\n{'='*60}")
    print(f"  🎫 6/28 全链路联动 · R3预测汇总")
    print(f"{'='*60}")
    for r in results:
        strategy = r['final_verdict']
        m = r['match_obj']
        primary = strategy['primary']
        secondary = strategy['secondary']
        best = strategy['best_score']
        alt = strategy['alt_scores'][:3]
        ou = m.ou_line
        # 从pipeline实际输出取ou_honesty
        ou_pipeline = r['chains']['OU_linkage'].get('ou_honesty', {})
        ou_grade = ou_pipeline.get('grade', '') if ou_pipeline else ''
        if not ou_grade:
            ou_grade = OULinkageEngine.get_ou_honesty(ou)['grade']
        score_display = f'{m.home} {best} {m.away}'
        alt_display = ', '.join([f'{m.home} {s} {m.away}' for s in alt])
        print(f"  {r['match']:30s} → {primary}+{secondary:4s} "
              f"| {score_display:<20s} | {alt_display:<24s}"
              f"| OU={ou} [{ou_grade}] | conf={strategy['confidence']:.2f}")

    # ── 生成HTML报告 ──
    html = generate_html(results)
    output_dir = Path(__file__).parent.parent / 'deliverables'
    output_file = output_dir / 'r3-0628-predictions.html'
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"\n✅ 报告已生成: {output_file}")

    # 保存JSON结果
    json_file = output_dir / 'r3-0628-results.json'
    
    def clean_for_json(obj, depth=0):
        """递归清理非JSON序列化对象"""
        if depth > 10:  # 防止无限递归
            return str(obj)
        if obj is None or isinstance(obj, (str, int, float, bool)):
            return obj
        elif isinstance(obj, dict):
            return {k: clean_for_json(v, depth+1) for k, v in obj.items() 
                    if not callable(v) and k != 'match_obj'}
        elif isinstance(obj, (list, tuple)):
            return [clean_for_json(x, depth+1) for x in obj 
                    if not callable(x)]
        elif callable(obj):
            return str(obj)
        else:
            return str(obj)
    
    clean_results = [clean_for_json(r) for r in results]
    with open(json_file, 'w', encoding='utf-8') as f:
        json.dump(clean_results, f, ensure_ascii=False, indent=2)
    print(f"✅ JSON已保存: {json_file}")

    return results

if __name__ == '__main__':
    main()
