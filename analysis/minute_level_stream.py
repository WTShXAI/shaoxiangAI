"""分钟级数据流重建 (analysis/minute_level_stream.py)

前提: 采集器已修复 (2026-08-20), odds_snapshots.minute_at/score_at 写入真实滚球
      分钟/比分。本模块从带分钟的快照重建:
        1. 盘口+比分时间线      (minute, score_at, OU去水隐含总球)
        2. 进球事件            (score_at 变化 -> 首球分钟等)
        3. 逐分钟剩余破蛋曲线   (复用 live_score_conditional.remaining_break_prob)

注意: 历史 events.db (2026-08-20 之前) 的 80万条滚球快照 minute_at=0/score_at='' 是采集器
      未写入的占位垃圾, 本模块会跳过 (minute_at<=0 视为无效), 只能重建修复后新采集的比赛。
"""
import math
import time

# ── 开赛时间解析 (matches.kickoff 为 'YYYY-MM-DD HH:MM' 北京时间 naive) ──
def _parse_kickoff(kickoff: str):
    """把 kickoff 文本解析为 Unix 时间戳(秒), 解析失败返回 None。"""
    if not kickoff:
        return None
    try:
        from datetime import datetime
        dt = datetime.strptime(str(kickoff).strip(), '%Y-%m-%d %H:%M')
        return dt.timestamp()
    except Exception:
        return None


def _estimate_minute(kickoff_ts: float, captured_at: float, cap_at_kickoff: float = None):
    """用 captured_at 与 kickoff 的偏移估算比赛分钟。

    参数:
      kickoff_ts:    开赛时间戳(秒)
      captured_at:   快照时间戳(秒)
      cap_at_kickoff: 若传入, 表示快照机在 kickoff 时刻的基准 captured_at(防系统时钟偏差)
    返回:
      int 分钟数, 限制在 [0, 130](含加时/点球合理上限)
    """
    if kickoff_ts is None or captured_at is None:
        return 0
    ref = cap_at_kickoff if cap_at_kickoff else kickoff_ts
    elapsed = captured_at - ref
    if elapsed < 0:
        return 0
    return min(130, max(0, int(elapsed / 60)))


# ── 自实现泊松尾概率 (供 OU 反推用, 与 live_score_conditional 内部一致) ──
def _poisson_sf(k: int, lam: float) -> float:
    """P(X >= k) for Poisson(lam)."""
    if lam <= 0:
        return 0.0 if k > 0 else 1.0
    p = math.exp(-lam)
    s = p
    i = 1
    while i < k:
        p *= lam / i
        s += p
        i += 1
    return max(0.0, 1.0 - s)


def _ou_implied_total(over_odds: float, under_odds: float, line: float):
    """OU over/under 去水概率 -> 独立泊松隐含总球 λ。

    去水后 p_over = (1/over)/(1/over + 1/under); 网格搜索 λ 使 poisson_sf(line, λ) ≈ p_over。
    滚球 OU 线多为半整数(2.5/2.75...), poisson_sf(line,λ)=P(总球 > line)。
    """
    if not over_odds or not under_odds or over_odds <= 0 or under_odds <= 0:
        return None
    try:
        p_over = (1.0 / over_odds) / (1.0 / over_odds + 1.0 / under_odds)
    except Exception:
        return None
    if not (0.01 < p_over < 0.99):
        return None
    best = None
    for lam in [i * 0.05 for i in range(1, 240)]:      # λ ∈ [0.05, 12]
        err = abs(_poisson_sf(line, lam) - p_over)
        if best is None or err < best[0]:
            best = (err, lam)
    return best[1] if best else None


try:
    from live_score_conditional import remaining_break_prob
except ImportError:
    from analysis.live_score_conditional import remaining_break_prob


