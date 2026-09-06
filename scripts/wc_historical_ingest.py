# -*- coding: utf-8 -*-
"""
WC 历史数据摄入 + 透明特征工程 (跨届 walk-forward 准备)
========================================================
数据源(本地, 禁虚拟):
  - tournament_history.json : WC2014/2018/2022 (各64, 中文队名, 有比分无日期无赔率)
  - matches 表 league_name='世界杯' 完赛 : WC2026 (含淘汰赛, 中英文混排)
  - wc2022_complete_with_odds.json : 2022 部分场赔率(并回)

设计:
  - 新建专用表 wc_all_matches / wc_features, 不污染生产 matches/match_features
  - 队名规范化(中英文别名→统一中文 canonical)
  - 特征=届间先验(仅用更早届聚合) + 历史交锋(H2H, 仅更早届) + 阶段
    + 丰富特征(B): 届内滚动(gf/ga/pts, 按stage顺序, KO用本属小组聚合) + 赔率隐含概率(imp_h/d/a) + 先验分差
    → 完全无泄漏(测试届看不到未来届); 历史届无matchday→组赛届内先验=0, 仅KO有届内信号
  - 2026 测试届: 先验来自 2014+2018+2022

用法: .venv/Scripts/python.exe scripts/wc_historical_ingest.py
"""
import sqlite3, os, json, re, collections

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'data', 'football_data.db')
TH = os.path.join(ROOT, 'data', 'tournament_history.json')
W22 = os.path.join(ROOT, 'data', 'wc2022_complete_with_odds.json')

# ---- 队名规范化: 英文→中文 + 中文别名统一 ----
ALIAS = {
    # 英文 -> 中文
    'Brazil': '巴西', 'Argentina': '阿根廷', 'France': '法国', 'Germany': '德国',
    'Spain': '西班牙', 'England': '英格兰', 'Belgium': '比利时', 'Portugal': '葡萄牙',
    'Netherlands': '荷兰', 'Italy': '意大利', 'Croatia': '克罗地亚', 'Uruguay': '乌拉圭',
    'Colombia': '哥伦比亚', 'Switzerland': '瑞士', 'Russia': '俄罗斯', 'Sweden': '瑞典',
    'Poland': '波兰', 'Denmark': '丹麦', 'Mexico': '墨西哥', 'Japan': '日本',
    'Korea Republic': '韩国', 'South Korea': '韩国', 'Senegal': '塞内加尔',
    'Morocco': '摩洛哥', 'Tunisia': '突尼斯', 'Iran': '伊朗', 'Australia': '澳大利亚',
    'Peru': '秘鲁', 'Nigeria': '尼日利亚', 'Egypt': '埃及', 'Serbia': '塞尔维亚',
    'Costa Rica': '哥斯达黎加', 'Iceland': '冰岛', 'Panama': '巴拿马',
    'Saudi Arabia': '沙特阿拉伯', 'Norway': '挪威', 'Ivory Coast': '科特迪瓦',
    'Czech Republic': '捷克', 'Ukraine': '乌克兰', 'Austria': '奥地利',
    'Canada': '加拿大', 'Cameroon': '喀麦隆', 'Ghana': '加纳', 'Ecuador': '厄瓜多尔',
    'Qatar': '卡塔尔', 'Wales': '威尔士', 'USA': '美国', 'United States': '美国',
    'Congo DR': '民主刚果', 'Algeria': '阿尔及利亚', 'Bosnia': '波黑',
    'Bosnia and Herzegovina': '波黑', 'Paraguay': '巴拉圭', 'Cape Verde': '佛得角',
    'South Africa': '南非', 'New Zealand': '新西兰', 'Romania': '罗马尼亚',
    'Slovakia': '斯洛伐克', 'Scotland': '苏格兰', 'Finland': '芬兰',
    'Turkey': '土耳其', 'Turkey': '土耳其', 'Congo': '刚果', 'Mali': '马里',
    'Honduras': '洪都拉斯', 'Chile': '智利', 'Bolivia': '玻利维亚',
    'Venezuela': '委内瑞拉', 'Greece': '希腊', 'Hungary': '匈牙利',
    # 中文别名统一
    '象牙海岸': '科特迪瓦', '伊朗伊斯兰共和国': '伊朗', '韩国共和国': '韩国',
    '民主刚果': '民主刚果', '刚果民主共和国': '民主刚果',
    '英格兰': '英格兰', '波斯尼亚': '波黑',
}
def norm_team(name):
    if name is None:
        return None
    n = name.strip()
    if n in ALIAS:
        return ALIAS[n]
    # 含中文直接保留(已规范)
    if re.search(r'[\u4e00-\u9fff]', n):
        return n
    # 其他英文: 尝试常见后缀清理后回退
    return n

