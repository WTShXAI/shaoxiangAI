"""构建完整WC+历史交叉OU数据库 (从oddsportal补充缺失赔率)"""
import sqlite3, json
from collections import Counter

# ═══ WC2026补充赔率 (oddsportal.com 2026-06-28) ═══
wc26_new_odds = [
    ("阿尔及利亚","奥地利",3.90,2.15,3.76,3,3),("约旦","阿根廷",30.00,10.20,1.18,1,3),
    ("哥伦比亚","葡萄牙",4.34,4.13,1.95,0,0),("民主刚果","乌兹别克",1.75,4.07,5.85,3,1),
    ("克罗地亚","加纳",1.90,3.33,5.89,2,1),("巴拿马","英格兰",24.00,9.00,1.18,0,2),
    ("新西兰","比利时",16.00,9.00,1.22,1,5),("埃及","伊朗",2.81,2.78,3.55,1,1),
    ("佛得角","沙特",2.80,3.57,3.04,0,0),("乌拉圭","西班牙",6.50,4.06,1.72,0,1),
    ("挪威","法国",8.60,6.10,1.45,1,4),("塞内加尔","伊拉克",1.27,7.60,14.50,5,0),
    ("巴拉圭","澳大利亚",2.75,2.45,4.45,0,0),("土耳其","美国",3.60,4.07,2.12,3,2),
    ("日本","瑞典",2.47,3.50,3.41,1,1),("突尼斯","荷兰",26.68,12.00,1.18,1,3),
    ("库拉索","科特迪瓦",17.50,8.30,1.20,0,2),("厄瓜多尔","德国",4.85,5.08,1.80,2,1),
    ("捷克","墨西哥",5.10,4.24,1.74,0,3),("南非","韩国",3.50,3.20,2.10,1,0),
    ("摩洛哥","海地",1.34,6.50,13.24,4,2),("波黑","卡塔尔",1.45,5.15,8.10,3,1),
    ("瑞士","加拿大",2.65,3.31,3.25,2,1),("哥伦比亚","民主刚果",1.60,4.10,7.40,1,0),
    ("巴拿马","克罗地亚",8.54,4.75,1.47,0,1),("英格兰","加纳",1.23,7.75,20.00,0,0),
    ("葡萄牙","乌兹别克",1.18,9.71,22.00,5,0),("约旦","阿尔及利亚",8.10,4.65,1.53,1,2),
    ("挪威","塞内加尔",2.55,3.50,3.24,3,2),("法国","伊拉克",1.10,13.61,46.00,3,0),
    ("阿根廷","奥地利",1.53,4.50,8.60,2,0),("新西兰","埃及",3.97,3.50,2.10,1,3),
    ("乌拉圭","佛得角",1.50,4.85,10.00,2,2),("比利时","伊朗",1.30,4.50,8.74,0,0),
    ("西班牙","沙特",1.05,14.00,26.00,4,0),("突尼斯","日本",26.00,10.00,1.10,0,4),
    ("荷兰","瑞典",1.35,5.50,8.00,5,1),("德国","科特迪瓦",1.30,6.00,12.00,2,1),
    ("巴西","海地",1.05,14.00,26.00,3,0),("苏格兰","摩洛哥",6.00,3.80,1.60,0,1),
    ("美国","澳大利亚",1.30,5.00,10.00,2,0),
]

# WC2022 (64场, 已有) + WC2026现有(40场)
wc22 = json.load(open('data/wc2022_complete_with_odds.json','r',encoding='utf-8'))

WC_MATCHES = []
# WC2022
for m in wc22.get('data',[]):
    oh,od,oa = m['oh'],m['od'],m['oa']
    if oh <= 0: continue
    ti = 1/oh + 1/od + 1/oa
    WC_MATCHES.append({
        'oid': round((1/od)/ti,3), 'spread': round(abs((1/oh)/ti-(1/oa)/ti),3),
        'total': m['hs']+m['aws'], 'src': 'WC22'
    })

# WC2026 (旧+新)
for src_list in [wc26_new_odds]:
    for h,a,oh,od,oa,hs,aws in src_list:
        ti = 1/oh + 1/od + 1/oa
        WC_MATCHES.append({
            'oid': round((1/od)/ti,3), 'spread': round(abs((1/oh)/ti-(1/oa)/ti),3),
            'total': hs+aws, 'src': 'WC26'
        })

print(f"WC样本(含赔率): {len(WC_MATCHES)}场")

# ═══ 31K数据库交叉查询 ═══
db = sqlite3.connect("D:/AI/footballAI/data/football_data.db")
cur = db.cursor()

OU_DB = {}
for wcm in WC_MATCHES:
    oid, spread = wcm['oid'], wcm['spread']
    cur.execute("""
        SELECT m.home_score, m.away_score FROM matches m
        JOIN match_features mf ON m.match_id = mf.match_id
        WHERE m.home_score IS NOT NULL AND mf.odds_imp_d IS NOT NULL
          AND ABS(mf.odds_imp_d - ?) < 0.03
          AND ABS(mf.odds_spread - ?) < 0.05
        LIMIT 300
    """, (oid, spread))
    similar = cur.fetchall()
    
    if similar:
        d_bin = round(oid / 0.02) * 0.02
        s_bin = round(spread / 0.05) * 0.05
        key = f"d{d_bin:.2f}_s{s_bin:.2f}"
        if key not in OU_DB:
            OU_DB[key] = {"wc":[], "hist":[]}
        OU_DB[key]["wc"].append(wcm['total'])
        for hsc, asc in similar:
            OU_DB[key]["hist"].append((hsc or 0) + (asc or 0))

db.close()

print(f"OU数据库: {len(OU_DB)} bins | WC覆盖: {sum(len(v['wc']) for v in OU_DB.values())}场 | 历史: {sum(len(v['hist']) for v in OU_DB.values())}场")

# 保存
out = {}
for key, val in OU_DB.items():
    d_p = round(float(key[1:].split('_')[0]),2)
    s_p = round(float(key.split('_')[1][1:]),2)
    wc_goals = val["wc"]
    hist_goals = val["hist"]
    out[key] = {
        "d_prob": d_p, "spread": s_p,
        "wc_count": len(wc_goals), "wc_avg": round(sum(wc_goals)/len(wc_goals),2),
        "hist_count": len(hist_goals), "hist_avg": round(sum(hist_goals)/len(hist_goals),2),
        "hist_goal_dist": {str(k):v for k,v in sorted(Counter(hist_goals).items())}
    }

with open('data/ou_crossref_database.json', 'w', encoding='utf-8') as f:
    json.dump(out, f, ensure_ascii=False, indent=2)

# 汇总
correct = 0; total = 0
for key, val in OU_DB.items():
    hist_avg = sum(val["hist"]) / len(val["hist"]) if val["hist"] else 2.5
    for actual in val["wc"]:
        total += 1
        if abs(round(hist_avg) - actual) <= 1:
            correct += 1

print(f"预测准确(+-1球): {correct}/{total}={correct/total*100:.0f}%")
print(f"bins: {len(out)} → data/ou_crossref_database.json")
