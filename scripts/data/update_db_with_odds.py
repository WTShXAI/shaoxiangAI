import json, sqlite3, requests, time, base64, gzip, zlib, os

COOKIES = {'X-API-UUID':'3120eaa7-3e5f-4c84-8c45-32d3a461b5fc','TRACK-HOUR':'13','X-API-TOKEN':'d6381b31f1324c13f1a950009d11d08badee878fda9b7f0dfc01447a503e745f7c31887b86d85271efbe44c26f838cb9'}
import io as _io
TOKEN = next((_l.split("=", 1)[1].strip() for _l in _io.open(r"gq/.env", encoding="utf-8")
             if _l.strip().startswith("GQ_REQUEST_ID=")), "")
CUID = "526002076777845380"
PREFIX = "pc-c6cf3aabe2a84dd3a870d669b8ba5094"
API_ODDS = "https://api.u92tiil.com/yewu11/v1/w/getMatchBaseInfoByOddsPB"
DB_PATH = r"D:\Architecture\data\events.db"
LOG_FILE = r"D:\Architecture\update_db.log"

def log(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(s + "\n")

def make_checkid():
    return f"{PREFIX}-{CUID}-{int(time.time()*1000)}"

def decode_data(t):
    try: j = json.loads(t)
    except: return None
    if not isinstance(j, dict) or j.get('code') != '0000000' or not j.get('data'): return None
    raw = base64.b64decode(j['data'])
    for fn in (lambda r: gzip.decompress(r), lambda r: zlib.decompress(r, -zlib.MAX_WBITS), lambda r: zlib.decompress(r)):
        try: return json.loads(fn(raw).decode('utf-8'))
        except: pass
    return None

# 要提取的玩法列表
PLAY_NAMES = {
    '全场让球': '全场让球',
    '全场大小': '全场大小',
    '全场独赢': '全场独赢',
    '全场波胆': '全场波胆',
    '全场让球胜平负': '全场让球胜平负',
    '双重机会': '双重机会',
    '两队都进球': '两队都进球',
    '上半场让球': '上半场让球',
    '上半场大小': '上半场大小',
    '上半场独赢': '上半场独赢',
    '上半场波胆': '上半场波胆',
    '下半场大小': '下半场大小',
    '下半场让球': '下半场让球',
    '下半场独赢': '下半场独赢',
}

def extract_all_odds(obj):
    result = {}
    for x in (obj.get('playData') or []):
        if not isinstance(x, dict): continue
        hpn = x.get('hpn', '')
        topKey = str(x.get('topKey', ''))
        # 匹配波胆用 topKey=7
        if topKey == '7' and '波胆' not in hpn:
            hpn = '全场波胆'
        # 映射到标准名称
        matched_name = None
        for std_name, keyword in PLAY_NAMES.items():
            if keyword in hpn:
                matched_name = std_name
                break
        if not matched_name:
            continue
        lines = []
        for line in (x.get('hl') or []):
            if not isinstance(line, dict): continue
            for o in (line.get('ol') or []):
                if not isinstance(o, dict): continue
                sc = o.get('ot') or o.get('otv')
                ov_raw = o.get('ov')
                if sc is not None and ov_raw is not None:
                    try: ov = float(ov_raw) / 100000
                    except: ov = ov_raw
                    lines.append({'name': str(sc).replace(' ', ''), 'odds': round(ov, 2)})
        if lines:
            result[matched_name] = lines
    return result

s = requests.Session()
s.cookies.update(COOKIES)

# 1. 加载 76 场未开始比赛的 mid
early_data = json.load(open(r'D:\Architecture\early_cs_all.json', encoding='utf-8'))
mids = [(item['mid'], item['home'], item['away'], item['league']) for item in early_data]
log(f"加载 {len(mids)} 场未开始比赛")

# 2. 逐个拉取完整赔率结构
all_odds = {}
for idx, (mid, home, away, league) in enumerate(mids):
    body = {'cuid': CUID, 'cos': 0, 'orpt': 0, 'euid': '3020101', 'mid': mid, 'mcid': 0, 'newUser': 0}
    headers = {'Content-Type': 'application/json;charset=utf-8', 'checkid': make_checkid(),
               'requestid': TOKEN, 'User-Agent': 'Mozilla/5.0'}
    try:
        resp = s.post(API_ODDS, json=body, headers=headers, timeout=15)
        t = resp.text
        if resp.json().get('code') == '0000000' and resp.json().get('data'):
            obj = decode_data(t)
            if obj:
                odds = extract_all_odds(obj)
                all_odds[mid] = odds
                cs_cnt = len(odds.get('全场波胆', []))
                hdcp_cnt = len(odds.get('全场让球', []))
                ou_cnt = len(odds.get('全场大小', []))
                dnw_cnt = len(odds.get('全场独赢', []))
                log(f"[{idx+1}/{len(mids)}] {home} vs {away} 波胆{cs_cnt} 让球{hdcp_cnt} 大小{ou_cnt} 独赢{dnw_cnt}")
    except Exception as e:
        log(f"[{idx+1}/{len(mids)}] {mid} 异常: {str(e)[:60]}")
    time.sleep(1.2)

log(f"\n赔率拉取完成: {len(all_odds)} 场有赔率数据")

# 3. 写入 events.db
con = sqlite3.connect(DB_PATH)
cur = con.cursor()

# 确保 match_odds 表存在
cur.execute('''
CREATE TABLE IF NOT EXISTS match_odds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mid TEXT, home TEXT, away TEXT, league TEXT,
    play_name TEXT, option_name TEXT, odds REAL,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
''')

# 清除旧数据（如果之前有已完赛的比赛赔率）
cur.execute("DELETE FROM match_odds")
log("已清空 match_odds 旧数据")

count = 0
for mid, odds in all_odds.items():
    # 找到对应的比赛信息
    item = next((x for x in early_data if x['mid'] == mid), {})
    home = item.get('home', '')
    away = item.get('away', '')
    league = item.get('league', '')
    for play_name, lines in odds.items():
        for line in lines:
            cur.execute('''
                INSERT INTO match_odds (mid, home, away, league, play_name, option_name, odds)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (mid, home, away, league, play_name, line['name'], line['odds']))
            count += 1

# 同时更新 bet_analysis 表中的 odds_structure 字段（针对有 mid 匹配的记录）
# 但由于 bet_analysis 是已完赛比赛，mid 不同，不匹配

con.commit()
con.close()
log(f"写入 match_odds: {count} 条赔率记录")
log(f"数据库: {DB_PATH}")

# 4. 统计各玩法数量
stats = {}
for odds in all_odds.values():
    for play, lines in odds.items():
        stats[play] = stats.get(play, 0) + len(lines)
log("\n=== 赔率统计 ===")
for play, cnt in sorted(stats.items(), key=lambda x: -x[1]):
    log(f"  {play}: {cnt} 条")

# 5. 查看中奖波胆在未开始比赛中的参考
log("\n=== 中奖波胆参考 ===")
win_cs = [
    ('1:0', 2.23, '厄瓜多尔甲级联赛'),
    ('1:2', 11.5, '世界杯2026'),
    ('3:2', 56.0, '韩国杯'),
    ('0:2', 27.0, '韩国杯'),
]
for score, odds_val, league in win_cs:
    # 在所有未开始比赛中找相同比分的赔率
    matches_found = []
    for item in early_data:
        for cs in item.get('cs', []):
            if cs['score'] == score:
                matches_found.append((item['home'], item['away'], item['league'], cs['odds']))
    if matches_found:
        log(f"  中奖波胆 {score} @ {odds_val} [{league}] 在未开始比赛中:")
        for h, a, l, o in matches_found[:5]:
            log(f"    {h} vs {a} [{l}] -> {o}")

log("完成")
