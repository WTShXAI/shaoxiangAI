#!/usr/bin/env python3
"""哨响AI · 赛前赔率结构相似检索引擎 (SSoT)
================================================================
用途: 输入一场未开赛比赛的【初盘 + 临盘】1X2 赔率, 从 31.2 万场历史
      赛前赔率结构中检索最相似的 K 场, 输出【唯一】赛果结论 + 三方向 ROI。

为什么重造 (2026-08-08 实证, 见 _verify_tmp/verify_prematch_knn*.py):
  旧 odds_vectors.db 用 odds_snapshots "最后一帧快照" 建库:
    · 仅 741 场 (样本量不足)
    · 45.1% 记录单边赔率 <1.08 → 是滚球尾盘赔率, 赛前不可能出现
    · 平局占比仅 16.2% (真实 25.7%) → 系统性低估平局 9.5pp
  结果: 用赛前盘去查一个滚球污染库, 近邻几乎全是"已见分晓"的结构,
        平局被结构性抹掉 → 前端"显示胜负、实际打平"。

本模块数据源: football_data.db::historical_matches (311,976 场齐全)
  open_{home,draw,away}_odds  = 初盘
  close_{home,draw,away}_odds = 临盘(收盘)
  天然赛前, 滚球污染仅 0.65%; 平局 25.7% 与真实一致。

铁律遵守:
  · 只吃赛前结构。比赛一旦开赛, 赔率含比分信息 → applicable=False,
    明确移交滚球系统, 绝不用赛前库解读滚球盘 (用户 2026-08-08 明确要求)。
  · 唯一结论: 胜平负只出一个 (含平局), 不再罗列多方向让用户自己挑。
  · ROI 三方向全算, 不只算赔率最低那一个。
  · 样本外验证并排 naive 基线, 不吹不藏 (见下方 VALIDATION)。

VALIDATION (2024-07-01 后样本外, N=5000, K=200):
  基线平局率 26.1% | naive(市场favorite)命中 52.70%
  众数判据命中 52.52% (与市场持平, 无超额 → 不吹"跑赢市场")
  平局升级 freq_draw>=0.30: 触发 24.9%, 实际平局率 33.1% (+7.0pp), ROI +1.61%
  众数=平局子集 126 场: 实际平局率 37.3% (+11.2pp)
  → "该说平局时说平局" 是本引擎唯一稳定为正的信号。
"""

from __future__ import annotations

import datetime
import math
import os
import sqlite3
import threading
import time
from typing import Dict, List, Optional, Sequence, Tuple

try:
    import numpy as np
except ImportError:  # pragma: no cover - numpy 是硬依赖
    np = None  # type: ignore

FOOTBALL_DB = os.environ.get(
    'SHAOXIANG_FOOTBALL_DB', r'D:\Architecture\data\football_data.db'
)

# ── 距离权重: 临盘信息更足(含全部盘前调整), 权重高于初盘 ──
OPEN_W, CLOSE_W = 0.35, 0.65

# ── 平局阈值 (实证标定, 勿随意调) ──
# freq_draw >= 0.30 时: 实际平局率 33.1%, 超基线 +7.0pp, 押平 ROI +1.61%
#
# 两种用法, 语义不同, 不要混淆:
#   1) draw_alert  (默认, 恒开): 仅【预警】, 不改结论。
#   2) draw_upgrade(默认 开启, 经 query_match/API 验证路径): 把众数结论【改判】为平局。
#      实证 (GQ 已结束比赛 N=246): 严格众数命中 41.5%/平局召回 0%;
#      改判后 45.9%/平局召回 23% —— 同时提升命中率与平局召回,
#      直接满足用户"查询结果是平局就显示平局"。低层 query_prematch 仍默认严格众数,
#      调用方按需开启; 改判前保留 mode_verdict(严格众数) 供对照, 不隐藏信息。
DRAW_UPGRADE_T = 0.30
# 平局预警(不改结论, 仅提示): 近邻平局率显著高于市场隐含
DRAW_ALERT_EXCESS = 0.04

