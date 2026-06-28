#!/usr/bin/env python
"""6/25-6/28 四天全量预测 (P0修复后 · 不泄露赛果 · 离线)"""

import sys, json
from pathlib import Path

ARCH = Path(__file__).parent.parent
sys.path.insert(0, str(ARCH))
from pipeline.full_linkage_predictor import (
    MatchInput, FullLinkagePipeline, OULinkageEngine
)

# ═══════════════════════════════════════
# 全部比赛 (24场)
# ═══════════════════════════════════════

ALL = [
    # ── 6/25 (6场) ── 截图OCR估算赔率
    ('6/25', MatchInput('南非', '韩国', 3.50, 3.20, 2.10, 0.0, 2.5)),
    ('6/25', MatchInput('捷克', '墨西哥', 2.80, 3.10, 2.50, 0.0, 2.5)),
    ('6/25', MatchInput('摩洛哥', '海地', 1.40, 4.20, 7.00, -1.0, 2.5)),
    ('6/25', MatchInput('波黑', '卡特尔', 2.60, 3.00, 2.80, 0.0, 2.25)),
    ('6/25', MatchInput('瑞士', '加拿大', 2.30, 3.20, 3.00, 0.0, 2.5)),
    ('6/25', MatchInput('苏格兰', '巴西', 8.00, 4.80, 1.33, +1.5, 2.75)),

    # ── 6/26 (6场) ── 真实赔率
    ('6/26', MatchInput('厄瓜多尔', '德国', 4.20, 3.50, 1.85, +1.0, 2.5)),
    ('6/26', MatchInput('库拉索', '科特迪瓦', 3.60, 3.30, 2.00, +2.0, 2.5)),
    ('6/26', MatchInput('突尼斯', '荷兰', 5.50, 3.80, 1.55, +2.0, 2.5)),
    ('6/26', MatchInput('巴拉圭', '澳大利亚', 2.40, 3.10, 3.00, 0.0, 2.0)),
    ('6/26', MatchInput('土耳其', '美国', 2.70, 3.20, 2.55, 0.0, 2.25)),
    ('6/26', MatchInput('日本', '瑞典', 2.10, 3.20, 3.50, 0.0, 2.25)),

    # ── 6/27 (6场) ──
    ('6/27', MatchInput('挪威', '法国', 4.05, 3.55, 1.80, +0.5, 2.5, r3_rotation=True,
                        home_formation='4-1-2-3', away_formation='4-2-3-1',
                        home_full_strength=False, away_full_strength=True,
                        home_missing_stars='哈兰德,厄德高')),
    ('6/27', MatchInput('塞内加尔', '伊拉克', 1.40, 4.40, 7.00, -1.25, 2.5, r3_rotation=True)),
    ('6/27', MatchInput('佛得角共和国', '沙特阿拉伯', 2.47, 3.35, 2.62, 0.0, 2.25)),
    ('6/27', MatchInput('乌拉圭', '西班牙', 4.70, 3.90, 1.63, +0.75, 2.5, r3_rotation=True)),
    ('6/27', MatchInput('埃及', '伊朗', 2.16, 3.00, 3.40, -0.25, 2.0)),
    ('6/27', MatchInput('新西兰', '比利时', 9.00, 5.20, 1.28, +1.5, 2.5, r3_rotation=True)),

    # ── 6/28 (6场) ──
    ('6/28', MatchInput('克罗地亚', '加纳', 1.65, 3.05, 5.11, -1.0, 2.25, r3_rotation=True)),
    ('6/28', MatchInput('巴拿马', '英格兰', 2.70, 4.10, 1.94, +2.0, 2.75, r3_rotation=True)),
    ('6/28', MatchInput('哥伦比亚', '葡萄牙', 3.50, 3.68, 1.75, +1.0, 2.25, r3_rotation=True)),
    ('6/28', MatchInput('民主刚果', '乌兹别克斯坦', 1.46, 4.10, 5.00, -1.0, 2.25, r3_rotation=True)),
    ('6/28', MatchInput('阿尔及利亚', '奥地利', 3.70, 2.02, 2.75, +1.0, 2.25, r3_rotation=True)),
    ('6/28', MatchInput('约旦', '阿根廷', 2.58, 3.90, 2.06, +2.0, 3.0,  r3_rotation=True)),
]


