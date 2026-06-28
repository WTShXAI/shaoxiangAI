#!/usr/bin/env python
"""生成6/25-28四天24场预测HTML报告"""
import sys, json
from pathlib import Path

ARCH = Path(__file__).parent.parent

from datetime import datetime, timezone

# 运行24场预测
from pipeline.full_linkage_predictor import MatchInput, FullLinkagePipeline, OULinkageEngine

ALL = [
    ('6/25', MatchInput('南非', '韩国', 3.50, 3.20, 2.10, 0.0, 2.5)),
    ('6/25', MatchInput('捷克', '墨西哥', 2.80, 3.10, 2.50, 0.0, 2.5)),
    ('6/25', MatchInput('摩洛哥', '海地', 1.40, 4.20, 7.00, -1.0, 2.5)),
    ('6/25', MatchInput('波黑', '卡特尔', 2.60, 3.00, 2.80, 0.0, 2.25)),
    ('6/25', MatchInput('瑞士', '加拿大', 2.30, 3.20, 3.00, 0.0, 2.5)),
    ('6/25', MatchInput('苏格兰', '巴西', 8.00, 4.80, 1.33, +1.5, 2.75)),
    ('6/26', MatchInput('厄瓜多尔', '德国', 4.20, 3.50, 1.85, +1.0, 2.5)),
    ('6/26', MatchInput('库拉索', '科特迪瓦', 3.60, 3.30, 2.00, +2.0, 2.5)),
    ('6/26', MatchInput('突尼斯', '荷兰', 5.50, 3.80, 1.55, +2.0, 2.5)),
    ('6/26', MatchInput('巴拉圭', '澳大利亚', 2.40, 3.10, 3.00, 0.0, 2.0)),
    ('6/26', MatchInput('土耳其', '美国', 2.70, 3.20, 2.55, 0.0, 2.25)),
    ('6/26', MatchInput('日本', '瑞典', 2.10, 3.20, 3.50, 0.0, 2.25)),
    ('6/27', MatchInput('挪威', '法国', 4.05, 3.55, 1.80, +0.5, 2.5, r3_rotation=True,
                        home_formation='4-1-2-3', away_formation='4-2-3-1',
                        home_full_strength=False, away_full_strength=True,
                        home_missing_stars='哈兰德,厄德高', sporttery_hcp=+1.0)),
    ('6/27', MatchInput('塞内加尔', '伊拉克', 1.40, 4.40, 7.00, -1.25, 2.5, r3_rotation=True, sporttery_hcp=-2.0)),
    ('6/27', MatchInput('佛得角共和国', '沙特阿拉伯', 2.47, 3.35, 2.62, 0.0, 2.25, sporttery_hcp=-1.0)),
    ('6/27', MatchInput('乌拉圭', '西班牙', 4.70, 3.90, 1.63, +0.75, 2.5, r3_rotation=True, sporttery_hcp=+1.0)),
    ('6/27', MatchInput('埃及', '伊朗', 2.16, 3.00, 3.40, -0.25, 2.0, sporttery_hcp=-1.0)),
    ('6/27', MatchInput('新西兰', '比利时', 9.00, 5.20, 1.28, +1.5, 2.5, r3_rotation=True, sporttery_hcp=+2.0)),
    ('6/28', MatchInput('克罗地亚', '加纳', 1.65, 3.05, 5.11, -1.0, 2.25, r3_rotation=True, sporttery_hcp=-1.0)),
    ('6/28', MatchInput('巴拿马', '英格兰', 2.70, 4.10, 1.94, +2.0, 2.75, r3_rotation=True, sporttery_hcp=+2.0)),
    ('6/28', MatchInput('哥伦比亚', '葡萄牙', 3.50, 3.68, 1.75, +1.0, 2.25, r3_rotation=True, sporttery_hcp=+1.0)),
    ('6/28', MatchInput('民主刚果', '乌兹别克斯坦', 1.46, 4.10, 5.00, -1.0, 2.25, r3_rotation=True, sporttery_hcp=-1.0)),
    ('6/28', MatchInput('阿尔及利亚', '奥地利', 3.70, 2.02, 2.75, +1.0, 2.25, r3_rotation=True, sporttery_hcp=+1.0)),
    ('6/28', MatchInput('约旦', '阿根廷', 2.58, 3.90, 2.06, +2.0, 3.0, r3_rotation=True, sporttery_hcp=+2.0)),
]

