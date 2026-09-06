#!/usr/bin/env python3
"""
哨响AI · 分析中心引擎 (供 bridge API 调用)
============================================
shaoxiang_center.py 的核心逻辑提取，返回 JSON 供前端消费。
"""

import sqlite3, math, time

GQ_DB = 'data/events.db'

# 三色情绪标签系统 (红绿灯+关键词) — 哨响AI "最强信号" 速查表 (2026-08-08)
from pipeline.signal_label import compute_signal_label, signed_deviation_from_freq
_DIRS = ('主胜', '平局', '客胜')

def _devig_3way(oh, od, oa):
    """1X2 去水, 返回 {主胜/平局/客胜: 0..1}。任一赔率非法返回 None。"""
    try:
        inv = 1.0 / float(oh) + 1.0 / float(od) + 1.0 / float(oa)
        if inv <= 0 or inv > 1.20:  # 抽水>20% 视为异常, 不伪造
            return None
        return {
            '主胜': (1.0 / float(oh)) / inv,
            '平局': (1.0 / float(od)) / inv,
            '客胜': (1.0 / float(oa)) / inv,
        }
    except (ValueError, ZeroDivisionError, TypeError):
        return None


def tick(o):
    try: o=float(o); return int(round(o*100))%10 if 1.0<=o<1.5 else None
    except: return None

def poisson_top3(oh, od, oa, ou_over=None, ou_under=None):
    h=1.0/oh; d=1.0/od; a=1.0/oa; s=h+d+a
    hp=h/s; ap=a/s
    scale = 2.5  # 锚定2.5线
    if ou_over and ou_under:
        ov=float(ou_over); un=float(ou_under)
        if ov <= 1.75: scale = 2.8
        elif un <= 1.75: scale = 2.0
    hl=scale*hp; al=scale*ap
    scores=[]
    for hg in range(7):
        for ag in range(7):
            ph=((hl**hg)*math.exp(-hl)/max(1,math.factorial(hg)))
            pa=((al**ag)*math.exp(-al)/max(1,math.factorial(ag)))
            scores.append((f'{hg}-{ag}', round(ph*pa,6)))
    scores.sort(key=lambda x:-x[1])
    return [{'score':s,'prob':round(p*100,1),'source':'poisson'} for s,p in scores[:3]]

# ── 优先级1: 市场真实CS赔率 (events.db odds_snapshots.market='CS' 的去水概率) ──
# 覆盖93.2%比赛, 反映真实市场对各比分的看法 (含战术修正:领先方收缩/落后方反扑等).
# 比泊松从1X2赔率反推准确, 特别是"弱队进1球"型 (如 1-1/2-1/0-1) 泊松严重低估.
_CS_CACHE = {}      # key=match_key -> (ts, top3_list 或 None)
_CS_CACHE_TTL = 120  # 与 scan 缓存对齐

def cs_top3_from_market(match_key: str):
    """从 events.db 取市场 CS 赔率, 去水推导 Top3. 失败返回 None (回退泊松).
    每个比分取最新 odds, 而非全局最新快照 (避免最后快照只含1个比分的偏)."""
    if not match_key:
        return None
    now = __import__('time').time()
    cached = _CS_CACHE.get(match_key)
    if cached and (now - cached[0]) < _CS_CACHE_TTL:
        return cached[1]
    try:
        c = sqlite3.connect(GQ_DB)
        # 每个 selection 取最新 odds
        rows = c.execute("""
            SELECT selection, odds FROM odds_snapshots
            WHERE market='CS' AND match_key=?
              AND selection NOT LIKE 'other%' AND selection NOT LIKE '其他%'
              AND selection NOT LIKE '%/%' AND selection != 'other'
              AND id IN (
                SELECT MAX(id) FROM odds_snapshots
                WHERE market='CS' AND match_key=?
                GROUP BY selection
              )
        """, (match_key, match_key)).fetchall()
        c.close()
        if len(rows) < 5:
            return None  # 数据不足,回退泊松
        # 去水推导概率 (归一化)
        inv_sum = sum(1.0 / float(o) for _, o in rows if float(o) > 1.01)
        if inv_sum <= 0:
            return None
        scored = [(sel, (1.0 / float(odds)) / inv_sum)
                  for sel, odds in rows if float(odds) > 1.01]
        scored.sort(key=lambda x: -x[1])
        top = [{'score': s, 'prob': round(p * 100, 1), 'source': 'market'}
               for s, p in scored[:3]]
        # ── 波胆概率校准 (3728场真值推导, 2026-08-04) ──
        # 庄家系统性低估 0-0 (因子1.40), 高估所有其他比分.
        # 用实际频率/隐含概率的校准因子修正后重归一化.
        try:
            from pipeline.cs_calibration import apply_calibration
            top = apply_calibration(top)
        except Exception:
            pass
        _CS_CACHE[match_key] = (now, top)
        return top
    except Exception:
        return None

