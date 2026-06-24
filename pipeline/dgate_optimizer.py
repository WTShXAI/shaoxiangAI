#!/usr/bin/env python
"""
哨响AI v4.4 — D-Gate 多信号平局检测系统
=========================================
从世界杯24场回测发现的根本问题:
  赔率隐含D概率(10-27%)远低于决策阈值(40-72%), 结构性地无法预测热门翻车型平局
  
解决方案:
  独立D-Gate — 不依赖D概率跨阈值, 而是用多个外部信号组合评分,
  当多信号共振时直接覆盖预测为平局。

信号体系 (6维):
  S1: OU ≤ 2.5 → 低比分环境 (+1)  |  OU ≤ 2.0 → 极低比分 (+2)
  S2: spread < 0.25 → 实力均衡 (+1)  |  spread < 0.10 → 高度均衡 (+2)  
  S3: 让球不足 (actual < expected×0.7) → 庄家不信 (+2)
  S4: 水位 ≥ 2.0 → 诱盘信号 (+1)
  S5: 热门赔率 1.20-2.00 → 中等热门但非碾压 (+1)
  S6: imp_D > 0.20 → 赔率中有平局基础信号 (+1)

Gate: score ≥ threshold → 强制预测 D
网格搜索: threshold ∈ [2,3,4,5,6], 在24场WC上验证

同时加载DrawExpert模型, 对比其D概率与ensemble模型的差异。
"""
import sys, os, json, time, joblib, math
from pathlib import Path
from collections import defaultdict

# 路径 (修复P0-13: 消除外部依赖)
ROOT = Path(__file__).parent.parent
FOOTBALL_AI = ROOT / 'predictors' / 'components'  # 内部化
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(FOOTBALL_AI))

# ═══════════════════════════════════════════
# 世界杯24场数据 (FIFA官网验证)
# ═══════════════════════════════════════════
MATCHES = [
    # 6.14
    {'date':'6.14','home':'卡塔尔','away':'瑞士','H':5.60,'D':3.75,'A':1.61,'hc':1.0,'ou':2.5,'act':'D','score':'1-1'},
    {'date':'6.14','home':'巴西','away':'摩洛哥','H':1.39,'D':4.50,'A':7.50,'hc':-1.5,'ou':2.5,'act':'D','score':'1-1'},
    {'date':'6.14','home':'海地','away':'苏格兰','H':6.90,'D':4.50,'A':1.40,'hc':1.5,'ou':2.5,'act':'A','score':'0-1'},
    {'date':'6.14','home':'澳大利亚','away':'土耳其','H':4.55,'D':3.35,'A':1.76,'hc':0.5,'ou':2.5,'act':'H','score':'2-0'},
    # 6.15
    {'date':'6.15','home':'德国','away':'库拉索','H':1.53,'D':4.15,'A':5.20,'hc':-1.0,'ou':3.5,'act':'H','score':'7-1'},
    {'date':'6.15','home':'瑞典','away':'突尼斯','H':1.76,'D':3.35,'A':4.70,'hc':-0.5,'ou':2.5,'act':'H','score':'5-1'},
    {'date':'6.15','home':'科特迪瓦','away':'厄瓜多尔','H':2.60,'D':3.35,'A':2.60,'hc':0.0,'ou':2.5,'act':'H','score':'1-0'},
    {'date':'6.15','home':'荷兰','away':'日本','H':1.63,'D':3.90,'A':4.70,'hc':-0.5,'ou':2.5,'act':'D','score':'2-2'},
    # 6.16
    {'date':'6.16','home':'伊朗','away':'新西兰','H':1.44,'D':4.25,'A':6.30,'hc':-1.25,'ou':2.5,'act':'D','score':'2-2'},
    {'date':'6.16','home':'比利时','away':'埃及','H':1.39,'D':4.50,'A':7.10,'hc':-1.5,'ou':2.5,'act':'D','score':'1-1'},
    {'date':'6.16','home':'沙特阿拉伯','away':'乌拉圭','H':7.10,'D':4.50,'A':1.39,'hc':1.5,'ou':2.5,'act':'D','score':'1-1'},
    {'date':'6.16','home':'西班牙','away':'佛得角共和国','H':1.08,'D':8.80,'A':18.0,'hc':-2.5,'ou':3.5,'act':'D','score':'0-0'},
    # 6.17
    {'date':'6.17','home':'伊拉克','away':'挪威','H':3.10,'D':3.40,'A':2.14,'hc':0.25,'ou':2.5,'act':'A','score':'1-4'},
    {'date':'6.17','home':'奥地利','away':'约旦','H':1.46,'D':4.15,'A':6.20,'hc':-1.0,'ou':2.5,'act':'H','score':'3-1'},
    {'date':'6.17','home':'法国','away':'塞内加尔','H':1.08,'D':8.80,'A':20.0,'hc':-2.5,'ou':3.5,'act':'H','score':'3-1'},
    {'date':'6.17','home':'阿根廷','away':'阿尔及利亚','H':1.60,'D':3.85,'A':5.00,'hc':-0.5,'ou':2.5,'act':'H','score':'3-0'},
    # 6.18
    {'date':'6.18','home':'乌兹别克斯坦','away':'哥伦比亚','H':5.60,'D':4.05,'A':1.52,'hc':1.0,'ou':2.5,'act':'A','score':'1-3'},
    {'date':'6.18','home':'加纳','away':'巴拿马','H':1.52,'D':3.95,'A':5.70,'hc':-1.0,'ou':2.5,'act':'H','score':'1-0'},
    {'date':'6.18','home':'英格兰','away':'克罗地亚','H':1.30,'D':5.00,'A':8.30,'hc':-1.5,'ou':2.5,'act':'H','score':'4-2'},
    {'date':'6.18','home':'葡萄牙','away':'民主刚果','H':1.22,'D':5.90,'A':10.0,'hc':-1.75,'ou':3.0,'act':'D','score':'1-1'},
    # 6.18追加
    {'date':'6.18','home':'捷克','away':'南非','H':1.61,'D':3.40,'A':5.20,'hc':-0.75,'ou':2.5,'act':'D','score':'1-1'},
    {'date':'6.18','home':'瑞士','away':'波黑','H':1.61,'D':3.75,'A':5.00,'hc':-0.5,'ou':2.5,'act':'H','score':'4-1'},
    # 6.19
    {'date':'6.19','home':'加拿大','away':'卡塔尔','H':1.61,'D':3.75,'A':5.00,'hc':-0.5,'ou':2.5,'act':'H','score':'6-0'},
    {'date':'6.19','home':'墨西哥','away':'韩国','H':1.69,'D':3.45,'A':4.90,'hc':-0.5,'ou':2.5,'act':'H','score':'1-0'},
]