print("Running 24-match pipeline...")
pipeline = FullLinkagePipeline()
results_by_date = {'6/25': [], '6/26': [], '6/27': [], '6/28': []}
count = 0
for date_key, m in ALL:
    count += 1
    r = pipeline.predict(m)
    r['match_obj'] = m
    results_by_date[date_key].append(r)
    s = r['final_verdict']
    print(f'  [{count}/24] {date_key} {m.home} vs {m.away} -> {s["primary"]}+{s["secondary"]} | {s["best_score"]}')

# ═══ HTML生成 ═══
def _ou_tag(ou_line, match=None):
    h = OULinkageEngine.get_ou_honesty(ou_line)
    g = h['grade']
    if match and match.r3_rotation and 2.75 <= ou_line < 3.0:
        nears = {'瑞士', '瑞典', '英格兰', '法国', '阿根廷', '日本', '伊朗'}
        massacres = {'巴西', '德国', '荷兰', '美国', '加拿大'}
        if match.home in nears or match.away in nears or match.home in massacres or match.away in massacres:
            return '名气陷阱小球', 'trap_low'
    if 'trap_low' == g: return '↓陷阱小球', 'trap_low'
    if 'trap_high_side' == g: return '↑↑高侧陷阱', 'trap_high_side'
    if 'honest_high' in g: return '↑↑诚实大球', 'honest_high'
    if 'honest_mid' in g: return '≈标准', 'honest_mid'
    if 'honest_low' in g: return '↓诚实小球', 'honest_low'
    return f'OU={ou_line}', ''

def _hcp_disp(match):
    hcp = match.hcp
    if abs(hcp) < 0.01: return '平手'
    if hcp < 0: return f'亚盘{abs(hcp):.2f}'
    return f'亚盘+{abs(hcp):.2f}'

def _vc(p):
    if '胜' in str(p): return 'win'
    if '平' in str(p): return 'draw'
    return 'lose'

def _label(s):
    best = s['best_score']
    try:
        h, a = map(int, best.split('-'))
        if h > a: return '主胜'
        if h < a: return '客胜'
        return '平'
    except: return '?'

def _alt_label(s, primary_1x2):
    alts = []
    for sc in s.get('alt_scores', [])[:3]:
        try:
            h, a = map(int, sc.split('-'))
            d = '主胜' if h > a else ('客胜' if h < a else '平')
            if d != primary_1x2 and d not in alts: alts.append(d)
        except: pass
    return alts[0] if alts else ('客胜' if primary_1x2 == '主胜' else '主胜')

def _ou_label(ou_line):
    h = OULinkageEngine.get_ou_honesty(ou_line)
    return h['note']

html = '''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<title>FootballAI v5.10 · 6/25-28 四天全量预测</title>
<style>
:root{--bg:#f5f5f5;--card:#fff;--text:#222;--muted:#888;--win:#2e7d32;--draw:#f57c00;--lose:#1565c0;--border:#e0e0e0;--r:8px;--massacre:#d32f2f}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,'Microsoft YaHei',sans-serif;background:var(--bg);color:var(--text);line-height:1.6}
.container{max-width:1300px;margin:0 auto;padding:16px}
h2{font-size:1.1em;margin:20px 0 10px;padding:6px 14px;background:linear-gradient(135deg,#1a237e,#283593);color:#fff;border-radius:var(--r)}
.row{display:flex;gap:12px;flex-wrap:wrap}
.card{background:var(--card);border-radius:var(--r);padding:12px;flex:1;min-width:290px;max-width:400px;box-shadow:0 1px 3px rgba(0,0,0,.06)}
.card-hd{display:flex;justify-content:space-between;align-items:center;gap:4px;margin-bottom:6px;flex-wrap:wrap}
.card-teams{font-weight:700;font-size:.9em}
.card-ou{font-size:.66em;padding:2px 6px;border-radius:4px;font-weight:600}
.card-ou.trap_low{background:#e8f5e9;color:#2e7d32}
.card-ou.trap_high_side{background:#ffebee;color:#b71c1c}
.card-ou.honest_high{background:#e3f2fd;color:#1565c0}
.odds{display:flex;gap:3px;margin-bottom:6px;flex-wrap:wrap}
.odds span{font-size:.66em;padding:2px 7px;border-radius:4px;background:#fafafa;border:1px solid var(--border)}
.odds span.dr{color:#f57c00;font-weight:700}
.odds span.hcp{background:#f3e5f5;color:#7b1fa2;font-weight:600}
.verdict{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:3px}
.v-main{font-size:.95em;font-weight:800;padding:2px 9px;border-radius:6px;color:#fff}
.v-main.win{background:var(--win)}.v-main.draw{background:var(--draw)}.v-main.lose{background:var(--lose)}
.v-main.massacre{background:var(--massacre)}
.v-sec{font-size:.72em;color:var(--muted)}
.score{font-size:.82em;font-weight:700;padding:2px 7px;background:#e3f2fd;border-radius:6px;color:#1565c0}
.alt-s{font-size:.64em;color:var(--muted)}
.chains{font-size:.62em;color:var(--muted);margin-top:3px}
.tag-row{display:flex;gap:4px;flex-wrap:wrap;margin:4px 0}
.tag{font-size:.62em;padding:1px 6px;border-radius:10px;font-weight:600}
.tag.massacre-warn{background:#ffebee;color:#d32f2f}
.tag.qualified{background:#e8f5e9;color:#2e7d32}
.tag.r3{background:#fff3e0;color:#e65100}
.footer{margin-top:20px;padding:12px;background:var(--card);border-radius:var(--r);font-size:.7em}
</style></head><body><div class="container">
<h1 style="font-size:1.2em;margin-bottom:2px">⚽ FootballAI v5.10 · 6/25-28 四天全量预测</h1>
<p style="font-size:.72em;color:var(--muted);margin-bottom:12px">
P0修复: trap_low降权 | 已出线→1X2胜平 | 亚盘联动恢复 | 屠杀预警优先级最高 | 不泄露赛果
</p>'''

