#!/usr/bin/env python3
"""
哨响AI · 赔率结构向量库构建器
================================
从 events.db odds_snapshots 提取每场比赛的五维原始赔率，
关联 match_outcomes 赛果，构建相似度检索系统。

核心理念（涛哥指定）：
  不是"特征→赛果"分类器，而是"赔率结构→近邻→历史赛果分布"。
  保留 0.01 tick 精度，不做工程特征抽象。

五维赔率结构：
  1X2 — 3 维浮点 [h, d, a]
  AH  — 多线 [line, odds_home, odds_away] × N
  OU  — 多线 [line, odds_over, odds_under] × M
  CS  — 多比分 [score, odds] × K
  (上半场 OU/1X2 如有也纳入)

相似度：余弦相似度，五维加权融合。
输出：SQLite 向量库 + 检索 CLI。
"""

import sqlite3, json, os, math, sys, time, threading
from collections import defaultdict
from itertools import combinations
from typing import Dict, List, Tuple, Optional
import warnings
warnings.filterwarnings('ignore')

GQ_DB = r'D:\Architecture\data\events.db'
VECTOR_DB = r'D:\Architecture\data\odds_vectors.db'


# ─── 1. 数据采集: odds_snapshots → 按比赛聚合 ────────────────────────
def collect_match_odds() -> List[Dict]:
    """从 odds_snapshots 提取每场比赛的最后一轮赔率快照（收盘值）"""
    c = sqlite3.connect(GQ_DB)
    c.row_factory = sqlite3.Row

    # 取每场比赛每条 market+selection 的最后一帧（最大 captured_at）
    # odds_snapshots 有行级时间戳，取每组合的最后一条
    print('[1/5] 扫描 odds_snapshots 取各市场收盘赔率...')
    c.execute("""
        CREATE TEMP TABLE _last_snap AS
        SELECT match_key, market, selection, odds, captured_at
        FROM odds_snapshots
        WHERE (
            market IN ('1X2','CS','OU_1H','1X2_1H')
            OR market LIKE 'AH_%'
            OR (market LIKE 'OU_%' AND market NOT LIKE 'OU_1H%' AND market NOT LIKE 'OU_2H%')
        )
          AND odds IS NOT NULL
          AND odds != ''
    """)

    # 取每 (match_key, market, selection) 的 max captured_at 行
    c.execute("""
        CREATE TEMP TABLE _final AS
        SELECT match_key, market, selection, odds,
               ROW_NUMBER() OVER (
                   PARTITION BY match_key, market, selection
                   ORDER BY captured_at DESC
               ) AS rn
        FROM _last_snap
    """)

    rows = c.execute("SELECT match_key, market, selection, odds FROM _final WHERE rn=1").fetchall()
    c.close()

    # 聚合为 match_key -> {market: [(selection, odds), ...]}
    agg = defaultdict(lambda: defaultdict(list))
    for r in rows:
        agg[r['match_key']][r['market']].append((r['selection'], float(r['odds'])))

    print(f'   原始比赛数: {len(agg)}')
    return agg


# ─── 2. 清洗 & 编码 ────────────────────────────────────────────────
CS_SCORES = [
    '0-0','1-0','0-1','1-1','2-0','0-2','2-1','1-2','2-2',
    '3-0','0-3','3-1','1-3','3-2','2-3','3-3',
    '4-0','0-4','4-1','1-4','4-2','2-4','4-3','3-4','4-4',
    'other'
]
AH_LINES = [-3.0, -2.75, -2.5, -2.25, -2.0, -1.75, -1.5, -1.25, -1.0, -0.75, -0.5, -0.25,
             0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0]
OU_LINES = [0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0, 3.25, 3.5, 3.75,
            4.0, 4.25, 4.5, 4.75, 5.0, 5.25, 5.5, 5.75, 6.0, 6.25, 6.5, 6.75, 7.0, 7.5, 8.0, 9.0, 10.0]

def _safe_key(line: float) -> str:
    """将盘口线转为 SQL-safe 列名: -0.25 -> m0_25, 3.0 -> p3_0"""
    if line < 0:
        return 'm' + str(abs(line)).replace('.', '_')
    return 'p' + str(line).replace('.', '_')