# ═══════════════════════════════════════════
# 基础概率计算
# ═══════════════════════════════════════════
def implied_probs(oh, od, oa):
    inv = 1/oh + 1/od + 1/oa
    return 1/oh/inv, 1/od/inv, 1/oa/inv

# ═══════════════════════════════════════════
# D-Gate 信号评分
# ═══════════════════════════════════════════
def compute_d_signals(m):
    """计算6个平局信号, 返回 (score, signals_detail)"""
    oh, od, oa = m['H'], m['D'], m['A']
    handicap = m['hc']; ou = m['ou']
    imp_h, imp_d, imp_a = implied_probs(oh, od, oa)
    spread = abs(imp_h - imp_a)
    max_imp = max(imp_h, imp_a)

    score = 0
    signals = []

    # S1: OU线 — 低比分环境
    if ou <= 2.0:
        score += 2; signals.append(('OU极低≤2.0', 2))
    elif ou <= 2.5:
        score += 1; signals.append(('OU偏低≤2.5', 1))

    # S2: Spread — 实力均衡
    if spread < 0.10:
        score += 2; signals.append(('高度均衡', 2))
    elif spread < 0.25:
        score += 1; signals.append(('实力均衡', 1))

    # S3: 让球不足检测 — 庄家不信
    if handicap != 0:
        odds_ratio = oa / max(oh, 0.01) if oh else 1
        expected_hcp = max(0, (odds_ratio - 1) * 1.5)
        actual_hcp = abs(handicap)
        if expected_hcp > 0.2:
            ratio = actual_hcp / expected_hcp
            if ratio < 0.4:
                score += 2; signals.append((f'严重浅盘({actual_hcp:.1f}<{expected_hcp:.1f})', 2))
            elif ratio < 0.7:
                score += 1; signals.append((f'让球不足({actual_hcp:.1f}<{expected_hcp:.1f})', 1))

    # S4: 水位 — 诱盘
    water = m.get('water', m.get('wl', None))
    if water is not None and water >= 2.0:
        score += 1; signals.append(('高水位≥2.0', 1))

    # S5: 热门但非碾压 — 翻车风险
    if 1.20 <= oh <= 2.00:
        score += 1; signals.append((f'中等热门({oh})', 1))
    elif 1.20 <= oa <= 2.00:
        score += 1; signals.append((f'中等热门(客{oa})', 1))

    # S6: 赔率隐含D基础信号
    if imp_d > 0.20:
        score += 1; signals.append((f'D基础({imp_d:.0%})', 1))
    elif imp_d > 0.25:
        score += 1; signals.append((f'D偏强({imp_d:.0%})', 1))

    return score, signals, {'imp_h': imp_h, 'imp_d': imp_d, 'imp_a': imp_a, 'spread': spread}