DEFAULT_K = 200
LABELS = ('H', 'D', 'A')
LABEL_CN = {'H': '主胜', 'D': '平局', 'A': '客胜'}

# ── 全库常驻内存 (31.2万 × 6 float32 ≈ 7.5MB), 懒加载 + 线程安全 ──
_LIB_LOCK = threading.Lock()
_LIB: Optional[Dict] = None

# ── 查询结果缓存 (同赔率结构 120s 内复用) ──
_Q_LOCK = threading.Lock()
_Q_CACHE: Dict[tuple, Tuple[float, dict]] = {}
_Q_TTL = 120.0
_Q_MAX = 1024


# ══════════════════════════════════════════════════════════════
# 基础工具
# ══════════════════════════════════════════════════════════════
def devig(odds: Sequence[float]) -> Optional[List[float]]:
    """三路赔率 → 去水概率 (和恒为1)。任一非法则返回 None (不伪造)。"""
    try:
        vals = [float(o) for o in odds]
    except (TypeError, ValueError):
        return None
    if len(vals) != 3 or any((not math.isfinite(v)) or v <= 1.01 for v in vals):
        return None
    inv = [1.0 / v for v in vals]
    s = sum(inv)
    if s <= 0:
        return None
    return [x / s for x in inv]


def _valid_triplet(odds) -> bool:
    return devig(odds) is not None


# ══════════════════════════════════════════════════════════════
# 历史库加载
# ══════════════════════════════════════════════════════════════
def _load_library(force: bool = False) -> Dict:
    """加载 historical_matches 赛前结构到内存矩阵。首次约 3-6 秒。"""
    global _LIB
    if _LIB is not None and not force:
        return _LIB
    with _LIB_LOCK:
        if _LIB is not None and not force:
            return _LIB
        if np is None:
            raise RuntimeError('prematch_similarity 需要 numpy')
        t0 = time.time()
        c = sqlite3.connect(FOOTBALL_DB)
        rows = c.execute(
            """
            select match_date, league_name, home_team, away_team, final_result,
                   home_score, away_score,
                   open_home_odds, open_draw_odds, open_away_odds,
                   close_home_odds, close_draw_odds, close_away_odds
            from historical_matches
            where open_home_odds>1.01 and open_draw_odds>1.01 and open_away_odds>1.01
              and close_home_odds>1.01 and close_draw_odds>1.01 and close_away_odds>1.01
              and final_result in ('H','D','A')
              and match_date is not null and match_date != ''
            order by match_date
            """
        ).fetchall()
        c.close()

        n = len(rows)
        feat = np.zeros((n, 6), dtype=np.float32)   # [p_open_h,d,a, p_close_h,d,a]
        y = np.zeros(n, dtype=np.int8)              # 0=H 1=D 2=A
        close_odds = np.zeros((n, 3), dtype=np.float32)
        meta: List[dict] = []
        lab_idx = {'H': 0, 'D': 1, 'A': 2}

        for i, r in enumerate(rows):
            po = devig((r[7], r[8], r[9]))
            pc = devig((r[10], r[11], r[12]))
            if po is None or pc is None:
                continue
            feat[i, :3] = po
            feat[i, 3:] = pc
            y[i] = lab_idx[r[4]]
            close_odds[i] = (r[10], r[11], r[12])
            meta.append({
                'date': (r[0] or '')[:10],
                'league': r[1] or '',
                'home': r[2] or '',
                'away': r[3] or '',
                'result': r[4],
                'score_home': r[5],
                'score_away': r[6],
            })

        _LIB = {
            'feat': feat, 'y': y, 'close_odds': close_odds, 'meta': meta,
            'n': n, 'loaded_at': time.time(), 'load_sec': round(time.time() - t0, 2),
            'base_rate': {
                'H': float((y == 0).mean()),
                'D': float((y == 1).mean()),
                'A': float((y == 2).mean()),
            },
        }
        return _LIB


