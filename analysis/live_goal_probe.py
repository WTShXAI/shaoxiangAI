"""
Live Goal Probe — 滚球破蛋概率仪 (规则 + 数据校准)

核心假设(已由 GQ 历史数据实证):
- OU 水位差 Δ>=0.20 时, 低水方为庄家真实站队方向 (半场 OU 0.5 低水 over 命中率 ~75-87%)。
- 时间压力: 上半场越往后, 0-0 局面下破蛋概率越高。
- 热门深盘 (1X2 fav odds 低) 攻势更强, 破蛋概率更高。
- 大球/小球赔率快速下降 (>=1% 每分钟) 是盘中动量信号。

输出: 半场破蛋概率、全场破蛋概率、信号方向、置信度、理由。
"""
import os, sqlite3, json, re, math, time, threading
from datetime import datetime
from collections import defaultdict, OrderedDict
import numpy as np

# ── 滚球神器 v2 平局模块 (初盘1X2去水p_d + 类型识别, 全量AUC=0.57/0.579) ──
# 双重导入兜底: 作为 analysis 包被 bridge 导入时走前者; 直接 python 执行时走后者。
try:
    from analysis.draw_module import predict_draw as _dm_predict_draw, classify_type as _dm_classify_type
except Exception:
    try:
        from draw_module import predict_draw as _dm_predict_draw, classify_type as _dm_classify_type
    except Exception:
        _dm_predict_draw = None
        _dm_classify_type = None

GQ = os.environ.get('GQ_DB', 'D:/Architecture/data/events.db')

def _open_gq(timeout: float = 5.0):
    """打开 events.db 读连接(根治列表 30s 超时)。

    根因: 原列表/探测连接用 `timeout=30`, collector 高频写 events.db 时 WAL checkpoint
    会短暂阻塞读, 连接最多等 30s → 正好命中前端 30s 超时(timeout of 30000ms exceeded)。
    修复:
      - timeout=5: 即使遇锁最多等 5s, 远在 30s 前端超时内;
      - wal_autocheckpoint=0: 读连接永不自行触发 checkpoint, 避免读被自身 checkpoint 阻塞;
      - busy_timeout=3000: 拿锁重试而非直接报 locked。
    """
    c = sqlite3.connect(GQ, timeout=timeout)
    try:
        c.execute('PRAGMA busy_timeout=3000')
        c.execute('PRAGMA wal_autocheckpoint=0')
    except Exception:
        pass
    return c

# ── 线程安全 LRU + TTL 缓存 (probe_match 重查 6GB events.db 的护栏) ──
# 前端滚球焦点轮 3s 高频重复请求同一场, 短期内 score/minute 不变, 命中缓存可免反复
# 连 6GB 库 + 扫 odds_snapshots 大表。to_thread 把阻塞移出事件循环, 但重查询仍会占满
# 线程池导致单场延迟飙升; 缓存是关键性能层。minute 变化(进球)后旧条目自然过期。
_PROBE_CACHE = OrderedDict()
_PROBE_CACHE_LOCK = threading.Lock()
_PROBE_CACHE_TTL = 20.0   # 秒: 滚球分钟级变化, 20s 足够新鲜又避免重复重查
_PROBE_CACHE_MAX = 512

def _probe_cache_get(key):
    now = time.time()
    with _PROBE_CACHE_LOCK:
        if key in _PROBE_CACHE:
            val, ts = _PROBE_CACHE[key]
            if now - ts <= _PROBE_CACHE_TTL:
                _PROBE_CACHE.move_to_end(key)
                return val
            _PROBE_CACHE.pop(key, None)
    return None

def _probe_cache_put(key, val):
    now = time.time()
    with _PROBE_CACHE_LOCK:
        _PROBE_CACHE[key] = (val, now)
        _PROBE_CACHE.move_to_end(key)
        while len(_PROBE_CACHE) > _PROBE_CACHE_MAX:
            _PROBE_CACHE.popitem(last=False)

# ── 校准参数(来自历史回测, 会由 recalibrate() 更新) ──
CALIBRATION = {
    'ht_base_break_rate': 0.35,        # 联赛平均半场破蛋率
    'ft_base_break_rate': 0.72,        # 联赛平均全场破蛋率(0.5 line)
    'delta_threshold_weak': 0.10,      # (已废弃, 保留兼容) 原原始赔率差阈值
    'delta_threshold_strong': 0.20,    # (已废弃) 判定一律用 pgap_*
    'pgap_weak': 0.015,                # 去水概率偏离 |p-0.5| 弱信号阈值 (≈1.90/2.00)
    'pgap_strong': 0.03,               # 强信号阈值 (≈1.85/2.05 及以上)
    'over_drop_threshold': 0.01,       # 1 分钟内大球赔率下降 1%
    'fav_odds_deep': 1.40,             # (已废弃, 保留兼容) 原深盘热门原始赔率阈值
    'p_fav_deep': 0.65,                # 深盘热门去水概率阈值 (抗诱导: 替代 fav_odds_deep)
}


def load_calibration():
    path = os.path.join(os.path.dirname(__file__), 'live_goal_probe_calibration.json')
    if os.path.exists(path):
        with open(path) as f:
            CALIBRATION.update(json.load(f))
    return CALIBRATION


# ── 赛前→半场破蛋先验模型 (scripts/train_ht_break_model.py, AUC 0.6546) ──
# 懒加载。用途: OU_1H 盘口缺失时(部分 obscure 联赛不开半场盘)代替旧手工规则基线;
# 以及为 scheduled 场提供赛前破蛋先验。比赛中概率仍以盘口去水锚为主(市场最校准)。
_HT_MODEL = {'loaded': False, 'bundle': None}
_HT_MODEL_PATH = os.path.join(os.path.dirname(__file__), '..', 'models', 'ht_break_model.joblib')

def _load_ht_model():
    if not _HT_MODEL['loaded']:
        _HT_MODEL['loaded'] = True
        try:
            import joblib
            if os.path.exists(_HT_MODEL_PATH):
                _HT_MODEL['bundle'] = joblib.load(_HT_MODEL_PATH)
        except Exception:
            _HT_MODEL['bundle'] = None
    return _HT_MODEL['bundle']


def _ht_model_predict(odds, league, con=None, match_key=None, current_minute=0):
    """v2 抗诱导特征(全不变量, 无原始赔率值): 去水概率 + 开盘→临场漂移 + 联赛先验。
    漂移特征需要 con/match_key 查开盘快照; 无则 drift=0(诚实, 不伪造漂移)。"""
    b = _load_ht_model()
    if b is None:
        return None
    try:
        # 全场 OU pairs (临场=最新快照)
        ou_pairs = []
        for key in odds:
            if key.startswith('OU_') and not key.startswith('OU_1H') and not key.startswith('OU_2H') and key.endswith('__over'):
                lk = key[:-6]
                try:
                    # market 形如 OU_2.00 / OU_1H_1.25, 线值总在最后一段
                    line = float(lk.split('_')[-1])
                except Exception:
                    continue
                ov, un = odds.get(f'{lk}__over'), odds.get(f'{lk}__under')
                if ov and un:
                    ou_pairs.append((line, ov, un))
        T = _implied_total_from_pairs(ou_pairs)
        x2 = _dewater_1x2(odds.get('1X2__home'), odds.get('1X2__draw'), odds.get('1X2__away'))
        if T is None or x2 is None or not ou_pairs:
            return None
        main = min(ou_pairs, key=lambda p: abs(p[0] - 2.5))
        p_over_main = _dewatered_over_prob(main[1], main[2]) or 0.5
        p_fav = max(x2)  # 去水后热门概率 (替代原始 fav_odds)

        # 开盘→临场漂移 (抗诱导核心: 水平可伪装, 方向难伪装)
        T_drift = p_over_drift = x2h_drift = 0.0
        if con is not None and match_key is not None:
            try:
                cur = con.cursor()
                ko = cur.execute("SELECT kickoff FROM matches WHERE match_key=?", (match_key,)).fetchone()
                kots = _parse_kickoff(ko[0]) if (ko and ko[0]) else None
                open_rows = cur.execute("""
                    SELECT CAST(REPLACE(REPLACE(market,'OU_',''),'_','.') AS REAL) AS line,
                           selection, odds
                    FROM odds_snapshots
                    WHERE match_key=? AND market LIKE 'OU_%'
                      AND market NOT LIKE 'OU_1H%' AND market NOT LIKE 'OU_2H%'
                      AND captured_at < ?
                    ORDER BY captured_at ASC LIMIT 40
                """, (match_key, (kots + 300) if kots else 1e18)).fetchall()  # 仅开赛前快照当开盘
                from collections import defaultdict as _dd
                d = _dd(dict)
                for ln, sel, od in open_rows:
                    if ln is not None:
                        d[ln][sel] = od
                opairs = [(L, v.get('over'), v.get('under')) for L, v in d.items()
                          if v.get('over') and v.get('under')]
                T_o = _implied_total_from_pairs(opairs)
                if T_o is not None:
                    T_drift = T - T_o
                if opairs:
                    main_o = min(opairs, key=lambda p: abs(p[0] - 2.5))
                    p_over_o = _dewatered_over_prob(main_o[1], main_o[2])
                    if p_over_o:
                        p_over_drift = math.log(max(p_over_main, 0.01) / max(p_over_o, 0.01))
                x2o_rows = cur.execute("""
                    SELECT selection, odds FROM odds_snapshots
                    WHERE match_key=? AND market='1X2' AND odds > 1.01 AND odds <= 1000
                      AND captured_at < ?
                    ORDER BY captured_at ASC LIMIT 3
                """, (match_key, (kots + 300) if kots else 1e18)).fetchall()  # 仅开赛前快照当开盘
                x2o = {}
                for sel, od in x2o_rows:
                    if sel not in x2o:
                        x2o[sel] = od
                if all(k in x2o for k in ('home', 'draw', 'away')):
                    x2o_dw = _dewater_1x2(x2o['home'], x2o['draw'], x2o['away'])
                    if x2o_dw:
                        x2h_drift = x2[0] - x2o_dw[0]
            except Exception:
                pass  # 漂移查询失败则保持 0, 不影响主特征

        lg_prior = b.get('lg_prior', {})
        lg_rate = lg_prior.get(league or 'unknown', b.get('global_rate', 0.7154))
        X = np.array([[T, p_over_main, x2[0], x2[1], x2[2], p_fav,
                       T_drift, p_over_drift, x2h_drift, lg_rate]], dtype=float)
        X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
        if b['kind'] == 'poisson_isotonic':
            base = 1.0 - math.exp(-max(T, 0.05) * b.get('ht_share', 0.45))
        else:
            base = float(b['model'].predict_proba(X)[0, 1])
        return float(b['iso'].predict([base])[0])
    except Exception:
        return None


def sigmoid(x):
    return 1.0 / (1.0 + math.exp(-x))


# 盘口快照查询 TTL 缓存: 同一场比赛盘口数据短时间不变, 前端高频轮询(分钟随挂钟走字,
# 导致 probe_match 的 LRU 缓存 key 含 int(minute) 永远不命中)时, 底层 odds 查询可反复命中,
# 避免每次重扫 odds_snapshots(2026-08-21 诊断: 无索引+35 market 全扫导致单次 probe 7.5s,
# 线程池被前端 5s 轮询打满 -> matches/分钟流/探测全 HTTP:000 超时)。
_odds_snap_cache = {}
_odds_snap_cache_ttl = 5.0
_odds_snap_cache_lock = None


def get_latest_snapshot_odds(con, match_key, markets):
    """获取一场比赛多个 market 的最新赔率(带 TTL 缓存, key 含 markets 组合)。"""
    import threading
    global _odds_snap_cache_lock
    if _odds_snap_cache_lock is None:
        _odds_snap_cache_lock = threading.Lock()
    ck = (match_key, tuple(markets))
    now = time.time()
    with _odds_snap_cache_lock:
        cached = _odds_snap_cache.get(ck)
        if cached is not None and now - cached[0] < _odds_snap_cache_ttl:
            return cached[1]
    cur = con.cursor()
    res = {}
    for mkt in markets:
        # 2026-08-28: 最新 5 帧指数加权 (α=0.6, 越近权重越高) 替代"单帧最新",
        # 去 WS 单帧抖动/诱盘闪动噪声; 帧 = 同 captured_at 的 (selection->odds) 组.
        # 2026-08-28 修根源: 总权重用 5 帧, 但 over/under 出现帧不同时(如 GQ 推流 over 帧1, under 帧2-4),
        # 总权重错配导致 under 被稀释丢失 → OU 推算全返回 None. 修: per-key 维护自身总权重.
        if mkt.startswith('OU_') or mkt.startswith('AH_'):
            # 2026-08-28 v2 流内自洽配对: 采集流把同一条线的 over/under 拆进后缀市场名
            # (OU_2.00 只有 over, under 在 OU_2.00_2), 精确名查询永远配不成对 → probe 判
            # "无全场 OU"退 0.72 基线, 而 DB 里实时盘明明在更新。
            # 但盲目按前缀合并会跨子盘假配对(实测 OU_2.50 赛前残 over 5.5 × OU_2.50_11
            # 变体盘 under 1.07 → 假 p_over=16%)。故按"流内自洽": 每个市场名(含后缀)
            # 独立做指数加权配对, over+under 齐全才算有效流; 同线值多流取最新帧最活跃者。
            req_line = _extract_line_from_market(mkt)
            rows = cur.execute(r"""
                SELECT market, selection, odds, captured_at FROM odds_snapshots
                WHERE match_key=? AND (market=? OR market LIKE ? ESCAPE '\')
                  AND odds>? AND odds<?
                ORDER BY captured_at DESC LIMIT 400
            """, (match_key, mkt, mkt + r'\_%', 1.01, 1000.0)).fetchall()
            # 按市场名(流)分组, 流内按帧做指数加权
            streams: dict = {}
            for smkt, sel, odds, ts in rows:
                streams.setdefault(smkt, []).append((ts, sel, odds))
            _sel_pair = ('home', 'away') if mkt.startswith('AH_') else ('over', 'under')
            best = None  # (latest_ts, pair_dict)
            for smkt, srows in streams.items():
                frames: dict = {}
                for ts, sel, odds in srows:
                    frames.setdefault(ts, {})[sel] = odds
                fkeys = sorted(frames.keys(), reverse=True)[:5]
                per_key: dict = {}
                for i, ts in enumerate(fkeys):
                    w = 0.6 ** i
                    for sel, odds in frames[ts].items():
                        per_key.setdefault(sel, []).append((w, odds))
                agg = {sel: sum(w * o for w, o in wts) / sum(w for w, _ in wts)
                       for sel, wts in per_key.items()}
                # 流内自洽: 两个方向都存在才有效(单边流是残盘/变体盘, 不配对)
                if not (_sel_pair[0] in agg and _sel_pair[1] in agg):
                    continue
                # 线值一致性: 后缀流解析出的线值须与请求市场一致(OU_2.50_11 线值≠2.5 的变体)
                s_line = _extract_line_from_market(smkt)
                if req_line is not None and s_line is not None and abs(s_line - req_line) > 1e-6:
                    continue
                cand = (fkeys[0], agg)
                if best is None or cand[0] > best[0]:
                    best = cand
            if best is not None:
                for sel, odds in best[1].items():
                    res[f"{mkt}__{sel}"] = round(odds, 4)  # 消指数加权浮点长尾(1.9337499...)
        else:
            rows = cur.execute("""
                SELECT selection, odds, captured_at FROM odds_snapshots
                WHERE match_key=? AND market=? AND odds>? AND odds<?
                ORDER BY captured_at DESC LIMIT 40
            """, (match_key, mkt, 1.01, 1000.0)).fetchall()
            frames = {}
            for sel, odds, ts in rows:
                frames.setdefault(ts, {})[sel] = odds
            frame_keys = sorted(frames.keys(), reverse=True)[:5]
            per_key = {}
            for i, ts in enumerate(frame_keys):
                w = 0.6 ** i
                for sel, odds in frames[ts].items():
                    per_key.setdefault(sel, []).append((w, odds))
            for key, wts in per_key.items():
                num = sum(w * o for w, o in wts)
                den = sum(w for w, _ in wts)
                # 2026-08-30 修: 此前写 res[key] (key='home'/'draw'/'away'),
                # 导致 predict_fulltime_outcome 用 odds.get('1X2__home') 拿不到,
                # 误报"1X2 盘口缺失"。补 mkt__ 前缀与 OU/AH 分支保持一致。
                res[f"{mkt}__{key}"] = round(num / den, 4)
    with _odds_snap_cache_lock:
        _odds_snap_cache[ck] = (now, res)
    return res


def get_recent_ou_changes(con, match_key, line_key='OU_1H_0.50', window_sec=120):
    """获取 OU 盘口最近 window_sec 秒内的 over/under 变化。"""
    cur = con.cursor()
    now = datetime.now().timestamp()
    rows = cur.execute("""
        SELECT captured_at, selection, odds FROM odds_snapshots
        WHERE match_key=? AND market=? AND selection IN ('over','under')
          AND odds>? AND odds<? AND captured_at>?
        ORDER BY captured_at DESC
    """, (match_key, line_key, 1.01, 1000.0, now - window_sec)).fetchall()
    if not rows:
        return {}
    # 取最新和 window 前最早各一个
    by_sel = defaultdict(list)
    for cap, sel, odds in rows:
        by_sel[sel].append((cap, odds))
    out = {}
    for sel in ['over', 'under']:
        pts = by_sel.get(sel, [])
        if len(pts) >= 2:
            latest = pts[-1][1]
            earliest = pts[0][1]
            out[f'{sel}_change'] = (latest - earliest) / earliest
            out[f'{sel}_latest'] = latest
        elif pts:
            out[f'{sel}_change'] = 0.0
            out[f'{sel}_latest'] = pts[-1][1]
        else:
            out[f'{sel}_change'] = 0.0
            out[f'{sel}_latest'] = None
    return out




def _parse_kickoff(s):
    """把 matches.kickoff (naive GMT+8) 解析为 Unix 时间戳。"""
    if not s:
        return None
    from datetime import datetime, timezone, timedelta
    try:
        dt = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
        return dt.timestamp()
    except Exception:
        return None


HALFTIME_BREAK_MIN = 15.0   # 标准中场休息时长(分钟)


def resolve_true_minute(kickoff, feed_minute=None, now_ts=None):
    """真实比赛分钟解析(SSoT) — 2026-08-21 重大修复。

    ⚠ 背景(实测铁证, 2026-08-21 23:30 抽样):
      乐鱼 feed 的 minute **整个上半场恒报 45, 整个下半场恒报 90**, 是占位垃圾:
        kickoff=23:15(真实15′) → feed_minute=45
        kickoff=23:00(真实30′) → feed_minute=45
        kickoff=22:30(真实60′) → feed_minute=90
      events.db 全库验证: odds_snapshots 带 minute_at>0 的 454,115 条里 443,585 条
      (97.7%) 是 45 或 90; matches.minute 同样污染(live 中 27 场=45 / 8 场=90)。
      → feed minute 不可作时间基准; kickoff 与 captured_at/now 是真实时钟, 可信。

    策略: kickoff 精算 elapsed 为主, feed 的 45/90 仅当"半场标识"用(粗粒度但可靠),
          feed>90 的真实递增值(补时)直接采信。

    返回 {minute, phase, is_halftime, source, elapsed}
      phase: 'pre'|'first'|'ht'|'second'|'et'|'unknown'
      source: 'kickoff'|'kickoff+feed_half'|'feed_extra'|'feed_only'
    """
    now_ts = now_ts if now_ts is not None else time.time()
    kots = _parse_kickoff(kickoff)
    try:
        fm = int(feed_minute) if feed_minute is not None else None
    except Exception:
        fm = None

    # 补时区: feed 报 >90 时是真实递增值(实测 91/92/93/95...117 均为真值), 直接采信
    if fm is not None and fm > 90:
        return {'minute': fm, 'phase': 'et', 'is_halftime': False,
                'source': 'feed_extra',
                'elapsed': (now_ts - kots) / 60.0 if kots else None}

    if kots is None:
        # 无 kickoff: 只能退回 feed, 明确标注不可信
        return {'minute': fm or 0, 'phase': 'unknown',
                'is_halftime': bool(fm and 45 <= fm < 47),
                'source': 'feed_only', 'elapsed': None}

    elapsed = (now_ts - kots) / 60.0
    if elapsed < 0:
        return {'minute': 0, 'phase': 'pre', 'is_halftime': False,
                'source': 'kickoff', 'elapsed': elapsed}

    # feed 半场标识: 45=上半场进行中, 90=下半场进行中(仅作 phase 提示, 不作分钟)
    feed_half = 1 if fm == 45 else (2 if fm == 90 else None)

    if feed_half == 2:
        minute = max(46.0, elapsed - HALFTIME_BREAK_MIN)
        return {'minute': int(round(min(minute, 90.0))), 'phase': 'second',
                'is_halftime': False, 'source': 'kickoff+feed_half', 'elapsed': elapsed}
    if feed_half == 1:
        return {'minute': int(round(min(elapsed, 45.0))), 'phase': 'first',
                'is_halftime': False, 'source': 'kickoff+feed_half', 'elapsed': elapsed}

    # feed 无半场标识 → 纯 kickoff 推算
    if elapsed <= 45:
        return {'minute': int(round(elapsed)), 'phase': 'first', 'is_halftime': False,
                'source': 'kickoff', 'elapsed': elapsed}
    if elapsed <= 45 + HALFTIME_BREAK_MIN:
        return {'minute': 45, 'phase': 'ht', 'is_halftime': True,
                'source': 'kickoff', 'elapsed': elapsed}
    return {'minute': int(round(min(elapsed - HALFTIME_BREAK_MIN, 90.0))), 'phase': 'second',
            'is_halftime': False, 'source': 'kickoff', 'elapsed': elapsed}