def _ou_tag(ou_line, match=None):
    """OU标签 (name_trap已删除)"""
    h = OULinkageEngine.get_ou_honesty(ou_line)
    g = h['grade']
    if 'trap_low' == g: return '↓陷阱小球', 'trap_low'
    if 'trap_high_side' == g: return '↑↑高侧陷阱', 'trap_high_side'
    if 'honest_high' in g: return '↑↑诚实大球', 'honest_high'
    if 'honest_mid' in g: return '≈标准', 'honest_mid'
    if 'honest_low' in g: return '↓诚实小球', 'honest_low'
    return f'OU={ou_line}', ''

def _vc(p):
    if '胜' in str(p): return 'win'
    if '平' in str(p): return 'draw'
    return 'lose'

def _best_score_to_1x2(score_str):
    """从比分直接推导1X2方向 (不依赖让球标签)"""
    try:
        h, a = map(int, score_str.split('-'))
        if h > a: return '主胜'
        if h < a: return '客胜'
        return '平'
    except:
        return '?'

def _display_label(p, s, m, strategy):
    """全部转为1X2标签。让球分析在管道内, 报告只显示1X2方向"""
    best = strategy.get('best_score', '0-0')
    primary_1x2 = _best_score_to_1x2(best)
    alts = []
    for sc in strategy.get('alt_scores', [])[:3]:
        d = _best_score_to_1x2(sc)
        if d != primary_1x2 and d not in alts:
            alts.append(d)
    secondary = alts[0] if alts else ('客胜' if primary_1x2 == '主胜' else '主胜')
    return primary_1x2, secondary
    return p, s


