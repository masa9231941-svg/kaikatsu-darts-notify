# Generate the darts-store master for kaikatsu-darts-notify (run manually during dev).
#
#   .\generate-darts-stores.ps1              # from shop.js only (fast)
#   .\generate-darts-stores.ps1 -WithUnits   # also scrape the board count from each
#                                            # detail page (~248 requests, a few minutes)
#
# Output: darts_stores.json
#
# How:
#   1. fetch https://www.kaikatsu.jp/shop/data/shop.js
#   2. parse  var stores = { "<pref>": [ {store_code, store_name, darts:[...], ...} ] }
#   3. keep stores whose `darts` array is non-empty (has a darts booth; verified 1:1
#      with the empty_seat API category_id "10")
#   4. with -WithUnits, read the darts board count from the store detail page: the
#      darts <h2> heading is immediately followed by a "(count: N units)" paragraph.
#
# Pure ASCII on purpose: Windows PowerShell 5.1 loads .ps1 as ANSI and mangles UTF-8
# source. The few Japanese chars needed for matching are built from code points below.

param(
  [switch]$WithUnits,
  [int]$DelayMs = 800
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$OutFile = Join-Path $PSScriptRoot 'darts_stores.json'
$ua = @{ 'User-Agent' = 'Mozilla/5.0' }

# Build the Japanese regex fragments from code points (keeps this file ASCII):
#   U+30C0 U+30FC U+30C4 = "darts"            U+FF08 / U+FF09 = fullwidth ( )
#   U+53F0 = board/unit   U+6570 = count      U+FF1A = fullwidth colon
#   U+203B = note mark that precedes free-text directions inside addresses
$dartsWord = [string]([char]0x30C0 + [char]0x30FC + [char]0x30C4)
$parenOpen = [char]0xFF08
$paren2    = [string]([char]0x53F0 + [char]0x6570 + [char]0xFF1A)   # "count:"
$unitClose = [string]([char]0x53F0 + [char]0xFF09)                  # "units)"
$reUnits = '<h2>\s*' + $dartsWord + '\s*</h2>\s*<p[^>]*>' + $parenOpen + $paren2 + '(\d+)' + $unitClose
$reKome  = [string]([char]0x203B) + '.*$'
$reBr    = '<br\s*/?>'

Write-Host 'Fetching shop.js ...'
$res = Invoke-WebRequest -Uri 'https://www.kaikatsu.jp/shop/data/shop.js' -UseBasicParsing -Headers $ua
$text = [System.Text.Encoding]::UTF8.GetString($res.RawContentStream.ToArray())

# --- pull out the `stores` object literal by brace matching ---
$start = $text.IndexOf('var stores')
if ($start -lt 0) { throw 'could not find "var stores" in shop.js' }
$braceStart = $text.IndexOf('{', $start)
$depth = 0; $end = -1
for ($i = $braceStart; $i -lt $text.Length; $i++) {
  $c = $text[$i]
  if ($c -eq '{') { $depth++ }
  elseif ($c -eq '}') { $depth--; if ($depth -eq 0) { $end = $i; break } }
}
if ($end -lt 0) { throw 'unbalanced braces while scanning stores object' }
$block = $text.Substring($braceStart, $end - $braceStart + 1)

# JS -> JSON: drop trailing commas before } or ]
$json = [regex]::Replace($block, ',(\s*[}\]])', '$1')
$stores = $json | ConvertFrom-Json

$out = New-Object System.Collections.Generic.List[object]
foreach ($pref in $stores.PSObject.Properties) {
  foreach ($s in $pref.Value) {
    if (-not $s.darts -or $s.darts.Count -eq 0) { continue }

    $units = $null
    if ($WithUnits) {
      try {
        $d = Invoke-WebRequest -Uri "https://www.kaikatsu.jp/shop/detail/$($s.store_code).html" -UseBasicParsing -Headers $ua
        $html = [System.Text.Encoding]::UTF8.GetString($d.RawContentStream.ToArray())
        $m = [regex]::Match($html, $reUnits)
        if ($m.Success) { $units = [int]$m.Groups[1].Value }
      }
      catch { Write-Warning "detail fetch failed: $($s.store_code)" }
      Start-Sleep -Milliseconds $DelayMs
    }

    $addr = [string]$s.address
    $addr = [regex]::Replace($addr, $reBr, ' ')
    $addr = [regex]::Replace($addr, $reKome, '')
    $addr = $addr.Trim()

    $out.Add([ordered]@{
        store_code  = [string]$s.store_code
        store_name  = [string]$s.store_name
        pref        = [string]$pref.Name
        city        = [string]$s.cf_store_city
        address     = $addr
        tel         = [string]$s.tel
        darts       = @($s.darts)
        darts_units = $units
        vacancy_url = "https://www.kaikatsu.jp/shop/detail/vacancy.html?store_code=$($s.store_code)"
      })
  }
}

$sorted = $out | Sort-Object { $_.store_code }
$srcNote = 'https://www.kaikatsu.jp/shop/data/shop.js (darts field)'
if ($WithUnits) { $srcNote += ' + detail pages (board count)' }
$payload = [ordered]@{
  generated_at = (Get-Date).ToString('o')
  source       = $srcNote
  count        = $sorted.Count
  stores       = $sorted
}
$payload | ConvertTo-Json -Depth 6 | Set-Content -Path $OutFile -Encoding utf8
Write-Host "Wrote $($sorted.Count) darts stores -> $OutFile"
