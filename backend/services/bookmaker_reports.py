"""
庄家操盘报告生成 — 从 backend/main.py 拆分 (2026-06-28)
====================================================
原 backend/main.py L206-735:
  - _build_bookmaker_report()   — 庄家四段式操盘推演文本报告
  - _build_bookmaker_card()     — 结构化庄家操盘数据 (前端卡片)
  - _build_analysis_card()      — 分析卡片 (1X2+大小球+信号)

拆分记录: 2026-06-28 God File 拆分 第1块
"""
import logging

logger = logging.getLogger(__name__)

# D-Gate 引擎 (延迟导入, 避免顶层循环依赖)
_apply_dgate = None

def _get_dgate():
    global _apply_dgate
    if _apply_dgate is None:
        from rules.d_gate_engine import apply_dgate as _apply_dgate
    return _apply_dgate

# ═══════════════════════════════════════════
# _build_bookmaker_report — 庄家四段式操盘推演
# ═══════════════════════════════════════════

def build_bookmaker_report(home: str, away: str, odds: dict,
                           engine_h: float, engine_d: float, engine_a: float) -> str:
    """基于赔率+引擎概率, 生成庄家四段式操盘推演报告"""
    oh, od, oa = odds.get('home', 2.0), odds.get('draw', 3.2), odds.get('away', 3.5)
    inv_sum = 1 / oh + 1 / od + 1 / oa
    imp_h = (1 / oh) / inv_sum
    imp_d = (1 / od) / inv_sum
    imp_a = (1 / oa) / inv_sum
    overround = inv_sum - 1

    def _bias(eng, imp):
        gap = imp - eng
        if gap > 0.08: return '**市场严重高估**'
        if gap > 0.03: return '市场高估'
        if gap < -0.08: return '**市场严重低估**'
        if gap < -0.03: return '市场低估'
        return '基本一致'

    lines = [
        f'# 🏦 庄家操盘视角: {home} vs {away}',
        '',
        '## 🎯 模块1: 真实概率判断',
        '| 赛果 | 庄家真实判断 | 市场隐含概率 | 偏差 |',
        '|------|:---:|:---:|------|',
        f'| {home}胜 | **{engine_h:.0%}** | {imp_h:.1%} | {_bias(engine_h, imp_h)} |',
        f'| 平局 | **{engine_d:.0%}** | {imp_d:.1%} | {_bias(engine_d, imp_d)} |',
        f'| {away}胜 | **{engine_a:.0%}** | {imp_a:.1%} | {_bias(engine_a, imp_a)} |',
        f'| **抽水率** | | **{overround:.1%}** | |',
        '',
    ]

    # 模块2: 三套赔率方案
    margin_a = 0.048
    a_h = round(1 / (engine_h * (1 + margin_a)), 2)
    a_d = round(1 / (engine_d * (1 + margin_a)), 2)
    a_a = round(1 / (engine_a * (1 + margin_a)), 2)
    margin_b = 0.087
    b_h = round(1 / (engine_h * 1.15 * (1 + margin_b)), 2)
    b_d = round(1 / (engine_d * 0.80 * (1 + margin_b)), 2)
    b_a = round(1 / (engine_a * 0.85 * (1 + margin_b)), 2)
    margin_c = 0.065
    c_h = round(1 / (engine_h * 1.30 * (1 + margin_c)), 2)
    c_d = round(1 / (engine_d * 0.55 * (1 + margin_c)), 2)
    c_a = round(1 / (engine_a * 0.70 * (1 + margin_c)), 2)

    lines += [
        '## 📊 模块2: 三套赔率操盘方案', '',
        '### 方案A: 保守平衡 (✅ 推荐)',
        '| 选项 | 赔率 | 隐含概率 | 设计意图 |',
        '|------|:---:|:---:|------|',
        f'| {home} | **{a_h}** | {1 / a_h:.1%} | 小幅抬高赔率吸引散户, 利用市场热度制造盈利垫 |',
        f'| 平局 | **{a_d}** | {1 / a_d:.1%} | 贴近真实概率, 压低平局赔付压力 |',
        f'| {away} | **{a_a}** | {1 / a_a:.1%} | 承接少量专业玩家, 对冲主胜赔付 |',
        f'| **抽水率** | | **{margin_a:.1%}** | |', '',
        f'**心理诱导**: {home}{a_h}高于市场{oh}, 散户觉得性价比高大量买入; 平局{a_d}偏低抑制投注; {away}{a_a}保留冷门空间。', '',
        '### 方案B: 激进收割',
        '| 选项 | 赔率 | 隐含概率 | 设计意图 |',
        '|------|:---:|:---:|------|',
        f'| {home} | **{b_h}** | {1 / b_h:.1%} | 极度压低赔率制造强队假象, 吸引大量散户 |',
        f'| 平局 | **{b_d}** | {1 / b_d:.1%} | 压缩赔率弱化平局价值, 让市场忽略平局 |',
        f'| {away} | **{b_a}** | {1 / b_a:.1%} | 分流极小部分冷资金 |',
        f'| **抽水率** | | **{margin_b:.1%}** | |', '',
        f'**心理诱导**: 平局{b_d}视觉回报低, 玩家认定{home}稳赢; 一旦平局/{away}取胜, 平台通吃全部主胜投注。', '',
        '### 方案C: 激进诱平 (⚠️ 高风险)',
        '| 选项 | 赔率 | 隐含概率 | 设计意图 |',
        '|------|:---:|:---:|------|',
        f'| {home} | **{c_h}** | {1 / c_h:.1%} | 压低主胜赔率弱化吸引力 |',
        f'| 平局 | **{c_d}** | {1 / c_d:.1%} | 大幅抬高平局赔率, 引诱博弈型玩家重仓 |',
        f'| {away} | **{c_a}** | {1 / c_a:.1%} | 同步抬高客胜, 边缘化客胜投注 |',
        f'| **抽水率** | | **{margin_c:.1%}** | |', '',
        f'**心理诱导**: 平局{c_d}超高回报吸引追求高赔玩家集中买入; {home}真实胜率{engine_h:.0%}, 一旦打出主胜, 平台吃掉全部平局投注获得巨额盈余。', '',
    ]

    lines += [
        '## 🧠 模块3: 我会怎么选？', '',
        '**选择方案A**。理由:', '',
        '| 维度 | 方案A | 方案B | 方案C |',
        '|------|:---:|:---:|:---:|',
        '| 长期稳定盈利 | ✅ | ✅ | ❌ 平局打出亏损大 |',
        '| 不被市场识破 | ✅ | ⚠️ 平局赔率过低 | ❌ 超高平局=标准诱盘 |',
        '| 资金分配可控 | ✅ 三方均衡 | ⚠️ 倾斜主胜 | ⚠️ 集中平局 |',
        '| 任何赛果都赚 | ✅ | ✅ | ❌ 平局会大额亏损 |', '',
        '## 🔑 模块4: 核心操盘底层逻辑', '',
        f'1. **{home}{a_h}**: 高于市面{oh}主流赔率, 利用价格优势从其他平台抢夺主胜流量;',
        f'2. **平局{a_d}**: 隐含概率{1 / a_d:.1%}接近引擎{engine_d:.0%}真实判断, 严格控制平局赔付上限;',
        f'3. **{away}{a_a}**: 保留客胜博弈空间, 少量承接冷单对冲主胜巨额投注, 平衡整体赔付结构。', '',
        f'最终盈利来源: 固定{margin_a:.1%}抽水 + 市场与庄家真实概率的不对称盈余。',
        f'本窗口对外观感赔率更优, 长期风险更低, 三类赛果均留存稳定盈利空间。', '',
        '---',
        '> ⚠️ 以上为庄家操盘视角推演, 不构成投注建议。仅供理解博彩市场运作机制。',
    ]
    return '\n'.join(lines)

