"""
ETL: football-data.co.uk WorldCup2026.xlsx -> DB
================================================
发现: 该xlsx内含 5 个 sheet: WorldCup2026(89行)/2022(65)/2018(65)/2014(65) + 预选赛(890)
每一届都带 bet365/Pinnacle/Betfair 赔率 + 真实比分(HGFT/AGFT) + xG/射门.

动作:
  1) 建干净英文表 wc_xlsx_matches (edition, home, away, date, hg, ag, oh, od, oa, ...)
     - 赔率优先 H/D/A-Avg(市场均值, 最适合去抽水), 回退 bet365/Pinny/Betfair/Max
  2) 用 ZH->EN 映射回填 wc_all_matches.oh/od/oa (仅填 NULL, 不覆盖已有)

作者: 赵统筹(总工) | 2026-07-07
"""
import os, sqlite3, datetime
import openpyxl

ROOT = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(ROOT, '..', 'data', 'football_data.db')
XLSX = os.path.join(ROOT, '..', 'data', 'wc_xlsx', 'WorldCup2026.xlsx')

SHEETS = {'2014': 'WorldCup2014', '2018': 'WorldCup2018',
          '2022': 'WorldCup2022', '2026': 'WorldCup2026'}

# 中文队名 -> 英文 (覆盖 wc_all_matches 全部 64 个中文队名, 含变体)
ZH2EN = {
    '阿尔及利亚': 'Algeria', '阿根廷': 'Argentina', '澳大利亚': 'Australia',
    '奥地利': 'Austria', '比利时': 'Belgium', '波黑': 'Bosnia & Herzegovina',
    '巴西': 'Brazil', '喀麦隆': 'Cameroon', '加拿大': 'Canada',
    '佛得角': 'Cape Verde', '智利': 'Chile', '哥伦比亚': 'Colombia',
    '哥斯达黎加': 'Costa Rica', '克罗地亚': 'Croatia', '库拉索': 'Curacao',
    '捷克': 'Czech Republic', '民主刚果': 'D.R. Congo', '丹麦': 'Denmark',
    '厄瓜多尔': 'Ecuador', '埃及': 'Egypt', '英格兰': 'England',
    '法国': 'France', '德国': 'Germany', '加纳': 'Ghana',
    '希腊': 'Greece', '海地': 'Haiti', '洪都拉斯': 'Honduras',
    '冰岛': 'Iceland', '伊朗': 'Iran', '伊拉克': 'Iraq',
    '意大利': 'Italy', '科特迪瓦': 'Ivory Coast', '日本': 'Japan',
    '约旦': 'Jordan', '墨西哥': 'Mexico', '摩洛哥': 'Morocco',
    '荷兰': 'Netherlands', '新西兰': 'New Zealand', '尼日利亚': 'Nigeria',
    '挪威': 'Norway', '巴拿马': 'Panama', '巴拉圭': 'Paraguay',
    '秘鲁': 'Peru', '波兰': 'Poland', '葡萄牙': 'Portugal',
    '卡塔尔': 'Qatar', '俄罗斯': 'Russia', '沙特': 'Saudi Arabia',
    '沙特阿拉伯': 'Saudi Arabia', '塞尔维亚': 'Serbia', '南非': 'South Africa',
    '韩国': 'South Korea', '西班牙': 'Spain', '瑞典': 'Sweden',
    '瑞士': 'Switzerland', '突尼斯': 'Tunisia', '土耳其': 'Turkey',
    '美国': 'USA', '苏格兰': 'Scotland', '乌拉圭': 'Uruguay',
    '乌兹别克': 'Uzbekistan', '乌兹别克斯坦': 'Uzbekistan', '威尔士': 'Wales',
}


def get_odds(row, idx):
    """取 (oh, od, oa): 优先 *-Avg, 回退 bet365/Pinny/Betfair/*-Max"""
    def pick(*names):
        for n in names:
            i = idx.get(n)
            if i is not None and row[i] is not None:
                try:
                    return float(row[i])
                except (ValueError, TypeError):
                    return None
        return None
    oh = pick('H-Avg', 'bet365-H', 'Pinny-H', 'Betfair_Exch-H', 'H-Max')
    od = pick('D-Avg', 'bet365-D', 'Pinny-D', 'Betfair_Exch-D', 'D-Max')
    oa = pick('A-Avg', 'bet365-A', 'Pinny-A', 'Betfair_Exch-A', 'A-Max')
    return oh, od, oa