def norm_cs_score(sel: str) -> str:
    """归一化 CS selection: home/1-0→1-0, away/0-1→1-0(翻转)"""
    s = sel.strip()
    if s.startswith('home/'):
        return s[5:]
    if s.startswith('away/'):
        # away/0-1 → 1-0
        parts = s[5:].split('-')
        if len(parts) == 2:
            return f'{parts[1]}-{parts[0]}'
        return s[5:]
    # 直接比分或 other
    if s in CS_SCORES:
        return s
    if '-' in s and all(p.isdigit() for p in s.split('-')):
        return s
    return 'other'


def encode_match(match_key: str, markets: Dict[str, List]) -> Optional[Dict]:
    """将一场比赛的 odds_snapshot 聚合编码为结构字典

    markets 格式: {market_name: [(selection, odds), ...]}
    其中 market_name 编码了盘口线:
      '1X2' -> home/draw/away
      'AH_+0.25' / 'AH_-0.75' / 'AH_0.00' -> home/away (draw for int lines)
      'OU_2.75' -> over/under
      'CS' -> 比分选择
    """
    vec = {'match_key': match_key}

    # ── 1X2 ──
    x1 = {sel: odds for sel, odds in markets.get('1X2', [])}
    if set(x1.keys()) == {'home', 'draw', 'away'}:
        vec['x1_h'] = x1['home']
        vec['x1_d'] = x1['draw']
        vec['x1_a'] = x1['away']
    else:
        return None

    # ── AH: 从 market 名解析盘口线 ──
    ah_map = defaultdict(dict)  # line -> {home/away/draw: odds}
    for mkt, items in markets.items():
        if not mkt.startswith('AH_'):
            continue
        # 解析盘口线: AH_+0.25 / AH_-0.75 / AH_0.00 / AH_-1.0
        line_str = mkt[3:]  # 去掉 'AH_'
        # 处理负号规范化: -1.0 -> -1.00
        try:
            line = float(line_str.replace('+', ''))
        except ValueError:
            continue
        for sel, odds in items:
            if sel in ('home', 'away', 'draw'):
                ah_map[line][sel] = odds

    for line in AH_LINES:
        entry = ah_map.get(line, {})
        vec[f'ah_h_{_safe_key(line)}'] = entry.get('home', None)
        vec[f'ah_a_{_safe_key(line)}'] = entry.get('away', None)

    # ── OU: 从 market 名解析盘口线 ──
    ou_map = defaultdict(dict)  # line -> {over/under: odds}
    for mkt, items in markets.items():
        if not mkt.startswith('OU_'):
            continue
        if mkt.startswith('OU_1H') or mkt.startswith('OU_2H'):
            continue  # 上半场/下半场分开处理
        line_str = mkt[3:]  # 去掉 'OU_'
        try:
            line = float(line_str)
        except ValueError:
            continue
        for sel, odds in items:
            if sel in ('over', 'under'):
                ou_map[line][sel] = odds

    for line in OU_LINES:
        entry = ou_map.get(line, {})
        vec[f'ou_o_{_safe_key(line)}'] = entry.get('over', None)
        vec[f'ou_u_{_safe_key(line)}'] = entry.get('under', None)

    # ── CS ── 波胆: 25 比分 + other
    cs_items = markets.get('CS', [])
    cs_map = {}
    for sel, odds in cs_items:
        score = norm_cs_score(sel)
        # 同比分多条取最低赔率（最被看好）
        if score in cs_map:
            cs_map[score] = min(cs_map[score], odds)
        else:
            cs_map[score] = odds
    for score in CS_SCORES:
        vec[f'cs_{score.replace("-","m").replace(".","_")}'] = cs_map.get(score, None)

    # ── 上半场 (附赠) ──
    x1h = {sel: odds for sel, odds in markets.get('1X2_1H', [])}
    if set(x1h.keys()) == {'home', 'draw', 'away'}:
        vec['x1h_h'] = x1h['home']
        vec['x1h_d'] = x1h['draw']
        vec['x1h_a'] = x1h['away']
    else:
        vec['x1h_h'] = vec['x1h_d'] = vec['x1h_a'] = None

    return vec


