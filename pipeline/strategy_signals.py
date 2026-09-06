"""
策略方向信号 (Strategy Direction Signals)
========================================
三方向信号 — 2026-07-21 起按运营指令**解除联赛过滤**: 原仅 obscure 低级别联赛层触发, 现全联赛(含 main/cup)统一触发。全部为面板提示级信号: 不改 verdict / 不自动下注。
⚠ 但校准源自低流动性市场(obscure 层), 对 main/cup 精英联赛该偏差未必成立 -> 信号仍全联赛输出, 但精英域置信自动降级(`_conf_for_tier`: high->low)并附 `calibration` 溯源字段, 供审慎加权。

触发域 (2026-07-21 起解除过滤, 全联赛触发; tier 仅作溯源标注):
  - cup    : 杯赛/国际赛会模型域 -> 现亦触发(信号溯源标注 cup)
  - main   : 全球高流动性主流联 -> 现亦触发(信号溯源标注 main)
  - obscure: 其余低级别/青年/女子/地区/小国联 -> 触发(溯源标注 obscure)

校准来源 (GQ match_outcomes, obscure 层 N=276 有初盘1X2+终赛果):
  - 平局   : 隐含 0.250 vs 实际 0.159  -> +0.090 高估  => 做空平局
  - 客队长尾: 隐含<0.10 时 实际客胜 0.800 (N=15)       => 做多客队
  - 大小球 : 有 OU 实时赔率支撑时 Fade Over 置信升级 (medium); 仅方向性启发时为 low。2026-07-21 修复采集器 OU 解析 bug 后 GQ 已能稳定采集 OU, 后端 _live_predict 缺失时从 GQ OU 快照兜底。

⚠ 校准溯源说明: 上述校准源自**低流动性市场**。解除过滤后信号对 main/cup 精英联赛
同样输出, 但精英市场更 sharp、该偏差未必成立 —— 信号仅作面板提示, 不自动下注/不改 verdict。
每个信号附 `tier`(溯源域) + `calibration`(校准域: 'obscure-low-liquidity') 字段; main/cup 精英域置信经 `_conf_for_tier` 自动降级, 前端显示"方向性"而非"高置信"。
"""
from __future__ import annotations

# ---- 联赛 tier 分类 (tier 单一真相源) ----
# 设计: tier 仅作信号溯源/置信标注, 不再门控触发(2026-07-21 解除过滤)。
#   - main : 全球高流动性精英联 (top-5 欧洲) -> 信号仍触发, 但置信自动降级(校准未在此域验证)
#   - cup  : 杯赛/赛会 (WC/欧冠等, 用杯赛模型) -> 信号仍触发, 但置信自动降级
#   - obscure: 其余全部 (挪威超/墨超/巴乙/MLS/日职/中超/韩K/瑞典超/小国联/青年/女子/友谊...) -> 触发, 校准域直接适用
MAIN_SPORT_KEYS = {
    'soccer_epl', 'soccer_spain_la_liga', 'soccer_italy_serie_a',
    'soccer_germany_bundesliga', 'soccer_france_ligue_one',
    # 2026-08-12 反推复盘: 挪超/比甲/荷甲等有明确流动性的主流联赛纳入 main,
    # 不再误归 obscure 触发错误的低级别专属信号 + 高置信.
    'soccer_norway_eliteserien', 'soccer_belgium_first_division',
    'soccer_netherlands_eredivisie', 'soccer_portugal_primeira_liga',
    'soccer_scotland_premiership', 'soccer_austria_bundesliga',
    'soccer_switzerland_super_league', 'soccer_denmark_superliga',
    'soccer_turkey_super_lig', 'soccer_greece_super_league',
    'soccer_croatia_first_division', 'soccer_czech_first_division',
    'soccer_romania_liga_i', 'soccer_serbia_super_liga',
    'soccer_russia_premier_league', 'soccer_ukraine_premier_league',
    'soccer_poland_ekstraklasa', 'soccer_sweden_allsvenskan',
}
MAIN_LEAGUES = {
    '英格兰超级联赛','西班牙甲级联赛','意大利甲级联赛','德国甲级联赛','法国甲级联赛',
    '英超','西甲','意甲','德甲','法甲','欧冠','世界杯','欧洲杯',
    '挪威超级联赛','比利时甲级联赛','荷兰甲级联赛','葡萄牙超级联赛','苏格兰超级联赛',
    '奥地利甲级联赛','瑞士超级联赛','丹麦超级联赛','土耳其超级联赛','希腊超级联赛',
    '克罗地亚甲级联赛','捷克甲级联赛','罗马尼亚甲级联赛','塞尔维亚超级联赛',
    '俄罗斯超级联赛','乌克兰超级联赛','波兰甲级联赛','瑞典超级联赛',
}
OBSCURE_MARKERS = ['杯','友谊','U20','U23','U19','U21','U17','后备','青年','女子',
                   '地区','丙级','丁级','乙级','业余','图图','附加赛','元老','K2']