def build_minute_timeline(con, match_key, market_prefix='OU_', exclude_prefixes=None, estimate=False):
    """从 odds_snapshots 重建该场分钟级时间线。

    返回: [{minute, score_at, line, over, under, implied_total, estimated}, ...] 按 minute 升序。
    每个 minute 取该分钟内 captured_at 最大的完整 (over,under) 对作主参考线。
    minute_at>0 的快照视为真实分钟数据; minute_at<=0 时:
      - 若 estimate=True 且 matches.kickoff 可解析, 用 captured_at - kickoff 估算分钟;
      - 否则跳过(无效滚球数据)。
    """
    if exclude_prefixes is None:
        exclude_prefixes = ['OU_1H', 'OU_2H']
    cur = con.cursor()

    # 如需估算, 先取 kickoff
    kickoff_ts = None
    if estimate:
        try:
            ko_row = cur.execute("SELECT kickoff FROM matches WHERE match_key=?", (match_key,)).fetchone()
            if ko_row:
                kickoff_ts = _parse_kickoff(ko_row[0])
        except Exception:
            kickoff_ts = None

    rows = cur.execute(
        """SELECT minute_at, score_at, market, selection, odds, line, captured_at
           FROM odds_snapshots
           WHERE match_key=? AND market LIKE ? AND selection IN ('over','under')
           ORDER BY captured_at ASC""",
        (match_key, market_prefix + '%')).fetchall()

    # 按 (minute, line) 配对 over/under
    pairs = {}
    for minute, score, market, sel, odds, line, cap in rows:
        if exclude_prefixes and any(market.startswith(e) for e in exclude_prefixes):
            continue
        if line is None:
            continue
        is_estimated = False
        effective_minute = minute
        # 真实分钟无效且允许估算时, 尝试用 kickoff 估算
        if effective_minute is None or effective_minute <= 0:
            if estimate and kickoff_ts is not None and cap is not None:
                effective_minute = _estimate_minute(kickoff_ts, cap)
                is_estimated = True
            else:
                continue
        key = (effective_minute, round(line, 2))
        d = pairs.setdefault(key, {'minute': effective_minute, 'score': score, 'cap': cap,
                                   'line': round(line, 2), 'over': None, 'under': None,
                                   'estimated': is_estimated})
        d['score'] = score
        d['cap'] = cap
        d['estimated'] = is_estimated
        if sel == 'over':
            d['over'] = odds
        elif sel == 'under':
            d['under'] = odds

    # 每个 minute 取 captured 最大的完整对
    best_per_min = {}
    for d in pairs.values():
        if d['over'] and d['under']:
            m = d['minute']
            if m not in best_per_min or d['cap'] > best_per_min[m]['cap']:
                best_per_min[m] = d

    timeline = []
    for d in sorted(best_per_min.values(), key=lambda x: x['minute']):
        implied = _ou_implied_total(d['over'], d['under'], d['line'])
        timeline.append({
            'minute': d['minute'],
            'score_at': d['score'] or '',
            'line': d['line'],
            'over': round(d['over'], 3),
            'under': round(d['under'], 3),
            'implied_total': round(implied, 3) if implied else None,
            'estimated': bool(d.get('estimated', False)),
        })
    return timeline


def detect_goal_events(timeline):
    """从时间线 score_at 变化检测进球事件。

    返回: [{minute, score_at, is_first_goal}, ...]。
    规则:
      - 首个状态(通常 0-0/空)只建立基线, 不记为进球;
      - 仅当 score_at 变为**非 0-0** 才算进球事件 (0-0->0-0 不算);
      - is_first_goal=True 标记该场第一次出现非 0-0 比分(首球)。

    已知局限(2026-08-20 审计): score_at 滞后可达 12-20 分钟, 且采集器修复前
    断档段(特罗姆瑟U19 19:33-19:46 含第1球)score_at 全空 → 进球漏检。
    配套 detect_goal_events_with_flip() 用 OU 水位翻转兜底。
    """
    events = []
    prev = None
    first_recorded = False
    for pt in timeline:
        s = pt.get('score_at') or ''
        if s == prev:
            continue
        if prev is None:           # 建立基线, 不计事件
            prev = s
            continue
        if s == '0-0':             # 回到 0-0(理论极少) 不计进球
            prev = s
            continue
        is_first = not first_recorded
        events.append({'minute': pt['minute'], 'score_at': s, 'is_first_goal': is_first})
        first_recorded = True
        prev = s
    return events