# ─── 3. 关联赛果 ──────────────────────────────────────────────────
def join_results(vectors: List[Dict]) -> List[Dict]:
    """通过 matches 表桥接 match_outcomes，给向量打赛果标签"""
    c = sqlite3.connect(GQ_DB)
    c.row_factory = sqlite3.Row

    # matches 索引
    mk_to_match = {}
    for r in c.execute("SELECT match_key, home, away, league, kickoff FROM matches"):
        mk_to_match[r['match_key']] = {
            'home': r['home'], 'away': r['away'],
            'league': r['league'], 'kickoff': r['kickoff']
        }

    # match_outcomes 索引: (home, away, kickoff_大概) → result
    # 用 home+away 做 key
    mo_key = {}
    for r in c.execute("SELECT home, away, result, score_home, score_away FROM match_outcomes WHERE result IN ('home','draw','away')"):
        k = (r['home'].strip(), r['away'].strip())
        mo_key[k] = {'result': r['result'], 'score_home': r['score_home'], 'score_away': r['score_away']}

    c.close()

    print(f'[2/5] 关联赛果: matches={len(mk_to_match)} match_outcomes={len(mo_key)}')

    labeled = []
    for v in vectors:
        mk = v['match_key']
        m = mk_to_match.get(mk)
        if not m:
            continue
        v['home'] = m['home']
        v['away'] = m['away']
        v['league'] = m['league']
        v['kickoff'] = m['kickoff']

        rk = (m['home'].strip(), m['away'].strip())
        result = mo_key.get(rk)
        if result:
            v['result'] = result['result']
            v['score_home'] = result['score_home']
            v['score_away'] = result['score_away']
            labeled.append(v)
        else:
            v['result'] = None
            v['score_home'] = None
            v['score_away'] = None
            # 仍保留（未来可打标签），但不进检索库
            # labeled.append(v)

    print(f'   有赛果标签: {len(labeled)} / 总 {len(vectors)}')
    return labeled


# ─── 4. 向量化 & 存库 ──────────────────────────────────────────────
def build_vector_db(labeled: List[Dict]):
    """将带标签的结构字典写入 SQLite 向量库"""
    c = sqlite3.connect(VECTOR_DB)
    c.execute("DROP TABLE IF EXISTS odds_vectors")
    c.execute("DROP TABLE IF EXISTS vector_meta")

    # 收集所有字段名
    all_keys = set()
    for v in labeled:
        for k in v:
            if k != 'match_key':
                all_keys.add(k)
    sorted_keys = sorted(all_keys)

    # 建表（稀疏列，大量 NULL）
    col_defs = ', '.join(f'"{k}" REAL' if not k.startswith('result') and not k.startswith('h_goals') and not k.startswith('a_goals')
                          and k not in ('home','away','league','kickoff')
                          else f'"{k}" TEXT'
                          for k in sorted_keys)
    c.execute(f'CREATE TABLE odds_vectors (match_key TEXT PRIMARY KEY, {col_defs})')

    # 插入
    for v in labeled:
        cols = ['match_key'] + [k for k in sorted_keys]
        vals = []
        for k in cols:
            val = v.get(k)
            vals.append(val)
        placeholders = ','.join('?' * len(cols))
        c.execute(f'INSERT OR REPLACE INTO odds_vectors ({",".join(cols)}) VALUES ({placeholders})', vals)

    # 建索引
    c.execute('CREATE INDEX IF NOT EXISTS idx_result ON odds_vectors(result)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_league ON odds_vectors(league)')

    c.commit()
    c.close()

    print(f'[3/5] 向量库落盘: {VECTOR_DB} ({len(labeled)} 场)')
    return sorted_keys