def classify_league_tier(sport_key: str = None, league: str = None) -> str:
    """返回 'main' | 'cup' | 'obscure'。
    sport_key 优先(The Odds API 英文 key); 缺省时用中文 league 名兜底。
    调用方通常已用 classify_cup() 判定杯赛, 此处双保险。"""
    sk = str(sport_key or '').lower()
    # 1) 明确精英联 -> main
    if sk in MAIN_SPORT_KEYS:
        return 'main'
    # 2) 杯赛/赛会 -> cup (双保险, 调用方多已判定)
    if sk and any(k in sk for k in ['world_cup', 'champions_league', 'europa_league',
                                     'europa_conference_league', 'conference_league',
                                     'cup', 'pokal', 'copa', 'liber']):
        return 'cup'
    if league and ('WC' in str(league).upper() or '杯' in str(league) or '杯赛' in str(league)):
        return 'cup'
    # 3) 中文名兜底: 精英联 -> main
    nm = league or ''
    if nm in MAIN_LEAGUES or any(k in nm for k in ['英超', '西甲', '意甲', '德甲', '法甲']):
        return 'main'
    # 4) 低级别标记 -> obscure
    if any(k in nm for k in OBSCURE_MARKERS):
        return 'obscure'
    # 5) 默认: 非明确精英/杯赛 -> obscure (低流动性偏差域, 与 GQ 校准一致)
    return 'obscure'


def _dew(oh, od, oa):
    s = 1.0/oh + 1.0/od + 1.0/oa
    return 1.0/oh/s, 1.0/od/s, 1.0/oa/s


# 校准溯源: 全部校准源自低流动性市场(obscure 层)。对 main/cup 精英联赛,
# 该偏差未必成立 -> 置信自动降级(high->low), 但信号仍输出(尊重"解除过滤")。
CALIB_TIER = 'obscure-low-liquidity'

def _conf_for_tier(base: str, tier: str) -> str:
    """精英联赛(main/cup)信号置信自动降级: 校准源自低流动性市场, 精英更 sharp。"""
    if tier in ('main', 'cup') and base == 'high':
        return 'low'
    return base


