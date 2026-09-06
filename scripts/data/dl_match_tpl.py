import urllib.request, re, os
base="https://user-pc-new.realcpf.com/2026-07-04-21-54-02/static/js/"
files=["match-list-tpl-CSyb4afa.js","join-left-record-B5PgKCH3.js"]
for f in files:
    try:
        req=urllib.request.Request(base+f, headers={'User-Agent':'Mozilla/5.0','Referer':'https://user-pc-new.realcpf.com/'})
        data=urllib.request.urlopen(req,timeout=30).read()
        open(f"D:/Architecture/{f}","wb").write(data)
        print("DL", f, len(data))
        # 搜 betRecord / history 相关接口路径
        txt=data.decode('utf-8','ignore')
        apis=set(re.findall(r'["\']([^"\']*(?:betRecord|historyRecord|history_record|getHistory|order/[a-zA-Z]+|settlement|queryRecord)[^"\']*)["\']', txt))
        print("  API候选:", list(apis)[:20])
        funcs=re.findall(r'(?:function\s+)?[a-zA-Z_$]+\s*=\s*async\s*\([^)]*\)\s*=>\s*\{[^}]{0,200}?betRecord[^}]{0,100}', txt)
        print("  betRecord函数片段:", funcs[:3])
    except Exception as e:
        print("ERR", f, str(e)[:80])
