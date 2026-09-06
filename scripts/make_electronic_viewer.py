#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 data/electronic_poll_*.jsonl 转成自包含 HTML 浏览器（表格 + 赔率走势）。
哨响AI · 电子盘(EAFC25/PANDA) 采集数据可视化。"""
import json, glob, os, re

DATA_DIR = "data"
OUT = os.path.join(DATA_DIR, "electronic_viewer.html")
OU_RE = re.compile(r"^OU_(\d+(?:\.\d+)?)$")

def get_1x2(mk):
    m = (mk or {}).get("1X2")
    if not isinstance(m, dict):
        return (None, None, None)
    for ks in (("h", "d", "a"), ("home", "draw", "away"), ("主", "平", "客")):
        if all(k in m for k in ks):
            return (m[ks[0]], m[ks[1]], m[ks[2]])
    for v in m.values():
        if isinstance(v, dict):
            for ks in (("h", "d", "a"), ("home", "draw", "away")):
                if all(k in v for k in ks):
                    return (v[ks[0]], v[ks[1]], v[ks[2]])
    return (None, None, None)

def get_ou(mk, line):
    v = (mk or {}).get("OU_%s" % line)
    if isinstance(v, dict):
        ov = v.get("over"); un = v.get("under")
        return (ov, un)
    return (None, None)

def get_dd(mk):
    v = (mk or {}).get("单/双") or (mk or {}).get("单双")
    if isinstance(v, dict):
        items = list(v.items())
        if len(items) >= 2:
            return (items[0][1], items[1][1])
    return (None, None)

def get_dc(mk):
    v = (mk or {}).get("双重机会")
    if isinstance(v, dict):
        return (v.get("1X"), v.get("12"), v.get("X2"))
    return (None, None, None)

def main():
    files = sorted(glob.glob(os.path.join(DATA_DIR, "electronic_poll_*.jsonl")))
    matches = []
    for p in files:
        mid = os.path.basename(p).replace("electronic_poll_", "").replace(".jsonl", "")
        rows = []
        with open(p, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rows.append(json.loads(ln))
                except Exception:
                    pass
        if not rows:
            continue
        # 统计 OU 线频率选主线
        line_cnt = {}
        for r in rows:
            for k in (r.get("markets") or {}):
                m = OU_RE.match(k)
                if m:
                    line_cnt[m.group(1)] = line_cnt.get(m.group(1), 0) + 1
        main_line = max(line_cnt, key=line_cnt.get) if line_cnt else None
        ticks = []
        for r in rows:
            mk = r.get("markets") or {}
            h, d, a = get_1x2(mk)
            ov = un = None
            if main_line:
                ov, un = get_ou(mk, main_line)
            dd_s, dd_d = get_dd(mk)
            dc1, dc2, dc3 = get_dc(mk)
            ticks.append({
                "t": r.get("ts_iso"), "s": r.get("score") or "", "m": r.get("minute"),
                "h": h, "d": d, "a": a, "ov": ov, "un": un,
                "dds": dd_s, "ddd": dd_d, "dc1": dc1, "dc2": dc2, "dc3": dc3,
            })
        matches.append({
            "mid": mid, "home": rows[0].get("home"), "away": rows[0].get("away"),
            "league": (rows[0].get("league") or "")[:40],
            "n": len(ticks), "line": main_line, "ticks": ticks,
        })
    matches.sort(key=lambda x: -x["n"])
    data_js = json.dumps(matches, ensure_ascii=False)
    html = HTML_TEMPLATE.replace("__DATA__", data_js)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print("已生成 %s | %d 场 | 总 ticks=%d" % (OUT, len(matches), sum(m["n"] for m in matches)))
    for m in matches:
        print("  %s %s vs %s | %d ticks | OU主线=%s" % (m["mid"], m["home"], m["away"], m["n"], m["line"]))

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>哨响AI · 电子盘采集数据浏览器</title>
<style>
  :root{--bg:#0f1419;--panel:#1a2129;--card:#222b35;--txt:#e6edf3;--mut:#8b98a5;--line:#2d3a47;
        --hcol:#4ea1ff;--dcol:#f0b429;--acol:#ff6b6b;--ov:#3ddc97;--un:#c792ea;}
  *{box-sizing:border-box}
  body{margin:0;font-family:-apple-system,"Segoe UI",Roboto,"Microsoft YaHei",sans-serif;background:var(--bg);color:var(--txt)}
  header{padding:14px 20px;border-bottom:1px solid var(--line);background:var(--panel)}
  header h1{margin:0;font-size:18px}
  header small{color:var(--mut)}
  .wrap{display:flex;height:calc(100vh - 58px)}
  .side{width:300px;overflow-y:auto;border-right:1px solid var(--line);padding:10px;background:var(--panel)}
  .side button{display:block;width:100%;text-align:left;margin:4px 0;padding:9px 11px;border:1px solid var(--line);
        background:var(--card);color:var(--txt);border-radius:8px;cursor:pointer;font-size:13px}
  .side button:hover{border-color:var(--hcol)}
  .side button.on{border-color:var(--hcol);background:#1d2b3a}
  .side .mt{font-weight:600;font-size:13px}
  .side .ms{color:var(--mut);font-size:11px;margin-top:2px}
  .main{flex:1;overflow-y:auto;padding:16px}
  .title{font-size:16px;margin-bottom:4px}
  .meta{color:var(--mut);font-size:12px;margin-bottom:12px}
  .charts{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:16px}
  .chart{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px}
  .chart h3{margin:0 0 6px;font-size:13px;color:var(--mut)}
  table{border-collapse:collapse;width:100%;font-size:12px;font-variant-numeric:tabular-nums}
  th,td{border:1px solid var(--line);padding:4px 7px;text-align:center;white-space:nowrap}
  th{background:var(--panel);position:sticky;top:0}
  td.h{color:var(--hcol)} td.d{color:var(--dcol)} td.a{color:var(--acol)}
  td.ov{color:var(--ov)} td.un{color:var(--un)}
  .scroll{max-height:520px;overflow-y:auto;border:1px solid var(--line);border-radius:8px}
  .legend span{margin-right:12px;font-size:12px}
</style>
</head>
<body>
<header><h1>哨响AI · 电子盘采集数据浏览器</h1>
<small>EAFC25 / PANDA 独家 · 1秒级轮询 · 数据来自 data/electronic_poll_*.jsonl · 时间轴为墙钟</small></header>
<div class="wrap">
  <div class="side" id="side"></div>
  <div class="main" id="main">
    <div class="title" id="mtitle">请选择左侧场次</div>
    <div class="meta" id="mmeta"></div>
    <div class="charts" id="charts" style="display:none">
      <div class="chart"><h3>1X2 赔率走势</h3><div id="c1"></div>
        <div class="legend"><span style="color:var(--hcol)">主胜</span><span style="color:var(--dcol)">平</span><span style="color:var(--acol)">客胜</span></div></div>
      <div class="chart"><h3 id="ouTitle">OU 大/小走势</h3><div id="c2"></div>
        <div class="legend"><span style="color:var(--ov)">大</span><span style="color:var(--un)">小</span></div></div>
    </div>
    <div class="scroll" id="tbl"></div>
  </div>
</div>
<script>
const DATA = __DATA__;
const side=document.getElementById('side');
DATA.forEach((m,i)=>{
  const b=document.createElement('button');
  b.innerHTML=`<div class="mt">${m.home} vs ${m.away}</div><div class="ms">${m.mid} · ${m.n} ticks · OU主线 ${m.line||'-'}</div>`;
  b.onclick=()=>{select(i);document.querySelectorAll('.side button').forEach(x=>x.classList.remove('on'));b.classList.add('on');};
  side.appendChild(b);
});
function num(v){return (typeof v==='number'&&isFinite(v))?v:null;}
function svgLine(series,W,H,pad,color){
  const pts=series.map(s=>s.p).filter(p=>p.y!=null);
  if(!pts.length)return '';
  const xs=series.map(s=>s.x), ys=pts.map(p=>p.y);
  const xmin=Math.min(...xs),xmax=Math.max(...xs),ymin=Math.min(...ys),ymax=Math.max(...ys);
  const dx=xmax-xmin||1, dy=(ymax-ymin)||1;
  const sx=x=>pad+(x-xmin)/dx*(W-2*pad);
  const sy=y=>H-pad-(y-ymin)/dy*(H-2*pad);
  let pl=pts.map(p=>`${sx(p.x).toFixed(1)},${sy(p.y).toFixed(1)}`).join(' ');
  // y 轴标签
  let labels='';
  [ymin,ymax].forEach(yv=>{labels+=`<text x="2" y="${sy(yv).toFixed(1)}" fill="#8b98a5" font-size="9">${yv.toFixed(2)}</text>`;});
  return `<svg width="${W}" height="${H}" style="display:block">
    <line x1="${pad}" y1="${H-pad}" x2="${W-pad}" y2="${H-pad}" stroke="#2d3a47"/>
    <polyline points="${pl}" fill="none" stroke="${color}" stroke-width="1.4"/>
    ${labels}</svg>`;
}
function select(i){
  const m=DATA[i];
  document.getElementById('mtitle').textContent=`${m.home} vs ${m.away}`;
  document.getElementById('mmeta').textContent=`mid=${m.mid} | 联赛=${m.league} | ticks=${m.n} | OU主线=${m.line||'-'}`;
  document.getElementById('charts').style.display='flex';
  document.getElementById('ouTitle').textContent=`OU ${m.line||''} 大/小走势`;
  const W=420,H=200,pad=28;
  const n=m.ticks.length;
  const s1=m.ticks.map((t,k)=>({x:k,p:{x:k,y:num(t.h)}}));
  const s2=m.ticks.map((t,k)=>({x:k,p:{x:k,y:num(t.d)}}));
  const s3=m.ticks.map((t,k)=>({x:k,p:{x:k,y:num(t.a)}}));
  document.getElementById('c1').innerHTML=svgLine(s1,W,H,pad,'#4ea1ff')+svgLine(s2,W,H,pad,'#f0b429')+svgLine(s3,W,H,pad,'#ff6b6b');
  const o1=m.ticks.map((t,k)=>({x:k,p:{x:k,y:num(t.ov)}}));
  const o2=m.ticks.map((t,k)=>({x:k,p:{x:k,y:num(t.un)}}));
  document.getElementById('c2').innerHTML=svgLine(o1,W,H,pad,'#3ddc97')+svgLine(o2,W,H,pad,'#c792ea');
  // 表格
  let html='<table><thead><tr><th>#</th><th>时间</th><th>比分</th><th>分钟</th>'+
    '<th>主胜</th><th>平</th><th>客胜</th>'+
    `<th>OU${m.line||''}大</th><th>OU${m.line||''}小</th>`+
    '<th>单</th><th>双</th><th>1X</th><th>12</th><th>X2</th></tr></thead><tbody>';
  m.ticks.forEach((t,k)=>{
    html+=`<tr><td>${k}</td><td>${t.t||''}</td><td>${t.s||''}</td><td>${t.m??''}</td>`+
      `<td class="h">${t.h??''}</td><td class="d">${t.d??''}</td><td class="a">${t.a??''}</td>`+
      `<td class="ov">${t.ov??''}</td><td class="un">${t.un??''}</td>`+
      `<td>${t.dds??''}</td><td>${t.ddd??''}</td><td>${t.dc1??''}</td><td>${t.dc2??''}</td><td>${t.dc3??''}</td></tr>`;
  });
  html+='</tbody></table>';
  document.getElementById('tbl').innerHTML=html;
}
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
