r"""build_dashboard.py — 评估看板构建 (P-DASH, SYSTEM_BLUEPRINT §2.2)

只读 reports/verification_report.json + reports/data_integrity.json +
reports/verification_sample_growth.{json,csv},
内联为独立 HTML deliverables/dashboard/evaluation_dashboard.html(无后端, file:// 可直接开)。
零碰 events.db; 仅渲染既有评审产物。

用法: .venv/Scripts/python.exe scripts/build_dashboard.py
"""
import os
import csv
import json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(ROOT, "reports")
OUT_DIR = os.path.join(ROOT, "deliverables", "dashboard")
OUT = os.path.join(OUT_DIR, "evaluation_dashboard.html")

VER = os.path.join(REPORTS, "verification_report.json")
INT = os.path.join(REPORTS, "data_integrity.json")
SAMPLE = os.path.join(REPORTS, "verification_sample_growth.json")
SAMPLE_CSV = os.path.join(REPORTS, "verification_sample_growth.csv")


def load(p):
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return None


def load_sample():
    """读取样本累积产物。返回 (summary_dict, series_dict)。

    summary_dict: verification_sample_growth.json 全文(含 by_source / g1_accept_n)。
    series_dict: {model_source: [[date, cum_n], ...]} (按 CSV 还原时间序列, 用于折线图)。
    """
    summary = load(SAMPLE) or {}
    series = {}
    if os.path.exists(SAMPLE_CSV):
        try:
            with open(SAMPLE_CSV, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    src = (row.get("model_source") or "").strip()
                    date = (row.get("date") or "").strip()
                    cum = row.get("cum_n")
                    if not src or cum in (None, ""):
                        continue
                    try:
                        cum = int(float(cum))
                    except (TypeError, ValueError):
                        continue
                    series.setdefault(src, []).append([date, cum])
        except Exception:
            pass
    return summary, series


def main():
    ver = load(VER) or {"models": {}, "generated_at": "n/a", "disclaimer": ""}
    integ = load(INT) or {"overall": "n/a", "checks": {}, "generated_at": "n/a"}
    sample_summary, sample_series = load_sample()

    html = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>哨响AI 评估看板</title>
<style>
:root{--ok:#2e7d32;--warn:#f9a825;--fail:#c62828;--bg:#fafafa;--card:#fff;--ink:#222;--mut:#666;}
*{box-sizing:border-box} body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:var(--bg);color:var(--ink);padding:20px}
h1{font-size:20px;margin:0 0 4px} .sub{color:var(--mut);font-size:12px;margin-bottom:18px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}
.card{background:var(--card);border:1px solid #eee;border-radius:10px;padding:14px;box-shadow:0 1px 3px rgba(0,0,0,.04)}
.tag{display:inline-block;padding:2px 10px;border-radius:20px;font-size:12px;font-weight:600;color:#fff}
.t-ok{background:var(--ok)} .t-warn{background:var(--warn)} .t-fail{background:var(--fail)} .t-na{background:#9e9e9e}
.k{color:var(--mut);font-size:11px} .v{font-size:15px;font-weight:600;margin-bottom:6px}
.row{display:flex;justify-content:space-between;font-size:12px;padding:2px 0;border-bottom:1px dashed #f0f0f0}
.reasons{font-size:11px;color:var(--mut);margin-top:8px;white-space:pre-wrap}
.sec{margin-top:26px;font-size:16px;font-weight:600;border-left:4px solid var(--ok);padding-left:8px}
table{width:100%;border-collapse:collapse;font-size:12px;margin-top:8px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #eee}
th{color:var(--mut)}
.disc{font-size:11px;color:var(--mut);margin-top:20px;border-top:1px solid #eee;padding-top:10px}
.chartcard{background:var(--card);border:1px solid #eee;border-radius:10px;padding:14px;margin-top:10px}
.legend{font-size:11px;margin-top:6px;display:flex;flex-wrap:wrap;gap:12px}
.lg{display:flex;align-items:center;gap:4px}
.sw{width:14px;height:3px;display:inline-block;border-radius:2px}
</style></head><body>
<h1>哨响AI · 盈利验证与数据完整性看板</h1>
<div class="sub" id="sub"></div>
<div class="grid" id="models"></div>
<div class="sec">数据完整性 (每日自动检查)</div>
<div id="integrity"></div>
<div class="sec">样本累积趋势 (G1 验收线 2500 进度)</div>
<div class="chartcard">
  <div id="samplechart"></div>
  <div class="legend" id="samplelegend"></div>
  <div id="sampletable"></div>
</div>
<div class="disc" id="disc"></div>
<script>
const VER = __VER__;
const INT = __INT__;
const SAMPLE = __SAMPLE__;
const MODEL_CN = {candles_ensemble:'K线集成', market_baseline:'市场基线', KNN:'KNN赛前',
  independent_model:'独立模型', william_inter:'威廉校准镜'};
function pct(x){return x==null?'—':(x*100).toFixed(2)+'%';}
function cls(v){return v==='NO EDGE'?'t-fail':v==='INCONCLUSIVE'?'t-warn':v==='EDGE'?'t-ok':'t-na';}
function card(name,d){
  const cn=MODEL_CN[name]||name;
  const reasons=(d.reasons||[]).map(r=>'• '+r).join('\\n');
  return `<div class="card">
    <span class="tag ${cls(d.verdict)}">${d.verdict||'—'}</span>
    <div class="v" style="margin-top:8px">${cn}</div>
    <div class="row"><span class="k">样本 n</span><span>${d.matches??'—'}</span></div>
    <div class="row"><span class="k">ROI</span><span>${pct(d.roi)}</span></div>
    <div class="row"><span class="k">95% CI</span><span>[${pct(d.ci_low)}, ${pct(d.ci_high)}]</span></div>
    <div class="row"><span class="k">LogLoss</span><span>${d.log_loss==null?'—':d.log_loss.toFixed(4)}</span></div>
    <div class="row"><span class="k">Brier</span><span>${d.brier==null?'—':d.brier.toFixed(4)}</span></div>
    <div class="row"><span class="k">ECE</span><span>${d.ece==null?'—':d.ece.toFixed(4)}</span></div>
    <div class="row"><span class="k">vs_market_LL</span><span>${d.vs_market_ll==null?'—':d.vs_market_ll.toFixed(4)}</span></div>
    <div class="row"><span class="k">方向二项 p</span><span>${d.direction_binomial_p==null?'—':d.direction_binomial_p.toFixed(3)}</span></div>
    <div class="reasons">${reasons.replace(/\\n/g,'<br>')}</div>
  </div>`;
}
document.getElementById('sub').textContent='验证报告生成: '+(VER.generated_at||'?')+'  |  看板构建: '+new Date().toLocaleString();
const mg=document.getElementById('models');
Object.keys(VER.models||{}).forEach(n=>{mg.insertAdjacentHTML('beforeend',card(n,VER.models[n]));});
const INT_=INT||{};
const icls=INT_.overall==='OK'?'t-ok':INT_.overall==='WARN'?'t-warn':INT_.overall==='FAIL'?'t-fail':'t-na';
let ih='<div style="margin-bottom:8px"><span class="tag '+icls+'">overall: '+(INT_.overall||'?')+'</span> '+
  '<span class="k"> 生成: '+(INT_.generated_at||'?')+'</span></div><table><tr><th>检查</th><th>计数</th><th>级别</th><th>说明</th></tr>';
Object.entries(INT_.checks||{}).forEach(([k,v])=>{
  const l=v.level||'INFO';
  const lc=l==='OK'?'t-ok':l==='WARN'?'t-warn':l==='FAIL'?'t-fail':'t-na';
  ih+=`<tr><td>${k}</td><td>${v.count??'—'}</td><td><span class="tag ${lc}">${l}</span></td><td style="color:var(--mut)">${v.note||''}</td></tr>`;
});
ih+='</table>';
document.getElementById('integrity').innerHTML=ih;
document.getElementById('disc').textContent=VER.disclaimer||'本系统仅解释概率偏差, 不提供下注建议。';

/* ---- 样本累积趋势面板 ---- */
(function(){
  const sum = SAMPLE || {};
  const series = sum.__series__ || {};
  const g1 = sum.g1_accept_n || 2500;
  const col = {KNN:'#1565c0', candles_ensemble:'#6a1b9a', market_baseline:'#2e7d32',
    independent_model:'#ef6c00', william_inter:'#00838f'};
  const W=680,H=300,pad=38,top=14;
  const names = Object.keys(series);
  if(!names.length){
    document.getElementById('samplechart').innerHTML='<div class="k">暂无样本累积数据 (reports/verification_sample_growth.csv)</div>';
    return;
  }
  let allPts=[];
  names.forEach(n=>series[n].forEach(p=>allPts.push(p[1])));
  const maxY = Math.max(g1, ...allPts) * 1.08;
  let dates=[]; names.forEach(n=>series[n].forEach(p=>{ if(!dates.includes(p[0])) dates.push(p[0]); }));
  dates.sort();
  const xOf = i => pad + (dates.length<=1?0:(i/(dates.length-1))*(W-pad-10));
  const yOf = v => top + (1 - v/maxY)*(H-top-30);
  let svg = `<svg viewBox="0 0 ${W} ${H}" width="100%" style="font-family:inherit">`;
  // y 网格 + 轴
  [0, g1, maxY].forEach(v=>{
    const y=yOf(v);
    if(v===g1) svg+=`<line x1="${pad}" y1="${y}" x2="${W-10}" y2="${y}" stroke="#c62828" stroke-dasharray="5 4" stroke-width="1"/>`+
      `<text x="${W-8}" y="${y-4}" fill="#c62828" font-size="10" text-anchor="end">G1=${v}</text>`;
    else svg+=`<line x1="${pad}" y1="${y}" x2="${W-10}" y2="${y}" stroke="#eee" stroke-width="1"/>`+
      `<text x="${pad-4}" y="${y+3}" fill="#999" font-size="10" text-anchor="end">${Math.round(v)}</text>`;
  });
  names.forEach(n=>{
    const pts = series[n]; const c = col[n]||'#555';
    let d='';
    pts.forEach(p=>{ const xi=dates.indexOf(p[0]); d += (d?'L':'M')+xOf(xi).toFixed(1)+' '+yOf(p[1]).toFixed(1)+' '; });
    svg += `<path d="${d}" fill="none" stroke="${c}" stroke-width="2"/>`;
    // 末点
    const last=pts[pts.length-1]; const xi=dates.indexOf(last[0]);
    svg += `<circle cx="${xOf(xi).toFixed(1)}" cy="${yOf(last[1]).toFixed(1)}" r="3" fill="${c}"/>`;
  });
  svg += `<text x="${pad}" y="${H-6}" fill="#999" font-size="10">${dates[0]} → ${dates[dates.length-1]}</text>`;
  svg += '</svg>';
  document.getElementById('samplechart').innerHTML = svg;
  // 图例
  let lg='';
  names.forEach(n=>{ const c=col[n]||'#555'; lg+=`<span class="lg"><span class="sw" style="background:${c}"></span>${MODEL_CN[n]||n}</span>`; });
  document.getElementById('samplelegend').innerHTML=lg;
  // 进度表
  let th='<table><tr><th>模型源</th><th>当前累计</th><th>G1目标</th><th>缺口</th><th>状态</th></tr>';
  const by=sum.by_source||{};
  names.forEach(n=>{
    const d=by[n]||{}; const cur=d.cum_n??series[n][series[n].length-1][1];
    const gap=d.gap_to_g1!=null?d.gap_to_g1:(g1-cur);
    const ok=d.g1_reached!=null?d.g1_reached:(cur>=g1);
    const sc=ok?'t-ok':'t-warn';
    th+=`<tr><td>${MODEL_CN[n]||n}</td><td>${cur}</td><td>${g1}</td><td>${gap}</td><td><span class="tag ${sc}">${ok?'已达标':'缺'+gap}</span></td></tr>`;
  });
  th+='</table>';
  document.getElementById('sampletable').innerHTML=th;
})();
</script></body></html>"""
    sample_payload = {
        "g1_accept_n": sample_summary.get("g1_accept_n"),
        "by_source": sample_summary.get("by_source", {}),
        "generated_at": sample_summary.get("generated_at"),
        "__series__": sample_series,
    }
    payload = (
        html.replace("__VER__", json.dumps(ver, ensure_ascii=False))
        .replace("__INT__", json.dumps(integ, ensure_ascii=False))
        .replace("__SAMPLE__", json.dumps(sample_payload, ensure_ascii=False))
    )
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(payload)
    print(f"[看板] 已生成 → {OUT}  (验证模型 {len(ver.get('models',{}))} | 完整性 {integ.get('overall')} | 样本源 {len(sample_series)})")


if __name__ == "__main__":
    main()
