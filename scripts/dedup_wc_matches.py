"""WC matches 表去重规范化 (安全合并, 无损).

策略:
- 用双语队名映射 + match_date 归一配对, 找出同一赛事的重复行.
- keeper 优先选「有 match_features 的 537xxx 英文行」(特征权威源), 否则有特征的 2130xxx, 否则首行.
- 把冗余副本的赛果字段(final_result/home_score/away_score/status/halftime/minute) 并回 keeper(仅填空).
- 删除冗余副本. 4 行 away=None 残缺行一并删除.
- 全程事务 + 事前整库备份 + 事后断言验证.
"""
import sqlite3, os, shutil, datetime, sys

DB = "data/football_data.db"
BACKUP = f"data/_backup_dedup_{datetime.datetime.now():%Y%m%d_%H%M%S}.db"

EN2ZH = {
 'Algeria':'阿尔及利亚','Argentina':'阿根廷','Australia':'澳大利亚','Austria':'奥地利','Belgium':'比利时',
 'Bosnia-H.':'波黑','Brazil':'巴西','Canada':'加拿大','Cape Verde':'佛得角','Colombia':'哥伦比亚',
 'Congo DR':'民主刚果','Croatia':'克罗地亚','Curaçao':'库拉索','Czechia':'捷克','Ecuador':'厄瓜多尔',
 'Egypt':'埃及','England':'英格兰','France':'法国','Germany':'德国','Ghana':'加纳','Haiti':'海地',
 'Iran':'伊朗','Iraq':'伊拉克','Ivory Coast':'科特迪瓦','Japan':'日本','Jordan':'约旦',
 'Korea Republic':'韩国','Mexico':'墨西哥','Morocco':'摩洛哥','Netherlands':'荷兰','New Zealand':'新西兰',
 'Norway':'挪威','Panama':'巴拿马','Paraguay':'巴拉圭','Portugal':'葡萄牙','Qatar':'卡塔尔',
 'Scotland':'苏格兰','Senegal':'塞内加尔','South Africa':'南非','Spain':'西班牙','Sweden':'瑞典',
 'Switzerland':'瑞士','Tunisia':'突尼斯','Turkey':'土耳其','USA':'美国','Uruguay':'乌拉圭','Uzbekistan':'乌兹别克斯坦',
 '乌兹别克':'乌兹别克斯坦','沙特':'沙特阿拉伯','捷克':'捷克',
}
def canon(n):
    if n is None: return None
    n = n.strip()
    return EN2ZH.get(n, n)

RESULT_FIELDS = ['final_result','home_score','away_score','status','halftime_home','halftime_away','minute']