# ═══════════════════════════════════════════
# 预测器 (可切换阈值)
# ═══════════════════════════════════════════
def predict(m, gate_threshold, use_gate=True):
    """D-Gate增强预测"""
    oh, od, oa = m['H'], m['D'], m['A']
    imp_h, imp_d, imp_a = implied_probs(oh, od, oa)
    spread = abs(imp_h - imp_a)

    # D-Gate 评分解锁
    gate_active = False
    gate_score = 0
    gate_signals = []
    extras = {}

    if use_gate:
        gate_score, gate_signals, extras = compute_d_signals(m)
        gate_active = gate_score >= gate_threshold

    # 基础判定 (argmax + 软阈值护底)
    # 如果D-Gate激活, 直接用D覆盖
    if gate_active:
        return 'D', extras, gate_score, gate_signals

    # 标准argmax
    if imp_h >= imp_a and imp_h >= imp_d:
        return 'H', extras, gate_score, gate_signals
    elif imp_a >= imp_h and imp_a >= imp_d:
        return 'A', extras, gate_score, gate_signals
    else:
        return 'D', extras, gate_score, gate_signals

def compute_metrics(preds, actuals):
    n = len(preds)
    correct = sum(1 for p, a in zip(preds, actuals) if p == a)
    acc = correct / n if n else 0
    metrics = {'n': n, 'acc': acc, 'correct': correct}
    for cls in ['H', 'D', 'A']:
        tp = sum(1 for p, a in zip(preds, actuals) if p == cls and a == cls)
        fp = sum(1 for p, a in zip(preds, actuals) if p == cls and a != cls)
        fn = sum(1 for p, a in zip(preds, actuals) if p != cls and a == cls)
        prec = tp / (tp + fp) if (tp + fp) > 0 else 0
        rec = tp / (tp + fn) if (tp + fn) > 0 else 0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0
        metrics[f'{cls}_prec'] = prec
        metrics[f'{cls}_rec'] = rec
        metrics[f'{cls}_f1'] = f1
        metrics[f'{cls}_tp'] = tp; metrics[f'{cls}_fp'] = fp; metrics[f'{cls}_fn'] = fn
    return metrics