def library_stats() -> dict:
    """供 /api/prematch/stats 自检: 库规模 / 基线分布 / 加载耗时。"""
    lib = _load_library()
    return {
        'source': 'football_data.db::historical_matches',
        'matches': lib['n'],
        'base_rate': {k: round(v, 4) for k, v in lib['base_rate'].items()},
        'load_sec': lib['load_sec'],
        'open_weight': OPEN_W,
        'close_weight': CLOSE_W,
        'draw_upgrade_threshold': DRAW_UPGRADE_T,
    }


# ══════════════════════════════════════════════════════════════
# 核心检索
# ══════════════════════════════════════════════════════════════
def _cache_key(po, pc, k, league) -> tuple:
    return (tuple(round(x, 4) for x in po), tuple(round(x, 4) for x in pc),
            int(k), (league or '').strip().lower())


def query_prematch(
    close_odds: Sequence[float],
    open_odds: Optional[Sequence[float]] = None,
    k: int = DEFAULT_K,
    league: Optional[str] = None,
    status: Optional[str] = None,
    top_samples: int = 5,
    draw_upgrade: bool = False,
) -> dict:
    """赛前赔率结构检索 → 唯一赛果结论。

    参数
    ----
    close_odds : (home, draw, away) 临盘赔率 — 必填
    open_odds  : (home, draw, away) 初盘赔率 — 可选; 缺失时用临盘代替(退化为
                 纯静态结构匹配, drift 置 None 并降低 confidence, 不伪造)
    status     : 比赛状态。'live'/'finished'/'inplay' 等已开赛状态 →
                 applicable=False, 拒绝出结论 (赛前库不解读滚球盘)
    draw_upgrade : True 时, 近邻平局率≥30% 直接改判平局 (投注价值口径,
                 提高 ROI 但降低命中率)。默认 False = 忠实众数口径。

    返回
    ----
    dict, 关键字段:
      applicable   : 是否适用 (开赛后 False)
      verdict      : 'H'/'D'/'A' 唯一结论 (含平局)
      verdict_cn   : 中文
      verdict_from : 'mode' | 'draw_upgrade'  结论来源, 可审计
      freq         : 近邻三方向频率
      market_prob  : 临盘去水概率
      excess       : freq - market_prob (相对市场超额, 正=历史比市场更看好)
      roi          : 三方向历史 ROI% (近邻集按临盘赔率平注结算, 全部都算)
      drift        : 初盘→临盘漂移 (pp), 操盘手意图
      draw_alert   : 平局风险预警
      samples      : Top N 最相似历史比赛 (可查证)
    """
    # ── 仅【进行中】比赛拒绝出赛前结论 (用户 2026-08-08: 进球后赔率变了,
    #    要用另一套系统)。已结束(finished/ft/ended)比赛允许 —— 用其赛前初盘临盘
    #    回测, 恰好满足"查询结果是平局就显示平局"的历史验证诉求。
    st = (status or '').strip().lower()
    IN_PLAY = ('live', 'inplay', 'playing', 'half')
    if st in IN_PLAY:
        return {
            'applicable': False,
            'reason': 'in_play',
            'note': ('比赛进行中, 赔率已含比分信息 (进球后结构剧变), '
                     '赛前相似检索不适用 → 请用滚球系统'),
            'status': st,
        }

    pc = devig(close_odds)
    if pc is None:
        return {'applicable': False, 'reason': 'bad_close_odds',
                'note': '临盘 1X2 赔率缺失或非法, 无法检索 (不伪造)'}

    po = devig(open_odds) if open_odds is not None else None
    open_missing = po is None
    if open_missing:
        po = pc[:]   # 退化: 无初盘时以临盘充当, drift 置 None

    ck = _cache_key(po, pc, k, league) + (bool(draw_upgrade),)
    now = time.time()
    with _Q_LOCK:
        hit = _Q_CACHE.get(ck)
        if hit and (now - hit[0]) < _Q_TTL:
            return hit[1]

    lib = _load_library()
    feat, y, codds, meta = lib['feat'], lib['y'], lib['close_odds'], lib['meta']
    n = len(meta)
    if n == 0:
        return {'applicable': False, 'reason': 'empty_library', 'note': '历史库为空'}

    q = np.array(po + pc, dtype=np.float32)
    d = (OPEN_W * ((feat[:n, :3] - q[:3]) ** 2).sum(axis=1)
         + CLOSE_W * ((feat[:n, 3:] - q[3:]) ** 2).sum(axis=1))

    kk = int(max(20, min(k, n)))
    idx = np.argpartition(d, kk - 1)[:kk]
    idx = idx[np.argsort(d[idx])]          # 按距离升序, 便于取 top samples

    yy = y[idx]
    cnt = np.array([(yy == 0).sum(), (yy == 1).sum(), (yy == 2).sum()], dtype=np.float64)
    freq = cnt / kk

    # ── 三方向 ROI 全算 (平注1, 按各自临盘赔率结算) ──
    roi = {}
    for j, lb in enumerate(LABELS):
        won = (yy == j)
        ret = float(codds[idx][won, j].sum())
        roi[lb] = round((ret - kk) / kk * 100, 2)

    market = {'H': pc[0], 'D': pc[1], 'A': pc[2]}
    _market_fav = max(LABELS, key=lambda lb: market[lb])
    fr = {'H': float(freq[0]), 'D': float(freq[1]), 'A': float(freq[2])}
    excess = {lb: round(fr[lb] - market[lb], 4) for lb in LABELS}

    # ── 唯一结论: 近邻众数 (含平局)。平局是众数就说平局, 绝不硬掰成胜负 ──
    # 旧系统真凶: 用 min(赔率) 的 favorite 当结论, 平赔几乎永远不是最低
    #             → 平局在结论层被结构性抹掉。这里改为频率众数。
    mode_j = int(np.argmax(freq))
    mode_verdict = LABELS[mode_j]
    verdict = mode_verdict
    verdict_from = 'mode'
    # 平局改判 (用户 2026-08-08 明确: "查询结果是平局就显示平局"):
    # 近邻平局率≥30% 时直接改判平局。实证 (GQ 已结束比赛 N=246):
    #   严格众数命中 41.5% / 平局召回 0%; 改判后 45.9% / 平局召回 23%。
    #   → 改判同时提升命中率与平局召回, 是本引擎默认行为。
    if draw_upgrade and fr['D'] >= DRAW_UPGRADE_T and verdict != 'D':
        verdict = 'D'
        verdict_from = 'draw_upgrade'

    draw_alert = (fr['D'] >= DRAW_UPGRADE_T) or (excess['D'] >= DRAW_ALERT_EXCESS)

    # ── 初盘→临盘漂移 (操盘手意图) ──
    drift = None
    if not open_missing:
        drift = {lb: round((pc[i] - po[i]) * 100, 2) for i, lb in enumerate(LABELS)}

    # ── 把握度: 众数领先幅度 + 样本紧密度 ──
    srt = sorted(fr.values(), reverse=True)
    margin = srt[0] - srt[1]
    mean_dist = float(d[idx].mean())
    tight = mean_dist < 0.0008
    if margin >= 0.18 and tight:
        tier, tscore = '高', 3
    elif margin >= 0.10:
        tier, tscore = '中', 2
    else:
        tier, tscore = '低', 1
    if open_missing:
        tscore = max(1, tscore - 1)
        tier = {3: '中', 2: '低', 1: '低'}[tscore + 1] if tscore < 3 else tier

    samples = []
    for i in idx[:max(0, top_samples)]:
        m = meta[int(i)]
        samples.append({
            'date': m['date'], 'league': m['league'][:24],
            'home': m['home'][:18], 'away': m['away'][:18],
            'result': m['result'],
            'score': (f"{m['score_home']}-{m['score_away']}"
                      if m['score_home'] is not None and m['score_away'] is not None else '--'),
            'dist': round(float(d[int(i)]), 6),
        })

    base = lib['base_rate']
    out = {
        'applicable': True,
        'verdict': verdict,
        'verdict_cn': LABEL_CN[verdict],
        'verdict_from': verdict_from,
        'mode_verdict': mode_verdict,
        'mode_verdict_cn': LABEL_CN[mode_verdict],
        'confidence_tier': tier,
        'confidence_score': tscore,
        'sample': kk,
        'freq': {lb: round(fr[lb], 4) for lb in LABELS},
        'market_prob': {lb: round(market[lb], 4) for lb in LABELS},
        'excess': excess,
        'roi': roi,
        'roi_verdict': roi[verdict],
        'drift': drift,
        'open_missing': open_missing,
        'draw_alert': bool(draw_alert),
        'draw_freq': round(fr['D'], 4),
        'draw_base': round(base['D'], 4),
        'draw_lift_pp': round((fr['D'] - base['D']) * 100, 1),
        # 市场倾向(赔率最低=隐含概率最高方向) — 仅作对照, 不再当结论用
        'market_fav': _market_fav,
        'agree_with_market': verdict == _market_fav,
        'draw_upgrade_available': (not draw_upgrade) and fr['D'] >= DRAW_UPGRADE_T and verdict != 'D',
        'mean_dist': round(mean_dist, 6),
        'samples': samples,
        'library': {'source': 'historical_matches', 'size': n},
        'note': _build_note(verdict, verdict_from, fr, market, draw_alert, open_missing),
    }

    with _Q_LOCK:
        if len(_Q_CACHE) >= _Q_MAX:
            _Q_CACHE.clear()
        _Q_CACHE[ck] = (time.time(), out)
    return out