def query_neighbors(oh, od, oa):
    try:
        from scripts.build_odds_vector_library import query_by_odds
        nbs=query_by_odds(oh,od,oa,k=5,min_sim=0.80)
        if not nbs: return None
        res={'home':0,'draw':0,'away':0}; details=[]
        for sim,nb in nbs:
            r=nb.get('result')
            if r in res: res[r]+=1
        for i,(sim,nb) in enumerate(nbs[:3]):
            details.append({
                'home': nb.get('home','?')[:14],
                'away': nb.get('away','?')[:14],
                'result': nb.get('result','?'),
                'score': f'{nb.get("score_home","?")}-{nb.get("score_away","?")}',
                'sim': round(sim,4),
                'league': nb.get('league','?')[:20]
            })
        return {'count':sum(res.values()),'home':res['home'],'draw':res['draw'],'away':res['away'],'top':details}
    except Exception: return None

# 单场版赔率结构分析 (供 ranked_predictor 调用, 零延迟/本地可算)
_NB_CACHE = {}          # key=(round(oh,4),round(od,4),round(oa,4)) -> (ts, [p_h,p_d,p_a] or None)
_NB_TTL = 120

def _analyze_odds(oh, od, oa, ovo=None, ovu=None, with_neighbors=False, match_key=None):
    """单场赔率结构指纹 + 可选相似盘口经验频率.
    返回 {score, signals, warns, verdict, poisson, neighbor_freq, cs_source}.
    poisson/cs_top3: 优先市场CS赔率 (93.2%覆盖), 回退泊松.
    neighbor_freq=[p_h,p_d,p_a] 仅当 with_neighbors=True 且查到相似盘口时非空, 否则 None."""
    o = {'home': float(oh), 'draw': float(od), 'away': float(oa)}
    fav = min(o, key=lambda k: o[k]); fav_odds = o[fav]
    bname = {'home':'主胜','draw':'平局','away':'客胜'}[fav]
    score = 3; signals = []; warns = []
    ht = tick(oh); at = tick(oa)
    if ht == 4: score -= 2; warns.append('主陷.04')
    elif ht in (1,2,9): score += 3; signals.append('主强.{}(+8pp)'.format(ht))
    if at == 4: score -= 2; warns.append('客陷.04')
    elif at in (1,2,9): score += 3; signals.append('客强.{}(+8pp)'.format(at))
    if ovo:
        ov=float(ovo); un=float(ovu)
        if ov<=1.75: score+=2; signals.append('大优')
        if un<=1.75: score+=2; signals.append('小优')
    gap=abs(float(oh)-float(oa))
    if fav_odds<1.5:
        dgt=int(round(fav_odds*100))%10
        if dgt==4: score-=2; warns.append('冷门.04(翻车15.3%)')
        elif dgt in(1,2,9): score+=1
    if gap<0.5 and float(od)<3.5: score-=1; warns.append('胶着(冷门62.9%)')
    if ht in(1,2,9) and at==4: score+=2; signals.append('双刃:主强客陷')
    if at in(1,2,9) and ht==4: score+=2; signals.append('双刃:客强主陷')
    ps = poisson_top3(float(oh),float(od),float(oa), ovo, ovu)
    # [P0-4] 优先用市场真实CS赔率 (覆盖93.2%), 泊松仅回退.
    # 关键差异: 弱队进1球型 (1-1/2-1/0-1) 泊松严重低估, 市场赔率反映真实战术修正.
    market_top = cs_top3_from_market(match_key) if match_key else None
    if market_top:
        ps = market_top
        cs_source = 'market'
    else:
        cs_source = 'poisson'
    verdict=[]
    if score>=7: verdict.append('强烈关注')
    elif score>=5: verdict.append('可考虑')
    if warns: verdict.append('注意风险')
    else: verdict.append('信号干净')
    neighbor_freq = None
    if with_neighbors:
        key=(round(float(oh),4),round(float(od),4),round(float(oa),4))
        now=time.time()
        if key in _NB_CACHE and (now-_NB_CACHE[key][0])<_NB_TTL:
            neighbor_freq=_NB_CACHE[key][1]
        else:
            nb=query_neighbors(float(oh),float(od),float(oa))
            if nb and nb.get('count',0)>0:
                h_=nb['home']; d_=nb['draw']; a_=nb['away']; tot=h_+d_+a_
                if tot>0: neighbor_freq=[h_/tot, d_/tot, a_/tot]
            _NB_CACHE[key]=(now, neighbor_freq)
    return {'score':score,'signals':signals,'warns':warns,
            'verdict':' / '.join(verdict),'poisson':ps,'neighbor_freq':neighbor_freq,
            'cs_source': cs_source}

