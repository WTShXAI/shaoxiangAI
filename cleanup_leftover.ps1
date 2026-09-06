$ErrorActionPreference = 'Continue'
$target = 'D:\Architecture\archive'
$log    = 'D:\Architecture\cleanup_log.txt'

"=== cleanup run $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Out-File $log -Encoding utf8

# move the whole archive dir (all aiqiuke leftovers) to system temp,
# so nothing aiqiuke-related stays under D:\Architecture.
if (Test-Path -LiteralPath $target) {
  $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
  $tmp   = Join-Path $env:TEMP ('aiqiuke_leftover_' + $stamp)
  New-Item -ItemType Directory -Force -Path $tmp | Out-Null
  try {
    Move-Item -LiteralPath $target -Destination (Join-Path $tmp 'archive') -Force
    "MOVED: $target -> $tmp\archive" | Out-File $log -Append -Encoding utf8
  } catch {
    "ERROR moving: $_" | Out-File $log -Append -Encoding utf8
  }
} else {
  "SKIP: $target not present" | Out-File $log -Append -Encoding utf8
}

"--- verify: D:\Architecture top level ---" | Out-File $log -Append -Encoding utf8
Get-ChildItem -LiteralPath 'D:\Architecture' -Force -Directory -ErrorAction SilentlyContinue |
  ForEach-Object { "   [dir]  " + $_.Name } | Out-File $log -Append -Encoding utf8

"--- verify: aiqiuke dir present? ---" | Out-File $log -Append -Encoding utf8
("   D:\Architecture\aiqiuke exists = " + (Test-Path -LiteralPath 'D:\Architecture\aiqiuke')) | Out-File $log -Append -Encoding utf8
"--- verify: archive dir present? ---" | Out-File $log -Append -Encoding utf8
("   D:\Architecture\archive exists = " + (Test-Path -LiteralPath 'D:\Architecture\archive')) | Out-File $log -Append -Encoding utf8

"--- temp target ---" | Out-File $log -Append -Encoding utf8
Get-ChildItem -LiteralPath (Join-Path $env:TEMP 'aiqiuke_leftover_*') -Directory -ErrorAction SilentlyContinue |
  ForEach-Object { "   " + $_.FullName } | Out-File $log -Append -Encoding utf8
