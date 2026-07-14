#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
P2 队名归一化 — 哨响AI v6.0
================================================================================
把分散在多表的队名统一到 canonical 规范名, 打通跨源 JOIN:
  - 中文体系 (teams / matches / william_ht / interwetten_odds / wc_all_matches)
        → 去空格 + 异体归一 (如 "莱切斯特城"→"莱斯特城", "托特纳姆"→"热刺")
  - 英文体系 (wc_xlsx_matches, 四届WC英文队名 Germany/Argentina…)
        → 中英映射到中文 canonical, 对齐中文 WC 体系 (wc_all_matches)

落库:
  - 新建 team_canonical 主映射表 (canonical / aliases_json / note) — 人类可读、可维护
  - 各源表加 *_norm 列 (幂等, 可重复跑)

设计原则: 单一真源铁律 — 只新增映射表与 norm 列, 不改写任何原队名字段。
"""
import sqlite3, json, re

DB = r'D:/Architecture/data/football_data.db'


def norm_key(s):
    """归一化键: 去所有空白 + 转小写 (中文无大小写影响, 英文对齐小写)。"""
    return re.sub(r'\s+', '', (s or '').strip()).lower()


# ---------- 1. 别名种子 ----------
# 五大联赛 + 通用异体/英文别名. key=规范中文名, value=别名列表(异体中文 + 英文)
EXTRA = {
    # 英超
    '热刺': ['托特纳姆热刺', '托特纳姆', 'Tottenham Hotspur', 'Tottenham'],
    '纽卡斯尔': ['纽卡斯尔联', '纽卡', 'Newcastle United', 'Newcastle'],
    '曼联': ['曼彻斯特联', '曼彻斯特联队', 'Manchester United', 'Man United', 'MU'],
    '曼城': ['曼彻斯特城', 'Manchester City', 'MC'],
    '莱斯特城': ['莱切斯特城', 'Leicester City', 'Leicester'],
    '西汉姆': ['西汉姆联', 'West Ham', 'West Ham United'],
    '狼队': ['Wolverhampton Wanderers', 'Wolverhampton', 'Wolves'],
    '布莱顿': ['Brighton & Hove Albion', 'Brighton'],
    '富勒姆': ['Fulham'],
    '水晶宫': ['Crystal Palace'],
    '伯恩茅斯': ['AFC Bournemouth', 'Bournemouth'],
    '布伦特福德': ['Brentford'],
    '阿森纳': ['Arsenal'],
    '切尔西': ['Chelsea'],
    '利物浦': ['Liverpool'],
    '阿斯顿维拉': ['Aston Villa'],
    '埃弗顿': ['Everton'],
    '诺丁汉森林': ['Nottingham Forest'],
    '南安普顿': ['Southampton'],
    '桑德兰': ['Sunderland'],
    '西布罗姆维奇': ['West Bromwich Albion', 'West Brom'],
    '斯托克城': ['Stoke City', 'Stoke'],
    '斯旺西': ['Swansea City', 'Swansea'],
    '加的夫城': ['Cardiff City', 'Cardiff'],
    '诺维奇': ['Norwich City', 'Norwich'],
    '雷丁': ['Reading'],
    '女王公园巡游者': ['Queens Park Rangers', 'QPR'],
    '赫尔城': ['Hull City', 'Hull'],
    '伯恩利': ['Burnley'],
    '利兹联': ['Leeds United', 'Leeds'],
    '米德尔斯堡': ['Middlesbrough'],
    '德比郡': ['Derby County', 'Derby'],
    # 西甲
    '巴塞罗那': ['Barcelona', 'Barça', 'Barca'],
    '皇家马德里': ['Real Madrid', 'Madrid'],
    '马德里竞技': ['Atletico Madrid', 'Atlético Madrid', 'Atletico', '马竞'],
    '瓦伦西亚': ['Valencia'],
    '塞维利亚': ['Sevilla', 'Seville'],
    '比利亚雷亚尔': ['Villarreal'],
    '毕尔巴鄂竞技': ['Athletic Bilbao', 'Athletic Club', 'Athletic'],
    '皇家社会': ['Real Sociedad', 'Sociedad'],
    '西班牙人': ['Espanyol'],
    '塞尔塔': ['Celta Vigo', 'Celta'],
    '格拉纳达': ['Granada'],
    '赫塔菲': ['Getafe'],
    '马拉加': ['Malaga'],
    '皇家贝蒂斯': ['Real Betis', 'Betis'],
    '拉科鲁尼亚': ['Deportivo La Coruna', 'Deportivo', '拉科'],
    '奥萨苏纳': ['Osasuna'],
    '莱万特': ['Levante'],
    '阿尔梅里亚': ['Almeria'],
    '埃尔切': ['Elche'],
    # 意甲
    '尤文图斯': ['Juventus', 'Juve'],
    'AC米兰': ['Milan', 'AC Milano', 'Milano'],
    '国际米兰': ['Inter', 'Internazionale', 'Inter Milan'],
    '那不勒斯': ['Napoli', 'Naples'],
    '罗马': ['Roma'],
    '拉齐奥': ['Lazio'],
    '佛罗伦萨': ['Fiorentina', 'Florence'],
    '亚特兰大': ['Atalanta'],
    '都灵': ['Torino', 'Turin'],
    '桑普多利亚': ['Sampdoria'],
    '乌迪内斯': ['Udinese'],
    '博洛尼亚': ['Bologna'],
    '热那亚': ['Genoa'],
    '卡利亚里': ['Cagliari'],
    '帕尔马': ['Parma'],
    '维罗纳': ['Hellas Verona', 'Verona'],
    '恩波利': ['Empoli'],
    '萨索洛': ['Sassuolo'],
    # 德甲
    '拜仁慕尼黑': ['Bayern Munich', 'Bayern', '拜仁'],
    '多特蒙德': ['Borussia Dortmund', 'Dortmund', 'BVB', '多特'],
    '勒沃库森': ['Bayer Leverkusen', 'Leverkusen'],
    '沙尔克04': ['Schalke 04', 'Schalke'],
    '门兴格拉德巴赫': ['Borussia Monchengladbach', 'Monchengladbach', 'Gladbach'],
    '沃尔夫斯堡': ['Wolfsburg'],
    '汉堡': ['Hamburg'],
    '斯图加特': ['Stuttgart'],
    '法兰克福': ['Eintracht Frankfurt', 'Frankfurt'],
    '柏林赫塔': ['Hertha Berlin', 'Hertha'],
    '科隆': ['Cologne', 'Koln'],
    '不莱梅': ['Werder Bremen', 'Bremen'],
    '美因茨': ['Mainz'],
    '霍芬海姆': ['Hoffenheim'],
    '弗赖堡': ['Freiburg'],
    '奥格斯堡': ['Augsburg'],
    '莱比锡红牛': ['RB Leipzig', 'Leipzig'],
    '柏林联合': ['Union Berlin', 'Union'],
    '波鸿': ['Bochum'],
    # 法甲
    '巴黎圣日耳曼': ['Paris Saint-Germain', 'PSG', 'Paris SG'],
    '马赛': ['Marseille'],
    '里昂': ['Lyon'],
    '摩纳哥': ['AS Monaco', 'Monaco'],
    '里尔': ['Lille'],
    '波尔多': ['Bordeaux'],
    '圣埃蒂安': ['Saint-Etienne', 'St Etienne'],
    '尼斯': ['Nice'],
    '雷恩': ['Rennes'],
    '南特': ['Nantes'],
    '蒙彼利埃': ['Montpellier'],
    '朗斯': ['Lens'],
    '斯特拉斯堡': ['Strasbourg'],
    '兰斯': ['Reims'],
    # 其他欧洲常见
    '凯尔特人': ['Celtic'],
    '流浪者': ['Rangers'],
    '本菲卡': ['Benfica'],
    '波尔图': ['FC Porto', 'Porto'],
    '里斯本竞技': ['Sporting CP', 'Sporting Lisbon', 'Sporting'],
    '阿贾克斯': ['Ajax'],
    '埃因霍温': ['PSV Eindhoven', 'PSV'],
    '费耶诺德': ['Feyenoord'],
    '布拉加': ['Braga'],
    '顿涅茨克矿工': ['Shakhtar Donetsk', 'Shakhtar'],
    '基辅迪纳摩': ['Dynamo Kyiv', 'Dynamo Kiev'],
    '泽尼特': ['Zenit'],
    '莫斯科中央陆军': ['CSKA Moscow', 'CSKA'],
    '莫斯科火车头': ['Lokomotiv Moscow'],
    '莫斯科斯巴达克': ['Spartak Moscow'],
    '加拉塔萨雷': ['Galatasaray'],
    '费内巴切': ['Fenerbahce'],
    '贝西克塔斯': ['Besiktas'],
    '哥本哈根': ['FC Copenhagen', 'Copenhagen'],
    '萨尔茨堡红牛': ['Red Bull Salzburg', 'Salzburg'],
    '维也纳快速': ['Rapid Vienna'],
    '奥林匹亚科斯': ['Olympiacos'],
    '帕纳辛奈科斯': ['Panathinaikos'],
    '雅典AEK': ['AEK Athens', 'AEK'],
    '比尔森胜利': ['Viktoria Plzen', 'Plzen'],
    '萨格勒布迪纳摩': ['Dinamo Zagreb'],
    '贝尔格莱德红星': ['Red Star Belgrade', 'Crvena Zvezda'],
    '游击队': ['Partizan Belgrade', 'Partizan'],
    # 南美
    '博卡青年': ['Boca Juniors'],
    '河床': ['River Plate'],
    '弗拉门戈': ['Flamengo'],
    '帕尔梅拉斯': ['Palmeiras'],
    '桑托斯': ['Santos'],
    '科林蒂安': ['Corinthians'],
    '圣保罗': ['Sao Paulo'],
    '米内罗竞技': ['Atletico Mineiro'],
    '弗鲁米嫩塞': ['Fluminense'],
    '独立': ['Independiente'],
    '拉普拉塔大学生': ['Estudiantes'],
    '竞技': ['Racing Club'],
    '萨斯菲尔德': ['Velez Sarsfield'],
    '天主教大学': ['Universidad Catolica'],
    '哥伦比亚国民竞技': ['Atletico Nacional'],
    # 亚洲/其他
    '广州恒大': ['Guangzhou Evergrande', 'Guangzhou'],
    '上海上港': ['Shanghai SIPG'],
    '全北现代': ['Jeonbuk Hyundai Motors', 'Jeonbuk'],
    '首尔': ['FC Seoul'],
    '浦和红钻': ['Urawa Red Diamonds', 'Urawa'],
    '鹿岛鹿角': ['Kashima Antlers'],
    '阿尔艾因': ['Al Ain'],
    '希拉尔': ['Al Hilal'],
    '利雅得胜利': ['Al Nassr', 'Al Nassr FC'],
}

# WC 英文 -> 中文 (四届WC国家队, wc_xlsx_matches 英文队名)
WC_EN2ZH = {
    'Germany': '德国', 'Argentina': '阿根廷', 'Brazil': '巴西', 'Netherlands': '荷兰',
    'France': '法国', 'Spain': '西班牙', 'England': '英格兰', 'Italy': '意大利',
    'Portugal': '葡萄牙', 'Belgium': '比利时', 'Croatia': '克罗地亚',
    'Colombia': '哥伦比亚', 'Switzerland': '瑞士', 'Uruguay': '乌拉圭',
    'Mexico': '墨西哥', 'USA': '美国', 'United States': '美国',
    'Costa Rica': '哥斯达黎加', 'Japan': '日本', 'Korea Republic': '韩国',
    'South Korea': '韩国', 'Korea': '韩国', 'Australia': '澳大利亚', 'Chile': '智利',
    'Greece': '希腊', 'Ivory Coast': '科特迪瓦', "Côte d'Ivoire": '科特迪瓦',
    'Cameroon': '喀麦隆', 'Algeria': '阿尔及利亚', 'Nigeria': '尼日利亚',
    'Ghana': '加纳', 'Russia': '俄罗斯', 'Denmark': '丹麦', 'Sweden': '瑞典',
    'Poland': '波兰', 'Serbia': '塞尔维亚', 'Bosnia and Herzegovina': '波黑',
    'Bosnia': '波黑', 'Iran': '伊朗', 'Morocco': '摩洛哥', 'Tunisia': '突尼斯',
    'Senegal': '塞内加尔', 'Canada': '加拿大', 'Qatar': '卡塔尔',
    'Ecuador': '厄瓜多尔', 'Peru': '秘鲁', 'Paraguay': '巴拉圭',
    'Bolivia': '玻利维亚', 'Venezuela': '委内瑞拉', 'Saudi Arabia': '沙特阿拉伯',
    'Egypt': '埃及', 'South Africa': '南非', 'New Zealand': '新西兰',
    'Honduras': '洪都拉斯', 'Panama': '巴拿马', 'Iceland': '冰岛',
    'Austria': '奥地利', 'Czech Republic': '捷克', 'Czechia': '捷克',
    'Slovakia': '斯洛伐克', 'Romania': '罗马尼亚', 'Ukraine': '乌克兰',
    'Turkey': '土耳其', 'Norway': '挪威', 'Scotland': '苏格兰', 'Wales': '威尔士',
    'Northern Ireland': '北爱尔兰', 'Bulgaria': '保加利亚', 'Hungary': '匈牙利',
    'Slovenia': '斯洛文尼亚', 'Finland': '芬兰', 'Israel': '以色列',
    'Albania': '阿尔巴尼亚', 'North Macedonia': '北马其顿', 'Georgia': '格鲁吉亚',
    'Montenegro': '黑山',     'Kosovo': '科索沃', 'Estonia': '爱沙尼亚',
    'Latvia': '拉脱维亚', 'Lithuania': '立陶宛', 'Belarus': '白俄罗斯',
    'Cyprus': '塞浦路斯', 'Luxembourg': '卢森堡', 'Republic of Ireland': '爱尔兰',
    'Ireland': '爱尔兰',
    # 特殊写法补齐
    'Bosnia & Herzegovina': '波黑', 'Curacao': '库拉索', 'Curaçao': '库拉索',
    'Cape Verde': '佛得角', 'D.R. Congo': '刚果（金）',
    'DR Congo': '刚果（金）', 'Congo DR': '刚果（金）', 'Congo': '刚果（布）',
    'FYR Macedonia': '北马其顿', 'Macedonia': '北马其顿',
    'Korea DPR': '朝鲜', 'North Korea': '朝鲜',
}


def build_index(con):
    """构建:
       - team_canonical 主表 (人类可读)
       - alias2canon 内存反向索引 (norm_key -> canonical)
       - canon_set 规范名集合
    """
    # teams 种子
    teams_rows = con.execute(
        'SELECT team_name, team_name_zh FROM teams').fetchall()

    canon_map = {}   # canonical -> set(aliases)
    alias2canon = {} # norm_key -> canonical

    for name, zh in teams_rows:
        nm = (name or '').strip()
        if not nm:
            continue
        canon_map.setdefault(nm, set()).add(nm)
        alias2canon[norm_key(nm)] = nm
        if zh and zh.strip():
            canon_map[nm].add(zh.strip())
            alias2canon.setdefault(norm_key(zh.strip()), nm)

    for canon, extras in EXTRA.items():
        canon_map.setdefault(canon, set()).add(canon)
        alias2canon[norm_key(canon)] = canon
        for a in extras:
            if a and a.strip():
                canon_map[canon].add(a.strip())
                alias2canon[norm_key(a.strip())] = canon

    for en, zh in WC_EN2ZH.items():
        if en and en.strip():
            alias2canon[norm_key(en.strip())] = zh
        if zh and zh.strip():
            canon_map.setdefault(zh.strip(), set()).add(zh.strip())
            canon_map[zh.strip()].add(en.strip())
            alias2canon.setdefault(norm_key(zh.strip()), zh.strip())

    # 落 team_canonical 主表
    rows = [(c, json.dumps(sorted(a), ensure_ascii=False), 'p2_seed')
            for c, a in canon_map.items()]
    con.execute('DELETE FROM team_canonical')
    con.executemany(
        'INSERT OR REPLACE INTO team_canonical(canonical, aliases_json, note) '
        'VALUES(?,?,?)', rows)

    canon_set = set(canon_map.keys())
    return alias2canon, canon_set


def ensure_col(con, table, col):
    cols = [r[1] for r in con.execute(
        f'PRAGMA table_info("{table}")').fetchall()]
    if col not in cols:
        con.execute(f'ALTER TABLE "{table}" ADD COLUMN "{col}" TEXT')


def norm_expr(col):
    """SQL 版 norm_key: 去空格/tab + 小写。"""
    return (f"LOWER(TRIM(REPLACE(REPLACE(\"{col}\", ' ', ''), '\t', '')))")


def normalize_table(con, table, from_col, to_col):
    ensure_col(con, table, to_col)
    expr = norm_expr(from_col)
    con.execute(f'''
        UPDATE "{table}"
        SET "{to_col}" = COALESCE(
            (SELECT c FROM _amap WHERE k = {expr}),
            TRIM("{from_col}")
        )
    ''')
    con.commit()


def coverage(con, table, from_col, to_col, canon_set):
    total = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    # 用归一后的 to_col 判断命中 (英文源/异体中文经映射后落在 canon_set)
    distinct_norm = [r[0] for r in con.execute(
        f'SELECT DISTINCT TRIM("{to_col}") FROM "{table}" '
        f'WHERE "{to_col}" IS NOT NULL AND TRIM("{to_col}") != ""')]
    hit = [d for d in distinct_norm if d in canon_set]
    unmatched = [d for d in distinct_norm if d not in canon_set][:25]
    pct = (len(hit) / len(distinct_norm) * 100) if distinct_norm else 0.0
    # 原值≠归一值 (走了映射/修正) 的行数
    changed = con.execute(
        f'SELECT COUNT(*) FROM "{table}" '
        f'WHERE TRIM("{from_col}") != TRIM("{to_col}") '
        f'AND "{from_col}" IS NOT NULL AND TRIM("{from_col}") != ""').fetchone()[0]
    return total, len(distinct_norm), len(hit), pct, unmatched, changed


def main():
    con = sqlite3.connect(DB)
    con.execute('PRAGMA foreign_keys=OFF')

    # 主映射表
    con.execute('''CREATE TABLE IF NOT EXISTS team_canonical (
        canonical TEXT PRIMARY KEY,
        aliases_json TEXT,
        note TEXT
    )''')

    alias2canon, canon_set = build_index(con)

    # TEMP 反向索引表 (供 SQL 批量 UPDATE)
    con.execute('DROP TABLE IF EXISTS _amap')
    con.execute('CREATE TEMP TABLE _amap(k TEXT PRIMARY KEY, c TEXT)')
    con.executemany('INSERT OR REPLACE INTO _amap(k, c) VALUES(?, ?)',
                    list(alias2canon.items()))

    # 归一化三表
    normalize_table(con, 'interwetten_odds', 'home_team', 'home_team_norm')
    normalize_table(con, 'interwetten_odds', 'away_team', 'away_team_norm')
    normalize_table(con, 'william_ht', 'home_team', 'home_team_norm')
    normalize_table(con, 'william_ht', 'away_team', 'away_team_norm')
    normalize_table(con, 'wc_xlsx_matches', 'home', 'home_norm')
    normalize_table(con, 'wc_xlsx_matches', 'away', 'away_norm')

    con.execute('DROP TABLE IF EXISTS _amap')

    # 覆盖率报告
    print('=' * 70)
    print('P2 队名归一化完成 — 覆盖率报告')
    print('=' * 70)
    print(f'team_canonical 规范名数: {len(canon_set)}')
    print(f'alias2canon 反向索引条目: {len(alias2canon)}')
    print('-' * 70)
    for table, fc, tc in [
        ('interwetten_odds', 'home_team', 'home_team_norm'),
        ('william_ht', 'home_team', 'home_team_norm'),
        ('wc_xlsx_matches', 'home', 'home_norm'),
    ]:
        total, ndist, nhit, pct, unmatched, changed = coverage(
            con, table, fc, tc, canon_set)
        print(f'\n[{table}] 总行={total}  distinct归一队名={ndist}')
        print(f'  命中canonical(归一到规范名): {nhit}/{ndist} = {pct:.1f}%')
        print(f'  原值经映射/异体修正的行数: {changed}')
        if unmatched:
            print(f'  未归一样例(前{len(unmatched)}): {unmatched}')
    print('\n注: interwetten_odds/william_ht 未归一项多为全球小联赛中文队名')
    print('    (中文已规范且跨源自洽, 无需纳入五大联赛canonical基准)')

    # 抽样验证: interwetten 异体归一
    print('-' * 70)
    print('抽样: interwetten_odds 异体归一校验')
    for raw in ['莱切斯特城', '托特纳姆热刺', '纽卡斯尔联', '曼彻斯特联']:
        k = norm_key(raw)
        print(f'  {raw} -> {alias2canon.get(k, raw)}')
    print('抽样: wc_xlsx_matches 英文->中文')
    for raw in ['Germany', 'Argentina', 'Brazil']:
        k = norm_key(raw)
        print(f'  {raw} -> {alias2canon.get(k, raw)}')

    con.close()
    print('\nDONE. 已落库: team_canonical + 三表 *_norm 列.')


if __name__ == '__main__':
    main()