def detect_goal_events_with_flip(con, match_key, timeline):
    """双源进球检测: score_at 变化 + OU 水位翻转(2026-08-20 审计新增)。

    背景: 特罗姆瑟U19场实证 score_at 滞后~20分钟(第1球~19:40, score_at 20:05才记),
    且采集器修复前断档段全空。但同一 OU 线的水位在进球时会瞬间翻转:
      进球前: 大2.08/小1.66 (市场看小)
      进球后: 大1.69/小2.05 (大球更近, 水位翻转)
    翻转特征: 3分钟窗口内同线 over/under 低水方互换且幅度显著。

    Returns:
      (events, flip_events): score_at 检测的进球 + 水位翻转推断进球(补漏用)
    """
    events = detect_goal_events(timeline)
    flip_events = []
    try:
        from collections import defaultdict
        cur = con.cursor()
        rows = cur.execute("""
            SELECT market, captured_at, selection, odds FROM odds_snapshots
            WHERE match_key=? AND market LIKE 'OU_%' AND market NOT LIKE 'OU_1H%'
              AND market NOT LIKE 'OU_2H%' AND market != 'OU'
            ORDER BY captured_at
        """, (match_key,)).fetchall()
        by_mkt_time = defaultdict(dict)
        for mkt, t, sel, odds in rows:
            by_mkt_time[(mkt, int(t))][sel] = odds
        seq = defaultdict(list)
        for (mkt, t), sels in sorted(by_mkt_time.items(), key=lambda x: x[0][1]):
            if 'over' in sels and 'under' in sels:
                seq[mkt].append((t, sels['over'], sels['under']))
        for mkt, frames in seq.items():
            for i in range(1, len(frames)):
                t0, ov0, un0 = frames[i - 1]
                t1, ov1, un1 = frames[i]
                # 窗口15分钟(实测特罗姆瑟U19场19:33→19:44间隔11分钟仍为有效进球翻转;
                # 采集轮次间隔45-60s, 但轮询排序会使同线相邻快照间隔拉大到10min+)
                if t1 - t0 > 900:
                    continue
                low_was_over = ov0 < un0
                low_is_over = ov1 < un1
                if low_was_over != low_is_over:
                    delta = abs((un0 - ov0) - (un1 - ov1))
                    if delta >= 0.30:  # 显著翻转(本场实测Δ≈0.78)
                        flip_events.append({
                            'ts': t1, 'market': mkt,
                            'from': f"{ov0:.2f}/{un0:.2f}",
                            'to': f"{ov1:.2f}/{un1:.2f}",
                            'delta': round(delta, 2),
                            'note': 'OU水位翻转推断进球(score_at可能滞后/断档)',
                        })
    except Exception:
        pass
    return events, flip_events


def build_remaining_break_curve(con, match_key, opening_total, line=2.5, league=None, estimate=False):
    """逐分钟剩余破蛋概率曲线 (复用 live_score_conditional.remaining_break_prob)。

    返回: [{minute, score_at, implied_total, prob, lambda_rem, method, estimated}, ...]
    每个分钟点用该分钟 OU 去水隐含总球作 λ_live, 配开盘锚 opening_total。
    """
    tl = build_minute_timeline(con, match_key, estimate=estimate)
    curve = []
    for pt in tl:
        if pt['implied_total'] is None:
            continue
        r = remaining_break_prob(
            implied_total_live=pt['implied_total'],
            current_score=pt['score_at'] or '0-0',
            current_minute=pt['minute'],
            line=line,
            segment='full',
            league=league,
            opening_implied_total=opening_total,
        )
        curve.append({
            'minute': pt['minute'],
            'score_at': pt['score_at'],
            'implied_total': pt['implied_total'],
            'prob': r.get('prob'),
            'lambda_rem': r.get('lambda_rem'),
            'method': r.get('method'),
            'estimated': pt.get('estimated', False),
        })
    return curve