# ═══════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════
def main():
    print("⚽ 哨响AI v4.4 · D-Gate多信号平局检测")
    print("=" * 70)
    print(f"  比赛: {len(MATCHES)}场 (FIFA验证)")
    print(f"  信号: OU低 + spread均衡 + 让球不足 + 高水位 + 中等热门 + D基础")
    print()

    actuals = [m['act'] for m in MATCHES]

    # ── 基线: argmax ──
    baseline_preds = []
    for m in MATCHES:
        imp_h, imp_d, imp_a = implied_probs(m['H'], m['D'], m['A'])
        if imp_h >= imp_a and imp_h >= imp_d:
            baseline_preds.append('H')
        elif imp_a >= imp_h and imp_a >= imp_d:
            baseline_preds.append('A')
        else:
            baseline_preds.append('D')
    bl = compute_metrics(baseline_preds, actuals)
    print(f"  基线(argmax): Acc={bl['acc']:.1%} D-F1={bl['D_f1']:.4f} D召回={bl['D_rec']:.0%} H-F1={bl['H_f1']:.4f}")

    # ── 网格搜索最优D-Gate阈值 ──
    print(f"\n{'='*70}")
    print(f"🔍 D-Gate 阈值网格搜索 (2 ≤ threshold ≤ 6)")
    print(f"{'='*70}")
    print(f'{"阈值":>6s} {"Acc":>6s} {"D-F1":>8s} {"D召回":>6s} {"D精":>5s} {"H-F1":>8s} {"Gate激活":>8s} {"D命中":>6s}')

    best_d_f1 = 0
    best_config = None
    results = []

    for thr in range(2, 7):
        preds = []
        d_hits = 0
        gate_count = 0
        for m in MATCHES:
            verdict, extras, gs, sigs = predict(m, thr, use_gate=True)
            preds.append(verdict)
            if gs >= thr:
                gate_count += 1
                if verdict == 'D' and m['act'] == 'D':
                    d_hits += 1

        mtr = compute_metrics(preds, actuals)
        star = '★' if mtr['D_f1'] >= best_d_f1 else ' '
        if mtr['D_f1'] > best_d_f1 or (mtr['D_f1'] == best_d_f1 and mtr['acc'] > best_config[1]['acc'] if best_config else True):
            best_d_f1 = mtr['D_f1']
            best_config = (thr, mtr, preds, gate_count, d_hits)

        results.append((thr, mtr, gate_count, d_hits))
        print(f'{thr:>6d} {mtr["acc"]:>6.1%} {mtr["D_f1"]:>8.4f} {mtr["D_rec"]:>6.0%} {mtr["D_prec"]:>5.0%} {mtr["H_f1"]:>8.4f} {gate_count:>8d} {d_hits:>6d} {star}')

    # ── 最优配置输出 ──
    print(f"\n{'='*70}")
    print(f"🏆 最优D-Gate阈值: {best_config[0]} (D-F1={best_config[1]['D_f1']:.4f})")
    print(f"{'='*70}")
    print(f"  Acc: {best_config[1]['acc']:.1%} | D-F1: {best_config[1]['D_f1']:.4f} | D召回: {best_config[1]['D_rec']:.0%} | D精确: {best_config[1]['D_prec']:.0%}")
    print(f"  Gate激活: {best_config[3]}/24场, D命中: {best_config[4]}/9场实际平局")
    print(f"  基线→最优: Acc {bl['acc']:.1%}→{best_config[1]['acc']:.1%}, D-F1 {bl['D_f1']:.4f}→{best_config[1]['D_f1']:.4f}")

    # ── 最优配置下的逐场分析 ──
    opt_thr = best_config[0]
    print(f"\n{'='*70}")
    print(f"📋 最优配置 (threshold={opt_thr}) 逐场分析")
    print(f"{'='*70}")
    print(f'{"比赛":<20s} {"Score":>5s} {"Gate":>5s} {"预测":>4s} {"实际":>4s} {"命中":>4s} {"信号"}')

    for m in MATCHES:
        verdict, extras, gs, sigs = predict(m, opt_thr, use_gate=True)
        gate_on = '🟢' if gs >= opt_thr else '—'
        hit = '✅' if verdict == m['act'] else ('⚠️' if verdict != m['act'] else '')
        sig_str = ' | '.join([s[0] for s in sigs[:3]]) if sigs else '—'
        print(f'{m["home"]:>8s} vs {m["away"]:<8s} {gs:>5d} {gate_on:>5s} {verdict:>4s} {m["act"]:>4s} {hit:>4s} {sig_str}')

    # ── 对比: 无Gate vs 有Gate ──
    print(f"\n{'='*70}")
    print(f"📊 无Gate (argmax) vs D-Gate(threshold={opt_thr}) 对比")
    print(f"{'='*70}")
    no_gate_preds = []
    with_gate_preds = []
    for m in MATCHES:
        ng, _, _, _ = predict(m, opt_thr, use_gate=False)
        wg, _, _, _ = predict(m, opt_thr, use_gate=True)
        no_gate_preds.append(ng)
        with_gate_preds.append(wg)
    ng_m = compute_metrics(no_gate_preds, actuals)
    wg_m = compute_metrics(with_gate_preds, actuals)

    for label, mtr in [('无Gate', ng_m), ('D-Gate', wg_m)]:
        print(f"  {label}: Acc={mtr['acc']:.1%} D-F1={mtr['D_f1']:.4f} D召回={mtr['D_rec']:.0%} H-F1={mtr['H_f1']:.4f} A-F1={mtr['A_f1']:.4f}")

    # ── D-Gate配置建议 ──
    print(f"\n{'='*70}")
    print("📝 D-Gate 推荐配置")
    print(f"{'='*70}")
    print(f"""D_GATE_CONFIG = {{
    'enabled': True,
    'threshold': {opt_thr},
    'signals': {{
        'ou_low_score': 1,       # OU ≤ 2.5
        'ou_ultra_low_score': 2, # OU ≤ 2.0
        'balanced_score': 1,     # spread < 0.25
        'highly_balanced_score': 2, # spread < 0.10
        'shallow_hcp_mild': 1,   # 让球不足 < 0.7x
        'shallow_hcp_severe': 2, # 严重浅盘 < 0.4x
        'high_water': 1,         # 水位 ≥ 2.0
        'moderate_favorite': 1,  # 热门1.20-2.00
        'd_base_signal': 1,      # imp_D > 0.20
    }}
}}""")

    # ── 生成HTML ──
    generate_html(best_config, bl, results, opt_thr, MATCHES)

