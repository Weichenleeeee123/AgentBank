param([string]$UserId = 'user_001', [string]$BaseUrl = 'http://127.0.0.1:8001', [string]$EnvFile = '')
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/Import-LocalEnv.ps1"
if (-not $EnvFile) { $EnvFile = Join-Path (Split-Path -Parent $PSScriptRoot) '.tmp/local-demo/eval.env' }
Import-LocalEnv -Path $EnvFile
if (-not $env:AGENTBANK_ADMIN_TOKEN) { throw 'An eval admin credential is required.' }
$uri = [Uri]$BaseUrl
if ($uri.Host -notin @('localhost', '127.0.0.1', '::1')) { throw 'This helper only sends credentials to localhost.' }
$result = Invoke-RestMethod -Uri "$($BaseUrl.TrimEnd('/'))/admin/v1/devices/pairing-code" -Method Post -Headers @{'X-Admin-Token'=$env:AGENTBANK_ADMIN_TOKEN} -ContentType 'application/json' -Body (@{user_id=$UserId} | ConvertTo-Json)
Write-Host "One-time pairing code: $($result.pairing_code)"
Write-Host "Enter it at $($BaseUrl.TrimEnd('/'))/app/device.html"