def main():
    wb = openpyxl.load_workbook(XLSX, read_only=True, data_only=True)
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS wc_xlsx_matches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        edition TEXT, stage TEXT, home TEXT, away TEXT, date TEXT,
        hg INTEGER, ag INTEGER, oh REAL, od REAL, oa REAL,
        hxg REAL, axg REAL, hs INTEGER, as_ INTEGER,
        source TEXT DEFAULT 'football-data.co.uk'
    )""")

    inserted = 0
    for edition, sn in SHEETS.items():
        ws = wb[sn]
        rows = list(ws.iter_rows(min_row=1, values_only=True))
        hdr = rows[0]
        idx = {h: i for i, h in enumerate(hdr) if h is not None}
        for r in rows[1:]:
            home = r[idx['Home']]
            away = r[idx['Away']]
            if not home or not away:
                continue
            hg = r[idx['HGFT']]
            ag = r[idx['AGFT']]
            date = r[idx['Date']]
            if isinstance(date, (datetime.datetime, datetime.date)):
                date = date.strftime('%Y-%m-%d')
            oh, od, oa = get_odds(r, idx)
            hxg = r[idx['HxG']] if 'HxG' in idx else None
            axg = r[idx['AxG']] if 'AxG' in idx else None
            hs = r[idx['HS']] if 'HS' in idx else None
            a_s = r[idx['AS']] if 'AS' in idx else None
            c.execute("""INSERT INTO wc_xlsx_matches
                (edition, stage, home, away, date, hg, ag, oh, od, oa, hxg, axg, hs, as_, source)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (edition, edition + ' World Cup', home, away, date, hg, ag,
                 oh, od, oa, hxg, axg, hs, a_s, 'football-data.co.uk'))
            inserted += 1
    c.commit()

    # 统计新表
    n_total = c.execute("SELECT COUNT(*) FROM wc_xlsx_matches").fetchone()[0]
    n_odds = c.execute("SELECT COUNT(*) FROM wc_xlsx_matches WHERE oh IS NOT NULL").fetchone()[0]
    n_score = c.execute("SELECT COUNT(*) FROM wc_xlsx_matches WHERE hg IS NOT NULL").fetchone()[0]
    n_both = c.execute("SELECT COUNT(*) FROM wc_xlsx_matches WHERE oh IS NOT NULL AND hg IS NOT NULL").fetchone()[0]
    print(f"[wc_xlsx_matches] 插入 {inserted} 行 | 总{n_total} 有赔率{n_odds} 有比分{n_score} 赔率+比分{n_both}")
    for ed in SHEETS:
        b = c.execute("SELECT COUNT(*) FROM wc_xlsx_matches WHERE edition=? AND oh IS NOT NULL AND hg IS NOT NULL", (ed,)).fetchone()[0]
        print(f"   {ed}: 赔率+比分 {b} 场")

    # ── 回填 wc_all_matches.oh/od/oa (仅填 NULL) ──
    # 建 xlsx 查找索引: (edition, home_en, away_en) -> odds
    xlsx_rows = c.execute("SELECT edition, home, away, oh, od, oa FROM wc_xlsx_matches").fetchall()
    lookup = {(e, h, a): (oh, od, oa) for e, h, a, oh, od, oa in xlsx_rows}

    filled = 0
    already = 0
    matched_no_odds = 0
    cam = c.execute("SELECT id, edition, home, away, oh, od, oa FROM wc_all_matches").fetchall()
    for mid, ed, hz, az, oh0, od0, oa0 in cam:
        he = ZH2EN.get(hz)
        ae = ZH2EN.get(az)
        if not he or not ae:
            continue
        key = (ed, he, ae)
        if key not in lookup:
            continue
        xoh, xod, xoa = lookup[key]
        if oh0 is None and xoh is not None:
            c.execute("UPDATE wc_all_matches SET oh=?, od=?, oa=? WHERE id=?", (xoh, xod, xoa, mid))
            filled += 1
        elif oh0 is not None:
            already += 1
        else:
            matched_no_odds += 1
    c.commit()
    print(f"\n[wc_all_matches 回填] 新填 {filled} 场 | 已有赔率跳过 {already} | 匹配到但xlsx也无赔率 {matched_no_odds}")

    # 回填后 wc_all_matches 含赔率+比分计数
    after = c.execute("SELECT COUNT(*) FROM wc_all_matches WHERE oh IS NOT NULL AND hg IS NOT NULL").fetchone()[0]
    before = 106  # 此前基线
    print(f"[wc_all_matches] 赔率+比分: 此前 {before} -> 现在 {after}")
    c.close()


if __name__ == '__main__':
    main()