def compute_signals(oh=None, od=None, oa=None, ou=None, tier: str = 'obscure',
                     model_p_over: Optional[float] = None):
    """
    计算三方向信号。**全联赛触发**(2026-07-21 起解除 obscure-only 门控)。
    tier 仅用于信号溯源标注, 不再门控。

    参数:
      oh, od, oa : 初盘 1X2 赔率 (可缺)
      ou         : (over_odds, under_odds, line) 初盘大小球, 可缺
      tier       : 'obscure' | 'main' | 'cup'  (溯源标注, 不影响触发)
      model_p_over: 模型自身 P(over) (OIP Poisson), 用于与 Fade Over 市场信号
                    一致性校验; 冲突时抑制 Fade Over(以模型为准).
    返回:
      list[dict] 每个信号含 {name, direction, strength(0-1), metric, note, confidence, tier,
                            suppressed?, suppress_reason?}
    """
    signals = []

    # ---- 1X2 派生: 做空平局 + 做多客队 ----
    if oh and od and oa:
        try:
            ph, pd, pa = _dew(oh, od, oa)
        except (ZeroDivisionError, TypeError):
            ph = pd = pa = None

        if pd is not None:
            # 做空平局: obscure 层平局被高估 ~9pp, 全域做空 (无需[1.31,1.45] carve-out)
            if pd > 0.20:
                strength = min(1.0, max(0.0, (pd - 0.18) / 0.12))
                signals.append({
                    'name': '做空平局 / Fade Draw',
                    'direction': '排除平局，倾向主胜或客胜',
                    'strength': round(strength, 2),
                    'metric': f'隐含平局 {pd:.0%} vs 实际 ~16%',
                    'note': '低流动性市场平局被系统性高估约 9pp (校准源自 obscure 层)',
                    'confidence': _conf_for_tier('high', tier),
                    'calibration': CALIB_TIER,
                    'tier': tier,
                })
            # 做多客队长尾: 隐含客胜 <0.12 时实际客胜显著偏高 (N=15 实测 80%)
            if pa is not None and pa < 0.12:
                strength = min(1.0, max(0.0, (0.12 - pa) / 0.12))
                # 2026-08-12 反推复盘: note 文案随 tier 适配, 避免欧战/主流联赛显示
                # "低级别联赛 Favorite-Longshot 反转" 的误标(博多格林特vs圣吉罗斯=欧战).
                _ll_note = ('低级别联赛 Favorite-Longshot 反转：客队长尾被低估'
                            if tier == 'obscure'
                            else '客队长尾被低估 (Favorite-Longshot 反转)')
                signals.append({
                    'name': '做多客队长尾 / Back Away Long-tail',
                    'direction': '勿过度低估客胜，关注客队方向',
                    'strength': round(strength, 2),
                    'metric': f'隐含客胜 {pa:.0%} vs 长尾实际 ~80%',
                    'note': _ll_note,
                    'confidence': _conf_for_tier('high', tier),
                    'calibration': CALIB_TIER,
                    'tier': tier,
                })

    # ---- 大小球: 看小 (Fade Over) ----
    # 有 OU 实时赔率支撑 -> 置信升级为 medium (不再无条件 low)。
    # p_over 高(市场过度看好大球) -> 看小更可信。elite 层仍经 _conf_for_tier(medium 不降, 因 OU 数据跨市场可靠)。
    # 2026-08-12 反推复盘: 真实全场2-2(总球4=大球)但信号说看小 -> 加模型P(over)一致性校验,
    # 模型明确倾向大球(model_p_over>0.55)时抑制Fade Over(以模型为准), 避免与自身概率打架.
    if ou:
        ov, un, line = ou
        if ov and un:
            try:
                p_over = (1.0/ov) / (1.0/ov + 1.0/un)
            except (ZeroDivisionError, TypeError):
                p_over = None
            if p_over is not None and p_over > 0.52:
                _conflict = (model_p_over is not None and model_p_over > 0.55)
                strength = min(1.0, max(0.0, (p_over - 0.50) * 2.0))
                _sig = {
                    'name': '看小 / Fade Over',
                    'direction': '倾向 Under（小球）',
                    'strength': round(strength, 2),
                    'metric': f'隐含大球 {p_over:.0%} (线 {line})',
                    'note': '市场过度看好大球(隐含>52%)，倾向 Under；GQ OU 实时赔率支撑',
                    'confidence': _conf_for_tier('medium', tier),
                    'calibration': CALIB_TIER,
                    'tier': tier,
                }
                if _conflict:
                    _sig['suppressed'] = True
                    _sig['suppress_reason'] = (f'与模型P(over)={model_p_over:.0%}冲突→以模型为准, '
                                              f'Fade Over 抑制')
                    _sig['strength'] = 0.0
                signals.append(_sig)

    return signals


if __name__ == '__main__':
    # 自测
    print('tier 分类测试 (sport_key 优先):')
    for sk, lg in [('soccer_epl', '英超'), ('soccer_norway_eliteserien', '挪威超'),
                   ('soccer_mexico_ligamx', '墨西哥'), ('soccer_brazil_serie_b', '巴乙'),
                   ('soccer_fifa_world_cup', '世界杯'), ('soccer_usa_mls', 'MLS'),
                   (None, '英格兰超级联赛'), (None, '挪威超级联赛'),
                   (None, '巴西乙级联赛'), (None, '球会友谊赛'), (None, '韩国K1联赛')]:
        print(f'  sk={str(sk):28s} lg={str(lg):12s} -> {classify_league_tier(sk, lg)}')
    print('\n信号测试 (obscure, 高平局):')
    s = compute_signals(oh=2.10, od=3.40, oa=3.20, tier='obscure')
    for x in s: print('  ', x['name'], x['direction'], x['strength'], x['confidence'])
    print('信号测试 (obscure, 客队长尾 pa<0.10):')
    s = compute_signals(oh=1.30, od=5.00, oa=11.0, tier='obscure')
    for x in s: print('  ', x['name'], x['metric'], x['strength'])
    print('信号测试 (main -> 现全联赛触发, 精英置信降级):')
    for x in compute_signals(oh=2.10, od=3.40, oa=3.20, tier='main'):
        print('  ', x['name'], '| tier=', x['tier'], '| conf=', x['confidence'], '| calib=', x.get('calibration'))
    print('信号测试 (cup -> 现全联赛触发, 精英置信降级):')
    for x in compute_signals(oh=2.10, od=3.40, oa=3.20, tier='cup'):
        print('  ', x['name'], '| tier=', x['tier'], '| conf=', x['confidence'], '| calib=', x.get('calibration'))
    print('信号测试 (obscure -> 校准域直接适用, 高置信):')
    for x in compute_signals(oh=2.10, od=3.40, oa=3.20, tier='obscure'):
        print('  ', x['name'], '| tier=', x['tier'], '| conf=', x['confidence'])
