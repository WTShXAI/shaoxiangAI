#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
D-Gate v5.2.12 — 保留引擎 (工具函数见 d_gate_utils.py)
======================================================
P0-5 (2026-06-28): 工具函数已迁移至 rules/d_gate_utils.py.
本文件仅保留 dgate_v52() 核心引擎 (供 tournament_dynamics.py 使用).

合并状态: tools → d_gate_utils.py, 引擎 ← 本文件保持不动
"""

import math
from rules.d_gate_utils import (
    ALL_RESULTS, COVER_DB, STAR_PLAYERS,
    get_cover_adjustment, get_efficiency_adjustment,
    get_s7_threshold, get_similar_odds_warning,
    detect_handicap_trap, find_similar_matches,
)

# ═══════════════════════════════════════
# D-Gate v5.2.12 核心引擎
# ═══════════════════════════════════════

def dgate_v52(ph, pd, pa, oh, od, oa, hcp, ou, home='', away='', cs_other=None, tournament=False):
    """
    参数:
        ph/pd/pa: 模型调整后的概率
        oh/od/oa: 原始赔率 (od>7.0触发平赔否决)
        hcp: 让球盘口(主队让球)
        ou: 大小球线
        home/away: 球队名(用于COVER_DB查表)
        cs_other: '其它比分'赔率 (<5.0否决平局)
        tournament: 杯赛模式 — Mode C阈值降至65%, 启动客场爆冷检测
    返回:
        (verdict, mode, d_boost, signals_list)
    """
    spread = abs(ph - pa)
    max_imp = max(ph, pa)
    s1_draw_cheapness = od / math.sqrt(oh * oa)
    s7_ou_hcp_ratio = ou / max(abs(hcp), 0.25)
    
    # ── 杯赛自适应阈值 ──
    mode_c_threshold = 0.65 if tournament else 0.70
    
    signals = []; verdict = None; final_mode = 'normal'; final_d = 0.0
    single_game_leaky = False; weak_team_name = ''
    
    # ── 球队风格 ──
    cover_mult, cover_note = 1.0, ''
    if home and away:
        cover_mult, cover_note = get_cover_adjustment(home, away)
        if cover_note != '无调整': signals.append(f'球队风格:{cover_note}')
        eff_mult, eff_note = get_efficiency_adjustment(home, away)
        if eff_note != '无虚高': cover_mult *= eff_mult; signals.append(f'效率差:{eff_note}')
    
    # ── 同赔率对照 ──
    odds_warn = 'none'
    if home and away:
        odds_warn, odds_note = get_similar_odds_warning(ph, pa, hcp)
        if odds_warn not in ('none', 'clean'): signals.append(f'同类赔率:{odds_note}')
    
    # ═══════════════════════════════════
    # Layer 1: Mode C — 超热门翻车 (杯赛65%, 联赛70%)
    # ═══════════════════════════════════
    if max_imp >= mode_c_threshold:
        d_boost = pd * 1.08 * 2.2; threshold = 0.14
        strong_name = home if ph > pa else away
        weak_name = away if ph > pa else home
        
        # 屠杀惯性检测
        is_blowout_team = False
        if strong_name and strong_name in COVER_DB:
            td = COVER_DB[strong_name]
            is_blowout_team = (td.get('blowout_ratio', 0) >= 0.33 and td.get('total', 0) >= 2)
        
        # 让球陷阱检测
        trap_signal = detect_handicap_trap(strong_name, weak_name, hcp, home, away)
        if trap_signal:
            d_boost *= 1.15; threshold = 0.12; signals.append(trap_signal)
        
        # 单场样本防线检查
        is_single_game = False
        if strong_name and strong_name in COVER_DB:
            is_single_game = (COVER_DB[strong_name].get('total', 0) == 1)
        if is_single_game and not trap_signal:
            wd = COVER_DB.get(weak_name, {}) if weak_name else {}
            if wd.get('ga90', 0) >= 2.0 and wd.get('total', 0) >= 1:
                d_boost *= 0.75; threshold = 0.28
                signals.append(f'{weak_name}防线漏(ga={wd["ga90"]:.1f})->单场样本Mode C降权')
                single_game_leaky = True; weak_team_name = weak_name
        
        # 对手防守质量过滤器 (v5.2.14: 防止65%阈值误杀)
        if tournament and mode_c_threshold < 0.70:
            wd = COVER_DB.get(weak_name, {}) if weak_name else {}
            weak_ga = wd.get('ga90', 0)
            if weak_ga >= 1.0 and wd.get('total', 0) >= 1:
                # 对手防线漏 → Mode C不太可信
                # 但如果imp超高(>72%)，市场已充分定价强者，即使对手防线漏也可能翻车
                if max_imp >= 0.72:
                    d_boost *= 0.85  # 温和降权 (英格兰vs巴拿马型)
                else:
                    d_boost *= 0.55  # 强力降权 (克罗地亚vs巴拿马型)
                threshold = max(threshold, 0.28)
                signals.append(f'{weak_name}防线漏(ga={weak_ga:.1f})→杯赛Mode C降权至{threshold:.2f}')
        
        if is_blowout_team:
            d_boost *= 1.4; threshold = 0.45
            signals.append(f'{strong_name}屠杀惯性->Mode C降权')
        
        # S7动态阈值 (深盘赔率惩罚已移除 v5.2.13: 西班牙/英格兰均被误杀)
        s7_thresh = get_s7_threshold(hcp)
        d_boost *= cover_mult
        if odds_warn == 'blowout_bias': d_boost *= 0.85
        
        if d_boost > threshold and verdict is None:
            verdict, final_mode, final_d = 'D', 'C', d_boost
    
    # ═══════════════════════════════════
    # Layer 1b: Mode C-away
    # ═══════════════════════════════════
    if pa > 0.65 and max_imp < 0.70:
        d_boost = pd * 1.08 * 1.5; d_boost *= cover_mult
        if d_boost > 0.35 and verdict is None:
            verdict, final_mode, final_d = 'D', 'C-away', d_boost
    
    # ═══════════════════════════════════
    # Layer 2: Mode A — 中等热门 (48-70%)
    # ═══════════════════════════════════
    if 0.48 <= max_imp <= 0.70:
        d_boost = pd * 1.08; d_boost *= max(0.80, 1.0 - spread * 0.30)
        if ou <= 2.5: d_boost *= 1.05
        s7_thresh = get_s7_threshold(hcp)
        if s7_ou_hcp_ratio >= s7_thresh and s1_draw_cheapness < 1.35:
            d_boost *= 0.70; signals.append(f'S7={s7_ou_hcp_ratio:.1f}>={s7_thresh}惩罚')
        d_boost *= cover_mult
        if odds_warn == 'blowout_bias': d_boost *= 0.85; signals.append('同类赔率偏屠杀->降权')
        elif odds_warn == 'draw_bias': d_boost *= 1.06; signals.append('同类赔率偏平局->微调')
        if d_boost > 0.28 and verdict is None:
            verdict, final_mode, final_d = 'D', 'A', d_boost
    
    # ═══════════════════════════════════
    # Layer 3: Mode B — 均衡赛
    # ═══════════════════════════════════
    if spread < 0.15 and ou <= 2.75 and 3.0 <= od <= 4.5:
        d_boost = pd * 1.08 * 1.20
        if home and away:
            h_style = COVER_DB.get(home, {}).get('style', '')
            a_style = COVER_DB.get(away, {}).get('style', '')
            if h_style == '沉闷型' and a_style == '沉闷型': d_boost *= 1.02
            if h_style == '互捅型' or a_style == '互捅型': d_boost *= 0.88
        if d_boost > 0.44 and verdict is None:
            verdict, final_mode, final_d = 'D', 'B', d_boost
    
    # ═══════════════════════════════════
    # Layer 4: Default
    # ═══════════════════════════════════
    d_boost = pd * 1.08
    if spread > 0.40: d_boost *= 0.70
    elif spread > 0.20: d_boost *= 0.85
    if spread < 0.25 and abs(hcp) < 1.0:
        d_boost *= 1.15; signals.append(f'窄spread={spread:.2f}+低Elo差->微提平局')
    s7_thresh = get_s7_threshold(hcp)
    if s7_ou_hcp_ratio >= s7_thresh and s1_draw_cheapness < 1.35:
        d_boost *= 0.70; signals.append(f'S7={s7_ou_hcp_ratio:.1f}>={s7_thresh}惩罚')
    d_boost *= cover_mult
    if d_boost > 0.32 and verdict is None:
        verdict, final_mode, final_d = 'D', 'default', d_boost
    
    if verdict is None: verdict = 'H' if ph > pa else 'A'
    
    # ═══════════════════════════════════
    # Layer 5: 客场爆冷检测 (v5.2.14, 杯赛模式)
    # ═══════════════════════════════════
    # 场景: 主队热门(ph>60%)但平赔偏低、客胜赔率暗示爆冷空间
    # 条件: ph>0.60 + od<5.0 + oa<8.0 + hcp<=0.75(浅让盘)
    # 信号: 主队让浅盘但赔率结构不稳定 → 客场爆冷可能
    if tournament and verdict == 'H' and ph > 0.60:
        # 浅让盘: 主队热门却只让不到0.75球
        shallow_hcp = abs(hcp) <= 0.75 if hcp < 0 else hcp <= 0.75
        draw_suspicious = od < 5.0  # 平赔不高 → 市场怀疑平局
        away_alive = oa < 8.0       # 客胜不太离谱 → 市场留有空间
        
        if shallow_hcp and draw_suspicious and away_alive:
            # 这是典型的"强队不稳"信号
            signals.append(f'杯赛客场爆冷信号: 浅盘({hcp:+.2f})+平赔{od:.1f}+客胜{oa:.1f}')
            # 不直接翻转判型, 但降级为平局倾向
            if pd * 1.5 > pa and pd * 1.5 > ph:
                verdict = 'D'; final_mode = 'upset_detector'
                signals.append('客场爆冷检测→倾向平局')
    
    # ═══════════════════════════════════
    # 安全阀: 平赔否决 (45万场校准)
    # ═══════════════════════════════════
    if verdict == 'D' and od > 7.0 and single_game_leaky:
        verdict = 'H' if ph > pa else 'A'
        final_mode = f'{final_mode}(平赔否决)'
        signals.append(f'平赔{od:.1f}>7.0+{weak_team_name}防线漏->45万场平局率仅8%否决')
    
    # ═══════════════════════════════════
    # 安全阀: cs_other否决
    # ═══════════════════════════════════
    if cs_other is not None and cs_other < 5.0 and verdict == 'D':
        verdict = 'H' if ph > pa else 'A'
        final_mode = f'{final_mode}(cs否决)'
        signals.append(f'cs={cs_other:.1f}<5.0->否决平局')
    
    return verdict, final_mode, final_d, signals

# 注: STAR_PLAYERS/get_star_adjustment/print_team_styles 见 d_gate_utils.py
