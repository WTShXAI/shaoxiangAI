import json, base64, gzip
# 重新解码 odds_pb_raw.bin
raw = open(r"D:\Architecture\odds_pb_raw.bin","rb").read()
g = gzip.decompress(raw)
obj = json.loads(g.decode('utf-8'))
print("TOP KEYS:", list(obj.keys()))
d = obj.get('data') or []
print("data count:", len(d))
m = d[0]
print("match mststi:", m.get('mststi'), "league:", m.get('tnjc'))
# playData 波胆
pd = obj.get('playData') or []
for x in pd:
    if isinstance(x,dict) and (x.get('hpn')=='全场波胆' or str(x.get('topKey'))=='7'):
        print("PLAYDATA 全场波胆 hps_count:", len(x.get('hps') or []))
        print("  sample:", json.dumps((x.get('hps') or [])[:2], ensure_ascii=False)[:300])
# hpsPns 波胆
hp = m.get('hpsPns') or []
print("hpsPns count:", len(hp))
for x in hp:
    if isinstance(x,dict) and '波胆' in str(x.get('hpn','')):
        print("hpsPns 波胆 hps_count:", len(x.get('hps') or []))
        print("  sample:", json.dumps((x.get('hps') or [])[:2], ensure_ascii=False)[:300])
        break
# 也看 data[0] 里有没有其他含 ov/otv 的字段
print("match keys sample:", list(m.keys())[:20])