def canon(name):
    return norm_team(name)

# ---- 阶段规范化 ----
def norm_stage(s):
    if not s:
        return 'group'
    s = str(s).upper()
    if 'GROUP' in s or s == 'GROUP':
        return 'group'
    if 'R16' in s or 'ROUND_OF_16' in s or '16' in s:
        return 'r16'
    if 'QUARTER' in s or 'QF' in s:
        return 'qf'
    if 'SEMI' in s or 'SF' in s:
        return 'sf'
    if 'THIRD' in s or '3RD' in s:
        return 'third'
    if 'FINAL' in s:
        return 'final'
    return 'ko'

def fr_from_scores(hg, ag):
    if hg is None or ag is None:
        return None
    if hg > ag: return 'H'
    if hg < ag: return 'A'
    return 'D'

def main():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.executescript("""
    DROP TABLE IF EXISTS wc_all_matches;
    DROP TABLE IF EXISTS wc_features;
    CREATE TABLE wc_all_matches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        edition TEXT, stage TEXT, home TEXT, away TEXT,
        hg INT, ag INT, final_result TEXT,
        oh REAL, od REAL, oa REAL
    );
    CREATE TABLE wc_features (
        match_id INTEGER, edition TEXT,
        home_prior_gp INT, home_prior_pts REAL, home_prior_gf REAL, home_prior_ga REAL,
        away_prior_gp INT, away_prior_pts REAL, away_prior_gf REAL, away_prior_ga REAL,
        h2h_hw INT, h2h_d INT, h2h_aw INT, h2h_n INT,
        stage_group INT, prior_available INT,
        -- 丰富特征 (B: 跨届walk-forward深化)
        home_intra_gf REAL, home_intra_ga REAL, home_intra_pts REAL,
        away_intra_gf REAL, away_intra_ga REAL, away_intra_pts REAL,
        imp_h REAL, imp_d REAL, imp_a REAL,
        prior_pts_diff REAL
    );
    """)
    conn.commit()

    matches = []  # list of dict

    # --- 1) tournament_history: 2014/2018/2022 ---
    th = json.load(open(TH, encoding='utf-8'))
    for m in th:
        comp = m.get('comp')
        if comp not in ('WC2014', 'WC2018', 'WC2022'):
            continue
        h = canon(m.get('home')); a = canon(m.get('away'))
        if not h or not a:
            continue
        hg = m.get('hg'); ag = m.get('ag')
        fr = fr_from_scores(hg, ag)
        if fr is None:
            continue
        matches.append({
            'edition': comp[2:], 'stage': norm_stage(m.get('stage')),
            'home': h, 'away': a, 'hg': hg, 'ag': ag, 'final_result': fr,
            'oh': None, 'od': None, 'oa': None,
        })

    # --- 2) 2026 from DB (清洗, 含淘汰赛) ---
    db_rows = c.execute(
        "SELECT home_team_name, away_team_name, home_score, away_score, final_result, matchday "
        "FROM matches WHERE league_name='世界杯' AND final_result IS NOT NULL "
        "AND home_team_name IS NOT NULL AND away_team_name IS NOT NULL"
    ).fetchall()
    for h, a, hs, as_, fr, md in db_rows:
        h = canon(h); a = canon(a)
        if not h or not a:
            continue
        # stage: matchday<=3 => group, else ko
        stage = 'group' if (md is not None and int(md) <= 3) else 'ko'
        matches.append({
            'edition': '2026', 'stage': stage,
            'home': h, 'away': a, 'hg': hs, 'ag': as_, 'final_result': fr,
            'md': md,
            'oh': None, 'od': None, 'oa': None,
        })

    # --- 3) 2022 赔率并回 (wc2022_complete_with_odds.json) ---
    w22 = json.load(open(W22, encoding='utf-8'))['data']
    odds_map = {}
    for m in w22:
        h = canon(m.get('home')); a = canon(m.get('away'))
        odds_map[(h, a)] = (m.get('oh'), m.get('od'), m.get('oa'))
    merged_odds = 0
    for mt in matches:
        if mt['edition'] == '2022':
            key = (mt['home'], mt['away'])
            if key in odds_map:
                oh, od, oa = odds_map[key]
                if oh and od and oa:
                    mt['oh'], mt['od'], mt['oa'] = oh, od, oa
                    merged_odds += 1

    # --- 3.5) 2026 小组赛真实赔率并回 (wc2026_group_odds_final.json) ---
    # (fold 进本脚本, 保证 re-run 后 wc_all_matches 含完整 2026 赔率, 无需单独跑 merge 脚本)
    O26 = os.path.join(ROOT, 'data', 'wc2026_group_odds_final.json')
    merged_odds26 = 0
    if os.path.exists(O26):
        o26 = json.load(open(O26, encoding='utf-8')).get('matches', [])
        om26 = {}
        for m in o26:
            h = canon(m.get('home_team')); a = canon(m.get('away_team'))
            oh = m.get('1x2_home'); od = m.get('1x2_draw'); oa = m.get('1x2_away')
            if h and a and oh and od and oa:
                om26[(h, a)] = (oh, od, oa)
        for mt in matches:
            if mt['edition'] == '2026' and (mt['oh'] is None or mt['od'] is None or mt['oa'] is None):
                key = (mt['home'], mt['away'])
                if key in om26:
                    mt['oh'], mt['od'], mt['oa'] = om26[key]; merged_odds26 += 1
                else:
                    # 反向尝试
                    rkey = (mt['away'], mt['home'])
                    if rkey in om26:
                        oa, od, oh = om26[rkey]
                        mt['oh'], mt['od'], mt['oa'] = oh, od, oa; merged_odds26 += 1

    # --- 写 wc_all_matches ---
    for mt in matches:
        c.execute(
            "INSERT INTO wc_all_matches (edition,stage,home,away,hg,ag,final_result,oh,od,oa) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (mt['edition'], mt['stage'], mt['home'], mt['away'], mt['hg'], mt['ag'],
             mt['final_result'], mt['oh'], mt['od'], mt['oa'])
        )
    conn.commit()
    n_by_ed = collections.Counter(m['edition'] for m in matches)
    print(f"[ingest] wc_all_matches total={len(matches)} by edition={dict(n_by_ed)}")
    print(f"[ingest] 2022 odds merged={merged_odds}; 2026 odds merged={merged_odds26}")

    # --- 特征: 届间先验(仅更早届) ---
    editions = ['2014', '2018', '2022', '2026']
    # 先按届聚合每队统计
    team_stats = {e: collections.defaultdict(lambda: {'gp':0,'w':0,'d':0,'l':0,'gf':0,'ga':0}) for e in editions}
    for mt in matches:
        e = mt['edition']
        for side, opp, gf, ga in [(mt['home'], mt['away'], mt['hg'], mt['ag']),
                                  (mt['away'], mt['home'], mt['ag'], mt['hg'])]:
            st = team_stats[e][side]
            st['gp'] += 1
            if gf > ga: st['w'] += 1
            elif gf < ga: st['l'] += 1
            else: st['d'] += 1
            st['gf'] += gf; st['ga'] += ga

    # 届序
    eidx = {e: i for i, e in enumerate(editions)}
    # H2H 累计(按届序逐个加入)
    h2h = collections.defaultdict(lambda: {'hw':0,'d':0,'aw':0,'n':0})

    # 遍历比赛(按届序), 计算该场先验(来自更早于当前届的所有届)
    STAGE_ORDER = ['group', 'ko', 'r16', 'qf', 'sf', 'third', 'final']
    def agg(p):
        if not p or p['gp'] == 0:
            return (0, 0.0, 0.0, 0.0)
        pts = p['w']*3 + p['d']
        return (p['gp'], round(pts/p['gp'], 3), round(p['gf']/p['gp'], 3), round(p['ga']/p['gp'], 3))
    for e in editions:
        # 当前届之前的所有届聚合 (届间先验, 无泄漏)
        prior = collections.defaultdict(lambda: {'gp':0,'w':0,'d':0,'l':0,'gf':0,'ga':0})
        for pe in editions:
            if eidx[pe] < eidx[e]:
                for team, st in team_stats[pe].items():
                    p = prior[team]
                    p['gp'] += st['gp']; p['w'] += st['w']; p['d'] += st['d']; p['l'] += st['l']
                    p['gf'] += st['gf']; p['ga'] += st['ga']
        cur = [mt for mt in matches if mt['edition'] == e]
        # 届内滚动状态累加器(按stage顺序: 组赛intra=0, KO用本属小组聚合)
        intra = collections.defaultdict(lambda: {'gp':0,'w':0,'d':0,'l':0,'gf':0,'ga':0})
        for stage_key in STAGE_ORDER:
            # 同stage内按matchday排序(2026有md→届内滚动无泄漏; 历史无md→保持插入序)
            sub = [mt for mt in cur if mt['stage'] == stage_key]
            sub.sort(key=lambda x: x.get('md') or 0)
            for mt in sub:
                mid = c.execute(
                    "SELECT id FROM wc_all_matches WHERE edition=? AND home=? AND away=? AND hg=? AND ag=?",
                    (e, mt['home'], mt['away'], mt['hg'], mt['ag'])
                ).fetchone()[0]
                # 届间先验
                hp = prior.get(mt['home']); ap = prior.get(mt['away'])
                prior_avail = 1 if (hp and hp['gp'] > 0) or (ap and ap['gp'] > 0) else 0
                hgp, hpt, hgf, hga = agg(hp)
                agp, apt, agf, aga = agg(ap)
                # 届内滚动(KO阶段=累加器; 组赛=0, 因无matchday无法排序, 防泄漏)
                if stage_key == 'group':
                    h_igf, h_iga, h_ip = (0.0, 0.0, 0.0)
                    a_igf, a_iga, a_ip = (0.0, 0.0, 0.0)
                else:
                    _, h_ip, h_igf, h_iga = agg(intra.get(mt['home']))
                    _, a_ip, a_igf, a_iga = agg(intra.get(mt['away']))
                # 赔率隐含概率(去中心化 margin)
                oh, od, oa = mt['oh'], mt['od'], mt['oa']
                if oh and od and oa:
                    ih, id_, ia = 1.0/oh, 1.0/od, 1.0/oa
                    s = ih + id_ + ia
                    imp_h, imp_d, imp_a = round(ih/s, 4), round(id_/s, 4), round(ia/s, 4)
                else:
                    imp_h = imp_d = imp_a = 0.0
                prior_pts_diff = round(hpt - apt, 3)
                hh = h2h[(mt['home'], mt['away'])]
                c.execute(
                    "INSERT INTO wc_features (match_id,edition,home_prior_gp,home_prior_pts,home_prior_gf,home_prior_ga,"
                    "away_prior_gp,away_prior_pts,away_prior_gf,away_prior_ga,h2h_hw,h2h_d,h2h_aw,h2h_n,stage_group,prior_available,"
                    "home_intra_gf,home_intra_ga,home_intra_pts,away_intra_gf,away_intra_ga,away_intra_pts,imp_h,imp_d,imp_a,prior_pts_diff) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (mid, e, hgp, hpt, hgf, hga, agp, apt, agf, aga,
                     hh['hw'], hh['d'], hh['aw'], hh['n'],
                     1 if mt['stage'] == 'group' else 0, prior_avail,
                     h_igf, h_iga, h_ip, a_igf, a_iga, a_ip, imp_h, imp_d, imp_a, prior_pts_diff)
                )
                # 更新 H2H(用本场结果, 供更晚届使用)
                r = mt['final_result']
                if r == 'H': hh['hw'] += 1
                elif r == 'D': hh['d'] += 1
                else: hh['aw'] += 1
                hh['n'] += 1
                # 更新届内累加器(组赛结果供KO; KO结果供后续KO, 均按stage顺序无泄漏)
                for side, opp, gf, ga in [(mt['home'], mt['away'], mt['hg'], mt['ag']),
                                          (mt['away'], mt['home'], mt['ag'], mt['hg'])]:
                    it = intra[side]
                    it['gp'] += 1
                    if gf > ga: it['w'] += 1
                    elif gf < ga: it['l'] += 1
                    else: it['d'] += 1
                    it['gf'] += gf; it['ga'] += ga
        conn.commit()

    # 统计
    feat_n = c.execute("SELECT COUNT(*) FROM wc_features").fetchone()[0]
    print(f"[ingest] wc_features rows={feat_n}")
    # 各届 prior_available
    for e in editions:
        pa = c.execute("SELECT COUNT(*) FROM wc_features WHERE edition=? AND prior_available=1", (e,)).fetchone()[0]
        tot = c.execute("SELECT COUNT(*) FROM wc_features WHERE edition=?", (e,)).fetchone()[0]
        print(f"  edition {e}: features={tot} prior_avail={pa}")
    # 未映射队名检查
    all_names = set()
    for mt in matches:
        all_names.add(mt['home']); all_names.add(mt['away'])
    print(f"[ingest] distinct teams={len(all_names)}")
    conn.close()
    print("[ingest] DONE")

if __name__ == '__main__':
    main()