# ─── 5. 相似度检索 ─────────────────────────────────────────────────
VEC_DIMS = {
    '1X2': ['x1_h', 'x1_d', 'x1_a'],
    'AH': [f'ah_h_{_safe_key(l)}' for l in AH_LINES] + [f'ah_a_{_safe_key(l)}' for l in AH_LINES],
    'OU': [f'ou_o_{_safe_key(l)}' for l in OU_LINES] + [f'ou_u_{_safe_key(l)}' for l in OU_LINES],
    'CS': [f'cs_{s.replace("-","m").replace(".","_")}' for s in CS_SCORES],
}
VEC_WEIGHTS = {'1X2': 0.15, 'AH': 0.30, 'OU': 0.30, 'CS': 0.15}  # AH/OU优先，1X2+CS压到配角


def cosine_sim(a, b):
    """两个向量（含 None）的余弦相似度。输入已归一化的概率值"""
    dot = 0.0; na = 0.0; nb = 0.0
    for x, y in zip(a, b):
        if x is None or y is None:
            continue
        dot += x * y
        na += x * x
        nb += y * y
    if na < 1e-12 or nb < 1e-12:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def odds_to_prob(o):
    """赔率 → 隐含概率（去水前）。无限大/None → None"""
    if o is None or o <= 0:
        return None
    return 1.0 / o


def match_similarity(m1: Dict, m2: Dict) -> float:
    """计算两场比赛的赔率结构相似度（加权融合，概率空间）。
    必须双方至少覆盖 2/4 维度（不含上半场）才计算，防止单维度灌水。"""
    total = 0.0
    total_w = 0.0
    dims_used = 0
    q_dims = 0

    for dim, keys in VEC_DIMS.items():
        v1_raw = [m1.get(k) for k in keys]
        v2_raw = [m2.get(k) for k in keys]
        v1 = [odds_to_prob(x) for x in v1_raw]
        v2 = [odds_to_prob(x) for x in v2_raw]

        valid1 = sum(1 for x in v1 if x is not None)
        valid2 = sum(1 for x in v2 if x is not None)
        if valid1 >= 2:
            q_dims += 1
        if valid1 < 2 or valid2 < 2:
            continue
        sim = cosine_sim(v1, v2)
        w = VEC_WEIGHTS.get(dim, 0.2)
        total += sim * w
        total_w += w
        dims_used += 1

    # 上半场 1X2 如果双方都有
    if 'x1h_h' in m1 and m1['x1h_h'] and 'x1h_h' in m2 and m2['x1h_h']:
        x1h1 = [odds_to_prob(m1.get('x1h_h')), odds_to_prob(m1.get('x1h_d')), odds_to_prob(m1.get('x1h_a'))]
        x1h2 = [odds_to_prob(m2.get('x1h_h')), odds_to_prob(m2.get('x1h_d')), odds_to_prob(m2.get('x1h_a'))]
        if all(x is not None for x in x1h1 + x1h2):
            sim = cosine_sim(x1h1, x1h2)
            total += sim * 0.10
            total_w += 0.10

    # query(m1) 自身维度不足(<2)时(如纯1X2手工查询), 放宽到"query提供的维度全部有效即可"
    # 仍受下方 min_sim 约束保证质量; 完整比赛(q_dims>=2)行为不变(零回归).
    min_dims = 2 if q_dims >= 2 else max(q_dims, 1)
    if dims_used < min_dims:
        return 0.0

    return total / total_w if total_w > 0 else 0.0


def query_similar(match_key: str, k: int = 10, min_sim: float = 0.80) -> List[Dict]:
    """检索与目标比赛最相似的 K 场比赛"""
    c = sqlite3.connect(VECTOR_DB)
    c.row_factory = sqlite3.Row

    # 查目标
    target = c.execute('SELECT * FROM odds_vectors WHERE match_key=?', (match_key,)).fetchone()
    if not target:
        c.close()
        return []

    target_dict = dict(target)

    # 取所有有标签的
    pool = [dict(r) for r in c.execute(
        "SELECT * FROM odds_vectors WHERE result IS NOT NULL AND match_key!=?",
        (match_key,)
    )]
    c.close()

    # 计算相似度并排序
    scored = []
    for m in pool:
        sim = match_similarity(target_dict, m)
        if sim >= min_sim:
            scored.append((sim, m))

    scored.sort(key=lambda x: -x[0])
    return scored[:k]