# ═══════════════════════════════════════════
# _build_bookmaker_card — 结构化庄家操盘数据
# ═══════════════════════════════════════════

def build_bookmaker_card(home: str, away: str, odds: dict,
                         engine_h: float, engine_d: float, engine_a: float,
                         d_gate_mode: str = "", ou_line: float = None,
                         handicap: float = None) -> dict:
    """构建结构化庄家操盘数据 (用于前端网格卡片渲染)"""
    oh, od, oa = odds.get('home', 2.0), odds.get('draw', 3.2), odds.get('away', 3.5)
    inv_sum = 1 / oh + 1 / od + 1 / oa
    imp_h = (1 / oh) / inv_sum
    imp_d = (1 / od) / inv_sum
    imp_a = (1 / oa) / inv_sum
    overround = inv_sum - 1
    spread = abs(imp_h - imp_a)

    def _bias(eng, imp):
        gap = imp - eng
        if gap > 0.08: return ('严重高估', 'danger')
        if gap > 0.03: return ('市场高估', 'warn')
        if gap < -0.08: return ('严重低估', 'safe')
        if gap < -0.03: return ('市场低估', 'safe')
        return ('一致', '')

    is_shallow_hcap = handicap is not None and abs(handicap) <= 0.5
    dgate_active = bool(d_gate_mode)
    dgate_draw_risk = d_gate_mode in ('A', 'C')
    high_draw_risk = is_shallow_hcap or dgate_draw_risk
    max_home_cut = 0.05 if (is_shallow_hcap or dgate_active) else 0.25

    margin_a = 0.048
    a_h, a_d, a_a = (round(1/(engine_h*(1+margin_a)),2), round(1/(engine_d*(1+margin_a)),2), round(1/(engine_a*(1+margin_a)),2))
    if high_draw_risk and a_h < oh * (1 - max_home_cut):
        a_h = round(oh * (1 - max_home_cut), 2)
        a_d = round(1/(engine_d * (1 + margin_a)), 2)
        a_a = round(1/(engine_a * (1 + margin_a)), 2)
    margin_b = 0.087
    b_h, b_d, b_a = (round(1/(engine_h*1.15*(1+margin_b)),2), round(1/(engine_d*0.80*(1+margin_b)),2), round(1/(engine_a*0.85*(1+margin_b)),2))
    margin_c = 0.065
    c_h, c_d, c_a = (round(1/(engine_h*1.30*(1+margin_c)),2), round(1/(engine_d*0.55*(1+margin_c)),2), round(1/(engine_a*0.70*(1+margin_c)),2))

    is_hot_fav = imp_h > 0.50
    is_super_fav = imp_h > 0.60
    is_balanced = abs(imp_h - imp_a) < 0.20
    cold_harvest = is_super_fav and dgate_draw_risk

    scheme_c_risk = 'high'
    if dgate_draw_risk: scheme_c_risk = 'extreme'
    elif is_balanced: scheme_c_risk = 'high'

    scheme_b_risk = 'medium'
    if is_shallow_hcap: scheme_b_risk = 'extreme'
    elif cold_harvest: scheme_b_risk = 'low'
    elif is_balanced and dgate_active: scheme_b_risk = 'high'

    matrix_headers = ['长期盈利', '不被识破', '资金可控', '全赛果赚']
    if dgate_active: matrix_headers.append('平局覆盖')
    if cold_harvest: matrix_headers.append('收割效率')
    if is_shallow_hcap: matrix_headers.append('浅让安全')

    if is_shallow_hcap:
        matrix_rows = [
            {'name':'方案A','values':['✅','✅ 贴市','✅ 均衡','✅'],'cls':'rec'},
            {'name':'方案B','values':['❌ 爆亏','❌ 浅让收割','❌ 倾斜','❌'],'cls':'danger'},
            {'name':'方案C','values':['❌ 平局亏','❌ 标准诱盘','❌ 集中','❌'],'cls':'danger'},
        ]
        if dgate_active:
            matrix_rows[0]['values'].append('✅ 覆盖')
            matrix_rows[1]['values'].append('❌ 禁忌')
            matrix_rows[2]['values'].append('❌ 致命')
        if is_shallow_hcap:
            matrix_rows[0]['values'].append('✅ 安全')
            matrix_rows[1]['values'].append('❌ 爆亏')
            matrix_rows[2]['values'].append('❌ 暴亏')
    elif cold_harvest:
        matrix_rows = [
            {'name':'方案A', 'values':['✅','✅','✅ 均衡','✅'], 'cls':'warn'},
            {'name':'方案B', 'values':['✅ 收割','✅ 热队掩护','✅ 倾斜','✅ 翻车通吃'], 'cls':'rec'},
            {'name':'方案C', 'values':['❌ 平局亏','❌ 标准诱盘','⚠️ 集中','❌'], 'cls':'danger'},
        ]
        if dgate_active:
            matrix_rows[0]['values'].append('⚠️ 被动')
            matrix_rows[1]['values'].append('✅ 主动收割')
            matrix_rows[2]['values'].append('❌ 致命')
        if cold_harvest:
            matrix_rows[0]['values'].append('⚠️ 保守')
            matrix_rows[1]['values'].append('✅ 高效')
            matrix_rows[2]['values'].append('❌ 暴亏')
    else:
        matrix_rows = [
            {'name':'方案A', 'values':['✅','✅','✅ 均衡','✅'], 'cls':'rec'},
            {'name':'方案B', 'values':['✅','⚠️ 偏低','⚠️ 倾斜','✅'], 'cls':'warn'},
            {'name':'方案C', 'values':['❌ 平局亏','❌ 标准诱盘','⚠️ 集中','❌'], 'cls':'danger'},
        ]
        if dgate_active:
            matrix_rows[0]['values'].append('✅ 保持')
            matrix_rows[1]['values'].append('⚠️ 风险')
            matrix_rows[2]['values'].append('❌ 致命')

    if is_shallow_hcap:
        choice = '方案A(约束版)'
        reasons = [f'浅让盘({handicap:+.1f})强制标记平局高危: 主胜定价不得低于{oh}×0.95={oh*0.95:.2f}',
                    f'方案A: {home}{a_h}贴近市场{oh}, 避免人为制造热度集中',
                    f'方案B/C已自动禁用: 激进压低主胜/诱平在浅让盘下必然爆亏']
    elif cold_harvest:
        choice = '方案B'
        reasons = [f'大热门场次(imp_H={imp_h:.0%}) + D-Gate翻车风险 → 收割窗口已打开',
                    f'方案B: 压低{home}赔率至{b_h}锁死散户资金, 一旦翻车庄家通吃全部主胜投注',
                    f'方案A过于保守: {margin_a:.1%}固定抽水 vs {margin_b:.1%}收割盈余, 差额{margin_b-margin_a:.1%}']
    else:
        choice = '方案A'
        reasons = [f'长期稳定盈利, 任何赛果均留存{margin_a:.1%}抽水+概率不对称盈余']
    if dgate_active:
        mode_label = {'A':'中热门翻车风险','B':'均衡赛平局风险','C':'超热门翻车风险'}.get(d_gate_mode,'平局风险')
        reasons.append(f'D-Gate[{mode_label}]确认翻车可能: {home}赔率{b_h}锁仓, 翻车收益最大化' if cold_harvest else f'D-Gate[{mode_label}]已激活: 方案C诱平在平局高发场景下极其危险')
    if is_hot_fav and not cold_harvest:
        reasons.append(f'中热门场次(spread={spread:.3f}): 方案A的均衡性保证庄家稳定抽水')
    if ou_line is not None and ou_line <= 2.5 and not cold_harvest:
        reasons.append(f'低OU环境(≤{ou_line}): 利好方案A控制赔付上限')
    choice_reason = '; '.join(reasons)

    scheme_a_risk = 'low'
    scheme_a_rec = not cold_harvest
    scheme_b_rec = cold_harvest and not is_shallow_hcap
    scheme_b_disabled = is_shallow_hcap
    scheme_c_disabled = is_shallow_hcap or dgate_draw_risk

    b_psych = f'平局{b_d}视觉回报低，玩家认定{home}稳赢'
    b_home_intent = '极度压低制造假象'
    if cold_harvest:
        b_home_intent = f'压低至{b_h}锁死散户资金'
        b_psych = f'{home}{b_h}赔率极低诱使散户重仓"稳赢"幻觉; 一旦翻车, 全部主胜投注被庄家通吃'

    schemes = [
        {'id':'A','name':'保守平衡','icon':'🛡️','rec':scheme_a_rec,
         'odds':{'home':a_h,'draw':a_d,'away':a_a},'margin':f'{margin_a:.1%}',
         'home_intent':'小幅抬高吸引散户','draw_intent':'贴近真实压低赔付','away_intent':'承接冷单对冲',
         'psych':f'{home}{a_h}高于市场{oh}，散户觉得性价比高大量买入',
         'risk':scheme_a_risk},
        {'id':'B','name':'激进收割','icon':'⚡','rec':scheme_b_rec,'disabled':scheme_b_disabled,
         'odds':{'home':b_h,'draw':b_d,'away':b_a},'margin':f'{margin_b:.1%}',
         'home_intent':b_home_intent,'draw_intent':'压缩赔率弱化平局','away_intent':'分流极少冷资金',
         'psych':b_psych,'risk':scheme_b_risk},
        {'id':'C','name':'激进诱平','icon':'⚠️','rec':False,'disabled':scheme_c_disabled,
         'odds':{'home':c_h,'draw':c_d,'away':c_a},'margin':f'{margin_c:.1%}',
         'home_intent':'压低主胜弱化吸引','draw_intent':'大幅抬高引诱重仓','away_intent':'边缘化客胜投注',
         'psych':f'平局{c_d}超高回报吸引博弈型玩家集中买入' if not scheme_c_disabled else(f'🚫 已禁用'),
         'risk':scheme_c_risk},
    ]

    if is_shallow_hcap:
        logic_text = (f'🛡️ **浅让盘防守模式**: {home}让球{handicap:+.1f}属浅让, 平局概率显著偏高。'
                      f'方案A主胜定价{a_h}贴市{oh}(下调≤5%), 避免人为制造{home}热度集中。'
                      f'平局{a_d}贴近市场{od}控制赔付; {away}{a_a}承接分流。'
                      f'方案B/C已自动禁用 — 浅让盘激进操盘=爆亏。')
    elif cold_harvest:
        logic_text = (f'🔥 **收割逻辑激活**: {home}隐含胜率{imp_h:.0%}, D-Gate确认翻车风险。'
                      f'方案B压低{home}赔率至{b_h}制造"稳赢"幻觉锁死散户资金; '
                      f'一旦打出平局或{away}胜, 庄家通吃全部{b_h}赔率仓位。'
                      f'收割效率远高于方案A的{margin_a:.1%}固定抽水。')
    else:
        logic_text = (f'{home}{a_h}利用价格优势抢夺主胜流量; 平局{a_d}严格控制赔付上限; {away}{a_a}保留博弈空间对冲主胜投注。')
        if dgate_active:
            mode_label = {'A':'中热门翻车','B':'均衡赛平局','C':'超热门翻车'}.get(d_gate_mode,'平局')
            logic_text += f' ⚠️ D-Gate[{mode_label}]激活: 此场平局风险高于常规, 方案A的保守设计恰好规避该风险。'
        logic_text += f'最终盈利=固定{margin_a:.1%}抽水+信息不对称盈余。'

    return {
        'module1': {'title':'真实概率判断','overround':f'{overround:.1%}',
                    'rows':[{'outcome':f'{home}胜','engine':f'{engine_h:.0%}','implied':f'{imp_h:.1%}','bias':_bias(engine_h,imp_h)[0],'tag':_bias(engine_h,imp_h)[1]},
                            {'outcome':'平局','engine':f'{engine_d:.0%}','implied':f'{imp_d:.1%}','bias':_bias(engine_d,imp_d)[0],'tag':_bias(engine_d,imp_d)[1]},
                            {'outcome':f'{away}胜','engine':f'{engine_a:.0%}','implied':f'{imp_a:.1%}','bias':_bias(engine_a,imp_a)[0],'tag':_bias(engine_a,imp_a)[1]}]},
        'module2':{'title':'三套赔率操盘方案','schemes':schemes},
        'module3':{'title':'决策矩阵','choice':choice,'choice_reason':choice_reason,'dgate_context':d_gate_mode if dgate_active else'',
                   'handicap_context':f'shallow_{handicap}' if is_shallow_hcap else '','matrix':{'headers':matrix_headers,'rows':matrix_rows}},
        'module4':{'title':'核心操盘逻辑','text':logic_text}
    }