def latest_ou_snapshot_phase(con, match_key, kickoff=None):
    """判定 probe 将读到的最新 OU 快照属**赛前**还是**滚球**, 供 remaining_break_prob
    选择 λ_rem 口径(赛前需 ×rem_ratio, 滚球用 λ-G)。

    ⚠ 判定基准用 captured_at(真实墙钟) 而非 minute_at —— 后者 97.7% 是 45/90 占位污染。
    返回 ('prematch'|'live', snap_offset_min or None)。
    """
    try:
        cur = con.cursor()
        if kickoff is None:
            r = cur.execute("SELECT kickoff FROM matches WHERE match_key=?", (match_key,)).fetchone()
            kickoff = r[0] if r else None
        kots = _parse_kickoff(kickoff)
        row = cur.execute("""
            SELECT MAX(captured_at) FROM odds_snapshots
            WHERE match_key=? AND market LIKE 'OU_%'
              AND market NOT LIKE 'OU_2H%' AND odds>1.01 AND odds<1000
        """, (match_key,)).fetchone()
        cap = row[0] if row else None
        if cap is None or kots is None:
            return 'prematch', None
        # 快照晚于开赛 60s 以上 → 滚球盘(庄家已随时间调线); 否则赛前盘
        if cap > kots + 60:
            return 'live', (cap - kots) / 60.0
        return 'prematch', (cap - kots) / 60.0
    except Exception:
        return 'prematch', None


def _dewatered_over_prob(over_odds, under_odds):
    if not over_odds or not under_odds or over_odds <= 1.01 or under_odds <= 1.01:
        return None
    a, b = 1.0 / over_odds, 1.0 / under_odds
    s = a + b
    if s <= 0:
        return None
    return a / s  # P(goals > L), margin-free


def _ok_ou_line_value(line) -> bool:
    """足球大小球线合法性: 全场 0.5~10.0 (含滚球降线)。0.25 步进。

    2026-08-28 根因修复(锚点污染): 采集流里混有非足球 OU 盘(角球/组合盘, 实测
    OU_18.00 / OU_194.50), 混进 _open_total_from_snapshots / _build_ou_trajectory 后
    算出 open_total=87.39 → fixed P(大2.5)=0.948 的荒谬值。所有按线聚合处必须过滤。
    半场线(1H 上限~3.5)同样落在该区间, 无需单独上限。
    """
    try:
        v = float(line)
    except (TypeError, ValueError):
        return False
    return 0.5 <= v <= 10.0