# ── query_by_odds 结果缓存 (2026-08-06) ──────────────────────────────
# 背景: batch_confidence 批量把握度会对每场 predict() → query_by_odds 全表扫
#       892万行向量库(纯Python cosine_sim, 秒级/次). 批量30场=30次全库扫描,
#       且 bridge 侧已把调用丢线程池, 多线程并发全表扫浪费严重.
# 方案: 同 (1X2, AH, OU, CS, k, min_sim) 赔率结构 120s 内直接命中缓存.
#       线程安全: bridge 多线程调用, 读写统一加锁.
_QB_CACHE: Dict[tuple, Tuple[float, List[Dict]]] = {}
_QB_CACHE_LOCK = threading.Lock()
_QB_CACHE_TTL = 120.0
_QB_CACHE_MAX = 2048


def _qbo_cache_key(x1_h, x1_d, x1_a, ah_data, ou_data, cs_data, k, min_sim):
    """构造可哈希缓存键 (赔率全部归一化到 3-4 位小数, 避免浮点噪声击穿缓存)."""
    ah = tuple(sorted((round(float(l), 3), round(float(h), 4), round(float(a), 4))
                      for l, h, a in (ah_data or [])))
    ou = tuple(sorted((round(float(l), 3), round(float(o), 4), round(float(u), 4))
                      for l, o, u in (ou_data or [])))
    cs = frozenset((str(s), round(float(v), 4)) for s, v in (cs_data or {}).items())
    return (round(float(x1_h), 4), round(float(x1_d), 4), round(float(x1_a), 4),
            ah, ou, cs, int(k), float(min_sim))


def query_by_odds(x1_h: float, x1_d: float, x1_a: float,
                  ah_data: List[Tuple[float, float, float]] = None,
                  ou_data: List[Tuple[float, float, float]] = None,
                  cs_data: Dict[str, float] = None,
                  k: int = 10, min_sim: float = 0.80) -> List[Dict]:
    """直接输入赔率数值查询 — 提供手工比赛的赔率结构"""
    # 缓存命中直接返回 (同赔率结构 120s 内不重扫 892万行)
    _ckey = _qbo_cache_key(x1_h, x1_d, x1_a, ah_data, ou_data, cs_data, k, min_sim)
    _now = time.time()
    with _QB_CACHE_LOCK:
        _hit = _QB_CACHE.get(_ckey)
        if _hit and (_now - _hit[0]) < _QB_CACHE_TTL:
            return _hit[1]
    # 构造查询向量
    query = {
        'x1_h': x1_h, 'x1_d': x1_d, 'x1_a': x1_a,
        'x1h_h': None, 'x1h_d': None, 'x1h_a': None,
    }
    for l in AH_LINES:
        query[f'ah_h_{_safe_key(l)}'] = None
        query[f'ah_a_{_safe_key(l)}'] = None
    for l in OU_LINES:
        query[f'ou_o_{_safe_key(l)}'] = None
        query[f'ou_u_{_safe_key(l)}'] = None
    for s in CS_SCORES:
        query[f'cs_{s}'] = None

    if ah_data:
        for line, h, a in ah_data:
            query[f'ah_h_{_safe_key(line)}'] = h
            query[f'ah_a_{_safe_key(line)}'] = a
    if ou_data:
        for line, o, u in ou_data:
            query[f'ou_o_{_safe_key(line)}'] = o
            query[f'ou_u_{_safe_key(line)}'] = u
    if cs_data:
        for score, odds in cs_data.items():
            safe_score = score.replace('-','m').replace('.','_')
            if f'cs_{safe_score}' in query:
                query[f'cs_{safe_score}'] = odds

    c = sqlite3.connect(VECTOR_DB)
    c.row_factory = sqlite3.Row
    pool = [dict(r) for r in c.execute("SELECT * FROM odds_vectors WHERE result IS NOT NULL")]
    c.close()

    scored = []
    for m in pool:
        sim = match_similarity(query, m)
        if sim >= min_sim:
            scored.append((sim, m))

    scored.sort(key=lambda x: -x[0])
    result = scored[:k]
    # 写缓存 (超上限时整体清空一次, 防内存无限增长)
    with _QB_CACHE_LOCK:
        if len(_QB_CACHE) >= _QB_CACHE_MAX:
            _QB_CACHE.clear()
        _QB_CACHE[_ckey] = (time.time(), result)
    return result


