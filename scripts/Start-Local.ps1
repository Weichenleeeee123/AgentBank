param(
    [ValidateSet('eval', 'public')][string]$Mode = 'eval',
    [string]$Python = 'python',
    [string]$EnvFile = '.env',
    [int]$Port = 0
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
. "$PSScriptRoot/Import-LocalEnv.ps1"
Import-LocalEnv -Path $EnvFile
if ($Port -eq 0) { if ($Mode -eq 'public') { $Port = 8000 } else { $Port = 8001 } }
$data = Join-Path $root '.tmp/local-demo'
New-Item -ItemType Directory -Path $data -Force | Out-Null
# Persist random local credentials once; never print them or overwrite an existing file.
$credentials = Join-Path $data "$Mode.env"
if (-not (Test-Path -LiteralPath $credentials)) {
    $values = @("AGENTBANK_SESSION_SECRET=$(New-LocalSecret)", "AGENTBANK_AUDIT_SECRET=$(New-LocalSecret)")
    if ($Mode -eq 'eval') { $values += @("AGENTBANK_ADMIN_TOKEN=$(New-LocalSecret)", "AGENTBANK_STAFF_TOKEN=$(New-LocalSecret)") }
    [IO.File]::WriteAllLines($credentials, $values)
}
Import-LocalEnv -Path $credentials
$env:AGENTBANK_ENV = $Mode
$db = (Join-Path $data "$Mode.db").Replace('\', '/')
$env:AGENTBANK_DATABASE_URL = "sqlite:///$db"
if ($Mode -eq 'public') {
    $env:AGENTBANK_ADMIN_TOKEN = ''
    $env:AGENTBANK_STAFF_TOKEN = ''
}
Write-Host "Local $Mode demo: http://127.0.0.1:$Port/app/ (credentials stay in .tmp/local-demo/$Mode.env)"
& $Python -m uvicorn agentbank.api.run:app --host 127.0.0.1 --port $Port
if ($LASTEXITCODE -ne 0) { throw 'Local server exited unsuccessfully.' }