def _implied_total_from_pairs(pairs):
    """pairs: list of (line, over_odds, under_odds) -> 隐含总球线 T (P>L=0.5 处)。"""
    pts = []
    for line, o, u in pairs:
        p = _dewatered_over_prob(o, u)
        if p is None:
            continue
        pts.append((line, p))
    if not pts:
        return None
    pts.sort()
    for i in range(len(pts) - 1):
        l0, p0 = pts[i]
        l1, p1 = pts[i + 1]
        if (p0 - 0.5) * (p1 - 0.5) <= 0 and p0 != p1:
            frac = (0.5 - p0) / (p1 - p0)
            return l0 + frac * (l1 - l0)

    # ── 无穿越点 → 外推 (2026-08-21 重大修复: 原两个回退分支方向完全写反) ──
    # 定义: P(over@L) 随 L 升高单调递减。P>0.5 ⇒ 隐含总球 T>L; P<0.5 ⇒ T<L。
    # 旧代码: 所有点 P<0.5 时返回 `最高线+0.25`(把 T 抬到线之上), 所有点 P>0.5 时返回
    # `最低线-0.25`(把 T 压到线之下) —— 两者都反了。
    # 实测危害(塔什干棉农 vs 铁尔米兹 35.6′ 0-0): 庄家挂 OU_1H_0.5 P(over)=0.389、
    # OU_1H_0.75 P(over)=0.296(明确看小), 旧逻辑却算出 T=1.00 → 半场剩 9min 却给出
    # λ_rem=2.0 / P(破蛋)=0.875。正确外推 T≈0.20。
    # 改为按相邻两点斜率线性外推, 并夹在物理合理区间。
    if pts[0][1] < 0.5:
        # 连最低线都 P<0.5 → T 低于最低线
        if len(pts) >= 2 and pts[1][1] != pts[0][1]:
            slope = (pts[1][0] - pts[0][0]) / (pts[1][1] - pts[0][1])   # dL/dP (<0)
            t = pts[0][0] + slope * (0.5 - pts[0][1])
        else:
            t = pts[0][0] - 0.25
        return max(0.05, min(t, pts[0][0]))
    if pts[-1][1] > 0.5:
        # 连最高线都 P>0.5 → T 高于最高线
        if len(pts) >= 2 and pts[-1][1] != pts[-2][1]:
            slope = (pts[-1][0] - pts[-2][0]) / (pts[-1][1] - pts[-2][1])
            t = pts[-1][0] + slope * (0.5 - pts[-1][1])
        else:
            t = pts[-1][0] + 0.25
        return min(8.0, max(t, pts[-1][0]))
    return pts[len(pts) // 2][0]


def _open_total_from_snapshots(con, match_key, prefix='OU_', exclude_prefixes=None, ref_line=2.5):
    """开盘锚唯一真相源 (2026-08-20 根因修复).

    返回 (open_line, open_T): 该 prefix 下 **开赛前** (captured_at < kickoff + 5min宽限)
    最早快照的盘口线(最接近 ref_line)与去水隐含总球. 每条线只取最早一次快照(ORDER BY
    captured_at ASC, 首次出现即开盘价).

    无真实开盘价(该场所有 OU 快照都在开赛 5min 后 → 实为活盘价)时返回 (None, None),
    调用方须诚实降级(anchor_used=False / 不使用伪造开盘), 绝不拿进球后赔率当开盘。
    """
    if con is None or not match_key:
        return None, None
    cur = con.cursor()
    ko = cur.execute("SELECT kickoff FROM matches WHERE match_key=?", (match_key,)).fetchone()
    if not ko or not ko[0]:
        return None, None
    kots = _parse_kickoff(ko[0])
    if kots is None:
        return None, None
    cap = kots + 300  # 开赛前 5 分钟宽限(采集器延迟)
    floor_ts = kots - 172800  # 2026-08-28: 只认赛前 48h 内的"初盘", 远古残盘(多庄换盘)不算开盘
    if exclude_prefixes:
        not_like = ' AND '.join(f"market NOT LIKE '{p}%'" for p in exclude_prefixes)
        rows = cur.execute(f"""
            SELECT market, selection, odds, minute_at FROM odds_snapshots
            WHERE match_key=? AND market LIKE ? AND {not_like}
              AND market != 'OU' AND captured_at < ? AND captured_at > ?
            ORDER BY captured_at ASC LIMIT 2000
        """, (match_key, prefix + '%', cap, floor_ts)).fetchall()
    else:
        rows = cur.execute("""
            SELECT market, selection, odds, minute_at FROM odds_snapshots
            WHERE match_key=? AND market LIKE ? AND market != 'OU'
              AND captured_at < ? AND captured_at > ?
            ORDER BY captured_at ASC LIMIT 2000
        """, (match_key, prefix + '%', cap, floor_ts)).fetchall()
    # 2026-08-28 流内自洽: 开盘配对必须来自同一条流(市场名), 否则 OU_2.50 残盘 over ×
    # OU_2.50_11 变体盘 under 跨流假配对污染 open_total。每条线只取最早(开盘)快照。
    # 2026-08-29 修复 (Bug-2 深根因, 莫斯科斯巴达U19 vs 罗迪那U19 实测):
    #   GQ 采集器会把半场/终场盘 (minute_at=45/90/112) 的 captured_at 打成**早于**真开盘帧
    #   (minute_at=0) —— 实测 minute_at=45 的 OU_3.50 captured_at=14:00:31 早于 minute_at=0 的
    #   OU_3.75 captured_at=14:03:37, 导致按 captured_at ASC 取"最早"时把半场残盘 3.50 当开盘价
    #   (真开盘是 3.75), 漂移方向整体反号("上修0.50" 实为 "下修0.25")。
    #   修正: 优先只用 minute_at=0 的开盘帧构建候选; 该批次无可用配对才回退全量(行为不退化)。
    def _collect(rowset):
        _d = {}  # stream_market -> {'line': float, 'over': ..., 'under': ...}
        for item in rowset:
            mkt, sel, odds = item[0], item[1], item[2]
            if odds is None or odds <= 1.01 or odds > 1000.0:
                continue
            line = _extract_line_from_market(mkt)
            if line is None:
                continue
            if not _ok_ou_line_value(line):
                continue  # 2026-08-28: 角球/组合盘(OU_18.00 等)污染开盘锚过滤
            s = _d.setdefault(mkt, {'line': line})
            if sel not in s:   # 每条线只取最早(开盘)快照
                s[sel] = odds
        return [(s['line'], s.get('over'), s.get('under')) for s in _d.values()
                if s.get('over') and s.get('under')]

    # 动态开盘批 (2026-08-29 补): 严格 minute_at==0 太窄 —— 实测部分比赛真主盘
    # (OU_2.50) 在 minute_at=1 才出现, minute_at=0 只有小线(OU_0.50 变体盘),
    # 会把开盘线算成 0.5。改为: 取该场最小 minute_at 作为"开盘帧分钟", 宽限 +1 分钟。
    # 若最小 minute_at > 5 → 该场根本没有开赛前后快照(全是半场/滚球残盘),
    # 不切批, 直接回退全量(原行为, 零回归)。
    _mas = [int(r[3] or 0) for r in rows if len(r) > 3]
    min_ma = min(_mas) if _mas else None
    rows_zero = []
    if min_ma is not None and min_ma <= 5:
        rows_zero = [r for r in rows if int(r[3] or 0) <= min_ma + 1]
    cand = _collect(rows_zero) if rows_zero else []
    if not cand:
        cand = _collect(rows)   # 回退全量(原行为, 零回归)
    if not cand:
        return None, None
    cand.sort(key=lambda x: abs(x[0] - ref_line))
    open_line, ov, un = cand[0]
    T = _implied_total_from_pairs(cand)
    # 2026-08-28 健全性闸门: 多庄早期混价插值可能给出荒谬 T(实测 0.5 → fixed P(大2.5)=0.016)。
    # 赛前开盘最低线 ~1.5, 隐含总球不可能 <1.0; 上限 8(进球大战上限)。超界 → 诚实降级
    # 为"无真实开盘", 绝不让坏锚流向 fixed/remaining_break/漂移对比。
    if T is not None and not (1.0 <= T <= 8.0):
        return None, None
    return open_line, T


def _dewater_1x2(h, d, a):
    """1X2 去水, 返回 [p_home, p_draw, p_away] (margin-free)。"""
    if not (h and d and a):
        return None
    try:
        inv = [1.0 / h, 1.0 / d, 1.0 / a]
    except Exception:
        return None
    s = sum(inv)
    if s <= 0:
        return None
    return [x / s for x in inv]


def _open_1x2_from_snapshots(con, match_key):
    """开盘(开赛前) 1X2 三方向赔率 (home/draw/away), kickoff 闸门。
    每个方向只取最早(开盘)快照。无真实开盘返回 (None,None,None)。"""
    if con is None or not match_key:
        return None, None, None
    cur = con.cursor()
    ko = cur.execute("SELECT kickoff FROM matches WHERE match_key=?", (match_key,)).fetchone()
    if not ko or not ko[0]:
        return None, None, None
    kots = _parse_kickoff(ko[0])
    if kots is None:
        return None, None, None
    cap = kots + 300
    rows = cur.execute("""
        SELECT selection, odds FROM odds_snapshots
        WHERE match_key=? AND market='1X2' AND captured_at < ?
        ORDER BY captured_at ASC
    """, (match_key, cap)).fetchall()
    d = {}
    for sel, odds in rows:
        if odds is None or odds <= 1.01 or odds > 1000.0:
            continue
        if sel not in d:   # 每个方向只取最早(开盘)
            d[sel] = odds
    if 'home' in d and 'draw' in d and 'away' in d:
        return d['home'], d['draw'], d['away']
    # 2026-08-28 fallback: obscure 联赛 1X2 最早快照可能晚于 kickoff+300s (实测米克斯克这场迟 22min),
    # 5min 闸门拿不到 h/d/a 时不限时间取最早帧补全, 保证 score_hint / analyze 能产出。
    if not ('home' in d and 'draw' in d and 'away' in d):
        rows_fb = cur.execute(
            "SELECT selection, odds FROM odds_snapshots "
            "WHERE match_key=? AND market='1X2' ORDER BY captured_at ASC",
            (match_key,),
        ).fetchall()
        for sel, odds in rows_fb:
            if odds is None or odds <= 1.01 or odds > 1000.0:
                continue
            if sel not in d:
                d[sel] = odds
        if 'home' in d and 'draw' in d and 'away' in d:
            return d['home'], d['draw'], d['away']
    return None, None, None


def _open_ah_from_snapshots(con, match_key):
    """开盘(开赛前) 主让球盘(AH, 全场, 排除1H/2H), 取线值最接近0(最均衡)的一条。
    返回 (line, home_odds, away_odds)。无真实开盘返回 (None,None,None)。"""
    if con is None or not match_key:
        return None, None, None
    cur = con.cursor()
    ko = cur.execute("SELECT kickoff FROM matches WHERE match_key=?", (match_key,)).fetchone()
    if not ko or not ko[0]:
        return None, None, None
    kots = _parse_kickoff(ko[0])
    if kots is None:
        return None, None, None
    cap = kots + 300
    rows = cur.execute("""
        SELECT market, selection, odds FROM odds_snapshots
        WHERE match_key=? AND market LIKE 'AH_%'
          AND market NOT LIKE 'AH_1H%' AND market NOT LIKE 'AH_2H%'
          AND selection IN ('home','away') AND captured_at < ?
        ORDER BY captured_at ASC
    """, (match_key, cap)).fetchall()
    d = {}
    for mkt, sel, odds in rows:
        if odds is None or odds <= 1.01 or odds > 1000.0:
            continue
        line = _extract_line_from_market(mkt)
        if line is None:
            continue
        if line not in d:
            d[line] = {}
        if sel not in d[line]:   # 每条线只取最早(开盘)
            d[line][sel] = odds
    cands = [(abs(L), L, v.get('home'), v.get('away')) for L, v in d.items()
             if v.get('home') and v.get('away')]
    if not cands:
        return None, None, None
    cands.sort()
    _, line, h, a = cands[0]
    return line, h, a


def _inplay_cap_ts(con, match_key, minute):
    """滚球帧【真实时基】上限: kickoff_ts + elapsed(minute)*60。

    ⚠ 全库铁证 (2026-08-29 实测, 莫斯科斯巴达U19 vs 罗迪那U19 3-1 暴露):
      events.db 878 万条 minute_at>0 快照里 **543 万条 (61.8%) 卡死在 45/90**
      (45 独占 432 万 = 49.2%) —— 乐鱼 feed 整个上半场恒报 45、整个下半场恒报 90,
      是占位垃圾(见 resolve_true_minute docstring, 2026-08-21 已记录, 但漏了这条
      消费链: 所有 `WHERE minute_at<=? ORDER BY minute_at DESC` 的查询)。
      后果: 排序键恒定 → 退化成 `ORDER BY id DESC`, **恒取最后写入的终场残盘**,
      等于拿已知终局倒推预测 —— 致命信息泄漏, 违反 IR-06/IR-30。

    修法: kickoff 与 captured_at 是真实时钟。用 kickoff_ts + elapsed*60 换算
          captured_at 上限, 把查询收窄到该分钟真实对应的墙钟窗口。
          elapsed 口径与 resolve_true_minute 一致 (中场休息 15 分钟):
            minute <= 45 → elapsed = minute
            minute >  45 → elapsed = minute + HALFTIME_BREAK_MIN

    返回 float 时间戳, 或 None (无法推算 → 调用方回退原逻辑, 保证零回归)。
    """
    try:
        m = int(minute or 0)
    except Exception:
        return None
    if m <= 0 or con is None or not match_key:
        return None
    try:
        row = con.execute(
            "SELECT kickoff FROM matches WHERE match_key=? LIMIT 1", (match_key,)
        ).fetchone()
    except Exception:
        return None
    if not row or not row[0]:
        return None
    kots = _parse_kickoff(row[0])
    if not kots:
        return None
    elapsed = float(m) if m <= 45 else (float(m) + HALFTIME_BREAK_MIN)
    return kots + elapsed * 60.0


def _current_inplay_odds(con, match_key, minute):
    """取该场滚球(in-play)【当前】盘口: 1X2 home/draw/away + 主 OU 线 over/under/line。

    用于决策智能体消费「滚球盘信号」(live 模型推理)。
    取 minute_at <= minute 的最新 in-play 快照; 若该分钟前无盘, 回退该场【最早】in-play 快照。

    2026-08-29 Bug-3 修复 (莫斯科斯巴达U19 vs 罗迪那U19 3-1 实测):
      原 fallback 用 ORDER BY minute_at **DESC** —— 当 minute=57 无快照时取到 minute=112
      **终场残盘** (1X2 1.01/4.87/8.22 主胜已定, OU_4.0 over=4.0), 等于拿已知终局倒推
      预测, 是致命的信息泄漏。改为 ORDER BY minute_at **ASC** 取最早的 in-play 帧
      (最接近开赛, 已发生的进球信息最少 → 泄漏最小); 语义上仍诚实标注为回退值。

    2026-08-29 Bug-5 修复 (同场次二次暴露, 全库 61.8% 污染):
      主查询的 `minute_at<=? ORDER BY minute_at DESC` 在 minute_at 卡死 45/90 的场次
      **完全失效** —— 排序键恒定, 实际退化成 ORDER BY id DESC, 恒取终场帧。
      实测: minute=45 与 minute=88 返回**同一帧** (1.28/4.89/8.70), minute=116 返回
      (1.01/11.42/41.0)。修法: ① 追加 `captured_at <= _inplay_cap_ts()` 真实时基
      过滤; ② 排序键加 captured_at DESC 二级键(对 minute_at 正常的场次零变化);
      ③ cap 后无帧则回退不加 cap 的原逻辑, 保证零回归。

    返回 {'x2':(h,d,a), 'ou':(line,over,under)} 或 None。
    """
    if con is None or not match_key:
        return None
    cur = con.cursor()
    cap_ts = _inplay_cap_ts(con, match_key, minute)

    def _q1x2(use_cap):
        sql = ("SELECT selection, odds FROM odds_snapshots WHERE match_key=? AND market='1X2' "
               "AND minute_at>0 AND minute_at<=?")
        ps = [match_key, max(1, minute)]
        if use_cap and cap_ts:
            sql += " AND captured_at<=?"
            ps.append(cap_ts)
        # Bug-5: captured_at 二级键 —— minute_at 卡死时仍按真实时钟取最新
        sql += " ORDER BY minute_at DESC, captured_at DESC, id DESC LIMIT 3"
        return cur.execute(sql, tuple(ps)).fetchall()

    rows = _q1x2(True)
    if not rows:
        rows = _q1x2(False)   # cap 后无帧(kickoff 缺失/时钟漂移) → 回退, 零回归
    if not rows:
        # Bug-3: 取最早 in-play 帧 (ASC), 绝不取终场残盘 (原为 DESC)
        rows = cur.execute(
            "SELECT selection, odds FROM odds_snapshots WHERE match_key=? AND market='1X2' AND minute_at>0 ORDER BY minute_at ASC, id ASC LIMIT 3",
            (match_key,)).fetchall()
    x2 = {}
    for sel, odds in rows:
        if odds is None or odds <= 1.01 or odds > 1000.0:
            continue
        if sel not in x2:
            x2[sel] = odds

    def _qou(use_cap):
        sql = ("SELECT market, selection, odds, line FROM odds_snapshots "
               "WHERE match_key=? AND market LIKE 'OU_%' AND market NOT LIKE 'OU_1H%' "
               "AND market NOT LIKE 'OU_2H%' AND selection IN ('over','under') "
               "AND minute_at>0 AND minute_at<=?")
        ps = [match_key, max(1, minute)]
        if use_cap and cap_ts:
            sql += " AND captured_at<=?"
            ps.append(cap_ts)
        sql += " ORDER BY minute_at DESC, captured_at DESC, id DESC"
        return cur.execute(sql, tuple(ps)).fetchall()

    ou_rows = _qou(True)
    if not ou_rows:
        ou_rows = _qou(False)   # Bug-5: 同上, cap 后无帧回退原逻辑
    if not ou_rows:
        # Bug-3: 取最早 in-play 帧 (ASC), 绝不取终场残盘 (原为 DESC)
        ou_rows = cur.execute(
            "SELECT market, selection, odds, line FROM odds_snapshots "
            "WHERE match_key=? AND market LIKE 'OU_%' AND market NOT LIKE 'OU_1H%' AND market NOT LIKE 'OU_2H%' "
            "AND selection IN ('over','under') AND minute_at>0 ORDER BY minute_at ASC, id ASC",
            (match_key,)).fetchall()
    ous = {}
    for mkt, sel, odds, line in ou_rows:
        if odds is None or odds <= 1.01 or odds > 1000.0 or line is None:
            continue
        ous.setdefault(line, {})[sel] = odds
    cand = [(abs(L - 2.5), L) for L, v in ous.items()
            if v.get('over') and v.get('under') and _ok_ou_line_value(L)]
    best_line = None
    if cand:
        cand.sort()
        best_line = cand[0][1]
    out = {}
    if 'home' in x2 and 'draw' in x2 and 'away' in x2:
        out['x2'] = (x2['home'], x2['draw'], x2['away'])
    if best_line is not None:
        out['ou'] = (best_line, ous[best_line]['over'], ous[best_line]['under'])
    return out if out else None


def _current_inplay_ah_odds(con, match_key, minute):
    """取该场滚球(in-play)【当前】主让球盘(AH, 全场, 排除1H/2H) 线值最接近0的一条。

    返回 (line, home_odds, away_odds) 或 None。用于「市场 AH↔1X2 一致性」对照,
    与开盘 AH(_open_ah_from_snapshots) 不同: 这里取 minute_at>0 的 live 快照。

    2026-08-29 Bug-3/Bug-5 修复: 与 _current_inplay_odds 同款两个 bug ——
      ① 原 fallback 用 ORDER BY minute_at DESC → 取终场残盘(信息泄漏), 改 ASC 取最早帧;
      ② 主查询 minute_at<=? ORDER BY minute_at DESC 在 minute_at 卡死 45/90 的
         61.8% 场次上退化为 id DESC → 恒取终场帧, 加 captured_at 真实时基上限过滤
         + captured_at DESC 二级排序键; cap 后无帧回退原逻辑, 零回归。
    """
    if con is None or not match_key:
        return None
    cur = con.cursor()
    cap_ts = _inplay_cap_ts(con, match_key, minute)

    def _qah(use_cap):
        sql = ("SELECT market, selection, odds FROM odds_snapshots "
               "WHERE match_key=? AND market LIKE 'AH_%' "
               "AND market NOT LIKE 'AH_1H%' AND market NOT LIKE 'AH_2H%' "
               "AND selection IN ('home','away') AND minute_at>0 AND minute_at<=?")
        ps = [match_key, max(1, minute)]
        if use_cap and cap_ts:
            sql += " AND captured_at<=?"
            ps.append(cap_ts)
        sql += " ORDER BY minute_at DESC, captured_at DESC, id DESC"
        return cur.execute(sql, tuple(ps)).fetchall()

    rows = _qah(True)
    if not rows:
        rows = _qah(False)
    if not rows:
        # Bug-3: 取最早 in-play 帧 (ASC), 绝不取终场残盘 (原为 DESC)
        rows = cur.execute(
            "SELECT market, selection, odds FROM odds_snapshots "
            "WHERE match_key=? AND market LIKE 'AH_%' "
            "AND market NOT LIKE 'AH_1H%' AND market NOT LIKE 'AH_2H%' "
            "AND selection IN ('home','away') AND minute_at>0 "
            "ORDER BY minute_at ASC, id ASC",
            (match_key,)).fetchall()
    d = {}
    for mkt, sel, odds in rows:
        if odds is None or odds <= 1.01 or odds > 1000.0:
            continue
        line = _extract_line_from_market(mkt)
        if line is None:
            continue
        d.setdefault(line, {})[sel] = odds   # 每条线取最新(live)
    cand = [(abs(L), L) for L, v in d.items() if v.get('home') and v.get('away')]
    if not cand:
        return None
    cand.sort()
    best_line = cand[0][1]
    return best_line, d[best_line]['home'], d[best_line]['away']


def _reverse_poisson_total(p_home, p_draw, p_away, maxg=12):
    """由去水胜平负概率反推独立泊松对抗的总进球期望 T=lh+la 与 (lh,la)。
    网格搜索匹配 _match_probs。返回 (T, lh, la) 或 None。
    T 搜索范围 [0.5, 12.0] (覆盖大球盘 5.0+ 高总球比赛, 避免 1X2翻译总球撞天花板)。"""
    best = None
    for T in [i * 0.1 for i in range(5, 121)]:           # T in [0.5, 12.0]
        for r in [0.35 + i * 0.02 for i in range(16)]:  # home 占比 [0.35, 0.65]
            lh = T * r
            la = T * (1 - r)
            mp = _match_probs(lh, la, maxg)
            if mp is None:
                continue
            err = (mp[0] - p_home) ** 2 + (mp[1] - p_draw) ** 2 + (mp[2] - p_away) ** 2
            if best is None or err < best[0]:
                best = (err, T, lh, la)
    if best is None:
        return None
    return best[1], best[2], best[3]


def get_opening_structure_diagnosis(con, match_key):
    """Per-match 开盘结构诊断档案 (破蛋 + 赛果, 该场特异, 非群体结论).

    提取该场 kickoff 前开盘 OU / OU_1H / 1X2 / AH 四件套(去水), 计算跨盘口隐含总球残差
    (structure_residual = OU隐含总球 − 1X2泊松翻译总球) 与早破蛋残差 (ht_ft_residual =
    2×半场OU隐含 − 全场OU隐含). 这些残差是该场庄家对"进球/早进球"的特异看法, 而非
    "高盘场平均77%破蛋"的群体规律.

    赛果侧输出该场 1X2 去水胜平负 + AH 让球方 + 两盘口一致性(该场 favorite 是否自洽).

    无真实开盘价时 has_real_open=False, 字段为 None, 不伪造.
    """
    if con is None or not match_key:
        return {'has_real_open': False}
    # 开盘四件套 (kickoff 闸门, 仅开赛前最早快照)
    ou_line, ou_T = _open_total_from_snapshots(con, match_key, 'OU_', exclude_prefixes=['OU_1H', 'OU_2H'], ref_line=2.5)
    ou1h_line, ou1h_T = _open_total_from_snapshots(con, match_key, 'OU_1H_', ref_line=1.5)
    h, d, a = _open_1x2_from_snapshots(con, match_key)
    ah_line, ah_h, ah_a = _open_ah_from_snapshots(con, match_key)

    x2 = _dewater_1x2(h, d, a)
    if ah_h and ah_a:
        ah_inv = [1.0 / ah_h, 1.0 / ah_a]
        ah_s = sum(ah_inv)
        ah_p_home = ah_inv[0] / ah_s
        ah_p_away = ah_inv[1] / ah_s
    else:
        ah_p_home = ah_p_away = None

    has_open = (ou_T is not None) or (x2 is not None) or (ah_p_home is not None)
    out = {'has_real_open': has_open, 'breakegg': None, 'result': None,
           'verdict': '初盘缺失(以当前盘口为准)'}
    if not has_open:
        return out

    # ── 破蛋侧 ──
    be = {}
    if ou_T is not None:
        be['ou_open_line'] = round(ou_line, 2) if ou_line is not None else None
        be['ou_implied_total'] = round(ou_T, 3)
    if ou1h_T is not None:
        be['ou1h_implied_total'] = round(ou1h_T, 3)
    if x2 is not None:
        rev = _reverse_poisson_total(x2[0], x2[1], x2[2])
        if rev is not None:
            T_x2, lh, la = rev
            be['x2_implied_total'] = round(T_x2, 3)
            if ou_T is not None:
                be['structure_residual'] = round(ou_T - T_x2, 3)
            out['result'] = {
                'p_home': round(x2[0], 3), 'p_draw': round(x2[1], 3), 'p_away': round(x2[2], 3),
                'fav_1x2': 'home' if x2[0] >= x2[2] else 'away',
            }
    if ou1h_T is not None and ou_T is not None:
        be['ht_ft_residual'] = round(2 * ou1h_T - ou_T, 3)

    # 破蛋倾向判定 (该场特异, 非群体平均)
    # 诚实边界: structure_residual 只在 OU 开盘线 <= 3.5 的良采样区可靠;
    # OU>=4.0 样本极小且 1X2 开盘快照常缺, 残差常撞 1X2 翻译天花板变垃圾值,
    # 此时只保留原始数值, 不输出方向判定 (避免假精确).
    OU_VERDICT_CEIL = 3.5
    p_fav_x2 = max(x2[0], x2[2]) if x2 else 0.0
    heavy_favorite = p_fav_x2 >= 0.60                  # 去水热门概率>=60% 视为深盘热门
    if be.get('structure_residual') is not None and ou_line is not None and ou_line <= OU_VERDICT_CEIL:
        sr = be['structure_residual']
        if heavy_favorite:
            # 2026-08-20: 重 favorites 场残差方向含义翻转 —— 市场因热门偏见会系统性高估进球,
            # 实证: OU2.0/1X2翻译3.4球(sr=-1.27) 终场4-1走大; 大3@1.87 终场1-0走小。
            if sr >= 0.20:
                be['break_tendency'] = 'bearish_goals'
            elif sr <= -0.20:
                be['break_tendency'] = 'bullish_goals'
            else:
                be['break_tendency'] = 'neutral'
            be['residual_verdict_status'] = 'ok_flipped_heavy_favorite'
            be['heavy_favorite_flip'] = True
        else:
            if sr >= 0.20:
                be['break_tendency'] = 'bullish_goals'      # OU比1X2翻译多开>=0.2球, 该场庄家特异看好进球
            elif sr <= -0.20:
                be['break_tendency'] = 'bearish_goals'
            else:
                be['break_tendency'] = 'neutral'
            be['residual_verdict_status'] = 'ok'
    elif be.get('structure_residual') is not None:
        be['break_tendency'] = None                     # OU>=4.0: 样本不足, 残差仅作原始参考, 不判方向
        be['residual_verdict_status'] = 'sample_insufficient'
    else:
        be['residual_verdict_status'] = 'no_1x2_open'   # 缺 1X2 开盘快照, 无法泊松翻译
    if be:
        out['breakegg'] = be

    # ── 赛果侧 AH 一致性 ──
    if out['result'] is not None and ah_p_home is not None:
        ah_fav = 'home' if ah_p_home > ah_p_away else 'away'
        ah_fav_prob = max(ah_p_home, ah_p_away)
        p_fav_x2 = max(x2[0], x2[2])
        out['result']['ah_line'] = round(ah_line, 2) if ah_line is not None else None
        out['result']['ah_fav_side'] = ah_fav
        out['result']['ah_fav_prob'] = round(ah_fav_prob, 3)
        out['result']['fav_consistency'] = 'consistent' if ah_fav == out['result']['fav_1x2'] else 'inconsistent'
        out['result']['fav_edge_gap'] = round(abs(p_fav_x2 - ah_fav_prob), 3)  # 两盘口对favorite定价差

    # ── 该场 verdict (逐场特异信号汇总, 不是群体结论) ──
    parts = []
    sr = be.get('structure_residual')
    flipped = be.get('heavy_favorite_flip') is True
    if be.get('break_tendency') == 'bullish_goals':
        if flipped:
            parts.append(f"重 favorites 场方向翻转: OU隐含比1X2泊松翻译少{-sr:.2f}球, 但 market favorite 偏见常高估进球 → 实际倾向大球")
        else:
            parts.append(f"市场分歧: OU隐含比1X2泊松翻译多开{sr:+.2f}球(OU相对1X2更看好进球)")
    elif be.get('break_tendency') == 'bearish_goals':
        if flipped:
            parts.append(f"重 favorites 场方向翻转: OU隐含比1X2泊松翻译多开{sr:+.2f}球, 但 market favorite 偏见常高估进球 → 实际倾向小球")
        else:
            parts.append(f"市场分歧: OU隐含比1X2泊松翻译少{-sr:.2f}球(OU相对1X2更保守)")
    if be.get('residual_verdict_status') == 'sample_insufficient':
        parts.append(f"OU开盘线{ou_line:.2f}>=4.0样本不足, 结构残差仅作原始参考(不判方向)")
    if be.get('ht_ft_residual') is not None and be['ht_ft_residual'] >= 0.30:
        parts.append(f"半场OU偏高→早破蛋倾向(2×半场{2 * ou1h_T:.2f} vs 全场{ou_T:.2f})")
    if out['result'] and out['result'].get('fav_consistency') == 'inconsistent':
        parts.append(f"1X2与AH让球方不一致(1X2看好{out['result']['fav_1x2']}, AH看好{out['result']['ah_fav_side']})")
    out['verdict'] = '; '.join(parts) if parts else '开盘结构自洽, 无特异信号'
    return out


def _poisson_pmf(k, lam):
    if k < 0:
        return 0.0
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    try:
        return math.exp(-lam) * (lam ** k) / math.factorial(k)
    except OverflowError:
        return 0.0


def _match_probs(lh, la, maxg=8):
    """两个独立泊松对抗的 主胜/平/客胜 概率。"""
    ph = pd_ = pa = 0.0
    for i in range(maxg + 1):
        pi = _poisson_pmf(i, lh)
        if pi == 0:
            break
        for j in range(maxg + 1):
            pj = _poisson_pmf(j, la)
            if pj == 0:
                break
            p = pi * pj
            if i > j:
                ph += p
            elif i == j:
                pd_ += p
            else:
                pa += p
    return ph, pd_, pa


AH_OPENING_ANCHOR_WEIGHT = 1.0  # 2026-08-20: 与 OU 同权重(开盘 AH 去水 P 是最优校准基准)


def predict_fulltime_outcome(odds, current_score='0-0', current_minute=0, con=None, match_key=None, league=None):
    """终场方向读数: 跟随庄家去水隐含概率 (live 1X2 + OU 隐含总球 + AH 让球 + 当前比分状态),
    非找 edge。用户洞察: 盘口结构本身高度校准(AUC 0.73), 直接跟随即'终场读数'——
    这正是哨响模型缺失的维度(之前只破蛋/大小, 没读'比赛结果')。"""
    sh = sa = 0
    if current_score:
        m = re.match(r"(\d+)\s*[-:]\s*(\d+)", current_score.strip())
        if m:
            sh, sa = int(m.group(1)), int(m.group(2))

    x2 = _dewater_1x2(odds.get('1X2__home'), odds.get('1X2__draw'), odds.get('1X2__away'))

    # OU 隐含总球
    ou_pairs = []
    for key in odds:
        if key.startswith('OU_') and not key.startswith('OU_1H') and not key.startswith('OU_2H') and key.endswith('__over'):
            lk = key[:-6]
            try:
                # 兼容 OU_2.00 / OU_1H_1.25, 线值总在最后一段
                line = float(lk.split('_')[-1])
            except Exception:
                line = 2.5
            ov = odds.get(f'{lk}__over')
            un = odds.get(f'{lk}__under')
            if ov and un:
                ou_pairs.append((line, ov, un))
    implied_total = _implied_total_from_pairs(ou_pairs)

    # AH 让球方向
    ah_dir = None
    ah_line = None
    ah_fav_odds = None
    ah_dog_odds = None
    for key in odds:
        if key.startswith('AH_') and key.endswith('__home'):
            lk = key[:-6]
            try:
                # 兼容 AH_0.00 / AH_-0.25, 线值总在最后一段
                ah_line = float(lk.split('_')[-1])
            except Exception:
                ah_line = 0.0
            hodds = odds.get(f'{lk}__home')
            aodds = odds.get(f'{lk}__away')
            if hodds and aodds:
                ah_dir = 'home' if hodds < aodds else 'away'
                ah_fav_odds = min(hodds, aodds)
                ah_dog_odds = max(hodds, aodds)
            break

    # AH 开盘锚 (2026-08-20): 用开盘去水 P(让球方覆盖) 拉住临场 AH 因进球过激的波动
    opening_ah = None
    if con is not None and match_key is not None:
        try:
            opening_ah = get_ah_open_and_current(con, match_key, current_minute)
        except Exception:
            opening_ah = None
    opening_ah_block = _build_ah_opening_anchor(opening_ah, ah_line, ah_fav_odds, ah_dog_odds)

    reasons = []
    odds_momentum = None   # 赔率动量弱特征(信息字段, 不进硬判定); 仅 OU 块后计算
    if x2 is None:
        return {
            'direction': None, 'confidence': None,
            'expected_total': round(implied_total, 2) if implied_total else None,
            'expected_score': None, 'expected_home_goals': None, 'expected_away_goals': None,
            'ah_side': ah_dir, 'ah_opening_anchor': opening_ah_block,
            'reasons': ['1X2 盘口缺失, 无法判定终场方向'],
            'note': '跟随庄家去水隐含概率(盘口即真相), 命中率≈去水概率, 非 edge。',
        }

    current_total = sh + sa
    # 剩余总球期望: 优先用 OU 隐含总球 - 已发生; 否则按剩余时间比例兜底
    base_total = implied_total if implied_total else 2.5
    if implied_total is None:
        reasons.append('OU 盘口缺失, 总球用时间比例估算')
    T_remain = base_total - current_total
    if T_remain < 0.3:
        # OU 线未跟上已进球 或 比赛末段: 用剩余时间比例兜底(避免预期比分<当前)
        T_remain = max(0.3, 2.5 * max(0, (90 - current_minute)) / 90.0)
    T_remain = max(0.0, T_remain)

    # ── IR-07 动态 λ(t) 校准 (2026-08-24 诊断 1-4/87' 根因修复) ──
    # 赛前静态队 λ 由 1X2 去水反推; 当已有进球时, 以当前比分+剩余时间做贝叶斯收缩后验,
    # 替代"赛前静态 λ 在比赛中继续输出"(客队已进 4 球但 λ=0.37 失真)。
    _static_lh = _static_la = None
    if x2 is not None:
        _rev = _reverse_poisson_total(x2[0], x2[1], x2[2])
        if _rev is not None:
            _, _static_lh, _static_la = _rev
    _score_known = (sh + sa) > 0
    _dyn_note = None
    _consistency_flag = None
    _static_flag = None
    if _score_known and _static_lh is not None and _static_la is not None:
        from analysis.inplay_calibration import (
            dynamic_team_lambda, simulate_inplay_1x2,
            isotonic_calibrate_1x2, lambda_consistency_flag,
        )
        # 原始赛前 λ 与比分一致性告警(诊断 IR-07: 客队进≥3但λ<0.5 → 原模型失真, 已动态修正)
        _static_flag = lambda_consistency_flag(_static_lh, _static_la, sh, sa)
        _hp, _ap, _rem = dynamic_team_lambda(_static_lh, _static_la, sh, sa, current_minute)
        _ph, _pd, _pa = simulate_inplay_1x2(_hp, _ap, sh, sa, current_minute)
        # IR-19 等渗校准仅用于「概率未定」区间; 近确定 in-play 状态(某方≥0.9, 比分已定)
        # 不应被赛前 opening 偏差校准裁剪下拉(否则 1-4@87' 客胜被压到 0.6, 失真)。
        _cal_note = None
        if max(_ph, _pd, _pa) < 0.9:
            _ph, _pd, _pa, _cal_note = isotonic_calibrate_1x2(_ph, _pd, _pa, league)
        if _cal_note:
            _dyn_note = _cal_note
        _consistency_flag = lambda_consistency_flag(_hp, _ap, sh, sa)
        lh, la = _hp * _rem, _ap * _rem   # 剩余进球期望(后验 λ(t))
        idx = int(np.argmax([_ph, _pd, _pa]))
        direction = ['主胜', '平', '客胜'][idx]
        conf = [_ph, _pd, _pa][idx]
        eh = sh + round(lh)
        ea = sa + round(la)
    else:
        # 0-0: 后验≈静态, 保持原网格搜索拟合静态 1X2 (行为不退化)
        best = None
        for ri in range(10, 91):
            r = ri / 100.0
            lh, la = T_remain * r, T_remain * (1 - r)
            ph, pd_, pa = _match_probs(lh, la)
            d = (ph - x2[0]) ** 2 + (pd_ - x2[1]) ** 2 + (pa - x2[2]) ** 2
            if best is None or d < best[0]:
                best = (d, lh, la, ph, pd_, pa)
        lh, la = best[1], best[2]  # 剩余进球期望
        idx = int(np.argmax(x2))
        direction = ['主胜', '平', '客胜'][idx]
        conf = x2[idx]
        eh = sh + round(lh)
        ea = sa + round(la)
    # 强制终场方向与 direction 一致(避免"主胜但比分平/客胜"的展示矛盾)
    if direction == '主胜' and eh <= ea:
        eh = ea + 1
    elif direction == '客胜' and ea <= eh:
        ea = eh + 1
    elif direction == '平':
        rem = round((lh + la) / 2.0)
        eh, ea = sh + rem, sa + rem

    # ── IR-19 OU 方向等渗校准 (2026-08-24, 与 1X2 同口径) ──
    # OU 方向做比分条件化: 用 IR-07 后验剩余总球期望(lh+la) 的泊松尾算 P(终场总球>线),
    # 与 1X2 同样以"当前比分+剩余时间"为条件; 再对该动态 over 概率做等渗校准
    # (按赛前 opening 偏差训的 isotonic, 经典 favorite-longshot 修正)。
    # 近确定态(已超线 / 动态 p_over≥0.9)跳过校准, 保留比分决定的确定性。
    ou_direction = None
    ou_confidence = None
    ou_note = None
    if ou_pairs:
        from analysis.inplay_calibration import _poisson_pmf, isotonic_calibrate_ou
        line0 = sorted(ou_pairs, key=lambda p: abs(p[0] - 2.5))[0][0]
        need = line0 - (sh + sa)          # 还需进几球才 over
        remain_total = lh + la            # IR-07 后验剩余总球期望
        if need <= -1e-9:
            p_over = 1.0                   # 已超线 → over 已定
        elif remain_total <= 1e-9:
            p_over = 0.0                   # 无剩余进球期望且未超线 → under
        else:
            kmax = int(math.floor(need))
            p_le = sum(_poisson_pmf(k, remain_total) for k in range(0, kmax + 1))
            p_over = 1.0 - p_le
        if p_over < 0.9 and need > -1e-9:
            p_over_cal, ou_note = isotonic_calibrate_ou(p_over, league)
            p_over = p_over_cal
        ou_direction = '大' if p_over >= 0.5 else '小'
        ou_confidence = max(p_over, 1.0 - p_over)
        if ou_note:
            reasons.append(f'低级别联赛 OU 等渗校准: {ou_note}')

    # ── 赔率动量弱特征(报告 P2#1, 信息字段, 不进硬判定) ──
    # 盘口漂移兼具赔付管理与热度示警双重语义, 仅作展示, 绝不参与 direction/ou_direction 硬判定。
    if con is not None and match_key is not None and ou_pairs:
        try:
            odds_momentum = _ou_momentum_for_match(con, match_key, line0, p_over)
        except Exception:
            odds_momentum = None

    # 比分状态 + 盘口一致性解读(用户核心诉求: 当前进程 + 全场盘口 结合)
    if sh != sa:
        leader = 'home' if sh > sa else 'away'
        leader_cn = '主' if leader == 'home' else '客'
        if (leader == 'home' and idx == 0) or (leader == 'away' and idx == 2):
            reasons.append(f'当前比分 {sh}-{sa} 领先方={leader_cn}, 与盘口看好方「{direction}」一致 → 趋势延续')
        else:
            rev = '客队' if idx == 2 else ('主队' if idx == 0 else '扳平')
            reasons.append(f'当前比分 {sh}-{sa} 领先方={leader_cn}, 但盘口看好「{direction}」 → 暗示{rev}逆转/追平')
    if _score_known and _static_lh is not None:
        reasons.append(f"IR-07 动态λ校准: 赛前λ 主{_static_lh:.2f}/客{_static_la:.2f} → 后验λ(t) 主{_hp:.2f}/客{_ap:.2f} (已进{sh}-{sa}@{int(current_minute)}') → 终场「{direction}」(置信{conf*100:.0f}%)")
        if _dyn_note:
            reasons.append(f'低级别联赛等渗校准: {_dyn_note}')
        if _static_flag:
            reasons.append(f'⚠ 原始赛前λ与比分背离(已用动态校准修正): {_static_flag}')
        elif _consistency_flag:
            reasons.append(f'⚠ λ一致性告警(概率输出不可信): {_consistency_flag}')
    else:
        reasons.append(f'1X2 去水概率 主 {x2[0]*100:.0f}% / 平 {x2[1]*100:.0f}% / 客 {x2[2]*100:.0f}% → 终场最可能「{direction}」(置信 {conf*100:.0f}%)')
    if implied_total is None:
        reasons = [r for r in reasons if not r.startswith('OU 隐含总球')]
        reasons.append('OU 盘口缺失, 无法估算终场总球/比分')
        return {
            'direction': direction,
            'confidence': round(conf, 3),
            'expected_total': None,
            'expected_score': None,
            'expected_home_goals': None,
            'expected_away_goals': None,
            'ah_side': ah_dir,
            'ah_opening_anchor': opening_ah_block,
            'ou_direction': ou_direction,
            'ou_confidence': (round(ou_confidence, 3) if ou_confidence is not None else None),
            'odds_momentum': odds_momentum,
            'reasons': reasons,
            'note': 'OU 盘口缺失, 无法估算终场总球/比分; 仅 1X2 方向只读。',
        }
    reasons.append(f'OU 隐含总球 {base_total:.2f} (已发生 {current_total}, 剩余期望 {T_remain:.2f}) → 泊松解得 主再进 {lh:.2f}/客再进 {la:.2f} (终场预期 {eh}-{ea})')
    if ah_dir:
        reasons.append(f'AH 让球方向: {"主让" if ah_dir=="home" else "客让"}{(" "+str(ah_line)) if ah_line else ""}')
    if ah_dir and ((ah_dir == 'home' and idx == 2) or (ah_dir == 'away' and idx == 0)):
        reasons.append('⚠ AH 让球方向与 1X2 去水方向不一致, 留意盘口分歧')

    return {
        'direction': direction,
        'confidence': round(conf, 3),
        'expected_total': round(base_total, 2),
        'expected_score': f'{eh}-{ea}',
        'expected_home_goals': round(sh + lh, 2),
        'expected_away_goals': round(sa + la, 2),
        'ah_side': ah_dir,
        'ah_opening_anchor': opening_ah_block,
        'ou_direction': ou_direction,
        'ou_confidence': (round(ou_confidence, 3) if ou_confidence is not None else None),
        'odds_momentum': odds_momentum,
        'reasons': reasons,
        'note': '跟随庄家去水隐含概率(盘口即真相), 命中率≈去水概率, 非 edge。这是哨响模型缺失的"终场读数"维度。',
    }


def _extract_line_from_market(mkt: str) -> float | None:
    """从市场名提取盘口线值。

    2026-08-28 修正: 采集流存在 <市场>_<流id> 后缀形态(OU_0.50_18 / AH_-0.50_4),
    旧实现取最后一段 → 'OU_0.50_18' 解析出 18.0(污染线值/锚点)。现优先取 OU/AH 后
    第一段数字(1H/2H 半场前缀跳过), 解析失败再回退最后一段:
      'OU_2.50'→2.5  'OU_1H_1.75'→1.75  'OU_0.50_18'→0.5  'AH_-0.50_4'→-0.5
    非足球线(角球盘 OU_18.00 等)由 _ok_ou_line_value 在聚合处过滤。
    """
    try:
        parts = str(mkt).split('_')
        # 找到 OU/AH 段的下一一段; 1H/2H 半场标记再顺延一位
        for i, p in enumerate(parts):
            if p in ('OU', 'AH'):
                j = i + 1
                if j < len(parts) and parts[j] in ('1H', '2H'):
                    j += 1
                if j < len(parts):
                    return float(parts[j])
        return float(parts[-1])
    except Exception:
        return None


def _build_ou_trajectory(con, match_key, market_prefix, ref_line, exclude_prefixes=None):
    """构建 OU 盘口轨迹: 返回按时间排序的 list[(t, line, implied_total)]。

    参数:
      market_prefix: 'OU_1H_' 或 'OU_'
      ref_line: 选参考线时最接近此值的优先(半场 1.5, 全场 2.5)
      exclude_prefixes: 排除前缀列表, 如全场需排除 ['OU_1H', 'OU_2H']

    修(2026-08-19): 全场前缀 'OU_' 会匹配裸市场 'OU'(无后缀的脏数据) 和
    滚球高线; ①裸 'OU' 在 _extract_line_from_market 解析失败已被跳过(安全),
    ②轨迹选线只在"该时刻存在的线"里选最接近 ref_line 的 — 滚球后期低线撤盘
    只剩高线属真实盘口变化, 不在此过滤(显示层标注锚点来源)。
    """
    cur = con.cursor()
    if exclude_prefixes:
        not_like = ' AND '.join(f"market NOT LIKE '{p}%'" for p in exclude_prefixes)
        rows = cur.execute(f"""
            SELECT market, selection, odds, captured_at FROM odds_snapshots
            WHERE match_key=? AND market LIKE ? AND {not_like}
              AND market != 'OU'
            ORDER BY captured_at
        """, (match_key, market_prefix + '%')).fetchall()
    else:
        rows = cur.execute("""
            SELECT market, selection, odds, captured_at FROM odds_snapshots
            WHERE match_key=? AND market LIKE ? AND market != 'OU'
            ORDER BY captured_at
        """, (match_key, market_prefix + '%')).fetchall()

    # 2026-08-28 流内自洽: 按市场名(流)分组, 同一秒的配对必须来自同一条流。
    # 否则 OU_2.50 赛前残 over 5.5 × OU_2.50_11 变体盘 under 1.07 会按线值 2.5 合并成
    # 假对 → implied_total 荒谬(实测污染)。角球/组合盘由 _ok_ou_line_value 过滤。
    by_sec = defaultdict(dict)  # int(t) -> stream_market -> {'over': odds, 'under': odds}
    for mkt, sel, odds, t in rows:
        if odds is None or odds <= 1.01 or odds > 1000.0:
            continue
        if _extract_line_from_market(mkt) is None:
            continue
        by_sec[int(t)].setdefault(mkt, {})[sel] = odds

    out = []
    for t in sorted(by_sec):
        cand = []
        for smkt, d in by_sec[t].items():
            line = _extract_line_from_market(smkt)
            if line is None or not _ok_ou_line_value(line):
                continue
            if d.get('over') and d.get('under'):
                cand.append((line, d.get('over'), d.get('under')))
        if not cand:
            continue
        cand.sort(key=lambda x: abs(x[0] - ref_line))
        line, ov, un = cand[0]
        p = _dewatered_over_prob(ov, un)
        if p is None:
            continue
        implied = line + (p - 0.5)
        out.append((t, line, implied))
    return out


def _r_mom(x):
    """四舍五入为 JSON 安全数值; None 透传。"""
    return None if x is None else round(float(x), 4)


def _ou_momentum_for_match(con, match_key, line, p_over_model=None):
    """赔率动量弱特征(报告 P2#1): 从 odds_snapshots 构造 OU over 去水概率时序(以比赛分钟为轴),
    计算滚动窗口(5/15/30min)斜率 + 加速度 + closing-line-value。

    纪律(IR-18/IR-04): 仅作**信息字段**, 绝不进硬判定; 盘口漂移兼具赔付管理与热度示警双重语义。
    无 in-play 快照(赛前/line 未定)→ 返回 None, 不伪造。

    Args:
      con: events.db 连接
      match_key: 比赛键
      line: OU 盘口线(如 2.5); None 则跳过(避免多线混算)
      p_over_model: 模型 over 概率(用于 CLV=模型−终盘去水); None 则 CLV=None
    """
    if line is None:
        return None
    try:
        cur = con.cursor()
        rows = cur.execute(
            """
            SELECT market, selection, odds, minute_at FROM odds_snapshots
            WHERE match_key=? AND market LIKE 'OU_%'
              AND market NOT LIKE 'OU_1H%' AND market NOT LIKE 'OU_2H%'
              AND market != 'OU'
              AND minute_at IS NOT NULL AND minute_at > 0
            ORDER BY minute_at
            """,
            (match_key,),
        ).fetchall()
    except Exception:
        return None
    if not rows:
        return None
    snaps = []
    for mkt, sel, odds, minute in rows:
        if _extract_line_from_market(mkt) is None:
            continue
        try:
            snaps.append((mkt, sel, float(odds), float(minute)))
        except Exception:
            continue
    if not snaps:
        return None
    from analysis.odds_momentum import momentum_features, closing_line_value, closing_devig_prob
    feat = momentum_features(snaps, line=line)
    if feat is None:
        return None
    clv = closing_line_value(p_over_model, closing_devig_prob(snaps, line=line))
    return {
        'line': line,
        'n_points': feat.get('n_points'),
        'p_first': _r_mom(feat.get('p_first')),
        'p_last': _r_mom(feat.get('p_last')),
        'last_minute': _r_mom(feat.get('last_minute')),
        'slope_5': _r_mom(feat.get('slope_5')),
        'slope_15': _r_mom(feat.get('slope_15')),
        'slope_30': _r_mom(feat.get('slope_30')),
        'accel': _r_mom(feat.get('accel')),
        'closing_line_value': _r_mom(clv),
    }


# 开盘 OU 线 → 半场破蛋(HT进≥1球)先验, 来自 events.db 7,353 场实证的 empirical 查表
# (2026-08-20 分析: 开盘OU线越高, 半场破蛋率单调 66.8%→86.3%; AUC~0.59 仅中等,
#  故开盘结构作"先验锚", 滚球实时状态才是真正判别器)
_HT_BREAKEGG_BY_OU = [
    (2.0, 0.668), (2.25, 0.692), (2.5, 0.726), (2.75, 0.731),
    (3.0, 0.738), (3.5, 0.770), (4.0, 0.786), (99.0, 0.863),
]
def opening_ht_breakegg_prior(open_ou_line):
    """给定全场开盘 OU 线, 返回半场破蛋(HT进≥1球)的 empirical 先验概率。
    无真实开盘价(open_ou_line=None)返回 None。"""
    if open_ou_line is None:
        return None
    for thr, p in _HT_BREAKEGG_BY_OU:
        if open_ou_line < thr:
            return round(p, 4)
    return 0.863


# ── 开盘锚-活盘线偏差信号 (2026-08-20 特罗姆瑟U19审计的直接模型化) ──
# 本场教训: 开盘 OU4.75(隐含总球4.8) → 半场后活盘3.25(隐含3.5), 收缩1.3球;
# 市场小球低水(1.76)被终场4球打脸。当跨时间收缩>1.0球, 活盘处于"过度收缩",
# 小球低水是赔付管理而非概率终点 → 大球方向存在模型-市场分歧。
OVERSHRINK_THRESHOLD = 1.0

def anchor_gap_signal(opening_total, current_total, minute=0, league=None):
    """开盘锚 vs 活盘隐含总球的收缩警报。

    Args:
      opening_total: 开盘去水隐含总球(开赛前快照)
      current_total: 当前活盘隐含总球
      minute: 当前比赛分钟(仅赛中生效)
      league: 联赛名(U19/青年联赛方差更大, 阈值可放宽)

    Returns:
      {gap, overshrink, direction, note} 或 None(数据不足)
    """
    if opening_total is None or current_total is None:
        return None
    if minute < 40:   # 仅半场后评估(半场前收缩多为赛前调盘, 非信号)
        return None
    gap = round(opening_total - current_total, 2)
    if gap < OVERSHRINK_THRESHOLD:
        return {'gap': gap, 'overshrink': False, 'direction': None,
                'note': f'锚差{gap}球<{OVERSHRINK_THRESHOLD}, 正常范围'}
    is_youth = league and any(k in str(league) for k in ('U19', 'U21', 'U23', '青年', '后备', '预备'))
    return {
        'gap': gap,
        'overshrink': True,
        'direction': 'OVER',
        'note': (f'过度收缩警报: 开盘锚{opening_total} vs 活盘{current_total}, 收缩{gap}球'
                 + ('; U19/青年联赛方差大' if is_youth else '')
                 + '; 活盘小球低水=赔付管理非概率终点, 大球方向存在模型-市场分歧'
                 + ('; 长补时场景进球窗口被低估' if is_youth else '')),
    }


def get_ou_drift_summary(con, match_key):
    """返回一场比赛 半场/全场 OU 的初盘→当前(滚盘)漂移摘要, 供前端卡片侧栏展示。

    开盘(初盘)严格取自 **开赛前** 快照(经 _open_total_from_snapshots 闸门),
    杜绝把进球后活盘价当"初盘"(2026-08-20 根因修复);
    当前(live)取滚盘轨迹最后一点(不闸门). 无真实开盘价时 has_real_open=False,
    前端标注"无真实开盘价", 不做虚假初滚对比。

    返回 dict:
      {
        'has_data': bool,
        'half': {'open_line','current_line','open_total','current_total','drift_line','drift_total','n_frames','has_real_open'},
        'full': {...},
        'verdict': str,   # '市场自降预期' / '市场升温' / '初滚一致' / '初盘缺失(以当前盘口为准)'
      }
    无数据时 has_data=False, 其余字段为 None。
    """
    if con is None or not match_key:
        return {'has_data': False}
    # 当前(live) 轨迹: 取最新快照 (不闸门, 这是"滚盘"实况)
    half_traj = _build_ou_trajectory(con, match_key, 'OU_1H_', 1.5)
    full_traj = _build_ou_trajectory(con, match_key, 'OU_', 2.5, exclude_prefixes=['OU_1H', 'OU_2H'])

    # 开盘(初盘, gated): 开赛前最早快照
    hl_open_line, hl_open_T = _open_total_from_snapshots(con, match_key, 'OU_1H_', ref_line=1.5)
    fl_open_line, fl_open_T = _open_total_from_snapshots(con, match_key, 'OU_', exclude_prefixes=['OU_1H', 'OU_2H'], ref_line=2.5)

    def summarize(open_line, open_T, traj):
        if traj is None or len(traj) == 0:
            return None
        t1, line1, total1 = traj[-1]   # 当前 = 轨迹最后一点(live)
        has_open = open_T is not None
        return {
            'open_line': round(open_line, 2) if open_line is not None else None,
            'current_line': round(line1, 2),
            'open_total': round(open_T, 2) if has_open else None,
            'current_total': round(total1, 2),
            'drift_line': round(line1 - open_line, 2) if open_line is not None else None,
            'drift_total': round(total1 - open_T, 2) if has_open else None,
            'n_frames': len(traj),
            'has_real_open': has_open,
        }

    half = summarize(hl_open_line, hl_open_T, half_traj)
    full = summarize(fl_open_line, fl_open_T, full_traj)
    if half is None and full is None:
        return {'has_data': False}

    # 克制结论: 初盘大但当前缩水 = '市场自降预期'; 初盘小但当前升温 = '市场升温'; 否则 '初滚一致'
    # 仅在有真实开盘价时才比较漂移(否则 drift_total=None, 不参与判定)。
    verdict = '初滚一致'
    hd = half.get('drift_total') if (half and half.get('has_real_open')) else None
    fd = full.get('drift_total') if (full and full.get('has_real_open')) else None
    if (hd is not None and hd <= -0.20) or (fd is not None and fd <= -0.20):
        verdict = '市场自降预期'
    elif (hd is not None and hd >= 0.20) or (fd is not None and fd >= 0.20):
        verdict = '市场升温'
    if half is None and full is not None and fd is not None:
        verdict = verdict if abs(fd) >= 0.20 else '初滚一致'
    if full is None and half is not None and hd is not None:
        verdict = verdict if abs(hd) >= 0.20 else '初滚一致'
    # 初盘缺失: 降级 verdict, 避免误导向"初滚一致"
    if (half and not half.get('has_real_open')) or (full and not full.get('has_real_open')):
        if verdict == '初滚一致':
            verdict = '初盘缺失(以当前盘口为准)'

    # 开盘结构 → 半场破蛋先验(实证查表, 透明不依赖模型文件)
    open_ou_for_prior = fl_open_line if (full and full.get('has_real_open')) else None
    ht_prior = opening_ht_breakegg_prior(open_ou_for_prior)

    return {
        'has_data': True,
        'half': half,
        'full': full,
        'verdict': verdict,
        'opening_ht_breakegg_prior': ht_prior,  # 半场破蛋先验: 开盘结构锚
    }


def get_ou_open_and_current_total(con, match_key, current_minute):
    """高效双查询: 返回 (open_T, current_T) 或 None。
    open_T = **开赛前**快照的隐含总球线(开盘, 经 kickoff 闸门); current_T = 当前分钟前最近快照(live)。
    开盘严格走 _open_total_from_snapshots (杜绝把进球后活盘价当开盘, 2026-08-20 根因修复)。"""
    cur = con.cursor()
    ko = cur.execute("SELECT kickoff FROM matches WHERE match_key=?", (match_key,)).fetchone()
    if not ko or not ko[0]:
        return None
    kots = _parse_kickoff(ko[0])
    if kots is None:
        return None
    cap_now = kots + (current_minute + 5) * 60.0
    # 开盘 = 开赛前(闸门)最早快照的隐含总球 (gated)
    _, open_T = _open_total_from_snapshots(con, match_key, 'OU_', exclude_prefixes=['OU_1H', 'OU_2H'], ref_line=2.5)
    # 当前 = 当前分钟前最近快照 (live, 不闸门)
    cur_rows = cur.execute("""
        SELECT CAST(REPLACE(REPLACE(market,'OU_',''),'_','.') AS REAL) AS line,
               selection, odds
        FROM odds_snapshots
        WHERE match_key=? AND market LIKE 'OU_%'
          AND market NOT LIKE 'OU_1H%' AND market NOT LIKE 'OU_2H%'
          AND CAST(REPLACE(REPLACE(market,'OU_',''),'_','.') AS REAL) >= 1.0
          AND CAST(REPLACE(REPLACE(market,'OU_',''),'_','.') AS REAL) <= 15.0
          AND captured_at <= ?
        ORDER BY captured_at DESC LIMIT 40
    """, (match_key, cap_now)).fetchall()

    def to_pairs(rows):
        d = defaultdict(dict)
        for line, sel, odds in rows:
            if line is None or odds is None:
                continue
            d[line][sel] = odds
        return [(L, v.get('over'), v.get('under')) for L, v in d.items()
                if v.get('over') and v.get('under')]

    cur_T = _implied_total_from_pairs(to_pairs(cur_rows))
    return open_T, cur_T


def get_ah_open_and_current(con, match_key, current_minute):
    """返回开盘 AH 各线赔率 {line: {home, away, draw}} (最早快照), 用于 AH 开盘锚定。
    与 get_ou_open_and_current_total 平行: 开盘 = 该 (match_key, AH线) 最早 captured_at 快照。
    仅全场比赛 AH (排除 1H/2H)。"""
    cur = con.cursor()
    ko = cur.execute("SELECT kickoff FROM matches WHERE match_key=?", (match_key,)).fetchone()
    if not ko or not ko[0]:
        return None
    kots = _parse_kickoff(ko[0])
    if kots is None:
        return None
    open_rows = cur.execute("""
        SELECT market, selection, odds
        FROM odds_snapshots
        WHERE match_key=? AND market LIKE 'AH_%'
          AND market NOT LIKE 'AH_%1H%' AND market NOT LIKE 'AH_%2H%'
          AND selection IN ('home','away','draw')
          AND captured_at < ?
        ORDER BY captured_at ASC
    """, (match_key, kots + 300)).fetchall()  # 仅开赛前快照当开盘(2026-08-20 闸门)
    seen = {}
    for market, sel, odds in open_rows:
        if market not in seen:
            seen[market] = {}
        if sel not in seen[market]:
            seen[market][sel] = odds
    out = {}
    for market, sd in seen.items():
        try:
            line = float(market[3:])   # 'AH_-1.0' -> -1.0, 'AH_+0.25' -> 0.25
        except Exception:
            continue
        if 'home' in sd and 'away' in sd:
            out[line] = sd
    return out if out else None


def _dewater_ah_p_fav(fav_odds, dog_odds):
    """AH 去水 P(让球方覆盖): 双方向赔率(忽略 draw=走水/void)去除抽水。
    返回 margin-free P(fav cover) in [0,1]; 入参非法返回 None。
    与 _dewatered_over_prob 同法(不变量: 去水概率, 禁用原始赔率作特征)。"""
    if not fav_odds or not dog_odds or fav_odds <= 1.01 or dog_odds <= 1.01:
        return None
    if fav_odds > 1000.0 or dog_odds > 1000.0:
        return None
    f, d = 1.0 / fav_odds, 1.0 / dog_odds
    s = f + d
    if s <= 0:
        return None
    return f / s


def _build_ah_opening_anchor(opening_ah, ah_line, ah_fav_odds, ah_dog_odds):
    """AH 开盘锚: 用开盘去水 P(让球方覆盖) 拉住临场 AH 因进球过激的波动。
    与 OU (get_ou_open_and_current_total + remaining_break_prob 开盘锚) 同法。
    返回块含 anchor_used。若开盘无对应线或入参缺失, anchor_used=False(诚实降级)。
    开盘锚本身是最优校准基准(分析结论: AH 模型 AUC 0.55 < naive 0.59, 开盘即真相),
    故本块只做一致性对照(初滚漂移), 不覆盖 live 方向。"""
    if opening_ah is None or ah_line is None:
        return {'anchor_used': False, 'verdict': '无开盘 AH 快照, 锚定不可用'}
    # 找开盘里最接近当前 ah_line 的线(滚球线可能漂走, 但开盘主盘线通常存在)
    best_line, best_diff = None, None
    for L in opening_ah:
        diff = abs(L - ah_line)
        if best_diff is None or diff < best_diff:
            best_diff, best_line = diff, L
    if best_line is None:
        return {'anchor_used': False, 'verdict': '开盘 AH 无匹配线, 锚定不可用'}
    sd = opening_ah[best_line]
    h, a = sd.get('home'), sd.get('away')
    if not h or not a:
        return {'anchor_used': False, 'verdict': '开盘 AH 主客赔率缺失, 锚定不可用'}
    open_fav, open_dog = min(h, a), max(h, a)
    open_p = _dewater_ah_p_fav(open_fav, open_dog)
    if open_p is None:
        return {'anchor_used': False, 'verdict': '开盘 AH 赔率非法, 锚定不可用'}
    cur_p = _dewater_ah_p_fav(ah_fav_odds, ah_dog_odds)
    # 开盘即校准基准: 当前相对开盘的漂移 = 真·盘口分歧(陷阱/机会信号), 非稳定+EV
    if cur_p is not None:
        drift = cur_p - open_p
        if abs(drift) < 0.03:
            verdict = '初滚一致 (开盘即校准基准, 跟随即真相)'
        elif drift > 0:
            verdict = '临场抬升让球方热度 (开盘锚示警: 堤防热门过热被诱导)'
        else:
            verdict = '临场压低让球方 (开盘锚示警: 堤防诱冷/真实反转)'
    else:
        verdict = '开盘锚仅展示, 无实时赔率对照'
    return {
        'anchor_used': True,
        'opening_line': round(best_line, 2),
        'line_matched': round(best_line, 2) == round(ah_line, 2),
        'opening_fav_odds': round(open_fav, 3),
        'opening_dog_odds': round(open_dog, 3),
        'opening_implied_p_fav': round(open_p, 4),
        'current_fav_odds': round(ah_fav_odds, 3) if ah_fav_odds else None,
        'current_dog_odds': round(ah_dog_odds, 3) if ah_dog_odds else None,
        'current_implied_p_fav': round(cur_p, 4) if cur_p is not None else None,
        'drift_p': round(cur_p - open_p, 4) if cur_p is not None else None,
        'verdict': verdict,
    }


# 降盘买小 历史回测 (analysis/line_drop_backtest.json, 2097 场 finished)
LINE_DROP_HISTORY = {
    'early_window': {'label': '20-70分钟(早/中)', 'n': 663, 'hit': 307,
                     'acc': 0.463, 'roi': 0.070, 'avg_under': 2.21},
    'late_window': {'label': '45-85分钟(晚)', 'n': 607, 'hit': 262,
                    'acc': 0.432, 'roi': -0.013, 'avg_under': 2.23},
    'baseline': {'acc': 0.449, 'n': 2510},
}


def detect_line_drop(con, match_key, current_minute):
    """降盘漂移检测 (派生特征, 独立成集):
    开盘总球线 - 当前总球线 >= 0.5 视为'降盘'。返回观察对象, 不含任何+EV承诺。"""
    if con is None or not match_key:
        return None
    res = get_ou_open_and_current_total(con, match_key, current_minute)
    if res is None:
        return None
    open_T, cur_T = res
    if open_T is None or cur_T is None:
        return None
    drop = open_T - cur_T
    if drop < 0.5:
        return {'detected': False, 'drop': round(drop, 2),
                'open_total': round(open_T, 2), 'current_total': round(cur_T, 2),
                'window': None, 'history': LINE_DROP_HISTORY,
                'note': '总球线未降>=0.5, 无降盘信号'}
    # 触发: 按时间窗口分流历史参照
    early = current_minute <= 70
    late = current_minute >= 45
    if early and not late:
        hist, window = LINE_DROP_HISTORY['early_window'], 'early'
    elif late:
        hist, window = LINE_DROP_HISTORY['late_window'], 'late'
    else:
        hist, window = LINE_DROP_HISTORY['early_window'], 'early'
    roi_pct = ('+' if hist['roi'] >= 0 else '') + f"{hist['roi'] * 100:.0f}"
    return {
        'detected': True,
        'drop': round(drop, 2),
        'open_total': round(open_T, 2),
        'current_total': round(cur_T, 2),
        'window': window,
        'history': hist,
        'verdict': (f"总球线 {open_T:.2f}→{cur_T:.2f} (降{drop:.2f}球)。"
                    f"历史{hist['label']}窗口: 小命中{hist['acc']*100:.0f}% / ROI {roi_pct}%。"
                    f"开盘锚定结果仅作方向参考, 非+EV信号。"),
    }


def probe_core(odds, current_score='0-0', current_minute=0, league=None, con=None, match_key=None, is_halftime=False):
    from analysis.live_score_conditional import remaining_break_prob, is_cup_league, fixed_fulltime_over_prob
    _ht_cup_note = _ft_cup_note = None
    ht_signal = None
    """核心判定逻辑(与 probe_match 共用)。odds 为 {market__selection: odds} 字典。
    con 传入时启用赔率动量(近 2 分钟变化); 回测传入 None 则跳过动量项。
    is_halftime=True 表示比赛处于中场休息(45'~60' 实际经过), 半场结果已定, 不再分析半场破蛋。"""
    cal = load_calibration()
    # 当前比分
    sh, sa = 0, 0
    if current_score:
        m = re.match(r"(\d+)\s*[-:]\s*(\d+)", current_score.strip())
        if m:
            sh, sa = int(m.group(1)), int(m.group(2))
    total_now = sh + sa

    # 1X2 热门判定 (2026-08-18 抗诱导: 弃原始赔率值 fav_odds<1.40, 改去水概率 P(fav)>=0.65)
    # 原始赔率值每日含义不同(抽水结构变), 去水概率才是跨日可比的"热门度"。
    x2h = odds.get('1X2__home')
    x2d = odds.get('1X2__draw')
    x2a = odds.get('1X2__away')
    fav_odds = min([v for v in (x2h, x2d, x2a) if v is not None], default=None)
    _x2dw = _dewater_1x2(x2h, x2d, x2a)
    p_fav = max(_x2dw) if _x2dw else None
    is_deep_fav = p_fav is not None and p_fav >= cal.get('p_fav_deep', 0.65)

    # 是否已破蛋
    ht_already_broken = total_now >= 1
    # 2026-08-20 修复: 信任调用方基于 kickoff 经过时长判定的半场标志, 不再用裸分钟 45 强行
    # 判定半场休息。否则上半场第45分钟(仍在进行、尚未中场)会被误判为半场, 半场信号退化成
    # SCORE_LAGGING/SETTLED_UNDER/ALREADY_BROKEN 等"已定型"终态, 失去实时破蛋潜力意义。
    # 调用方(list_live_matches.row_to_base / 前端 fetchProbe)均已用 kickoff 时长正确消歧。
    _is_halftime = bool(is_halftime)
    ou1h_over = ou1h_under = ou1h_delta = None
    ou1h_low_is_over = None
    ht_line = None
    over_change = under_change = 0.0
    # 2026-08-28: 数据源标记默认值 (各分支按实际覆盖; 防提前分支未定义)
    ht_data_source = 'league_prior'
    ht_no_odds_reason = None

    # 提前计算半场当前锚定线(用于中场休息时校验比分源是否滞后)
    _ht_candidates = []
    for key in odds:
        if key.startswith('OU_1H_') and key.endswith('__over'):
            line_key = key[:-6]
            ov = odds.get(f'{line_key}__over')
            un = odds.get(f'{line_key}__under')
            if ov is not None and un is not None:
                try:
                    line = float(line_key.split('_')[-1])
                except Exception:
                    line = 0.5
                pg = _dewatered_over_prob(ov, un)
                pgap = abs(pg - 0.5) if pg is not None else 0.0
                _ht_candidates.append((pgap, line))
    if _ht_candidates:
        _ht_candidates.sort(reverse=True)
        ht_line = _ht_candidates[0][1]

    # 半场破蛋概率(若已破蛋=1 或 中场休息已定型)
    if ht_already_broken:
        ht_prob = 1.0
        ht_signal = 'ALREADY_BROKEN'
        ht_direction = None
    elif _is_halftime:
        # 中场休息且比分 0-0: 半场结果已定(未破蛋), 不应显示"已达成"
        # 2026-08-19 加固: 若盘口线明显上移(>0.75)但比分仍 0-0, 判定为比分源滞后,
        # 不能 SETTLED_UNDER, 否则会把已进球比赛误判为"已定小"。
        if total_now == 0 and ht_line is not None and ht_line > 0.75:
            ht_prob = 0.0
            ht_signal = 'SCORE_LAGGING'
            ht_direction = None
        else:
            ht_prob = 0.0
            ht_signal = 'SETTLED_UNDER'
            ht_direction = 'UNDER'
    else:
        # OU_1H 水位判读 (2026-08-18 抗诱导v2: 选线与阈值全改去水概率坐标系)
        # 原始赔率差 Δ=abs(over-under) 非平稳: 0.20 在 1.5/1.7 是 6pp 概率差, 在 3.0/3.2 仅 1.6pp
        # → 选线按 |去水P(over)-0.5| 最大(概率坐标跨价格水平可比), 阈值同理。
        ht_candidates = []
        for key in odds:
            if key.startswith('OU_1H_') and key.endswith('__over'):
                line_key = key[:-6]
                ov = odds.get(f'{line_key}__over')
                un = odds.get(f'{line_key}__under')
                if ov is not None and un is not None:
                    try:
                        # OU_1H_1.25 的线值在最后一段, 不能取 [1]='1H'
                        line = float(line_key.split('_')[-1])
                    except:
                        line = 0.5
                    pg = _dewatered_over_prob(ov, un)
                    pgap = abs(pg - 0.5) if pg is not None else 0.0
                    ht_candidates.append((pgap, ov, un, line, 1 if ov < un else 0))
        ht_line = None
        if ht_candidates:
            ht_candidates.sort(reverse=True)
            _, ou1h_over, ou1h_under, ht_line, ou1h_low_is_over = ht_candidates[0]
            ou1h_delta = abs(ou1h_over - ou1h_under)  # 仅展示用, 判定一律用 pgap

        # ── 主锚: 盘口去水隐含概率 (2026-08-18 换锚) ──
        # 实证: 去水隐含概率高度校准(AUC 0.73 > 任何自训 ML), 旧手工规则
        # (base+时间压力+Δ加减) 校准差(说0%实际25.6%)。改为市场去水概率为主锚,
        # 规则项降级为微调(市场未定价的残差信息: 动量/深盘)。
        p_mkt = _dewatered_over_prob(ou1h_over, ou1h_under) if ou1h_over and ou1h_under else None
        ou1h_pgap = abs(p_mkt - 0.5) if p_mkt is not None else None  # 去水概率偏离度(抗诱导判定坐标)
        if p_mkt is not None:
            prob = p_mkt
            anchor = 'market'
        else:
            # 盘口缺失: 优先用赛前破蛋先验模型(v2 抗诱导, GroupKFold-by-day AUC 0.6654)
            p_model = _ht_model_predict(odds, league, con, match_key, current_minute)
            if p_model is not None:
                prob = p_model
                anchor = 'model'
            else:
                # 最终回退: 旧规则基线
                base = cal['ht_base_break_rate']
                time_pressure = min(1.0, current_minute / 45.0) * 0.25  # 最多 +25pp
                prob = base + time_pressure
                anchor = 'rule_fallback'

        # 微调项(残差): 权重收窄, 市场/模型已定价的信息不再重复加减
        # market 锚: fav 已在盘口里, +0.02 仅表深盘攻势残差; model 锚: fav 已是特征, 不加
        if is_deep_fav:
            prob += 0.02 if anchor == 'market' else (0.0 if anchor == 'model' else 0.10)

        # 赔率动量 (用选中的 OU_1H line)
        over_change = under_change = 0.0
        if ht_line is not None and con is not None and match_key is not None:
            line_key = f'OU_1H_{ht_line:g}'
            changes = get_recent_ou_changes(con, match_key, line_key, window_sec=120)
            over_change = changes.get('over_change', 0.0)
            under_change = changes.get('under_change', 0.0)
            mom = 0.03 if anchor in ('market', 'model') else 0.08
            if over_change <= -cal['over_drop_threshold']:
                prob += mom
            if under_change <= -cal['over_drop_threshold']:
                prob -= mom

        # clamp
        # ── 市场分歧闸门 (2026-08-30, 同全场逻辑, 结算数据驱动) ──
        # OU_1H list 阶段 UNDER 准确率仅 30% vs OVER 74%: 弱信号(|p-0.5|<0.06)且与
        # 市场去水方向相逆时尊重市场, 模型只在有真实置信时才覆盖。
        if p_mkt is not None and abs(prob - 0.5) < 0.06 and (prob >= 0.5) != (p_mkt >= 0.5):
            prob = p_mkt
        ht_prob = max(0.05, min(0.95, prob))

        # 杯赛场景置信收缩(2026-08-19): 模型用联赛校准, 对杯赛(尤其强弱悬殊/强强保守)
        # 系统性偏差且易过度自信。收缩至 0.82 向 0.5 靠拢, 避免高置信(STRONG)误判。
        if is_cup_league(league) and ht_signal not in ('ALREADY_BROKEN', 'SETTLED_UNDER', 'SCORE_LAGGING'):
            ht_prob = 0.5 + (ht_prob - 0.5) * 0.82
            _ht_cup_note = '杯赛场景·半场置信收缩(模型对杯赛校准弱, 不强行高置信)'

        # 信号方向 (2026-08-18 用户要求: 永不观望, 始终给大/小 lean)
        # signal 表强度(颜色), direction 表方向(永远有值, >=0.5 偏大, <0.5 偏小)
        if ht_prob >= 0.65:
            ht_signal = 'STRONG_BREAK'
            ht_direction = 'OVER'
        elif ht_prob <= 0.35:
            ht_signal = 'STRONG_HOLD'
            ht_direction = 'UNDER'
        elif ou1h_pgap is not None and ou1h_pgap >= cal['pgap_weak']:
            ht_signal = 'WEAK_TREND'
            ht_direction = 'OVER' if ou1h_low_is_over else 'UNDER'
        else:
            ht_signal = 'NO_EDGE'
            ht_direction = 'OVER' if ht_prob >= 0.5 else 'UNDER'
        # 2026-08-28: half 段标记数据源(主 OU_1H 盘口到位 = live_odds; 否则看 anchor)
        ht_data_source = 'live_odds' if anchor in ('market', 'model') else 'league_prior'
        ht_no_odds_reason = None if anchor in ('market', 'model') else f"半盘 OU_1H 缺失或仅基线, anchor={anchor}"

    # 全场破蛋概率(以当前 OU line 为目标)
    ft_prob, ft_signal, ft_direction = 0.5, 'NO_EDGE', None
    ft_inducement = None
    ouf_over = ouf_under = ouf_delta = None
    ouf_pgap = None
    ou_calib_note = None
    line = None
    # 找合适全场 OU line: 选信号最强(去水概率偏离 + 诱导甜点加成)且未被破的。
    # 2026-08-18: 有 OU 诱导甜点的线优先(甜点=edge≥+9pp 的实证误定价, 正是用户想看的)。
    # 全场破蛋选线 —— 与 ou_breakegg_decision 决策层统一为单一真相源 (2026-08-22 重建)
    # 旧逻辑按"去水概率偏离 + 诱导甜点+0.08"动态选线, 对诱导甜点过拟合 → 准确性回退。
    # 现统一走 select_ou_lines: 全场锚定 {2.0,2.25,2.5} (无锚定线时回退全部),
    # 仅在这些诚实锚定线里挑"去水概率偏离"最强的一条 (去掉诱导甜点加权, 避免学庄家诱导)。
    ft_candidates = []
    try:
        from pipeline.ou_breakegg_decision import select_ou_lines as _select_ou_lines
    except Exception:
        _select_ou_lines = None
    _raw_lines = []
    for key in odds:
        if key.startswith('OU_') and not key.startswith('OU_1H') and not key.startswith('OU_2H') and key.endswith('__over'):
            line_key = key[:-6]
            ov = odds.get(f'{line_key}__over')
            un = odds.get(f'{line_key}__under')
            if ov is not None and un is not None:
                try:
                    # OU_2.00 / OU_1H_1.25 线值均在最后一段
                    line = float(line_key.split('_')[-1])
                except:
                    line = 0.5
                if total_now >= line:
                    continue
                _raw_lines.append({'line': line, 'over': ov, 'under': un})
    _picked = _select_ou_lines(_raw_lines, is_halftime=False) if _select_ou_lines else _raw_lines
    for c in _picked:
        line = float(c['line']); ov = c['over']; un = c['under']
        pg = _dewatered_over_prob(ov, un)
        pgap = abs(pg - 0.5) if pg is not None else 0.0
        ft_candidates.append((pgap, ov, un, line, 1 if ov < un else 0))
    if ft_candidates:
        ft_candidates.sort(reverse=True)
        _, ouf_over, ouf_under, line, ouf_low_is_over = ft_candidates[0]
        already = total_now >= line
        if already:
            ft_prob = 1.0
            ft_signal = 'ALREADY_BROKEN'
            ft_direction = None
        else:
            ouf_delta = abs(ouf_over - ouf_under)  # 仅展示用, 判定一律用 pgap
            # ── 主锚: 盘口去水隐含概率 (2026-08-18 换锚, 同半场逻辑) ──
            p_mkt_f = _dewatered_over_prob(ouf_over, ouf_under)
            ouf_pgap = abs(p_mkt_f - 0.5) if p_mkt_f is not None else None
            if p_mkt_f is not None:
                prob = p_mkt_f
                anchor_f = 'market'
            else:
                # 2026-08-30 泊松基线修正: 旧公式 0.72*(0.5/line) 对 line 2.5 只给 14%
                # (真实 P(大2.5)≈48%) — 严重看小偏置, 是 UNDER 17% 准确率的兜底路径根源。
                # 改为剩余泊松尾: μ_rem = 2.6×剩余时间占比, P(剩余进球 > line-已进)。
                # 时间方向也修正: 比赛进行而未破线时, 剩余概率应递减(旧代码反而 +20%)。
                _mu_rem = 2.6 * max(0.0, 1.0 - current_minute / 90.0)
                _need = max(0.0, line - total_now)
                _k = int(_need)
                _cdf = 0.0
                _term = 1.0
                for _i in range(_k + 1):
                    _cdf += _term
                    _term *= _mu_rem / (_i + 1) if (_i + 1) else 0.0
                prob = min(0.9, max(0.05, 1.0 - _cdf * pow(2.718281828, -_mu_rem)))
                anchor_f = 'rule_fallback'
                if ouf_pgap is not None and ouf_pgap >= cal['pgap_strong']:
                    prob += 0.20 if ouf_low_is_over else -0.20
                elif ouf_pgap is not None and ouf_pgap >= cal['pgap_weak']:
                    prob += 0.08 if ouf_low_is_over else -0.08
            if is_deep_fav:
                prob += 0.02 if anchor_f == 'market' else 0.08
            # 动量
            if con is not None and match_key is not None:
                line_key = f'OU_{line:g}'
                fchanges = get_recent_ou_changes(con, match_key, line_key, window_sec=120)
                mom = 0.03 if anchor_f == 'market' else 0.05
                if fchanges.get('over_change', 0.0) <= -cal['over_drop_threshold']:
                    prob += mom
                if fchanges.get('under_change', 0.0) <= -cal['over_drop_threshold']:
                    prob -= mom

            # ── OU 不对称诱导校准 v2 (2026-08-18, 宽分段+正确half-win结算) ──
            # 庄家在特定 OU 线+价位段系统性误定价: 低线over(大球热门)被高估=陷阱(降权),
            # 低线under冷门被低估=甜点(升权)。查段命中 → 该方向概率 +calib_pp(可正可负)。
            # ft_prob=over方向; over: +=pp; under: -=pp(under升=over降, 符号由pp正负决定)。
            ou_calib_note = None
            try:
                from analysis.ou_inducement_calibrator import get_calib as _ou_calib
                _oc = _ou_calib(line, ouf_over, ouf_under)
                if _oc is not None:
                    _side, _pp, _detail = _oc
                    if _side == 'over':
                        prob += _pp / 100.0
                    else:
                        prob -= _pp / 100.0
                    ft_inducement = 'trap' if _pp < 0 else 'sweet'
                    _tag = '甜点' if _pp >= 0 else '陷阱'
                    _sign = '+' if _pp >= 0 else ''
                    ou_calib_note = (f"OU_{line:g} {_side}@[{_detail['odds_lo']:.2f},{_detail['odds_hi']:.2f}) "
                                     f"{_tag} {_side}方向{_sign}{_pp:.1f}pp"
                                     f"(实测{_detail['actual']*100:.0f}% vs 隐含{_detail['implied']*100:.0f}%, n={_detail['n']})")
            except Exception:
                pass
            # 通用 over-odds 诱盘带: 开盘 over 落 [2.0,2.2) = 庄家诱导大球嫌疑
            # 2026-08-30 数据驱动修正: 原硬帽 prob=min(prob,0.45) 会把市场 52-55% 的盘
            # 强翻成小球方向 — prediction_ledger 结算实证 list 阶段 UNDER 准确率
            # 仅 17%(OU)/30%(OU_1H) vs OVER 86%/74%, 方向性硬翻是净伤害(莫斯科斯巴达U19
            # 5-1 场即例证: 57' 2-0 模型 0.479 推小, 市场盘口升 4.0 看大, 大球打出)。
            # 改为温和收缩(50% 向 0.5)+保留 trap 标注, 不再强翻方向。
            if ouf_over is not None and 2.0 <= ouf_over < 2.2:
                ft_inducement = 'trap'
                prob = 0.5 + (prob - 0.5) * 0.5

            # ── 市场分歧闸门 (2026-08-30, 结算数据驱动) ──
            # 弱信号(|p-0.5|<0.06)且方向与市场去水相逆 → 尊重市场(市场 devig 校准良好,
            # AUC 0.73), 模型只在有真实置信时才覆盖市场。消除"48-49% 推小"类噪声方向。
            if p_mkt_f is not None and abs(prob - 0.5) < 0.06 and (prob >= 0.5) != (p_mkt_f >= 0.5):
                prob = p_mkt_f

            ft_prob = max(0.05, min(0.95, prob))
            # 杯赛场景置信收缩(2026-08-19): 同上, 全场方向也降置信
            if is_cup_league(league) and ft_signal != 'ALREADY_BROKEN':
                ft_prob = 0.5 + (ft_prob - 0.5) * 0.82
                _ft_cup_note = '杯赛场景·全场置信收缩(模型对杯赛校准弱, 不强行高置信)'
            if ft_prob >= 0.65:
                ft_signal = 'STRONG_BREAK'; ft_direction = 'OVER'
            elif ft_prob <= 0.35:
                ft_signal = 'STRONG_HOLD'; ft_direction = 'UNDER'
            elif ouf_pgap is not None and ouf_pgap >= cal['pgap_weak']:
                ft_signal = 'WEAK_TREND'; ft_direction = 'OVER' if ft_prob >= 0.5 else 'UNDER'
            else:
                ft_signal = 'NO_EDGE'; ft_direction = 'OVER' if ft_prob >= 0.5 else 'UNDER'
        # 2026-08-28: 全场 OU 盘口到位 → 标记即时盘口数据源(与 else 兜底 'league_prior' 区分)
        ft_data_source = 'live_odds'
        ft_no_odds_reason = None
    else:
        # 无全场 OU 盘口(obscure 联赛常见): 用基线规则算"全场破蛋(≥1球)"概率,
        # 并始终给方向(2026-08-18 用户要求永不观望)。基线 0.72 锚定实际全场破蛋率 85.5%。
        # 2026-08-28: 标记 data_source='league_prior' 让前端诚实显示"先验"而非"即时盘口"——避免误把
        # 基线概率当成庄家隐含。用户报"没有用即时盘口"指的就是这里(无 OU 时仍展示 72% 让人误以为是从盘口推的)。
        base = cal['ft_base_break_rate']  # line=0.5 语义: 全场至少 1 球
        time_pressure = min(1.0, current_minute / 90.0) * 0.20
        prob = base + time_pressure
        if is_deep_fav:
            prob += 0.08
        ft_prob = max(0.05, min(0.95, prob))
        line = 0.5
        ft_signal = 'NO_EDGE'
        ft_direction = 'OVER' if ft_prob >= 0.5 else 'UNDER'
        ft_data_source = 'league_prior'
        ft_no_odds_reason = '无全场 OU 盘口快照(WS 推流未到或该联赛无 OU) — 概率来自基线 0.72 + 时间衰减'

    # ── 状态感知剩余破蛋(live_score_conditional, 显式消费 current_score + league) ──
    # half 段去水隐含总球
    _ou1h_pairs = []
    for key in odds:
        if key.startswith('OU_1H_') and key.endswith('__over'):
            line_key = key[:-6]
            ov = odds.get(f'{line_key}__over')
            un = odds.get(f'{line_key}__under')
            if ov and un:
                try:
                    _l = float(line_key.split('_')[-1])
                except Exception:
                    continue
                _ou1h_pairs.append((_l, ov, un))
    _ht_implied = _implied_total_from_pairs(_ou1h_pairs) if _ou1h_pairs else None
    # λ 口径判定(2026-08-21): 赛前快照的 λ 不含时间衰减, 必须 ×rem_ratio; 滚球快照才用 λ-G
    _lambda_src = 'prematch'
    _snap_off = None
    if con is not None and match_key is not None:
        try:
            _lambda_src, _snap_off = latest_ou_snapshot_phase(con, match_key)
        except Exception:
            _lambda_src = 'prematch'
    remaining_break_half = remaining_break_prob(
        _ht_implied, current_score, current_minute,
        line=ht_line if ht_line is not None else 1.5,
        is_halftime=_is_halftime, segment='half', league=league,
        lambda_source=_lambda_src)

    # full 段去水隐含总球
    _ou_pairs = []
    for key in odds:
        if (key.startswith('OU_') and not key.startswith('OU_1H')
                and not key.startswith('OU_2H') and key.endswith('__over')):
            line_key = key[:-6]
            ov = odds.get(f'{line_key}__over')
            un = odds.get(f'{line_key}__under')
            if ov and un:
                try:
                    _l = float(line_key.split('_')[-1])
                except Exception:
                    continue
                _ou_pairs.append((_l, ov, un))
    _full_implied = _implied_total_from_pairs(_ou_pairs) if _ou_pairs else None
    # 开盘锚定总球(2026-08-20): 用已验证可靠的开盘去水总球给实时λ做贝叶斯式锚定,
    # 拉住临场因进球过激降线。get_ou_open_and_current_total 已算过 open_T(开盘隐含总球)。
    _full_opening_T = None
    if con is not None and match_key is not None:
        try:
            _oc_res = get_ou_open_and_current_total(con, match_key, current_minute)
            if _oc_res:
                _full_opening_T = _oc_res[0]
        except Exception:
            _full_opening_T = None
    remaining_break_full = remaining_break_prob(
        _full_implied, current_score, current_minute,
        line=line if line is not None else 2.5,
        is_halftime=_is_halftime, segment='full', league=league,
        opening_implied_total=_full_opening_T,
        lambda_source=_lambda_src)
    # 2026-08-23 用户硬性要求: 固定全场大 2.5, 不随比赛时间/庄家升盘衰减。
    # 用全场隐含总球 λ_full(开盘锚优先, 不×rem_ratio)评估"最终总球 > 2.5",
    # 阈值永远锁 2.5, 只随已实现进球 G 变化 → 末段进球能正确反映大 2.5 是否打穿。
    fixed_full_over = fixed_fulltime_over_prob(
        _full_opening_T, _full_implied, current_score, line=2.5, league=league)
    # 固定 2.5 线的实时赔率(庄家未升档时存在; 升档后缺失 → 前端提示"盘口已升档")
    _fixed_ov = _fixed_un = None
    for _l, _ov, _un in _ou_pairs:
        if abs(_l - 2.5) < 1e-6:
            _fixed_ov, _fixed_un = _ov, _un
            break
    fixed_full_over['over_odds'] = _fixed_ov
    fixed_full_over['under_odds'] = _fixed_un

    reasons = []
    if ht_already_broken:
        reasons.append("当前已破蛋, 半场概率=1")
    elif _is_halftime:
        reasons.append("中场休息: 半场结果已定, 不再评估半场破蛋")
    else:
        _anchor_cn = {'market': '盘口去水隐含概率(市场定价)',
                      'model': '赛前破蛋先验模型(AUC 0.65)',
                      'rule_fallback': '联赛基线+时间压力(无盘口)'}
        reasons.append(f"半场概率锚: {_anchor_cn.get(anchor, anchor)}")
        if is_deep_fav:
            reasons.append(f"深盘热门(去水P(fav)={p_fav:.2f}≥0.65)提升破蛋预期")
        if ou1h_delta is not None:
            reasons.append(f"半场OU Δ={ou1h_delta:.2f}, 低水方向={'大' if ou1h_low_is_over else '小'}")
        if abs(over_change) >= cal['over_drop_threshold'] or abs(under_change) >= cal['over_drop_threshold']:
            reasons.append(f"赔率动量: 大球变化={over_change*100:+.1f}%, 小球变化={under_change*100:+.1f}%")
        # 2026-08-27 修复: 原 f"时间压力: {current_minute}分钟/45分钟" 在滚球后段(如 minute=83)输出"83/45"无意义文案。
        # 改为"剩余X分钟"——上半场 45-minute, 下半场 90-minute + 5补时 = 95-minute.
        _remain = (45 - current_minute) if current_minute <= 45 else (95 - current_minute)
        _remain_note = f"剩余{_remain}分钟" if _remain > 0 else "末段加补时"
        reasons.append(f"时间压力: {_remain_note} → 0-0 越靠后越倾向破蛋")
    if ou_calib_note:
        reasons.append(ou_calib_note)
    if _ht_cup_note:
        reasons.append(_ht_cup_note)
    if _ft_cup_note:
        reasons.append(_ft_cup_note)

    # 一致性闸门(2026-08-27 Fix2): 防 ft_prob 与市场隐含总球余量期望 λ_full 自相矛盾
    # 案例: 维也纳快速 vs 哈茨 72' 1-0, ft_prob=0.86(STRONG_BREAK 强烈看大·大)
    #       但 fixed_full_over.lambda_full=1.56(<2.5 线) → 期望总球仅1.56 却喊"86%过2.5", 自相矛盾。
    # 诚实边界: 余量期望 λ_full 明显低于 line+margin 时, 不允许给 STRONG_BREAK(宁不喊"必大")。
    if (fixed_full_over.get('lambda_full') is not None
            and ft_signal == 'STRONG_BREAK'
            and fixed_full_over['lambda_full'] < 2.5 + cal.get('strong_break_lambda_margin', 0.3)):
        ft_signal = 'NO_EDGE'
        ft_direction = 'OVER' if ft_prob >= 0.5 else 'UNDER'
        reasons.append(
            f"一致性闸门: 余量期望λ_full={fixed_full_over['lambda_full']:.2f}<2.5+0.3, "
            f"与STRONG_BREAK(破蛋≥2.5)自相矛盾→降级NO_EDGE(诚实边界: 宁不喊必大)"
        )

    return {
        'current_score': current_score,
        'current_minute': current_minute,
        'league': league,
        'fav_odds': fav_odds,
        'half': {
            'prob': round(ht_prob, 3),
            'signal': ht_signal,
            'direction': ht_direction,
            'line': ht_line if ht_line is not None else 0.5,
            'target_total': total_now + 1,
            'over_odds': ou1h_over,
            'under_odds': ou1h_under,
            'delta': round(ou1h_delta, 3) if ou1h_delta is not None else None,
            'anchor': None if (ht_already_broken or _is_halftime) else anchor,
            'remaining_break': remaining_break_half,
            # 2026-08-28: 数据源标识(区分即时盘口 vs 先验兜底), 前端 SideCard 用来诚实标注
            'data_source': ht_data_source,
            'no_odds_reason': ht_no_odds_reason,
        },
        'full': {
            'prob': round(ft_prob, 3),
            'signal': ft_signal,
            'direction': ft_direction,
            'line': line if line is not None else 0.5,
            'target_total': total_now + (line if line is not None else 0.5),
            'over_odds': ouf_over,
            'under_odds': ouf_under,
            'delta': round(ouf_delta, 3) if ouf_delta is not None else None,
            'ou_calib': ou_calib_note,
            'inducement': ft_inducement,
            'remaining_break': remaining_break_full,
            'data_source': ft_data_source,
            'no_odds_reason': ft_no_odds_reason,
            # 2026-08-23: 固定全场大2.5(不随时间/庄家升盘衰减), 作为全场卡片主判定。
            # remaining_break 保留作"状态感知剩余破蛋"辅助参考。
            'fixed': fixed_full_over,
            # 2026-08-19: 标记当前分析线是否为滚球实时线(非赛前主盘2.5)。
            # line<2.0 且比赛已进行(current_minute>0)时, 必然是庄家降线后的滚盘,
            # 前端可显示"滚盘"徽标。
            'is_live_line': bool(line is not None and line < 2.0 and current_minute > 5),
        },
        'reasons': reasons,
    'warning': '概率警报仪, 非秒级预测器。绿灯=当前盘口去水隐含概率偏向(市场实时定价), 非必胜。'
               '重要: 活盘里"低 Over 水"常是庄家压低赔付(限负债)、高 Under 水"阻小", 属赔付管理信号, '
               '与概率方向可能相反 — 切勿把活盘低水直接读成"庄家看好大球"。开盘锚已加 kickoff 闸门, '
               '仅用开赛前快照, 无真实开盘价的比赛不伪造。'
               '历史回测: 全场方向命中~66% 但 ROI 仅~+1.8%(仅回收部分抽水, 非稳定 edge); '
               '半场信号样本不足(n≈39) 置信低。3 秒进球窗口在 45-60 秒轮询下无法捕捉, 需升级采集器到 3-5 秒。'
               '「降盘漂移」为独立观察特征: 开盘总球线降>=0.5时历史小方向命中~46%(早段ROI+7%/晚段-1%), '
               '开盘锚定结果仅作方向参考, 非+EV信号。',
    }


def probe_match_with_con(con, match_key, current_score='0-0', current_minute=0, league=None, is_halftime=False):
    """使用已打开的数据库连接输出滚球破蛋探测结果(批量扫描时避免反复创建连接)。"""
    # 修(2026-08-19 实时滚盘分析): 原全场OU只取 OU_2.00~2.50 三条线, 滚球下半场
    # 庄家降线到 1.5/1.75 甚至 0.5/0.75 时模型拿不到新线, 仍用过时的 2.5 赔率判读。
    # 现扩展到全部滚球线(0.5~4.25), get_latest_snapshot_odds 只返回"当前仍在挂"的最新快照,
    # 已撤盘的高线不会有新快照自然被淘汰 → probe_core 的 ft_candidates 永远在真实滚盘线里选。
    # 旧注释担心"误把新鲜低线当大1.5" — probe_core 内部已有 total_now>=line 跳过已破线
    # 的保护, 且选线按去水概率偏离度(自动选市场定价最清晰的线), 无结构错乱风险。
    markets = [
        '1X2', '1X2_1H',
        'OU_1H_0.50', 'OU_1H_0.75', 'OU_1H_1.00', 'OU_1H_1.25', 'OU_1H_1.50', 'OU_1H_1.75', 'OU_1H_2.00',
        # 全场OU: 赛前线 + 滚球降线全覆盖(取最新快照, 撤盘线自动缺席)
        'OU_0.50', 'OU_0.75', 'OU_1.00', 'OU_1.25', 'OU_1.50', 'OU_1.75',
        'OU_2.00', 'OU_2.25', 'OU_2.50', 'OU_2.75', 'OU_3.00', 'OU_3.25',
        'OU_3.50', 'OU_3.75', 'OU_4.00', 'OU_4.25',
        'AH_0.00', 'AH_0.25', 'AH_-0.25', 'AH_0.50', 'AH_-0.50', 'AH_0.75', 'AH_-0.75', 'AH_1.00', 'AH_-1.00'
    ]
    odds = get_latest_snapshot_odds(con, match_key, markets)
    res = probe_core(odds, current_score, current_minute, league, con, match_key, is_halftime=is_halftime)
    # 降盘漂移 (独立派生特征, 不污染核心 Δ 判定)
    ld = detect_line_drop(con, match_key, current_minute)
    res['line_drop'] = ld
    if ld and ld.get('detected'):
        res['reasons'].append(
            f"降盘漂移: 总球线 {ld['open_total']}→{ld['current_total']} (降{ld['drop']}球), "
            f"{ld['history']['label']}窗口")
    # 大小球锚点对比 (2026-08-19 用户需求): 初盘锚点 vs 实时滚盘锚点。
    # 初盘锚点 = 赛前最早的盘口线+隐含总球; 实时锚点 = 当前分钟最近快照的线+隐含总球。
    # 半场破蛋后全场大球支持: 用户在滚球界面一眼看到"全场初盘2.5 → 当前1.75(滚盘降线)"。
    try:
        anchor = get_ou_drift_summary(con, match_key)
        if anchor and anchor.get('has_data'):
            res['ou_anchor'] = {
                'full': anchor.get('full'),   # {open_line, current_line, open_total, current_total, drift_*}
                'half': anchor.get('half'),
                'verdict': anchor.get('verdict'),
                # 开盘结构 → 半场破蛋先验(实证查表, 2026-08-20): 滚球实时状态才是真正判别器
                'opening_ht_breakegg_prior': anchor.get('opening_ht_breakegg_prior'),
            }
            # 过度收缩警报 (2026-08-20 特罗姆瑟U19教训模型化):
            # 开盘锚 vs 活盘隐含总球收缩>1.0球(半场后) → 大球方向模型-市场分歧
            try:
                _fa = anchor.get('full') or {}
                _ags = anchor_gap_signal(_fa.get('open_total'), _fa.get('current_total'),
                                         current_minute, league)
                if _ags and _ags.get('overshrink'):
                    res['ou_anchor']['overshrink_alert'] = _ags
                    res['reasons'].append(f"⚠ {_ags['note']}")
            except Exception:
                pass
    except Exception:
        res['ou_anchor'] = None
    # Per-match 开盘结构诊断档案 (2026-08-20): 该场开盘 OU/AH/1X2 跨盘口残差,
    # 输出"这场庄家特异的破蛋倾向 + 赛果方向", 而非群体平均结论。
    try:
        res['structure_diagnosis'] = get_opening_structure_diagnosis(con, match_key)
    except Exception as _e:
        res['structure_diagnosis'] = {'has_real_open': False, 'error': str(_e)}
    # 比赛类型规则识别器 (2026-08-21 v2.2 2.0锚): 开盘AH×OU → 类型/平局温床/大球倾向。
    # 类型先验(hold-out验证): 磨盘35%/防守默契31%/超低线陷阱43%平局 vs 碾压19%;
    # 大球: 超低线(<2.0)诱导反弹53%, OU2.0-2.25大球仅33%。阈值0.30精确率43.2%。
    try:
        from analysis.rollball_v22 import analyze_match
        _ah_l, _, _ = _open_ah_from_snapshots(con, match_key)
        _sd = res.get('structure_diagnosis') or {}
        _be = _sd.get('breakegg') or {}
        _ou_l = _be.get('ou_open_line')
        _rs = _sd.get('result') or {}
        _ph = _rs.get('p_home'); _pd_ = _rs.get('p_draw'); _pa = _rs.get('p_away')
        if _ah_l is not None or _ou_l is not None:
            res['match_rules'] = analyze_match(_ah_l, _ou_l, _pd_, _ph, _pa)
        else:
            res['match_rules'] = None
    except Exception:
        res['match_rules'] = None
    # 终场方向读数: 跟随庄家去水隐含概率 (1X2+OU+AH 联合), 哨响模型缺失的"终场读数"维度
    try:
        res['fulltime'] = predict_fulltime_outcome(odds, current_score, current_minute, con, match_key, league)
    except Exception as _e:
        res['fulltime'] = {'direction': None, 'confidence': None,
                           'reasons': [f'终场读数失败: {_e}'], 'note': ''}

    # ── 预测账本不可变快照 (2026-08-23 用户铁律: 预测生成即记录, 错只记原因, 不回改结论/左侧标签) ──
    # 仅副作用: 快照 half/full 预测到 prediction_ledger; 失败绝不污染主流程返回值。
    try:
        from pipeline.prediction_ledger import record_from_probe_result
        record_from_probe_result(con, match_key, res, current_minute, is_halftime)
    except Exception:
        pass

    return res


def probe_match(match_key, current_score='0-0', current_minute=0, league=None, is_halftime=False):
    """对一场比赛输出滚球破蛋探测结果(使用最新盘口)。带线程安全 LRU+TTL 缓存:
    前端滚球焦点轮 3s 高频重复请求同一场, 短期内 score/minute 不变, 命中缓存免重查 6GB events.db。"""
    key = (match_key, current_score, int(current_minute or 0), league, bool(is_halftime))
    cached = _probe_cache_get(key)
    if cached is not None:
        return cached
    con = _open_gq()  # 只读连接: timeout=5 + wal_autocheckpoint=0, 避免读被自身 checkpoint 阻塞
    try:
        res = probe_match_with_con(con, match_key, current_score, current_minute, league, is_halftime=is_halftime)
    finally:
        con.close()
    # ── 极简结果(只给结论, 详细运算沉到 half/full/fulltime/ou_anchor 等) ──
    _ft = res.get("fulltime") or {}
    _full = res.get("full") or {}
    res["summary"] = {
        "expected_score": _ft.get("expected_score"),
        "fulltime_direction": _ft.get("direction"),
        "ou_signal": _full.get("ou_direction"),
        "ou_direction": _full.get("ou_direction"),
        "ou_prob": _full.get("ou_confidence"),
        "ou_confidence": _full.get("ou_confidence"),
    }
    _probe_cache_put(key, res)
    return res


# ⚠ 2026-08-21 修复: 原列表信号查的是 'OU_2.5'/'OU_2.0'/'OU_3.0'/'OU_1.5', 而库中
# market 命名是两位小数 'OU_2.50'/'OU_2.00'/'OU_3.00'/'OU_1.50' → 5 个线里 4 个永远
# 匹配不到, 只有 OU_2.25 偶然命中, 导致列表大量场次落 NO_EDGE(prob=0.5) 假信号。
LW_OU_FULL_MARKETS = ('OU_1.50', 'OU_1.75', 'OU_2.00', 'OU_2.25', 'OU_2.50', 'OU_2.75', 'OU_3.00')
LW_OU_HALF_MARKETS = ('OU_1H_0.50', 'OU_1H_0.75', 'OU_1H_1.00', 'OU_1H_1.25', 'OU_1H_1.50', 'OU_1H_1.75', 'OU_1H_2.00')
# 半场/全场线分开: 修复 half 信号误用全场线判方向的 bug (2026-08-22 用户截图"列表看大/详情看小")
LW_OU_MARKETS = LW_OU_FULL_MARKETS + LW_OU_HALF_MARKETS
_LW_MARKET_PLACEHOLDERS = ','.join('?' * len(LW_OU_MARKETS))


def _lightweight_signals_batch(con, items, window_sec=7200):
    """列表信号批量版 (2026-08-21 性能根治)。

    背景: 修掉 `minute<90` 误筛后 live 场次从 ~2 场恢复到 ~50 场, 逐场单查
    (50 × ~0.14s = 7.0s) 会再次打满 bridge 的 to_thread 线程池(前端 5s 轮询)。
    改为**一条 IN 批量 SQL** 取回全部场次的 OU 快照, Python 侧分组算信号。

    items: [{match_key, minute, score, is_halftime}, ...]
    返回: {match_key: signal_dict}
    """
    out = {}
    keys = [it['match_key'] for it in items if it.get('match_key')]
    if not keys:
        return out
    cutoff = time.time() - window_sec
    latest = {}   # match_key -> {market -> {selection -> odds}}
    CHUNK = 60    # 避免 SQLite 变量上限
    cur = con.cursor()
    for i in range(0, len(keys), CHUNK):
        chunk = keys[i:i + CHUNK]
        ph = ','.join('?' * len(chunk))
        try:
            rows = cur.execute(f"""
                SELECT match_key, market, selection, odds FROM odds_snapshots
                WHERE match_key IN ({ph})
                  AND market IN ({_LW_MARKET_PLACEHOLDERS})
                  AND selection IN ('over','under')
                  AND odds>1.01 AND odds<1000 AND captured_at > ?
                ORDER BY captured_at DESC
            """, (*chunk, *LW_OU_MARKETS, cutoff)).fetchall()
        except Exception:
            rows = []
        for mk, mkt, sel, od in rows:
            # ORDER BY captured_at DESC → 首次出现即该 (market, selection) 的最新价
            latest.setdefault(mk, {}).setdefault(mkt, {}).setdefault(sel, od)

    for it in items:
        mk = it.get('match_key')
        out[mk] = _signal_from_pairs(
            latest.get(mk) or {},
            current_minute=it.get('minute', 0),
            current_score=it.get('score', '0-0'),
        )
    return out


def _signal_from_pairs(pairs, current_minute=0, current_score='0-0'):
    """从 {market: {over, under}} 结构算列表信号。
    half 用半场线(OU_1H_*)判, full 用全场线(OU_* 且非 1H/2H)判, 独立计算。
    修复 2026-08-22 bug: 旧实现 half/full 共用全场线的去水方向, 半场信号串线(用户截图列表看大/详情看小)。"""
    def _best(is_half):
        best = None
        for mkt, d in pairs.items():
            if is_half:
                if not mkt.startswith('OU_1H_'):
                    continue
            else:
                if not mkt.startswith('OU_') or mkt.startswith('OU_1H_') or mkt.startswith('OU_2H_'):
                    continue
            if 'over' not in d or 'under' not in d:
                continue
            try:
                a, b = 1.0 / d['over'], 1.0 / d['under']
                if a + b <= 0:
                    continue
                p = a / (a + b)
                gap = abs(p - 0.5)
                if best is None or gap > best[0]:
                    best = (gap, p, mkt, d['over'], d['under'])
            except Exception:
                continue
        return best

    def _mk(best):
        if best is None:
            return {'signal': 'NO_EDGE', 'direction': None, 'prob': 0.5, 'line': None, 'inducement': None}
        _, p, mkt, ov, un = best
        line = _extract_line_from_market(mkt) if mkt else None
        sig = 'STRONG_BREAK' if p >= 0.60 else ('STRONG_HOLD' if p <= 0.40 else 'NO_EDGE')
        direction = 'OVER' if p >= 0.5 else 'UNDER'
        # 诱盘/甜点识别: 命中则方向对齐价值侧, 强陷阱降级(不喊 STRONG)
        inducement, corr_dir = _ou_inducement(line, ov, un)
        if corr_dir is not None:
            direction = corr_dir
            if inducement == 'trap' and sig == 'STRONG_BREAK':
                sig = 'WEAK_TREND'
        return {'signal': sig, 'direction': direction, 'prob': round(p, 3), 'line': line, 'inducement': inducement}

    return {
        'current_score': current_score, 'current_minute': current_minute,
        'half': _mk(_best(True)),
        'full': _mk(_best(False)),
    }


def _ou_inducement(line, over_odds, under_odds):
    """诱盘/甜点识别: 返回 (inducement, corrected_direction).
    inducement: 'trap'(庄家诱导, 大球被高估) / 'sweet'(冷门低估, 价值侧) / None.
    corrected_direction: 命中时应跟随的价值方向(OVER/UNDER), 否则 None.
    ① 查 ou_inducement_calibrator 表(线+价位段系统性误定价);
    ② 通用经验规则: 开盘 over 赔率落在 [2.0, 2.2) = 庄家诱导大球
       (跨线实测 over-hit 41% / 平局 30%, 2026-08-25 核验), 往往出平局 → 诱盘.
    """
    inducement = None
    direction = None
    try:
        from analysis.ou_inducement_calibrator import get_calib as _gc
        _cal = _gc(line, over_odds, under_odds) if line is not None else None
        if _cal:
            _side, _pp, _detail = _cal
            inducement = 'sweet' if _pp > 0 else 'trap'
            direction = _side if _pp > 0 else ('UNDER' if _side == 'OVER' else 'OVER')
    except Exception:
        pass
    # ② 通用 over-odds 诱盘带 (交叉验证稳健, 不限单线) —— 用户报"大2.09 往往平局"
    if over_odds is not None and 2.0 <= over_odds < 2.2:
        inducement = 'trap'
        direction = 'UNDER'
    return inducement, direction


def _lightweight_signal(con, match_key, current_minute=0, current_score='0-0', is_halftime=False):
    """列表专用轻量 OU 信号 (2026-08-21 根治 matches 超时):
    单次 SQL 取该场最新 OU 配对(OU_2.5/2.25/2.0/3.0/1.5), 算去水 P(over),
    按 |P-0.5| 选最显著线, 输出 half/full signal/direction/prob (形状与 probe_core 一致)。
    不查 1X2 / 不查动量 / 不查诱导校准 / 不调模型 → 毫秒级。
    完整探测(模型/calibrator/CS)由详情路由 /api/live-goal-probe 调 probe_match 完成。"""
    cur = con.cursor()
    try:
        rows = cur.execute(f"""
            SELECT market, selection, odds FROM odds_snapshots
            WHERE match_key=? AND market IN ({_LW_MARKET_PLACEHOLDERS})
              AND selection IN ('over','under') AND odds>1.01 AND odds<1000
            ORDER BY captured_at DESC LIMIT 24
        """, (match_key, *LW_OU_MARKETS)).fetchall()
    except Exception:
        rows = []
    pairs = {}
    for mkt, sel, odds in rows:
        pairs.setdefault(mkt, {}).setdefault(sel, odds)
    best = None
    for mkt, d in pairs.items():
        if 'over' not in d or 'under' not in d:
            continue
        try:
            a, b = 1.0 / d['over'], 1.0 / d['under']
            if a + b <= 0:
                continue
            p = a / (a + b)
            gap = abs(p - 0.5)
            if best is None or gap > best[0]:
                best = (gap, p, mkt)
        except Exception:
            continue
    if best is None:
        return {
            'current_score': current_score, 'current_minute': current_minute,
            'half': {'signal': 'NO_EDGE', 'direction': None, 'prob': 0.5},
            'full': {'signal': 'NO_EDGE', 'direction': None, 'prob': 0.5},
        }
    _, p, mkt = best
    if p >= 0.60:
        sig = 'STRONG_BREAK'
    elif p <= 0.40:
        sig = 'STRONG_HOLD'
    else:
        sig = 'NO_EDGE'
    direction = 'OVER' if p >= 0.5 else 'UNDER'
    return {
        'current_score': current_score, 'current_minute': current_minute,
        'half': {'signal': sig, 'direction': direction, 'prob': round(p, 3)},
        'full': {'signal': sig, 'direction': direction, 'prob': round(p, 3)},
    }


def _priority_from_probe(p):
    """为 live 列表排序计算优先级: 能破蛋/还能进球的排前面。"""
    half = p.get('half', {})
    full = p.get('full', {})
    rank = 0.0

    # 信号强度: 正向破蛋/进球优先
    if half.get('signal') == 'STRONG_BREAK' and half.get('direction') == 'OVER':
        rank = 400.0
    elif full.get('signal') == 'STRONG_BREAK' and full.get('direction') == 'OVER':
        rank = 350.0
    elif half.get('signal') == 'WEAK_TREND' and half.get('direction') == 'OVER':
        rank = 300.0
    elif full.get('signal') == 'WEAK_TREND' and full.get('direction') == 'OVER':
        rank = 250.0
    elif half.get('signal') == 'STRONG_BREAK' or full.get('signal') == 'STRONG_BREAK':
        rank = 200.0
    elif half.get('signal') == 'WEAK_TREND' or full.get('signal') == 'WEAK_TREND':
        rank = 150.0
    elif half.get('signal') == 'NO_EDGE' or full.get('signal') == 'NO_EDGE':
        rank = 50.0
    elif half.get('signal') == 'ALREADY_BROKEN' or full.get('signal') == 'ALREADY_BROKEN':
        rank = 20.0

    # 概率加成
    prob = max(half.get('prob', 0.0), full.get('prob', 0.0))
    rank += prob * 30.0

    # 0-0 比分下的时间压力加成(越往后破蛋概率越高)
    score = p.get('current_score', '0-0') or '0-0'
    total = 0
    try:
        total = sum(int(x) for x in score.split('-') if x.strip().isdigit())
    except Exception:
        total = 0
    minute = p.get('current_minute', 0) or 0
    if total == 0:
        rank += min(1.0, minute / 45.0) * 10.0
    return rank


# ── 未开赛(feed 直采): 滚球神器要显示尚未开赛的比赛 ──
# 根因: collector 仅在详情接口返回可解析数据时(已有队名+赔率)才写 matches 行,
# 未开赛比赛详情常返回 null → 永不进 matches 表, 故 list_live_matches 从历史只读到 live。
# 这里直接读乐鱼 feed(含未来 48h 赛程), 用详情接口取真实队名 + 真实状态(mlet),
# 仅输出 mlet 为空(确未开赛)的比赛。带 120s TTL 缓存, 避免每轮高频打接口。
_FEED_UPCOMING_CACHE = {'ts': 0.0, 'data': [], 'refreshing': False}
_FEED_UPCOMING_LOCK = threading.Lock()

def _unix_to_gmt8_str(ts: float) -> str:
    from datetime import timezone, timedelta
    return datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')

def _fetch_feed_upcoming_raw(now=None):
    """同步拉取 feed 未开赛比赛(网络调用, 可能数秒)。返回 LiveMatch 风格 dict 列表。

    判定策略(关键坑):
    - 列表分 livedata / nolivedata 两组, 分组可靠: nolivedata = 非进行中(未开赛/已完)。
    - 但列表 mgt(开赛时间)与 structure 端点的 mlet 都不可靠: structure 对未来场也返回
      mlet='45:00' 占位(已实测: 开赛在数小时外的场 mlet 仍='45:00'), 故**绝不能用 mlet
      判断未开赛**。正确做法: 用列表 nolivedata 组 + 列表 mgt 严格未来(now, now+48h]
      锁定"未开赛"; structure 端点仅用于取队名(mhn/man/tnjc), kickoff 用列表 mgt。
    - ODDS_PATH 对未开赛返回 0401038 无队名, 故必须用 STRUCT_PATH 取队名。
    - collector 不把未开赛写进 matches 表, 故绕过 DB 直采 feed。
    全部 is_scheduled=True, 信号恒 NO_EDGE —— 未开赛无可破蛋探测数据, 不伪造信号。
    """
    now = now or time.time()
    out = []
    try:
        from gq.auto_collector import fetch_match_list, fetch_match_structure
        items = fetch_match_list()
        # 候选 = nolivedata 且列表 mgt 在未来 (now, now+48h]
        cands = []  # (ko_unix, mid, tn)
        for it in items:
            if it.get('grp') != 'nolivedata':
                continue
            try:
                ko = float(it.get('mgt') or 0) / 1000.0
            except (TypeError, ValueError):
                continue
            if ko <= now or ko > now + 48 * 3600:
                continue
            cands.append((ko, it['mid'], (it.get('tn') or '').strip()))
        cands.sort(key=lambda x: x[0])
        top = cands[:300]  # 控请求量(300/25=12 批)
        mids = [m for _, m, _ in top]
        # 批量取 structure(每批<=25, 端点上限) → mid 映射队名/联赛
        struct_map = {}
        if mids:
            for m in fetch_match_structure(mids):
                smid = str(m.get('mid') or '').strip()
                if smid:
                    struct_map[smid] = m
        seen = set()
        for ko, mid, list_tn in top:
            m = struct_map.get(mid)
            # 2026-08-24 用户铁律: token 返回什么字符就映射什么名称, 绝不改写.
            # frmhn/frman 是字母代码列表(如 ['A']), 不是队名, 永不作兜底 → 否则 .strip() 对列表抛错.
            # 只用 mhn/man(=token 返回的真实队名), 缺失则跳过该场(不伪造).
            mhn = (m.get('mhn') or '').strip() if m else ''
            man = (m.get('man') or '').strip() if m else ''
            if not mhn or not man:
                continue
            mk = f"{mhn} vs {man}"
            if mk in seen:
                continue
            seen.add(mk)
            league = (m.get('tnjc') or m.get('csna') or list_tn or '').strip() if m else list_tn
            out.append({
                'match_key': mk,
                'home': mhn, 'away': man,
                'league': league,
                'score': '0-0', 'minute': 0,
                'kickoff': _unix_to_gmt8_str(ko),
                'last_seen': now,
                'is_scheduled': True,
                'half_signal': 'NO_EDGE', 'half_direction': None, 'half_prob': 0.5,
                'full_signal': 'NO_EDGE', 'full_direction': None, 'full_prob': 0.5,
            })
        out.sort(key=lambda x: x['kickoff'])  # kickoff 升序=越近越前
    except Exception:
        # 接口异常不影响 live 主路径; 返回已缓存(可能空)
        pass
    return out


def _feed_upcoming_refresh():
    """后台刷新 feed 未开赛缓存(网络调用, 可能数秒), 不阻塞列表请求路径。"""
    try:
        out = _fetch_feed_upcoming_raw()
        with _FEED_UPCOMING_LOCK:
            _FEED_UPCOMING_CACHE['data'] = out
            _FEED_UPCOMING_CACHE['ts'] = time.time()
    except Exception:
        pass
    finally:
        with _FEED_UPCOMING_LOCK:
            _FEED_UPCOMING_CACHE['refreshing'] = False


def _collect_feed_upcoming(limit: int = 50):
    """返回缓存的 feed 未开赛比赛。

    ⚠ 2026-08-21 性能根治: 原实现在列表请求路径内**同步**打乐鱼 feed
    (fetch_match_list 全量 + 12 批 structure 请求 ≈ 6.2s), 这是列表耗时 7s 的真正根因
    (比 _lightweight_signal 更重)。改为: 列表路径**永远只读缓存**(毫秒级); 缓存过期/为空时
    在守护线程里后台刷新, 本次直接返回旧缓存(首次为空则下一轮轮询补齐)。这样列表路径彻底
    不再被网络延迟拖垮, 前端 5s 轮询不会再打满 to_thread 线程池。
    """
    now = time.time()
    with _FEED_UPCOMING_LOCK:
        fresh = (now - _FEED_UPCOMING_CACHE['ts']) < 120
        refreshing = _FEED_UPCOMING_CACHE['refreshing']
        data = _FEED_UPCOMING_CACHE['data']
    if fresh:
        return data
    # 过期/空: 仅启动一次后台刷新(避免重复 spawn), 立即返回当前缓存(可能为空, 下轮补齐)
    if not refreshing:
        with _FEED_UPCOMING_LOCK:
            _FEED_UPCOMING_CACHE['refreshing'] = True
        try:
            threading.Thread(target=_feed_upcoming_refresh, daemon=True).start()
        except Exception:
            with _FEED_UPCOMING_LOCK:
                _FEED_UPCOMING_CACHE['refreshing'] = False
    return data


def _coalesce_cache(ttl: float = 3.0):
    """单飞 + 短 TTL 缓存装饰器。

    根治列表端点并发退化: 前端 5s 轮询 + 多窗口 → 多个列表请求同时打到 7.9GB WAL 库,
    与 collector 持续写竞争 → 并发 15+ 时 DB 查询排队爆炸(实测 wall 21s@15并发、47s@30并发)。
    本装饰器保证同一 key 同一时刻只有一个请求真正计算, 其余命中缓存(微秒级返回),
    将 DB 压力从 N 次/轮询 降到 1 次/TTL。TTL=3s 与前端 5s 轮询匹配, 实时性足够。
    """
    import functools, threading
    def decorator(fn):
        _cache = {}
        _lock = threading.Lock()
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            key = (args, tuple(sorted(kwargs.items())))
            now = time.time()
            with _lock:
                c = _cache.get(key)
                if c is not None and (now - c[0]) < ttl:
                    return c[1]
                data = fn(*args, **kwargs)  # 持锁期间独占计算, 其余请求在锁外等待后命中缓存
                _cache[key] = (now, data)
                return data
        return wrapper
    return decorator


def _attach_match_meta(con, matches):
    """批量给 matches 挂乐鱼扩展赛事内容 (match_meta: 伤病/前瞻休整/情报)。
    由 gq/content_collector.py 采集; 无数据场 m['meta']=None, 不伪造。
    摘要字段: injuries_count / injuries(主客列表) / rest_days / news。
    """
    if not matches:
        return
    keys = [m.get('match_key') for m in matches if m.get('match_key')]
    if not keys:
        return
    import json as _json
    qmarks = ','.join('?' * len(keys))
    try:
        rows = con.execute(
            f"SELECT match_key, preview, injuries_home, news FROM match_meta "
            f"WHERE match_key IN ({qmarks})",
            keys,
        ).fetchall()
    except Exception:
        for m in matches:
            m['meta'] = None
        return
    meta_map = {r[0]: r for r in rows}
    for m in matches:
        r = meta_map.get(m.get('match_key'))
        if not r:
            m['meta'] = None
            continue
        preview = injuries = None
        try:
            preview = _json.loads(r[1]) if r[1] else None
        except Exception:
            preview = None
        try:
            injuries = _json.loads(r[2]) if r[2] else None
        except Exception:
            injuries = None
        # 伤病摘要: injuries = {"1": [...主队], "2": [...客队]}
        inj_list = []
        inj_count = 0
        if isinstance(injuries, dict):
            for side_key, side_label in (("1", "主"), ("2", "客")):
                items = injuries.get(side_key)
                if isinstance(items, list):
                    for it in items[:6]:
                        if isinstance(it, dict) and it.get('playerName'):
                            inj_list.append({
                                'side': side_label,
                                'player': it.get('playerName'),
                                'pos': it.get('positionName'),
                                'reason': it.get('reason'),
                            })
                            inj_count += 1
        # 前瞻休整: preview = {"1": [...未来赛程], "2": [...]}, 取最近 intervalDay
        rest_days = None
        if isinstance(preview, dict):
            for side_key in ("1", "2"):
                items = preview.get(side_key)
                if isinstance(items, list) and items:
                    first = items[0]
                    if isinstance(first, dict) and first.get('intervalDay') is not None:
                        try:
                            rest_days = max(rest_days or 0, int(first['intervalDay']))
                        except Exception:
                            pass
        m['meta'] = {
            'injuries': inj_list,
            'injuries_count': inj_count,
            'rest_days': rest_days,
            'news': bool(r[3]),
        }


def _attach_1x2_odds(con, matches):
    """批量给 matches 挂 1X2 即时盘(最新帧) + 初盘(最早帧) + OU 大小球 + CS 波胆矩阵。
    供手动预测对比"点击比赛 → 自动回填"使用 (入参对齐 NFTB: 队名/联赛/1X2/OU/CS)。
    无数据场诚实置 None, 不伪造。
    """
    if not matches:
        return
    keys = [m.get('match_key') for m in matches if m.get('match_key')]
    if not keys:
        return
    qmarks = ','.join('?' * len(keys))
    rows = con.execute(
        f"SELECT match_key, market, selection, odds, captured_at FROM odds_snapshots "
        f"WHERE match_key IN ({qmarks})",
        keys,
    ).fetchall()
    # 按 (match_key, market) 分帧
    by_key = {}
    for mk, market, sel, odds, ts in rows:
        by_key.setdefault(mk, {}).setdefault(market, []).append((ts, sel, odds))
    for m in matches:
        mk = m.get('match_key')
        markets = by_key.get(mk)
        if not markets:
            m['odds_h'] = m['odds_d'] = m['odds_a'] = None
            m['opening_h'] = m['opening_d'] = m['opening_a'] = None
            m['ou_line'] = m['ou_over'] = m['ou_under'] = None
            m['ou_op_line'] = m['ou_op_over'] = m['ou_op_under'] = None
            m['cs_top'] = None
            continue
        # ── 1X2: 初盘=最早帧 / 即时=最新帧 ──
        x12 = markets.get('1X2')
        if x12:
            x12.sort(key=lambda x: x[0])
            first_ts = x12[0][0]
            op = {sel: odds for ts, sel, odds in x12 if abs(ts - first_ts) < 5}
            last_ts = x12[-1][0]
            cur = {sel: odds for ts, sel, odds in x12 if abs(ts - last_ts) < 5}
            m['opening_h'] = op.get('home'); m['opening_d'] = op.get('draw'); m['opening_a'] = op.get('away')
            m['odds_h'] = cur.get('home'); m['odds_d'] = cur.get('draw'); m['odds_a'] = cur.get('away')
        else:
            m['odds_h'] = m['odds_d'] = m['odds_a'] = None
            m['opening_h'] = m['opening_d'] = m['opening_a'] = None
        # ── OU: market 名 'OU_2.50' 含 line, selection=over/under; 排除 1H/2H 半场线 ──
        # 2026-08-28: 过滤组合盘(OU_194.50 进球数/GOALS 盘等, 线值>15 非足球大小球线),
        # 否则最活跃盘可能是进球数盘 → ou_line=194.5 污染前端显示与漂移计算。
        def _ok_ou_line(k):
            try:
                line = float(k.split('_')[1])
                return 0.5 <= line <= 15.0
            except Exception:
                return False
        ou_mkts = [k for k in markets if k.startswith('OU_') and 'H_' not in k and '_H' not in k and _ok_ou_line(k)]
        m['ou_line'] = m['ou_over'] = m['ou_under'] = None
        m['ou_op_line'] = m['ou_op_over'] = m['ou_op_under'] = None
        if ou_mkts:
            # 全场主盘: 取快照数最多的 OU 线(最活跃盘口)
            line_mk = max(ou_mkts, key=lambda k: len(markets[k]))
            items = markets[line_mk]
            items.sort(key=lambda x: x[0])
            last_ts = items[-1][0]
            cur = {sel: odds for ts, sel, odds in items if abs(ts - last_ts) < 5}
            first_ts = items[0][0]
            op = {sel: odds for ts, sel, odds in items if abs(ts - first_ts) < 5}
            try:
                m['ou_line'] = float(line_mk.split('_')[1])
            except Exception:
                m['ou_line'] = None
            m['ou_over'] = cur.get('over'); m['ou_under'] = cur.get('under')
            m['ou_op_line'] = m['ou_line']
            m['ou_op_over'] = op.get('over'); m['ou_op_under'] = op.get('under')
        # ── CS 波胆: market='CS', selection='1:1', 取最新帧 top5 ──
        cs_items = markets.get('CS')
        m['cs_top'] = None
        if cs_items:
            cs_items.sort(key=lambda x: x[0])
            last_ts = cs_items[-1][0]
            frame = [(sel, odds) for ts, sel, odds in cs_items if abs(ts - last_ts) < 5 and ':' in str(sel)]
            frame.sort(key=lambda x: x[1])  # 赔率升序 = 最便宜在前
            m['cs_top'] = [[sel, odds] for sel, odds in frame[:8]]


@_coalesce_cache(ttl=3.0)
def list_live_matches(limit=50, offset=0):
    """列出当前进行中(live)与未开赛(scheduled)的比赛, 并按破蛋/进球潜力排序。
    live 在前(优先级降序), scheduled 在后(kickoff 升序, 越近越前)。
    支持 offset 分页 (解决 live 场次过多被 limit 静默截断导致比赛"消失")。
    返回 {"matches": [...], "max_last_seen", "total_live", "total_scheduled", "offset", "limit"}。
    """
    con = _open_gq()  # 只读连接: timeout=5 + wal_autocheckpoint=0, 避免读被自身 checkpoint 阻塞
    try:
        cur = con.cursor()
        cutoff = time.time() - 3600
        now = time.time()
        # 2026-08-23 修复: 原 raw LIMIT=limit*3 且僵尸过滤后仅取前 limit 行 → 排序切片前已截断,
        # 导致 live 场次多时第 50 名之后的比赛永远进不了列表。改用 RAW_CAP 取全量候选再排序分页。
        raw_cap = max(600, limit * 3)

        # ── 进行中(live): 只纳入"真实在进行"的比赛 ──
        # ⚠ 2026-08-21 重大修复: 原 SQL 带 `minute < 90` 过滤僵尸, 但实测 feed 对
        # **整个下半场恒报 minute=90** → 所有下半场比赛被这条 WHERE 直接筛掉
        # (抽样: live 中 minute=45 有 27 场、minute=90 有 8 场, 后者全部消失),
        # 这是"又不显示比赛了"反复复发的第二根因(第一根因是列表跑完整 probe 的超时)。
        # 僵尸判据改用 kickoff 真实经过时长(>150min 才算卡 live 未归档), 不再信 feed minute。
        live_rows_raw = cur.execute("""
            SELECT match_key, home, away, league, score_home, score_away, minute, kickoff, last_seen
            FROM matches
            WHERE status='live' AND last_seen > ?
            ORDER BY last_seen DESC
            LIMIT ?
        """, (cutoff, raw_cap)).fetchall()
        live_rows = []
        for _r in live_rows_raw:
            _ko = _parse_kickoff(_r[7])
            if _ko is not None and (now - _ko) / 60.0 > 150:
                continue  # 开赛已超 150min(含加时点球仍足够) → 僵尸, 排除
            live_rows.append(_r)
            if len(live_rows) >= raw_cap:
                break

        # ── 未开赛(scheduled): 已在采集列表中、kickoff 在 (now, now+48h] ──
        # kickoff 是 TEXT('YYYY-MM-DD HH:MM', GMT+8), 不能与 Unix 浮点直接比较(SQLite 中
        # TEXT>REAL 恒真), 故先全量取 status='scheduled', 再在 Python 用 _parse_kickoff 做时间窗过滤。
        # last_seen 仅作 24h 健全性闸门(避开数周前的僵尸未开赛记录); 采集器对 scheduled 的
        # last_seen 刷新不频繁, 故窗口放宽到 24h(而非 live 的 1h)。
        sched_rows = cur.execute("""
            SELECT match_key, home, away, league, score_home, score_away, minute, kickoff, last_seen
            FROM matches
            WHERE status='scheduled'
        """).fetchall()
        ko_min, ko_max = now, now + 48 * 3600
        ls_min = now - 24 * 3600
        sched_filtered = []
        for r in sched_rows:
            ko = _parse_kickoff(r[7])
            if ko is None or not (ko_min < ko <= ko_max):
                continue
            if not r[8] or r[8] < ls_min:
                continue
            sched_filtered.append(r)
        sched_filtered.sort(key=lambda r: _parse_kickoff(r[7]) or 0)  # kickoff 升序=越近越前
        sched_filtered = sched_filtered[:limit]

        def row_to_base(r, is_scheduled):
            feed_minute = r[6] if r[6] is not None else 0
            # ⚠ 2026-08-21 重大修复(推翻同日早前"优先信任 feed minute"的判断):
            # 实测 feed minute 整个上半场恒报 45、整个下半场恒报 90(占位垃圾),
            # 97.7% 快照 minute_at 亦为 45/90。改用 resolve_true_minute:
            # kickoff 真实时钟精算分钟, feed 的 45/90 只当"半场标识"。
            if is_scheduled:
                minute, is_halftime, m_phase, m_src = 0, False, 'pre', 'kickoff'
            else:
                tm = resolve_true_minute(r[7], feed_minute, now_ts=now)
                minute = tm['minute']
                is_halftime = tm['is_halftime']
                m_phase, m_src = tm['phase'], tm['source']
            # 2026-08-27 修复(用户: 重建采集器后不显示比分): 原 `f"{r[4] or 0}-{r[5] or 0}"`
            # 把 DB score=None(乐鱼 obscure 场不推 C103 比分帧)塞成假 "0-0" → 前端永远显示 0-0,
            # "比分待采集"提示走不到。改为: 无比分 → score=None, 前端诚实显示"比分待采集"。
            score_str = None
            if r[4] is not None and r[5] is not None:
                score_str = f"{r[4]}-{r[5]}"
            return {
                'match_key': r[0], 'home': r[1], 'away': r[2], 'league': r[3],
                'score': score_str, 'minute': minute, 'kickoff': r[7],
                'last_seen': r[8], 'is_scheduled': is_scheduled, 'is_halftime': is_halftime,
                'feed_minute': feed_minute, 'minute_phase': m_phase, 'minute_source': m_src,
            }

        # 2026-08-21 性能根治: 列表信号**批量预取** (一条 IN SQL 取全部场次的 OU 快照, Python 侧
        # 分组算信号), 替代逐场 _lightweight_signal (50 场 × 0.14s ≈ 7s, 打满 to_thread 线程池)。
        # ou_drift 侧栏信息不参与排序/优先级, 延后到 final(已切片)再对"进行中"比赛逐场补算, 成本受 limit 约束。
        live_base = [row_to_base(r, False) for r in live_rows]
        sched_base = [row_to_base(r, True) for r in sched_filtered]
        sig_map = _lightweight_signals_batch(con, live_base + sched_base)

        def score_one(m):
            p = sig_map.get(m['match_key'])
            if p is None:
                p = {
                    'current_score': m.get('score', '0-0'), 'current_minute': m.get('minute', 0),
                    'half': {'signal': 'NO_EDGE', 'direction': None, 'prob': 0.5},
                    'full': {'signal': 'NO_EDGE', 'direction': None, 'prob': 0.5},
                }
            if m.get('is_scheduled'):
                # 未开赛: 保留概率与方向, 但优先级清零(不参与 live 排序插队); 无 live 漂移
                return {
                    **m,
                    'priority': 0,
                    'half_signal': p['half']['signal'],
                    'half_direction': p['half']['direction'],
                    'half_prob': p['half']['prob'],
                    'half_inducement': p['half'].get('inducement'),
                    'full_signal': p['full']['signal'],
                    'full_direction': p['full']['direction'],
                    'full_prob': p['full']['prob'],
                    'full_inducement': p['full'].get('inducement'),
                    'ou_drift': None,
                }
            return {
                **m,
                'priority': _priority_from_probe(p),
                'half_signal': p['half']['signal'],
                'half_direction': p['half']['direction'],
                'half_prob': p['half']['prob'],
                'half_inducement': p['half'].get('inducement'),
                'full_signal': p['full']['signal'],
                'full_direction': p['full']['direction'],
                'full_prob': p['full']['prob'],
                'full_inducement': p['full'].get('inducement'),
                'ou_drift': None,
            }

        # 进行中: 按优先级(破蛋潜力)降序、比赛分钟降序
        ascore = [score_one(m) for m in live_base]
        ascore.sort(key=lambda x: (-x['priority'], -(x['minute'] or 0)))
        live_scored = ascore

        # 2026-08-23 审计修复: 列表徽标预测也快照进账本 (model_tag='list_badge', phase='list'),
        # 按方向变化去重, 使用户在列表中看到的任何预测都有审计痕迹。覆盖全部候选(非仅本页)。
        try:
            from pipeline.prediction_ledger import record_list_badges_batch
            _badge_rows = []
            for _m in (live_base + sched_base):
                _p = sig_map.get(_m['match_key'])
                if not isinstance(_p, dict):
                    continue
                for _market, _side in (('OU_1H', _p.get('half')), ('OU', _p.get('full'))):
                    if not isinstance(_side, dict):
                        continue
                    _d = _side.get('direction')
                    if _d not in ('OVER', 'UNDER'):
                        continue
                    _badge_rows.append({
                        'match_key': _m['match_key'], 'market': _market,
                        'line': _side.get('line'), 'direction': _d,
                        'signal': _side.get('signal'), 'prob': _side.get('prob'),
                    })
            record_list_badges_batch(_badge_rows)
        except Exception:
            pass

        # 未开赛: 已按 kickoff 升序过滤, 直接计分
        sched_scored = [score_one(m) for m in sched_base]

        max_last_seen = None
        for r in live_rows:
            if r[8] and (max_last_seen is None or r[8] > max_last_seen):
                max_last_seen = r[8]

        # 合并: 进行中在前(最多 limit 场, 优先保 live), 未开赛在后(最多 limit 场)。
        # 不再用总 limit 一刀切裁掉未开赛 —— 用户明确要求"未开赛也显示出来", 故
        # live 与 scheduled 各自保留上限 limit, 总返回量可达 2*limit(前端按数组渲染, 可滚动)。
        live_part = live_scored[offset:offset + limit]

        # 未开赛池 = DB scheduled(若有未来行) + feed 直采(主要来源, 带真实队名)
        sched_pool = list(sched_scored)
        feed_up = _collect_feed_upcoming(limit)
        sched_pool.extend(feed_up)
        seen_s = set(m['match_key'] for m in live_part)
        sched_dedup = []
        for m in sched_pool:
            if m['match_key'] in seen_s:
                continue
            seen_s.add(m['match_key'])
            sched_dedup.append(m)
        sched_dedup.sort(key=lambda x: x.get('kickoff') or '')  # kickoff 升序=越近越前
        sched_part = sched_dedup[:limit]

        final = live_part + sched_part

        # 2026-08-28: 批量补 1X2 即时盘 + 初盘 (手动预测对比"点击跳转自动回填"数据源)
        # 初盘 = 该场最早 1X2 快照帧, 即时盘 = 最新帧; 无比分/无赔率场诚实置 None。
        try:
            _attach_1x2_odds(con, final)
        except Exception:
            pass
        # 2026-08-28: 批量挂乐鱼扩展 match_meta (伤病/休整/情报) — content_collector 已采集
        try:
            _attach_match_meta(con, final)
        except Exception:
            pass

        # 延后补算 ou_drift (侧栏初滚漂移, 不参与排序): 仅对最终展示的前 50 场进行中比赛补算,
        # 成本受此硬上限约束, 杜绝 limit 放大后 N 场 × 0.05s 拖垮列表耗时。
        # 未开赛/feed 直采比赛无 live 轨迹, 保持 score_one 写入的 ou_drift=None。
        _drift_capped = 0
        for fm in final:
            if _drift_capped >= 50:
                break
            if fm.get('is_scheduled'):
                continue
            try:
                fm['ou_drift'] = get_ou_drift_summary(con, fm.get('match_key'))
            except Exception:
                fm['ou_drift'] = None
            _drift_capped += 1

        return {"matches": final, "max_last_seen": max_last_seen,
                "total_live": len(live_scored), "total_scheduled": len(sched_scored),
                "offset": offset, "limit": limit, "server_now": time.time()}
    finally:
        con.close()


# ════════════════════════════════════════════════════════════════════════════
# 三盘联合分析 (滚球神器 v2 基准) — 胜平负 + 让球 + 大小球 联合盘定
# 数据源: match_outcomes (健康表, 直接存初盘 opening odds, 不受 odds_snapshots 坏页影响)
# 平局信号: draw_module (初盘1X2去水p_d + 类型识别, 全量AUC=0.57, 已证伪"跨市场不对称=藏平局")
# ════════════════════════════════════════════════════════════════════════════
def analyze_three_market(match_key=None, mid=None, con=None):
    """对单场做 胜平负+让球+大小球 三盘联合分析, 输出类型/三线概率/平局判定/模型准确性。

    返回 dict。无初盘数据时返回 {available:False, reason:...} 供前端降级。
    match_key 与 mid 二选一; match_key 会经 matches.mid 解析到 match_outcomes。
    """
    own = con is None
    if own:
        con = _open_gq()
    try:
        cur = con.cursor()
        # 1) 解析 mid
        if not mid and match_key:
            r = cur.execute("SELECT mid FROM matches WHERE match_key=?", (match_key,)).fetchone()
            if r and r[0]:
                mid = r[0]
        if not mid:
            return {"available": False, "reason": "no_match_key_or_mid",
                    "match_key": match_key}
        # 2) 拉初盘 (match_outcomes, 健康表)
        row = cur.execute("""
            SELECT home,away,league,op_1x2_h,op_1x2_d,op_1x2_a,
                   op_ah_line,op_ah_home,op_ah_away,
                   op_ou_line,op_ou_over,op_ou_under,odds_type,result,score_home,score_away
            FROM match_outcomes WHERE mid=?
        """, (mid,)).fetchone()
        if not row:
            return {"available": False, "reason": "no_opening_odds", "mid": mid,
                    "match_key": match_key}
        (home, away, league, h, d, a,
         ahL, ahH, ahA, ouL, ouO, ouU, otype, result, sh, sa) = row

        wdl = {"h": h, "d": d, "a": a, "available": bool(h and d and a and h > 1)}
        ah = {"line": ahL, "home": ahH, "away": ahA,
              "available": bool(ahL is not None and ahH and ahA and ahH > 1 and ahA > 1)}
        ou = {"line": ouL, "over": ouO, "under": ouU,
              "available": bool(ouL and ouO and ouU and ouO > 1 and ouU > 1)}

        # 3) 去水概率
        pw = _dewater_1x2(h, d, a) if wdl["available"] else None
        if pw:
            wdl["p_home"], wdl["p_draw"], wdl["p_away"] = [round(x, 3) for x in pw]
        if ah["available"]:
            ph = 1.0 / ahH; pa = 1.0 / ahA; s = ph + pa
            ah["p_fav_cover"] = round(max(ph, pa) / s, 3)   # 让球方(低赔)覆盖概率
            ah["fav"] = "home" if ahH < ahA else "away"
        if ou["available"]:
            po = _dewatered_over_prob(ouO, ouU)
            ou["p_over"] = round(po, 3) if po else None

        # 4) 平局模块 (初盘1X2 p_d + 类型) — 全量实证信号
        draw_out = None
        if _dm_predict_draw and wdl["available"]:
            draw_out = _dm_predict_draw(h, d, a, ahL, ouL, ahH, ahA, ouO, ouU)
        # 5) 比赛类型 (优先用 GQ 已标注 odds_type, 退化用 draw_module 反解)
        mtype = otype if otype else None
        if (not mtype) and _dm_classify_type and wdl["available"]:
            mtype, _ = _dm_classify_type(h, d, a, ahL, ouL)

        # 6) 三线一致性诊断 (诚实: 仅作展示, 不作为平局信号 — 全量已证伪"不对称=藏平局")
        consistency = None
        if wdl.get("p_draw") is not None:
            signals = [wdl["p_draw"]]
            if ou["available"] and ou.get("p_over") is not None:
                # OU 隐含"非大球"=under概率, 与平局无直接映射, 仅记录
                signals.append(1.0 - ou["p_over"])
            if ah["available"] and ah.get("p_fav_cover") is not None:
                signals.append(1.0 - ah["p_fav_cover"])
            consistency = round(float(np.std(signals)), 3) if len(signals) > 1 else 0.0

        return {
            "available": True,
            "match_key": match_key, "mid": mid,
            "home": home, "away": away, "league": league,
            "match_type": mtype,
            "wdl": wdl, "ah": ah, "ou": ou,
            "draw": draw_out,
            "three_line_consistency": consistency,
            "source": "match_outcomes(opening)",
            "accuracy": {
                "draw_method": "初盘1X2去水p_d + 类型识别",
                "draw_auc_full": 0.570, "draw_auc_gq": 0.579,
                "wdl_argmax_hit": 0.512,
                "ou_edge_note": "仅 p_over>=0.55 有真实edge(实际大球63->81%)",
                "verified_on": "football_data 312K + GQ 6.9K",
            },
            "actual": ({"result": result, "score": f"{sh}-{sa}"} if result else None),
        }
    finally:
        if own:
            con.close()


def list_three_market_candidates(limit: int = 50):
    """返回含初盘赔率的比赛清单 (match_outcomes, 健康表), 供前端选场。"""
    con = _open_gq()
    try:
        rows = con.execute("""
            SELECT mid, home, away, league, odds_type, op_1x2_h, op_1x2_d, op_1x2_a, result
            FROM match_outcomes
            WHERE op_1x2_h>1 AND op_1x2_d>1 AND op_1x2_a>1
            ORDER BY captured_at DESC LIMIT ?
        """, (limit,)).fetchall()
        return [{"mid": r[0], "home": r[1], "away": r[2], "league": r[3],
                 "odds_type": r[4], "op_1x2": [r[5], r[6], r[7]],
                 "result": r[8]} for r in rows]
    finally:
        con.close()


if __name__ == '__main__':
    # 三盘联合分析自检
    import sqlite3 as _sq
    c = _open_gq()
    mids = [r[0] for r in c.execute(
        "SELECT mid FROM match_outcomes WHERE op_1x2_h>1 AND op_1x2_d>1 AND op_1x2_a>1 LIMIT 3")]
    c.close()
    for m in mids:
        print(json.dumps(analyze_three_market(mid=m), ensure_ascii=False, indent=2))