def summarize_neighbors(neighbors: List[Tuple[float, Dict]]) -> str:
    """汇总近邻的赛果分布"""
    if not neighbors:
        return "无足够相似比赛。"
    results = defaultdict(int)
    goaltot = []
    sims = []
    for sim, m in neighbors:
        r = m.get('result')
        if r:
            results[r] += 1
            hg = m.get('score_home')
            ag = m.get('score_away')
            if hg is not None and ag is not None:
                goaltot.append((int(hg), int(ag)))
        sims.append(sim)

    total = sum(results.values())
    lines = [f"近邻数: {len(neighbors)}  相似度范围: [{min(sims):.4f}, {max(sims):.4f}]"]
    lines.append(f"赛果分布 (共{total}场):")
    for rk in ['home', 'draw', 'away']:
        cnt = results.get(rk, 0)
        pct = cnt / total * 100 if total else 0
        name = {'home': '主胜', 'draw': '平局', 'away': '客胜'}[rk]
        lines.append(f"  {name}: {cnt}/{total} = {pct:.1f}%")

    if goaltot:
        avg_h = sum(h for h, a in goaltot) / len(goaltot)
        avg_a = sum(a for h, a in goaltot) / len(goaltot)
        lines.append(f"平均比分: {avg_h:.1f}-{avg_a:.1f}")

    return '\n'.join(lines)


# ─── 6. 主入口 ─────────────────────────────────────────────────────
def main():
    print('=' * 60)
    print('哨响AI 赔率结构向量库构建')
    print('=' * 60)

    # 采集
    agg = collect_match_odds()

    # 编码
    print('[2/5] 编码赔率结构...')
    vectors = []
    for mk, markets in agg.items():
        v = encode_match(mk, markets)
        if v:
            vectors.append(v)
    print(f'   有效编码: {len(vectors)} 场 (含 1X2/AH/OU/CS)')

    # 关联赛果
    labeled = join_results(vectors)

    # 建库
    sorted_keys = build_vector_db(labeled)

    # 统计
    print(f'[4/5] 向量维度: {len(sorted_keys)}')
    res_dist = defaultdict(int)
    for v in labeled:
        if v.get('result'):
            res_dist[v['result']] += 1
    print(f'   赛果分布: {dict(res_dist)}')
    print(f'   向量库文件: {VECTOR_DB}')

    # 快速验证: 随机取 3 场检索 Top5
    print('\n[5/5] 检索验证 (随机 3 场 × Top5 近邻)...')
    import random
    random.seed(42)
    sample = random.sample(labeled, min(3, len(labeled)))
    for v in sample:
        mk = v['match_key']
        vhome = v.get('home', '?')
        vaway = v.get('away', '?')
        vx1h = v.get('x1_h')
        vx1d = v.get('x1_d')
        vx1a = v.get('x1_a')
        vres = v.get('result', '?')
        print('\n  -- 查询: {} vs {} --'.format(vhome, vaway))
        print('     1X2: h={:.2f} d={:.2f} a={:.2f}'.format(vx1h, vx1d, vx1a))
        print('     实际: {}'.format(vres))
        nbs = query_similar(mk, k=7, min_sim=0.75)
        if nbs:
            print('     {}'.format('-'*40))
            for i, (sim, nb) in enumerate(nbs):
                nh = nb.get('home', '?')
                na = nb.get('away', '?')
                nr = nb.get('result', '?')
                nhg = nb.get('score_home', '?')
                nag = nb.get('score_away', '?')
                print('     #{} sim={:.4f} | {} vs {} -> {} ({}-{})'.format(i+1, sim, nh, na, nr, nhg, nag))
        else:
            print('     (无相似邻居)')

    print(f'\n{"="*60}')
    print('✅ 向量库构建完成。')
    print(f'   CLI 检索: python -c "from scripts.build_odds_vector_library import query_similar, summarize_neighbors; ..."')
    print(f'   或直接调用 query_by_odds() 手工输入赔率查询。')

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
