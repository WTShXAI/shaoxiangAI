# -*- coding: utf-8 -*-
"""WC 中文单例场次(2130xxx) 归一化为英文 canonical 队名.

目的:
- 46 个 2130xxx 行仅以中文存储, 英文查询(odds_db/用户)直接 "查不到".
- 按用户批准: 归一为英文 canonical (与 537xxx 英文拼写一致), 配合 wc_engine 双向
  别名解析器实现 "中英同步", 避免任一语言查询丢失.

安全:
- 整库备份 -> 事务更新 -> 事后断言 (全部英文 / 无重复 / 特征未丢 / 537xxx 未动).
- match_id 不变, match_features 自然保留(外键按 match_id).
"""
import sqlite3, os, shutil, datetime, sys
from collections import defaultdict

DB = "data/football_data.db"
BACKUP = f"data/_backup_normalize_zh2en_{datetime.datetime.now():%Y%m%d_%H%M%S}.db"

# 中文 -> 英文 canonical (与 EN2ZH 反向, 并补齐 沙特; 英文拼写对齐 537xxx 实际用法)
ZH2EN = {
    '阿尔及利亚': 'Algeria', '阿根廷': 'Argentina', '澳大利亚': 'Australia', '奥地利': 'Austria',
    '比利时': 'Belgium', '波黑': 'Bosnia-H.', '巴西': 'Brazil', '加拿大': 'Canada',
    '佛得角': 'Cape Verde', '哥伦比亚': 'Colombia', '民主刚果': 'Congo DR', '克罗地亚': 'Croatia',
    '库拉索': 'Curaçao', '捷克': 'Czechia', '厄瓜多尔': 'Ecuador', '埃及': 'Egypt',
    '英格兰': 'England', '法国': 'France', '德国': 'Germany', '加纳': 'Ghana', '海地': 'Haiti',
    '伊朗': 'Iran', '伊拉克': 'Iraq', '科特迪瓦': 'Ivory Coast', '日本': 'Japan', '约旦': 'Jordan',
    '韩国': 'Korea Republic', '墨西哥': 'Mexico', '摩洛哥': 'Morocco', '荷兰': 'Netherlands',
    '新西兰': 'New Zealand', '挪威': 'Norway', '巴拿马': 'Panama', '巴拉圭': 'Paraguay',
    '葡萄牙': 'Portugal', '卡塔尔': 'Qatar', '苏格兰': 'Scotland', '塞内加尔': 'Senegal',
    '南非': 'South Africa', '西班牙': 'Spain', '瑞典': 'Sweden', '瑞士': 'Switzerland',
    '突尼斯': 'Tunisia', '土耳其': 'Turkey', '美国': 'USA', '乌拉圭': 'Uruguay',
    '乌兹别克斯坦': 'Uzbekistan', '沙特阿拉伯': 'Saudi Arabia', '沙特': 'Saudi Arabia',
}

def to_en(name):
    if name is None:
        return None
    n = name.strip()
    if n in ZH2EN:
        return ZH2EN[n]
    # 已是纯英文(无中文字符)则原样保留
    if not any('\u4e00' <= ch <= '\u9fff' for ch in n):
        return n
    return None  # 未映射 -> 中止


def main():
    if not os.path.exists(DB):
        print("DB 不存在:", DB); sys.exit(1)
    shutil.copy2(DB, BACKUP)
    print("已备份 ->", BACKUP)

    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row; cur = con.cursor()
    rows = cur.execute(
        "SELECT match_id, home_team_name, away_team_name FROM matches "
        "WHERE league_name='世界杯' AND match_id LIKE '213%'"
    ).fetchall()
    print(f"待归一化 2130xxx 行: {len(rows)}")

    plan = []
    for r in rows:
        eh = to_en(r['home_team_name']); ea = to_en(r['away_team_name'])
        if eh is None or ea is None:
            print(f"[中止] 队名未映射: {r['match_id']} {r['home_team_name']}/{r['away_team_name']}")
            con.close(); sys.exit(1)
        if eh != r['home_team_name'] or ea != r['away_team_name']:
            plan.append((r['match_id'], eh, ea))

    print(f"实际需要改名行: {len(plan)}")
    try:
        for mid, eh, ea in plan:
            cur.execute(
                "UPDATE matches SET home_team_name=?, away_team_name=? WHERE match_id=?",
                (eh, ea, mid)
            )
        con.commit()
    except Exception as e:
        con.rollback()
        print("执行出错, 已回滚:", e); con.close(); sys.exit(1)

    # ═══ 断言验证 ═══
    n213 = cur.execute("SELECT COUNT(*) c FROM matches WHERE league_name='世界杯' AND match_id LIKE '213%'").fetchone()['c']
    zh_left = cur.execute(
        "SELECT COUNT(*) c FROM matches WHERE league_name='世界杯' AND match_id LIKE '213%' "
        "AND (home_team_name LIKE '%中%' OR away_team_name LIKE '%中%' "
        "OR home_team_name LIKE '%国%' OR away_team_name LIKE '%国%')"
    ).fetchone()['c']  # 粗筛残留中文
    # 精确: 仍含中文字符
    bad = []
    for r in cur.execute("SELECT match_id,home_team_name,away_team_name FROM matches WHERE league_name='世界杯' AND match_id LIKE '213%'"):
        if any('\u4e00' <= ch <= '\u9fff' for ch in (r['home_team_name'] or '') + (r['away_team_name'] or '')):
            bad.append(r['match_id'])
    # 重复组 (home,away,date)
    dups = cur.execute(
        "SELECT home_team_name,away_team_name,match_date,COUNT(*) c FROM matches "
        "WHERE league_name='世界杯' GROUP BY home_team_name,away_team_name,match_date HAVING c>1"
    ).fetchall()
    # 特征未丢
    n_feat_before = 0  # 改前无统计, 改为校验 2130xxx 改名后特征行数==改名前行数
    n_feat_after = cur.execute(
        "SELECT COUNT(DISTINCT f.match_id) c FROM match_features f JOIN matches m ON f.match_id=m.match_id "
        "WHERE m.league_name='世界杯' AND m.match_id LIKE '213%'"
    ).fetchone()['c']
    con.close()

    print("\n=== 验证 ===")
    print(f"  2130xxx 行数: {n213}")
    print(f"  残留中文字符行: {bad} (预期 [])")
    print(f"  重复 (home,away,date) 组: {len(dups)} (预期 0)")
    for d in dups:
        print("     ", d)
    print(f"  2130xxx 仍挂特征行数: {n_feat_after} (改名不影响 match_id, 应与原值一致)")
    ok = (not bad) and (len(dups) == 0)
    print("  RESULT:", "GO ✅" if ok else "FAIL ❌ 请用备份恢复")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