# 简单缓存(避免重复扫描, 120秒TTL) — 键含过滤参数, 不同过滤互不串缓存
_CACHE = {}
_CACHE_TTL = 120

def run_scan(limit=300, top_n=6, min_odds=0.0, only_scheduled=False):
    import time
    now = time.time()
    key = (top_n, round(float(min_odds), 2), bool(only_scheduled))
    cached = _CACHE.get(key)
    if cached and (now - cached[0]) < _CACHE_TTL:
        return cached[1]
    data = _run_scan_impl(limit, top_n, min_odds=min_odds, only_scheduled=only_scheduled)
    _CACHE[key] = (now, data)
    return data

def _run_scan_impl(limit, top_n, min_odds=0.0, only_scheduled=False):
    c=sqlite3.connect(GQ_DB); c.row_factory=sqlite3.Row
    # ── 动态 where/order: 高赔(以小博大)优先未来赛程, 否则 live 优先 ──
    _where = "exists(select 1 from odds_snapshots where match_key=m.match_key and market='1X2') and m.status != 'filtered'"
    if only_scheduled:
        _where += " and m.status = 'scheduled'"
        _order = "m.kickoff asc"
    else:
        _where += " and m.status in ('live','scheduled')"
        # 高赔过滤激活时, 优先未来赛程以捕获长赔冷门方(否则300行窗口被live占满)
        _order = "(m.status='scheduled') desc, m.kickoff asc" if min_odds else "(m.status='live') desc, m.kickoff asc"
    rows=c.execute(f'''
        select m.match_key,m.home,m.away,m.league,m.kickoff,m.status,
          (select odds from odds_snapshots where match_key=m.match_key and market='1X2' and selection='home' order by captured_at desc limit 1) oh,
          (select odds from odds_snapshots where match_key=m.match_key and market='1X2' and selection='draw' order by captured_at desc limit 1) od,
          (select odds from odds_snapshots where match_key=m.match_key and market='1X2' and selection='away' order by captured_at desc limit 1) oa,
          (select odds from odds_snapshots where match_key=m.match_key and market like 'OU_%' and market not like 'OU_1H%' and market not like 'OU_2H%' and selection='over' order by captured_at desc limit 1) ovo,
          (select odds from odds_snapshots where match_key=m.match_key and market like 'OU_%' and market not like 'OU_1H%' and market not like 'OU_2H%' and selection='under' order by captured_at desc limit 1) ovu,
          (select market from odds_snapshots where match_key=m.match_key and market like 'OU_%' and market not like 'OU_1H%' and market not like 'OU_2H%' order by captured_at desc limit 1) oum,
          -- 球线值只存在于 market 字段名里(形如 OU_2.50), 与 ovo 用完全相同的 where/order,
          -- 取到的必是同一行快照的 market 名 → 解析出 ou_line 供 /api/predict/ranked 使用.
          (select market from odds_snapshots where match_key=m.match_key and market like 'OU_%' and market not like 'OU_1H%' and market not like 'OU_2H%' and selection='over' order by captured_at desc limit 1) ovm
        from matches m
        where {_where}
        order by {_order} limit ?
    ''',(limit,)).fetchall()
    c.close()

    matches=[]
    for r in rows:
        if not r['oh']: continue
        oh=float(r['oh']); od=float(r['od']); oa=float(r['oa'])

        # OU 球线: 从 market 名 'OU_2.50' 解析出 2.5; 解析失败一律降级 None (不抛错)
        ou_line=None
        if r['ovm'] and str(r['ovm']).startswith('OU_'):
            try: ou_line=float(str(r['ovm'])[3:])
            except (ValueError, TypeError): ou_line=None

        o={'home':oh,'draw':od,'away':oa}
        fav=min(o,key=lambda k:o[k]); fav_odds=o[fav]; bname={'home':'主胜','draw':'平局','away':'客胜'}[fav]
        max_odds=max(oh,od,oa)  # 冷门方(长赔)赔率 —— 以小博大的真实轴线

        # ── 前端过滤(赛前以小博大): 只看未开赛 + 只看冷门方高赔率 ──
        if only_scheduled and r['status'] != 'scheduled':
            continue
        if min_odds and max_odds < min_odds:
            continue

        # 评分/信号/风险/波胆/判读 统一走单场版 _analyze_odds (实现挪走, 行为不变)
        _a = _analyze_odds(oh, od, oa, r['ovo'], r['ovu'], match_key=r['match_key'])
        score=_a['score']; signals=_a['signals']; warns=_a['warns']
        ps=_a['poisson']; verdict=_a['verdict']
        cs_source=_a.get('cs_source','poisson')

        matches.append({
            'home': r['home'][:20], 'away': r['away'][:20], 'league': r['league'][:24],
            'oh': oh, 'od': od, 'oa': oa,
            'ou_line': ou_line,
            'ou_over': float(r['ovo']) if r['ovo'] else None,
            'ou_under': float(r['ovu']) if r['ovu'] else None,
            'score': score,
            'fav': bname, 'fav_odds': round(fav_odds,2),
            'signals': signals, 'warns': warns,
            'poisson': ps,
            'cs_source': cs_source,
            'verdict': verdict,
            'status': r['status'],
            'kickoff': r['kickoff'],
            'max_odds': round(max_odds, 2),
        })

    # 高赔过滤激活时, 按冷门方赔率降序(最大以小博大在前); 否则按结构评分降序
    if min_odds > 0:
        matches.sort(key=lambda x:-x.get('max_odds', 0))
    else:
        matches.sort(key=lambda x:-x['score'])
    top=matches[:top_n]

    # 近邻(仅Top N)
    for m in top:
        m['neighbors'] = query_neighbors(m['oh'],m['od'],m['oa'])
        # ── 三色情绪标签 (红绿灯+关键词) ──
        # 由「经验频率 vs 市场隐含概率」算有符号偏差(pp): 跑赢预期为正。
        # 最强信号方向 = 经验频率最高者; ROI 在扫描期未知(按≈0, 边界给保守/反买措辞)。
        nb = m['neighbors']
        if nb and nb.get('count'):
            h_ = nb.get('home', 0); d_ = nb.get('draw', 0); a_ = nb.get('away', 0)
            tot = h_ + d_ + a_
            if tot > 0:
                nf = {'主胜': h_ / tot, '平局': d_ / tot, '客胜': a_ / tot}
                mp = _devig_3way(m['oh'], m['od'], m['oa'])
                if mp:
                    dev = signed_deviation_from_freq(nf, mp)
                    if dev is not None:
                        dir_strong = max(_DIRS, key=lambda d: nf[d])
                        m['signal_tag'] = compute_signal_label(dir_strong, dev, roi=None)
                    else:
                        m['signal_tag'] = None
                else:
                    m['signal_tag'] = None
            else:
                m['signal_tag'] = None
        else:
            m['signal_tag'] = None

    # 统计
    stats={
        'total': len(matches),
        'strongest': sum(1 for m in matches if m['score']>=7),
        'strong': sum(1 for m in matches if m['score']>=5),
    }

    return {'stats': stats, 'overview': matches[:30], 'top': top}