def _build_note(verdict, verdict_from, fr, market, draw_alert, open_missing) -> str:
    cn = LABEL_CN[verdict]
    parts = []
    if verdict_from == 'draw_upgrade':
        parts.append(f'相似结构中 {fr["D"]*100:.0f}% 打平 (≥30% 阈值) → 判{cn}')
    else:
        parts.append(f'相似结构中 {fr[verdict]*100:.0f}% 为{cn} → 判{cn}')
    dm = fr['D'] - market['D']
    if draw_alert and verdict != 'D':
        parts.append(f'平局风险偏高 (历史{fr["D"]*100:.0f}% vs 市场{market["D"]*100:.0f}%)')
    elif verdict == 'D' and dm > 0:
        parts.append(f'市场平赔偏高 {dm*100:+.0f}pp, 平局被低估')
    if open_missing:
        parts.append('无初盘数据, 仅静态结构匹配, 把握度已下调')
    return ' · '.join(parts)


# ══════════════════════════════════════════════════════════════
# 从 events.db 自动抽取某场的【初盘 + 临盘】(严格开赛前)
# ══════════════════════════════════════════════════════════════
GQ_DB = os.environ.get('SHAOXIANG_GQ_DB', r'D:\Architecture\data\events.db')


def parse_kickoff(ko: Optional[str]) -> Optional[float]:
    """把 events.db 的 kickoff 文本 (两种格式) 解析为 Unix epoch 浮点。

    已观测格式:
      '2026-07-15 03:00'          (空格分隔, 无秒)
      '2026-07-14T15:59:08Z'      (ISO-8601, 带 Z)
    解析失败返回 None (调用方据此决定是否能做开赛前过滤)。
    """
    if not ko:
        return None
    s = ko.strip().replace('Z', '').replace('T', ' ')
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
        try:
            return datetime.datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    return None


