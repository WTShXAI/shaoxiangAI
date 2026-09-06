import requests, json, base64, gzip, zlib, time

cookies = {'X-API-UUID':'3120eaa7-3e5f-4c84-8c45-32d3a461b5fc','TRACK-HOUR':'13','X-API-TOKEN':'d6381b31f1324c13f1a950009d11d08badee878fda9b7f0dfc01447a503e745f7c31887b86d85271efbe44c26f838cb9'}
import io as _io
TOKEN = next((_l.split("=", 1)[1].strip() for _l in _io.open(r"gq/.env", encoding="utf-8")
             if _l.strip().startswith("GQ_REQUEST_ID=")), "")
CUID = '526002076777845380'
PREFIX = 'pc-c6cf3aabe2a84dd3a870d669b8ba5094'
s = requests.Session()
s.cookies.update(cookies)
def h():
    return {'Content-Type':'application/json;charset=utf-8','checkid':f'{PREFIX}-{CUID}-{int(time.time()*1000)}','requestid':TOKEN,'User-Agent':'Mozilla/5.0'}

tourney = json.load(open(r'D:\Architecture\tourney_raw.json', encoding='utf-8'))
o = tourney[0]
mids_list = []
for x in o.get('livedata',[]) + o.get('nolivedata',[]):
    if x.get('mids'):
        mids_list.append(str(x['mids']))

print(f'代表mid总数: {len(mids_list)}')

# 分批查，每批50个
all_matches = []
seen_mids = set()
for i in range(0, len(mids_list), 50):
    batch = mids_list[i:i+50]
    body = {'cuid':CUID, 'mids':','.join(batch), 'euid':'3020101', 'cos':0, 'orpt':0}
    try:
        r = s.post('https://api.u92tiil.com/yewu11/v1/w/structureMatchBaseInfoByMidsPB', json=body, headers=h(), timeout=15)
        j = r.json()
        if j.get('code') != '0000000' or not j.get('data'):
            print(f'批次{i} 无效响应')
            continue
        raw = base64.b64decode(j['data'])
        obj = json.loads(gzip.decompress(raw))
        data = obj.get('data', [])
        for m in data:
            mid = str(m.get('mid', ''))
            if mid and mid not in seen_mids:
                seen_mids.add(mid)
                all_matches.append(m)
        print(f'批次{i} 返回{len(data)}场, 累计{len(all_matches)}')
    except Exception as e:
        print(f'批次{i} 异常: {str(e)[:60]}')
    time.sleep(1)

print(f'\n总返回比赛: {len(all_matches)}')
# 统计未开始
notstart = [m for m in all_matches if str(m.get('mststi')) in ('0', 'None')]
print(f'未开始: {len(notstart)}')
for m in notstart[:10]:
    print(f'  mid={m.get("mid")} {m.get("mhn")} vs {m.get("man")} league={m.get("tnjc") or m.get("tn")}')

# 保存所有未开始的 mid
mids_to_fetch = [str(m['mid']) for m in notstart if m.get('mid')]
json.dump(mids_to_fetch, open(r'D:\Architecture\tourney_notstart_mids.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
print(f'未开始mid已保存: {len(mids_to_fetch)} 个')