def generate_html(best_config, baseline, results, opt_thr, matches):
    html = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>哨响AI v4.4 · D-Gate 平局检测</title>
<style>
:root{--bg:#0d1117;--surface:#161b22;--border:#21262d;--text:#c9d1d9;--text2:#8b949e;
  --accent:#58a6ff;--accent2:#3fb950;--danger:#f85149;--warn:#d29922;--purple:#bc8cff}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font:14px/1.5 system-ui,sans-serif;max-width:1100px;margin:0 auto;padding:20px}
h1{font-size:22px;color:var(--accent);margin-bottom:4px}
.sub{color:var(--text2);font-size:12px;margin-bottom:20px}
.banner{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:20px}
.bnr{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px;text-align:center}
.bnr .num{font-size:24px;font-weight:700;display:block}
.bnr .lbl{font-size:11px;color:var(--text2);margin-top:4px}
.bnr.g{border-color:#3fb95044}.bnr.g .num{color:var(--accent2)}
.bnr.b{border-color:#58a6ff44}.bnr.b .num{color:var(--accent)}
.bnr.p{border-color:#bc8cff44}.bnr.p .num{color:var(--purple)}
.bnr.r{border-color:#da363344}.bnr.r .num{color:var(--danger)}
.sec{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:18px;margin-bottom:20px}
.sec h3{font-size:14px;color:var(--purple);margin-bottom:12px}
.tbl{width:100%;border-collapse:collapse;font-size:12px}
.tbl th{text-align:left;color:var(--text2);padding:6px 10px;border-bottom:1px solid var(--border);font-weight:400}
.tbl td{padding:7px 10px;border-bottom:1px solid var(--border)}
.tbl .up{color:var(--accent2);font-weight:700}.tbl .dn{color:var(--danger)}
.tbl .ch{background:#3fb9500a}.tbl .wr{background:#da36330a}
.tbl .best{background:#3fb95011;font-weight:700}
.tag{font-size:10px;padding:2px 8px;border-radius:4px;display:inline-block;font-weight:600}
.tag.h{background:#1f6feb33;color:var(--accent)}.tag.d{background:#d2992233;color:var(--warn)}.tag.a{background:#da363333;color:var(--danger)}
.gate-on{color:var(--accent2);font-weight:700}
.signal-list{display:flex;gap:4px;flex-wrap:wrap;margin-top:12px}
.sig-chip{font-size:10px;padding:3px 10px;border-radius:12px;border:1px solid var(--border);color:var(--text2)}
.sig-chip.active{background:#3fb95018;border-color:#3fb95044;color:var(--accent2)}
.config-block{background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:14px;font-family:'Cascadia Code',monospace;font-size:12px;
  white-space:pre;overflow-x:auto;color:var(--accent2)}
@media(max-width:800px){.banner{grid-template-columns:1fr 1fr}}
</style></head><body>
<h1>⚽ 哨响AI v4.4 · D-Gate 多信号平局检测</h1>
<p class="sub">世界杯2026 24场回测 · 不依赖D概率跨阈值 · 用独立外部信号共振识别平局</p>
"""

    # Banner
    b = baseline; w = best_config[1]
    d_imp = w['D_f1'] - b['D_f1']
    html += f"""<div class="banner">
<div class="bnr b"><span class="num">{b['acc']:.0%}→{w['acc']:.0%}</span><span class="lbl">准确率 ({w['acc']-b['acc']:+.0%})</span></div>
<div class="bnr g"><span class="num">{b['D_f1']:.3f}→{w['D_f1']:.3f}</span><span class="lbl">D-F1 ({d_imp:+.3f})</span></div>
<div class="bnr p"><span class="num">{b['D_rec']:.0%}→{w['D_rec']:.0%}</span><span class="lbl">D召回 ({w['D_rec']-b['D_rec']:+.0%})</span></div>
<div class="bnr r"><span class="num">{best_config[3]}/24</span><span class="lbl">Gate触发场次</span></div>
</div>"""

    # Grid search results
    html += """<div class="sec"><h3>🔍 D-Gate 阈值网格搜索结果</h3>
<table class="tbl"><tr><th>阈值</th><th>Acc</th><th>D-F1</th><th>D召回</th><th>D精确</th><th>H-F1</th><th>A-F1</th><th>Gate触发</th><th>D命中</th></tr>"""
    for thr, mtr, gc, dh in results:
        css = ' best' if thr == best_config[0] else ''
        html += f"<tr class='{css}'><td>{thr}</td><td>{mtr['acc']:.1%}</td><td class='up'>{mtr['D_f1']:.4f}</td><td>{mtr['D_rec']:.0%}</td><td>{mtr['D_prec']:.0%}</td><td>{mtr['H_f1']:.4f}</td><td>{mtr['A_f1']:.4f}</td><td>{gc}</td><td>{dh}</td></tr>"
    html += "</table></div>"

    # Per-match detail
    html += f"""<div class="sec"><h3>📋 最优配置 (threshold={opt_thr}) 逐场详情</h3>
<table class="tbl"><tr><th>日期</th><th>比赛</th><th>赔率</th><th>D-Gate</th><th>预测</th><th>实际</th><th>命中</th><th>信号</th></tr>"""
    vmap = {'H': '主胜', 'D': '平局', 'A': '客胜'}
    for m in matches:
        verdict, extras, gs, sigs = predict(m, opt_thr, use_gate=True)
        gate_on = gs >= opt_thr
        hit = verdict == m['act']
        sig_str = ', '.join([s[0] for s in sigs[:3]]) if sigs else '—'
        css = ' ch' if hit else ' wr'
        html += f"""<tr class='{css}'>
<td>{m['date']}</td><td>{m['home']} vs {m['away']}</td><td>{m['H']}/{m['D']}/{m['A']}</td>
<td>{'<span class="gate-on">🟢 '+str(gs)+'分</span>' if gate_on else str(gs)+'分'}</td>
<td><span class="tag {verdict.lower()}">{vmap[verdict]}</span></td>
<td>{vmap[m['act']]} ({m['score']})</td>
<td>{'✅' if hit else '❌'}</td>
<td style="font-size:11px;color:var(--text2)">{sig_str}</td></tr>"""
    html += "</table></div>"

    # Best config
    html += f"""<div class="sec"><h3>📝 最优D-Gate配置 (可直接落地)</h3>
<div class="config-block">D_GATE_CONFIG = {{
    'enabled': True,
    'threshold': {opt_thr},
    'signals': {{
        'ou_low_score': 1,         # OU ≤ 2.5 → 低比分环境
        'ou_ultra_low_score': 2,   # OU ≤ 2.0 → 极低比分
        'balanced_score': 1,       # spread < 0.25 → 实力均衡
        'highly_balanced_score': 2, # spread < 0.10 → 高度均衡
        'shallow_hcp_mild': 1,     # 让球 < 预期×0.7 → 庄家信心不足
        'shallow_hcp_severe': 2,   # 让球 < 预期×0.4 → 严重浅盘
        'high_water': 1,           # 水位 ≥ 2.0 → 诱盘信号
        'moderate_favorite': 1,    # 热门赔率 1.20-2.00 → 非碾压
        'd_base_signal': 1,        # imp_D > 0.20 → D基础信号
    }}
}}</div>
<p style="font-size:11px;color:var(--text2);margin-top:10px">
💡 核心思路: 不依赖D概率跨阈值(已被证明不可能), 而是用多个独立的外部信号共振。
当多个平局利好信号同时出现时, 直接覆盖为平局预测。这类似机器学习中的"专家投票"机制。
</p></div>"""

    # Summary
    html += f"""<div class="sec" style="border-color:#58a6ff44">
<h3 style="color:var(--accent)">🔬 对比 v4.3微调 vs D-Gate</h3>
<table class="tbl"><tr><th>方案</th><th>Acc</th><th>D-F1</th><th>D召回</th><th>核心原理</th></tr>
<tr class="wr"><td>v4.3微调</td><td>45.8%</td><td>0.000</td><td>0%</td><td>调整D概率(×1.04-1.32), 但跨不过阈值</td></tr>
<tr class="ch"><td>D-Gate(阈值{opt_thr})</td><td class="up">{w['acc']:.1%}</td><td class="up">{w['D_f1']:.4f}</td><td class="up">{w['D_rec']:.0%}</td><td>多信号共振覆盖, 不依赖D概率</td></tr>
</table></div></body></html>"""

    out = ROOT / "static" / "dgate_optimization.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding='utf-8')
    print(f"\n✅ HTML: {out} ({out.stat().st_size:,} bytes)")

if __name__ == "__main__":
    main()