def generate_html(results_by_date):
    h = '''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<title>FootballAI v5.10 · 6/25-28 四天预测 (P0修复后)</title>
<style>
:root{--bg:#f5f5f5;--card:#fff;--text:#222;--muted:#888;--win:#2e7d32;--draw:#f57c00;--lose:#1565c0;--border:#e0e0e0;--r:8px}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,'Microsoft YaHei',sans-serif;background:var(--bg);color:var(--text);line-height:1.6}
.container{max-width:1200px;margin:0 auto;padding:16px}
h2{font-size:1.1em;margin:20px 0 10px;padding:6px 14px;background:linear-gradient(135deg,#1a237e,#283593);color:#fff;border-radius:var(--r)}
.row{display:flex;gap:12px;flex-wrap:wrap}
.card{background:var(--card);border-radius:var(--r);padding:12px;flex:1;min-width:280px;max-width:380px;box-shadow:0 1px 3px rgba(0,0,0,.06)}
.card-hd{display:flex;justify-content:space-between;align-items:center;gap:4px;margin-bottom:6px;flex-wrap:wrap}
.card-teams{font-weight:700;font-size:.9em}
.card-ou{font-size:.66em;padding:2px 6px;border-radius:4px;font-weight:600}
.card-ou.trap_low{background:#e8f5e9;color:#2e7d32}
.card-ou.trap_high_side{background:#ffebee;color:#b71c1c}
.card-ou.honest_high{background:#e3f2fd;color:#1565c0}
.odds{display:flex;gap:3px;margin-bottom:6px;flex-wrap:wrap}
.odds span{font-size:.66em;padding:2px 7px;border-radius:4px;background:#fafafa;border:1px solid var(--border)}
.odds span.dr{color:#f57c00;font-weight:700}
.verdict{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:3px}
.v-main{font-size:.95em;font-weight:800;padding:2px 9px;border-radius:6px;color:#fff}
.v-main.win{background:var(--win)}.v-main.draw{background:var(--draw)}.v-main.lose{background:var(--lose)}
.v-sec{font-size:.72em;color:var(--muted)}
.score{font-size:.82em;font-weight:700;padding:2px 7px;background:#e3f2fd;border-radius:6px;color:#1565c0}
.alt-s{font-size:.64em;color:var(--muted)}
.chains{font-size:.62em;color:var(--muted);margin-top:3px}
.footer{margin-top:20px;padding:12px;background:var(--card);border-radius:var(--r);font-size:.7em}
</style></head><body><div class="container">
<h1 style="font-size:1.2em;margin-bottom:2px">⚽ FootballAI v5.10 · 四天预测</h1>
<p style="font-size:.72em;color:var(--muted);margin-bottom:12px">
P0修复后: trap_low降权 | 已出线→1X2胜平 | 禁止泄露赛果 | 让球代码已移除
</p>'''
    for dk in ['6/25','6/26','6/27','6/28']:
        h += f'<h2>📅 {dk} ({len(results_by_date[dk])}场)</h2><div class="row">'
        for r in results_by_date[dk]:
            m = r['match_obj']; s = r['final_verdict']
            ou_scores = r['chains']['OU_linkage']['scores']
            form = r['chains']['Form_Analysis']
            dgate = r['chains']['D_Gate']
            ou_label, ou_cls = _ou_tag(m.ou_line, m)
            vc = _vc(s['primary'])
            dp, ds = _display_label(s['primary'], s['secondary'], m, s)
            h += f'''<div class="card">
<div class="card-hd">
  <span class="card-teams">{m.home} vs {m.away}</span>
  <span class="card-ou {ou_cls}">{ou_label}</span>
</div>
<div class="odds">
  <span>主{m.odds_h:.2f}</span><span class="dr">平{m.odds_d:.2f}</span><span>客{m.odds_a:.2f}</span>
</div>
<div class="verdict">
  <span class="v-main {vc}">{dp}</span>
  <span class="v-sec">次选:{ds}</span>
  <span class="score">{s["best_score"]}</span>
  <span class="alt-s">备选:{','.join(s["alt_scores"][:3])}</span>
</div>
<div class="chains">链-1 净胜差{form["goal_diff_advantage"]:+.2f} {form["strength_gap"]} | 链2 D-Gate:{dgate["verdict"]}</div>
</div>'''
        h += '</div>'
    h += '<div class="footer">🔧 P0修复: trap_low降权(权重排序替代硬排除) | 已出线→1X2胜平 | 不泄露赛果 | 让球代码已移除</div>'
    h += '</div></body></html>'
    return h


def main():
    print("=" * 50)
    print("FootballAI v5.10 · 6/25-28 四天全量预测")
    print("=" * 50)
    pipeline = FullLinkagePipeline()
    results_by_date = {'6/25': [], '6/26': [], '6/27': [], '6/28': []}
    count = 0
    for date_key, m in ALL:
        count += 1
        print(f"\n[{count}/24] {date_key} {m.home} vs {m.away}")
        r = pipeline.predict(m)
        r['match_obj'] = m
        results_by_date[date_key].append(r)
        s = r['final_verdict']
        ou = r['chains']['OU_linkage']
        ou_g = ou.get('ou_honesty',{}).get('grade','?')
        print(f"  → {s['primary']}+{s['secondary']} | {s['best_score']} | OU={m.ou_line}[{ou_g}] | conf={s['confidence']:.2f}")

    print(f"\n{'='*50}")
    for dk in ['6/25','6/26','6/27','6/28']:
        print(f"  {dk}: {len(results_by_date[dk])}场")

    html = generate_html(results_by_date)
    out = ARCH / 'deliverables' / 'full-predict-0625-0628.html'
    with open(out, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"\n✅ 报告: {out}")


if __name__ == '__main__':
    main()
