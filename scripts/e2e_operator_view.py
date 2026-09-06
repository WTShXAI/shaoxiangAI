import urllib.request, json

def call(body):
    req = urllib.request.Request('http://127.0.0.1:9111/api/predict/live',
        data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
    return json.load(urllib.request.urlopen(req))['data']

scenes = {
 '1_分歧局(1X2主/亚盘客)': {'home':'美国','away':'巴拉圭','oh':1.50,'od':4.0,'oa':6.0,
                          'hcp_line':-0.5,'hcp_home_odds':2.10,'hcp_away_odds':1.80},
 '2_一边倒强队(无亚盘)':   {'home':'西班牙','away':'佛得角','oh':1.30,'od':5.0,'oa':9.0},
 '3_高水降权':             {'home':'阿尔法','away':'贝塔','oh':1.40,'od':3.20,'oa':5.50},
 '4_防平测试':             {'home':'伽马','away':'德尔塔','oh':2.40,'od':3.00,'oa':3.10},
 '5_深盘一致(主让-1.5)':   {'home':'西班牙','away':'佛得角','oh':1.30,'od':5.0,'oa':9.0,
                          'hcp_line':-1.5,'hcp_home_odds':1.85,'hcp_away_odds':2.05},
}
for name, b in scenes.items():
    d = call(b)
    op = d.get('operator_view')
    print('=== %s ===' % name)
    print('  direction=%s | conf=%.1f%% | draw_alert=%s | high_vig=%s'
          % (d['direction'], d['market_conf']*100, d['draw_signal']['draw_alert'], d['risk']['high_vig']))
    if op:
        print('  stake=%s | verdict=%s' % (op['stake_hint'], op['verdict']))
        print('  rules=%s' % [r['id']+':'+r['label'] for r in op['rules_fired']])
    else:
        print('  !! operator_view MISSING')
    print()
print('E2E done')