# ═══════════════════════════════════════════
# _build_analysis_card — 分析卡片
# ═══════════════════════════════════════════

def build_analysis_card(home: str, away: str, odds: dict,
                        h_prob: float, d_prob: float, a_prob: float,
                        handicap: float = None, ou_line: float = None,
                        water_level: float = None,
                        fifa_rank_diff: int = None, group_round: int = None,
                        match_type: str = "tournament") -> dict:
    """构建分析卡片 (1X2赔率+大小球+庄家信号)"""
    oh, od, oa = odds.get('home', 2), odds.get('draw', 3.2), odds.get('away', 3.5)
    inv_sum = 1/oh + 1/od + 1/oa
    imp_h, imp_d, imp_a = (1/oh)/inv_sum, (1/od)/inv_sum, (1/oa)/inv_sum
    spread = abs(imp_h - imp_a)

    signals = []
    risk = 'low'

    ou_signal = None
    if ou_line is not None:
        if ou_line <= 2.0:
            ou_signal = {'type': 'draw', 'text': f'大小球极低 {ou_line:.1f}', 'detail': '极度低比分环境，利好平局'}
            signals.append('D-Boost')
        elif ou_line <= 2.5:
            ou_signal = {'type': 'draw', 'text': f'大小球偏低 {ou_line:.1f}', 'detail': '低比分环境'}

    water_signal = None
    if water_level is not None and water_level >= 2.0:
        water_signal = {'type': 'warn', 'text': f'水位偏高 {water_level:.2f}', 'detail': '庄家引诱下注嫌疑'}
        if risk == 'low': risk = 'medium'

    if oh < 1.60 and ou_signal and ou_signal['type'] == 'draw':
        signals.append('翻车风险')
        risk = 'high'

    WC_D_RATE = 0.268
    DEFAULT_D = 0.257
    d_boosted = imp_d * (WC_D_RATE / DEFAULT_D)
    if spread > 0.50: d_boosted *= 0.60
    elif 0.03 <= spread < 0.08: d_boosted *= 1.15
    else: d_boosted *= 1.08

    bm_skep = 0
    if ou_line and ou_line <= 2.0: bm_skep += 0.15
    elif ou_line and ou_line <= 2.5: bm_skep += 0.09
    if water_signal: bm_skep += 0.07
    if spread < 0.25 and ou_line and ou_line <= 2.5: bm_skep += 0.12

    if bm_skep > 0.15:
        d_boosted *= (1 + bm_skep * 0.5)
        h_adj = imp_h * (1 - bm_skep * 0.4)
        a_adj = imp_a * (1 - bm_skep * 0.4)
    else:
        h_adj, a_adj = imp_h, imp_a

    # D-Gate 统一引擎
    dg = _get_dgate()(imp_h, imp_d, imp_a, odds,
                      handicap=handicap, ou_line=ou_line, water_level=water_level,
                      fifa_rank_diff=fifa_rank_diff, group_round=group_round,
                      match_type=match_type,
                      h_adj=h_adj, a_adj=a_adj, d_boosted=d_boosted)
    verdict = dg['verdict']
    d_boosted = dg['d_boosted']

    analysis_points = []
    if oh < 1.30:
        analysis_points.append({'tag':'强队','text':f'{home}赔率{oh}，庄家极度看好。实力碾压型比赛，但穿盘不易。','color':'safe'})
    elif oh < 2.0:
        analysis_points.append({'tag':'热门','text':f'{home}赔率{oh}，热门但不稳。世界杯小组赛此类赔率翻车率超30%。','color':'warn'})
    if spread < 0.16 and ou_line and ou_line <= 2.5:
        analysis_points.append({'tag':'平局候选','text':f'均衡对战(spread={spread:.2f})+低比分环境(OU{ou_line})，经典平局候选。','color':'draw'})
    if ou_signal: analysis_points.append({'tag':'大小球','text':ou_signal['detail'],'color':'draw'})
    if water_signal: analysis_points.append({'tag':'水位','text':water_signal['detail'],'color':'warn'})
    if signals: analysis_points.append({'tag':'信号','text':' | '.join(signals),'color':'warn' if '翻车风险' in signals else 'info'})

    motives = []
    if oh < 1.60 and ou_line and ou_line <= 2.5:
        motives.append(f'{home}赔率{oh}表面强势，但庄家把大小球压低到{ou_line}，暴露了真实判断。赔率给散户信心，大小球线给庄家自己留后路——万一{home}陷入苦战打铁，低进球预期确保庄家在大小球方向有利润。"赔率看好、进球数不看好"的矛盾信号值得警惕。')
    if ou_line and ou_line <= 2.5 and spread < 0.25:
        motives.append(f'大小球仅开{ou_line}，是庄家对沉闷比赛的直接预判。两队实力均衡(spread={spread:.1%})，庄家将进球预期压到{ou_line}，暗示这将是一场低比分的消耗战——而低比分是平局的温床。如果庄家真的看好一方获胜，进球线不会这么低。')
    if water_level and water_level >= 2.0:
        motives.append(f'水位定在{water_level}是一个微妙的信号。高水位意味着庄家需要更多投注来平衡风险，或者庄家本身对热门方缺乏信心。正常情况下信心充足的盘口水位应在1.85-1.95区间。{water_level}的水位=庄家在说"快来买这个方向"——通常不是好事。')
    if spread < 0.16 and 3.0 <= od <= 4.5 and ou_line and ou_line <= 2.5:
        motives.append(f'⚖️ 均衡赛平局信号: spread={spread:.2f}（高度接近），主赔{oh} vs 客赔{oa}差距极小，庄家无法明确看好任何一方。平赔{od}处于中位区间[3.0,4.5]，说明庄家对平局结果有合理预期。在世界杯/大赛小组赛阶段，此类"势均力敌"的比赛平局率显著高于普通赛事——因为双方都倾向于"保平争胜"的保守策略，尤其是首轮比赛。')
    if not motives:
        if oh < 2.5:
            motives.append(f'庄家对这场比赛的赔率结构较为标准，{home}赔率{oh}、平赔{od}、客赔{oa}，抽水率约{(1/oh+1/od+1/oa-1)*100:.1f}%。赔率没有明显异常信号，庄家主要依靠自然的市场投注分布来维持账面平衡。')
        else:
            motives.append(f'这场比赛的赔率结构较为均衡，三线赔率差距不大，庄家没有明显倾向性，主要通过精细的赔率微调来平衡各方投注。此类比赛庄家利润最薄，但也最安全——任何结果都不会造成巨大损失。')

    verdict_map = {'H':'主胜','D':'平局','A':'客胜'}
    return {
        'verdict':verdict,'verdict_cn':verdict_map.get(verdict,'?'),'risk':risk,'signals':signals,
        'probs':{'home':round(imp_h,3),'draw':round(imp_d,3),'away':round(imp_a,3),'d_boosted':round(d_boosted,3),'h_adj':round(h_adj,3),'a_adj':round(a_adj,3)},
        'odds':{'home':oh,'draw':od,'away':oa},'ou_line':ou_line,'water_level':water_level,
        'ou_signal':ou_signal,'water_signal':water_signal,
        'analysis':analysis_points,'skepticism':round(bm_skep,2),'spread':round(spread,3),
        'd_gate_active':dg['d_gate_active'],'motives':motives,
    }