def fetch_gq_prematch_odds(match_key: str) -> dict:
    """取某场比赛的初盘(最早完整帧) + 临盘(开赛前最后一帧)。

    铁律: captured_at 必须 < kickoff(epoch)。开赛后的快照一律不取 —— 那已含
    比分信息, 属于滚球盘, 混进来会直接污染赛前结构匹配。

    修复 (2026-08-08):
      · kickoff 文本 → epoch 数值比较, 否则 REAL<TEXT 类型排序让 < 永远为真,
        等于没做开赛前过滤 (会混入滚球盘)。
      · home/draw/away 采集时 captured_at 相差 ~4ms, 不能用精确相等拼同一帧。
        改为按 round(captured_at) 做秒级分桶, 桶内凑齐 H/D/A 才算一帧。

    返回 {ok, open, close, status, kickoff, home, away, league, snapshots_used,
          kickoff_parsed}
    取不到就 ok=False 并说明原因, 绝不用滚球盘顶替。
    """
    out = {'ok': False, 'match_key': match_key}
    try:
        c = sqlite3.connect(GQ_DB)
        c.row_factory = sqlite3.Row
        m = c.execute(
            'select match_key, home, away, league, kickoff, status '
            'from matches where match_key=?', (match_key,)
        ).fetchone()
        if not m:
            c.close()
            out['reason'] = 'match_not_found'
            return out
        out.update({'home': m['home'], 'away': m['away'], 'league': m['league'],
                    'kickoff': m['kickoff'], 'status': m['status']})
        ko_epoch = parse_kickoff(m['kickoff'])
        out['kickoff_parsed'] = ko_epoch is not None
        if ko_epoch is None:
            # kickoff 缺失/无法解析: 无法保证开赛前过滤, 交回调用方, 不擅自取盘
            c.close()
            out['reason'] = 'no_kickoff'
            return out

        def _frames(order: str):
            """返回按顺序 (升/降) 的完整 1X2 帧列表, 每帧 {home,draw,away,ts}。
            严格 captured_at < kickoff_epoch (开赛前)。"""
            rows = c.execute(
                f"""
                select selection, odds, captured_at from odds_snapshots
                where match_key=? and market='1X2' and captured_at < ?
                order by captured_at {order}
                """, (match_key, ko_epoch)
            ).fetchall()
            frames, cur, bucket, ts0 = [], None, {}, None
            for r in rows:
                b = int(round(r['captured_at']))      # 秒级分桶(~4ms内为一帧)
                if cur is None:
                    cur = b
                    ts0 = r['captured_at']
                if b != cur:
                    if {'home', 'draw', 'away'} <= set(bucket):
                        frames.append({'home': bucket['home'], 'draw': bucket['draw'],
                                       'away': bucket['away'], 'ts': ts0})
                    cur, bucket, ts0 = b, {}, r['captured_at']
                bucket[r['selection']] = r['odds']
            if {'home', 'draw', 'away'} <= set(bucket):
                frames.append({'home': bucket['home'], 'draw': bucket['draw'],
                               'away': bucket['away'], 'ts': ts0})
            return frames

        asc = _frames('asc')          # 最早 → 初盘
        desc = _frames('desc')        # 最新 → 临盘
        c.close()

        if not desc:
            out['reason'] = 'no_prematch_1x2'
            out['note'] = ('该场无开赛前 1X2 快照 (可能只采到滚球盘或开赛后才有盘), '
                           '不用滚球顶替')
            return out

        op = (float(asc[0]['home']), float(asc[0]['draw']), float(asc[0]['away'])) if asc else None
        op_ts = asc[0]['ts'] if asc else None
        cl = (float(desc[0]['home']), float(desc[0]['draw']), float(desc[0]['away']))
        cl_ts = desc[0]['ts']
        out.update({'ok': True, 'open': op, 'close': cl,
                    'open_at': op_ts, 'close_at': cl_ts,
                    'frames_pre_kickoff': len(desc),
                    'snapshots_used': 'pre_kickoff_only'})
        return out
    except Exception as e:                       # pragma: no cover
        out['reason'] = f'error: {e}'
        return out