for dk in ['6/25','6/26','6/27','6/28']:
    html += f'<h2>📅 {dk} ({len(results_by_date[dk])}场)</h2><div class="row">'
    for r in results_by_date[dk]:
        m = r['match_obj']; s = r['final_verdict']
        form = r['chains']['Form_Analysis']
        dgate = r['chains']['D_Gate']
        ou = r['chains']['OU_linkage']
        ou_label, ou_cls = _ou_tag(m.ou_line, m)
        primary_1x2 = _label(s)
        secondary_1x2 = _alt_label(s, primary_1x2)
        vc = _vc(primary_1x2)
        if s.get('massacre_warning'): vc = 'massacre'
        
        hcp_disp = _hcp_disp(m)
        context_info = s.get('context_override', '') or ''
        short_info = s.get('short_circuit_reason', '') or ''
        
        tags = []
        if s.get('massacre_warning'): tags.append('<span class="tag massacre-warn">🔴屠杀</span>')
        if 'qualified' in str(context_info).lower() or '出线' in str(context_info): tags.append('<span class="tag qualified">已出线</span>')
        if m.r3_rotation: tags.append('<span class="tag r3">R3</span>')
        if short_info: tags.append(f'<span class="tag" style="background:#e3f2fd;color:#1565c0">⚡{short_info}</span>')
        tags_str = ' '.join(tags)
        
        form_line = f'净胜差{form["goal_diff_advantage"]:+.2f} | {form["strength_gap"]}'
        ou_line = f'OU:{ou["verdict"]} [{ou.get("hcp_class","?")}]'
        dgate_line = f'D-Gate:{dgate["verdict"]}'
        
        html += f'''<div class="card">
<div class="card-hd">
  <span class="card-teams">{m.home} vs {m.away}</span>
  <span class="card-ou {ou_cls}">{ou_label}</span>
</div>
<div class="odds">
  <span>主{m.odds_h:.2f}</span><span class="dr">平{m.odds_d:.2f}</span><span>客{m.odds_a:.2f}</span><span class="hcp">{hcp_disp}</span><span>OU{m.ou_line}</span>
</div>
{ '<div class="tag-row">' + tags_str + '</div>' if tags_str else '' }
<div class="verdict">
  <span class="v-main {vc}">{primary_1x2}</span>
  <span class="v-sec">次选:{secondary_1x2}</span>
  <span class="score">{s["best_score"]}</span>
  <span class="alt-s">备选:{','.join(s["alt_scores"][:3])}</span>
</div>
<div class="chains">{form_line} | {ou_line} | {dgate_line}</div>
</div>'''
    html += '</div>'

html += f'''<div class="footer">
🔧 v5.10 修复: trap_low降权(权重×1.5/×0.4) | 已出线→1X2胜平 | 亚盘联动恢复(HCP+OU矩阵) | 屠杀预警优先级最高 | 不泄露赛果
<br>生成时间: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")}
</div></div></body></html>'''

out = ARCH / 'deliverables' / 'full-predict-0625-0628.html'
with open(out, 'w', encoding='utf-8') as f:
    f.write(html)
print(f'\n✅ 报告: {out}')