def main():
    if not os.path.exists(DB):
        print("DB 不存在:", DB); sys.exit(1)
    # 1) 备份
    shutil.copy2(DB, BACKUP)
    print("已备份 ->", BACKUP)

    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    cur = con.cursor()
    mf = set(x[0] for x in cur.execute("SELECT DISTINCT match_id FROM match_features"))
    rows = cur.execute("SELECT match_id,match_date,home_team_name,away_team_name,"
                       "final_result,home_score,away_score,status,halftime_home,halftime_away,minute "
                       "FROM matches WHERE league_name='世界杯'").fetchall()

    # 2) 归一配组
    from collections import defaultdict
    groups = defaultdict(list)
    for r in rows:
        h = canon(r['home_team_name']); a = canon(r['away_team_name'])
        if h and a:
            groups[(h,a,r['match_date'])].append(r)

    plan = []  # (keeper_mid, [drop_mids], merge_from_mid_or_None)
    for k, v in groups.items():
        # keeper: 有特征的优先, 偏好 537xxx
        def hasf(r): return r['match_id'] in mf
        featured = [r for r in v if hasf(r)]
        if featured:
            keeper = next((r for r in featured if str(r['match_id']).startswith('537')), featured[0])
        else:
            keeper = next((r for r in v if str(r['match_id']).startswith('537')), v[0])
        drops = [r['match_id'] for r in v if r['match_id'] != keeper['match_id']]
        # 合并源: 同组里有 final_result 的兄弟(优先 2130xxx 补全, 但任意有值即可)
        merge_src = None
        for r in v:
            if r['match_id'] != keeper['match_id'] and r['final_result'] is not None:
                merge_src = r; break
        if merge_src is None and keeper['final_result'] is None:
            # 找任意有 final_result 的兄弟
            for r in v:
                if r['match_id'] != keeper['match_id'] and r['final_result'] is not None:
                    merge_src = r; break
        plan.append((keeper['match_id'], drops, merge_src['match_id'] if merge_src else None))

    total_drops = [m for _,drops,_ in plan for m in drops]
    print(f"去重组数: {len(plan)} | 计划删除行: {len(total_drops)}")
    print(f"  删 2130xxx: {sum(1 for m in total_drops if str(m).startswith('213'))}, "
          f"删 537xxx: {sum(1 for m in total_drops if str(m).startswith('537'))}")

    # 3) 执行 (事务)
    try:
        for keeper_mid, drops, merge_mid in plan:
            if merge_mid is not None:
                src = cur.execute("SELECT final_result,home_score,away_score,status,halftime_home,"
                                  "halftime_away,minute FROM matches WHERE match_id=?", (merge_mid,)).fetchone()
                # 仅填空
                cur.execute(
                    "UPDATE matches SET "
                    "final_result=COALESCE(final_result,?), "
                    "home_score=COALESCE(home_score,?), "
                    "away_score=COALESCE(away_score,?), "
                    "status=COALESCE(status,?), "
                    "halftime_home=COALESCE(halftime_home,?), "
                    "halftime_away=COALESCE(halftime_away,?), "
                    "minute=COALESCE(minute,?) "
                    "WHERE match_id=?",
                    (src['final_result'], src['home_score'], src['away_score'], src['status'],
                     src['halftime_home'], src['halftime_away'], src['minute'], keeper_mid))
            for m in drops:
                cur.execute("DELETE FROM matches WHERE match_id=?", (m,))
        # 4) 残缺行 (away=None)
        malformed = [r['match_id'] for r in cur.execute(
            "SELECT match_id FROM matches WHERE league_name='世界杯' AND away_team_name IS NULL").fetchall()]
        for m in malformed:
            cur.execute("DELETE FROM matches WHERE match_id=?", (m,))
        print(f"额外删除残缺行(away=None): {malformed}")
        con.commit()
    except Exception as e:
        con.rollback()
        print("执行出错, 已回滚:", e); sys.exit(1)
    finally:
        con.close()

    # 5) 验证
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row; cur = con.cursor()
    n_rows = cur.execute("SELECT COUNT(*) FROM matches WHERE league_name='世界杯'").fetchone()[0]
    n_feat_rows = cur.execute("SELECT COUNT(*) FROM match_features f JOIN matches m ON f.match_id=m.match_id "
                               "WHERE m.league_name='世界杯'").fetchone()[0]
    # 去重赛事数 (canon)
    rr = cur.execute("SELECT match_id,home_team_name,away_team_name,match_date FROM matches WHERE league_name='世界杯'").fetchall()
    g2 = defaultdict(list)
    for r in rr:
        h=canon(r['home_team_name']); a=canon(r['away_team_name'])
        if h and a: g2[(h,a,r['match_date'])].append(r['match_id'])
    dup_left = sum(1 for v in g2.values() if len(v)>1)
    # 删除行是否还有特征残留(应无)
    leftover_feat = cur.execute(f"SELECT COUNT(*) FROM match_features WHERE match_id IN "
                                f"({','.join('?'*len(total_drops+malformed))})",
                                total_drops+malformed).fetchone()[0] if (total_drops+malformed) else 0
    n213 = cur.execute("SELECT COUNT(*) FROM matches WHERE league_name='世界杯' AND match_id LIKE '213%'").fetchone()[0]
    con.close()

    print("\n=== 验证 ===")
    print(f"  剩余 WC 行: {n_rows} (预期 223-{len(total_drops)+len(malformed)}={223-len(total_drops)-len(malformed)})")
    print(f"  match_features 仍关联 WC 行: {n_feat_rows} (特征未丢)")
    print(f"  残留重复组: {dup_left} (预期 0)")
    print(f"  被删行中仍挂特征数: {leftover_feat} (预期 0)")
    print(f"  残留 2130xxx 单例: {n213} (无英文兄弟的独立赛事, 故意保留)")
    ok = (n_rows == 223-len(total_drops)-len(malformed)) and (dup_left==0) and (leftover_feat==0)
    print("  RESULT:", "GO ✅" if ok else "FAIL ❌ 请用备份恢复")
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