def _actual_result(match_key: str) -> Optional[dict]:
    """取某场真实赛果 (finished 才有), 用于回测对照。无则 None (不伪造)。"""
    try:
        c = sqlite3.connect(GQ_DB)
        c.row_factory = sqlite3.Row
        m = c.execute(
            'select score_home, score_away, status from matches where match_key=?',
            (match_key,)
        ).fetchone()
        c.close()
        if not m:
            return None
        sh, sa = m['score_home'], m['score_away']
        if sh is None or sa is None:
            return None
        res = 'H' if sh > sa else ('A' if sa > sh else 'D')
        return {'result': res, 'result_cn': LABEL_CN[res], 'score': f"{sh}-{sa}"}
    except Exception:
        return None


def query_match(match_key: str, k: int = DEFAULT_K,
                draw_upgrade: bool = True) -> dict:
    """按 GQ match_key 一站式检索: 自动取初盘+临盘 → 赛前结构结论。
    已结束比赛额外附真实赛果, 供"查询结果是平局就显示平局"回测对照。
    """
    src = fetch_gq_prematch_odds(match_key)
    if not src.get('ok'):
        return {'applicable': False, 'reason': src.get('reason', 'unknown'),
                'note': src.get('note', '无法获取该场赛前初盘/临盘赔率'),
                'match': {kk: src.get(kk) for kk in
                          ('match_key', 'home', 'away', 'league', 'kickoff', 'status')}}
    r = query_prematch(close_odds=src['close'], open_odds=src['open'], k=k,
                       league=src.get('league'), status=src.get('status'),
                       draw_upgrade=draw_upgrade)
    r['match'] = {kk: src.get(kk) for kk in
                  ('match_key', 'home', 'away', 'league', 'kickoff', 'status')}
    r['odds_used'] = {'open': src['open'], 'close': src['close'],
                      'open_at': src.get('open_at'), 'close_at': src.get('close_at')}

    # ── 已结束比赛: 真实赛果对照 (用户 2026-08-08 诉求: 平局就显示平局) ──
    if (src.get('status') or '').strip().lower() in ('finished', 'ft', 'ended'):
        act = _actual_result(match_key)
        if act:
            r['actual_result'] = act['result']
            r['actual_score'] = act['score']
            r['verdict_hit'] = (r.get('verdict') == act['result'])
            r['verify_note'] = (f"赛前模型判 {r.get('verdict_cn')}, 实际 {act['score']} "
                                f"({act['result_cn']}) → "
                                f"{'✓命中' if r['verdict_hit'] else '✗未中'}")
            r['note'] = (r.get('note', '') + ' · 已结束比赛基于赛前初盘临盘回测, '
                        '非实时预测').strip(' ·')
    return r


