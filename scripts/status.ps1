# Mostra o estado da automação Nikki.
$root = Split-Path -Parent $PSScriptRoot
$port = 8710
$envFile = Join-Path $root ".env"
if (Test-Path $envFile) { $m = Select-String -Path $envFile -Pattern '^NIKKI_PORT=(\d+)' ; if ($m) { $port = $m.Matches[0].Groups[1].Value } }
try {
    $r = Invoke-RestMethod "http://127.0.0.1:$port/api/status" -TimeoutSec 5
    $r | ConvertTo-Json -Depth 5
    Write-Host "`nPainel: http://127.0.0.1:$port/"
} catch {
    Write-Host "Nikki FORA DO AR na porta $port. Inicie com scripts\start.ps1"
    exit 1
}
