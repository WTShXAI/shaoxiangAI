$py = "C:\Users\ShXAI\.workbuddy\binaries\python\versions\3.13.12\python.exe"
$script = "D:\Architecture\bridge_service.py"
$port = 9111
$out = "D:\Architecture\logs\bridge_stdout.log"
$err = "D:\Architecture\logs\bridge_stderr.log"
if (-not (Test-Path "D:\Architecture\logs")) { New-Item -ItemType Directory -Path "D:\Architecture\logs" | Out-Null }

$proc = Start-Process -FilePath $py -ArgumentList $script,"--port",$port -NoNewWindow -PassThru -RedirectStandardOutput $out -RedirectStandardError $err
Write-Host ("STARTED pid={0}" -f $proc.Id)
Start-Sleep -Seconds 7

Write-Host "=== server stdout tail ==="
Get-Content $out -Tail 8 -ErrorAction SilentlyContinue

Write-Host "=== /health ==="
try { $h = Invoke-RestMethod -Uri "http://127.0.0.1:$port/health" -TimeoutSec 10; $h | ConvertTo-Json -Compress } catch { Write-Host ("health FAIL: "+$_.Exception.Message) }

function ShowPredict($label, $home, $away, $oh, $od, $oa, $hcp, $ou) {
  Write-Host ("=== /predict {0} ===" -f $label)
  $body = @{home=$home;away=$away;odds_h=$oh;odds_d=$od;odds_a=$oa;hcp=$hcp;ou_line=$ou;matchday=3;stage="knockout"} | ConvertTo-Json -Compress
  try {
    $r = Invoke-RestMethod -Uri "http://127.0.0.1:$port/predict" -Method Post -Body $body -ContentType "application/json" -TimeoutSec 40
    $fv = $r.final_verdict
    $v7 = $r.v7_raw
    Write-Host ("  verdict(primary): {0}" -f $fv.primary)
    Write-Host ("  best_score: {0}  alt: {1}" -f $v7.best_score, ($v7.alt_scores -join ", "))
    Write-Host ("  prediction: {0}  confidence: {1}" -f $v7.prediction, $v7.confidence)
    Write-Host ("  market_probs(H/D/A): {0}/{1}/{2}" -f $v7.market_probs.H, $v7.market_probs.D, $v7.market_probs.A)
    if ($r.ou_link) { Write-Host ("  ou_recommend: {0} line={1} exp_total={2}" -f $r.ou_link.recommend, $r.ou_link.line, $r.ou_link.expected_total) }
  } catch { Write-Host ("predict FAIL: "+$_.Exception.Message) }
}

ShowPredict "Argentina vs Egypt (1.32/5.0/10.0)" "Argentina" "Egypt" 1.32 5.0 10.0 -1.5 2.5
ShowPredict "Switzerland vs Colombia (3.45/3.0/2.29)" "Switzerland" "Colombia" 3.45 3.0 2.29 -0.5 2.25

Write-Host ("SERVER STILL RUNNING pid={0} on port {1}" -f $proc.Id, $port)
Write-Host "stop with: Stop-Process -Id $($proc.Id)"