def get_opening_total(con, match_key, prefer_line=2.5):
    """从 events.db 开盘 OU 快照(minute_at=0 的赛前盘)反推开盘隐含总球。

    选 earliest captured_at 的 OU over/under 对作主开盘; 优先 prefer_line 线, 否则取任意线。
    返回 隐含总球(float) 或 None。

    注意: 修复前(2026-08-20)滚球快照也全是 minute_at=0 占位垃圾, 但那些 captured_at
          晚于赛前开盘, 故取 earliest captured_at 的分钟=0 对即等于赛前开盘, 不受影响。
    """
    cur = con.cursor()
    rows = cur.execute(
        """SELECT minute_at, market, selection, odds, line, captured_at
           FROM odds_snapshots
           WHERE match_key=? AND market LIKE 'OU_%' AND selection IN ('over','under') AND minute_at=0
           ORDER BY captured_at ASC""",
        (match_key,)).fetchall()
    if not rows:
        return None
    pairs = {}
    for minute, market, sel, odds, line, cap in rows:
        if line is None:
            continue
        if market.startswith('OU_1H') or market.startswith('OU_2H'):
            continue
        key = round(line, 2)
        d = pairs.setdefault(key, {'over': None, 'under': None, 'cap': cap})
        if sel == 'over':
            d['over'] = odds
        elif sel == 'under':
            d['under'] = odds
    if prefer_line in pairs and pairs[prefer_line]['over'] and pairs[prefer_line]['under']:
        return _ou_implied_total(pairs[prefer_line]['over'], pairs[prefer_line]['under'], prefer_line)
    for ln, d in sorted(pairs.items()):
        if d['over'] and d['under']:
            return _ou_implied_total(d['over'], d['under'], ln)
    return None


def get_match_minute_stream(con, match_key, opening_total=None, line=2.5, league=None, estimate=False):
    """一键汇总: 时间线 + 进球事件 + 逐分钟剩余破蛋曲线。

    若未传 opening_total, 自动从 events.db 赛前 OU 反推开盘隐含总球;
    反推失败则 remaining_break_curve 的 anchoring 退化为 λ_live 单点。

    参数:
      estimate: 无真实 minute_at 时, 是否用 kickoff+captured_at 估算分钟(标注 estimated=True)。
    返回:
      dict 含 data_quality ('real'|'estimated'|'none'), reason, has_real_minute_data 等。
    """
    cur = con.cursor()
    # 判断比赛状态与真实分钟数据可用性
    status = 'unknown'
    kickoff_ts = None
    try:
        row = cur.execute("SELECT status, kickoff FROM matches WHERE match_key=?", (match_key,)).fetchone()
        if row:
            status = row[0] or 'unknown'
            kickoff_ts = _parse_kickoff(row[1])
    except Exception:
        pass

    if opening_total is None:
        try:
            opening_total = get_opening_total(con, match_key, prefer_line=line)
        except Exception:
            opening_total = None

    timeline = build_minute_timeline(con, match_key, estimate=estimate)
    # 估算模式下不检测进球事件: score_at 是占位空串, 无法信赖
    if estimate and any(t.get('estimated') for t in timeline):
        goals = []
        goal_events_note = '估算模式: 比分未知, 进球事件未检测'
    else:
        goals = detect_goal_events(timeline)
        goal_events_note = None

    curve = build_remaining_break_curve(con, match_key, opening_total, line=line, league=league, estimate=estimate)

    has_real = any(not t.get('estimated', False) for t in timeline)
    has_estimated = any(t.get('estimated', False) for t in timeline)

    # scheduled 比赛无论是否有赛前快照, 都不应生成滚球分钟曲线
    if status == 'scheduled':
        data_quality = 'none'
        reason = '比赛尚未开始, 无滚球分钟数据'
    elif has_real:
        data_quality = 'real'
        reason = '真实分钟数据'
    elif has_estimated:
        data_quality = 'estimated'
        reason = '按开赛时间估算分钟(比分未知, 曲线仅供参考)'
    elif kickoff_ts is None:
        data_quality = 'none'
        reason = '缺少开赛时间, 无法估算分钟'
    else:
        data_quality = 'none'
        reason = '无 OU 赔率快照, 无法重建分钟级曲线'

    return {
        'match_key': match_key,
        'opening_total': round(opening_total, 3) if opening_total else None,
        'n_snapshots_minute': len(timeline),
        'timeline': timeline,
        'goal_events': goals,
        'goal_events_note': goal_events_note,
        'remaining_break_curve': curve,
        'has_real_minute_data': has_real,
        'has_estimated_minute_data': has_estimated,
        'data_quality': data_quality,
        'reason': reason,
        'status': status,
    }