# ══════════════════════════════════════════════════════════════
# 兼容旧接口 (analysis_center.query_neighbors 平滑替换)
# ══════════════════════════════════════════════════════════════
def neighbors_compat(oh, od, oa, open_odds=None, k: int = DEFAULT_K,
                     status: Optional[str] = None) -> Optional[dict]:
    """返回旧 query_neighbors 的形状 {count,home,draw,away,top}, 额外附带
    verdict/roi/draw_alert, 供前端逐步迁移。不适用时返回 None。"""
    r = query_prematch((oh, od, oa), open_odds=open_odds, k=k, status=status)
    if not r.get('applicable'):
        return None
    kk = r['sample']
    return {
        'count': kk,
        'home': int(round(r['freq']['H'] * kk)),
        'draw': int(round(r['freq']['D'] * kk)),
        'away': int(round(r['freq']['A'] * kk)),
        'top': [{'home': s['home'], 'away': s['away'], 'result': s['result'],
                 'score': s['score'], 'sim': round(1.0 - min(s['dist'] * 50, 1.0), 4),
                 'league': s['league']} for s in r['samples'][:3]],
        # 新增: 唯一结论
        'verdict': r['verdict'], 'verdict_cn': r['verdict_cn'],
        'roi': r['roi'], 'draw_alert': r['draw_alert'],
        'excess': r['excess'], 'confidence_tier': r['confidence_tier'],
        'note': r['note'],
    }


if __name__ == '__main__':
    import json, sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    print(json.dumps(library_stats(), ensure_ascii=False, indent=2))
    demo = query_prematch(close_odds=(2.10, 3.30, 3.60), open_odds=(2.25, 3.20, 3.40))
    print(json.dumps(demo, ensure_ascii=False, indent=2)[:2000])
