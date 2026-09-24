r"""build_dashboard.py — 评估看板构建 (P-DASH, SYSTEM_BLUEPRINT §2.2)

只读 reports/verification_report.json + reports/data_integrity.json,
内联为独立 HTML deliverables/dashboard/evaluation_dashboard.html(无后端, file:// 可直接开)。
零碰 events.db; 仅渲染既有评审产物。

用法: .venv/Scripts/python.exe scripts/build_dashboard.py
"""
import os
import json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(ROOT, "reports")
OUT_DIR = os.path.join(ROOT, "deliverables", "dashboard")
OUT = os.path.join(OUT_DIR, "evaluation_dashboard.html")

VER = os.path.join(REPORTS, "verification_report.json")
INT = os.path.join(REPORTS, "data_integrity.json")


def load(p):
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return None


def main():
    ver = load(VER) or {"models": {}, "generated_at": "n/a", "disclaimer": ""}
    integ = load(INT) or {"overall": "n/a", "checks": {}, "generated_at": "n/a"}

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
</style></head><body>
<h1>哨响AI · 盈利验证与数据完整性看板</h1>
<div class="sub" id="sub"></div>
<div class="grid" id="models"></div>
<div class="sec">数据完整性 (每日自动检查)</div>
<div id="integrity"></div>
<div class="disc" id="disc"></div>
<script>
const VER = __VER__;
const INT = __INT__;
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
let html='<div style="margin-bottom:8px"><span class="tag '+icls+'">overall: '+(INT_.overall||'?')+'</span> '+
  '<span class="k"> 生成: '+(INT_.generated_at||'?')+'</span></div><table><tr><th>检查</th><th>计数</th><th>级别</th><th>说明</th></tr>';
Object.entries(INT_.checks||{}).forEach(([k,v])=>{
  const l=v.level||'INFO';
  const lc=l==='OK'?'t-ok':l==='WARN'?'t-warn':l==='FAIL'?'t-fail':'t-na';
  html+=`<tr><td>${k}</td><td>${v.count??'—'}</td><td><span class="tag ${lc}">${l}</span></td><td style="color:var(--mut)">${v.note||''}</td></tr>`;
});
html+='</table>';
document.getElementById('integrity').innerHTML=html;
document.getElementById('disc').textContent=VER.disclaimer||'本系统仅解释概率偏差, 不提供下注建议。';
</script></body></html>"""
    payload = html.replace("__VER__", json.dumps(ver, ensure_ascii=False)).replace("__INT__", json.dumps(integ, ensure_ascii=False))
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(payload)
    print(f"[看板] 已生成 → {OUT}  (验证模型 {len(ver.get('models',{}))} | 完整性 {integ.get('overall')})")


if __name__ == "__main__":
    main()
