# Inicia a automação Nikki em segundo plano (supervisor + servidor + worker).
$root = Split-Path -Parent $PSScriptRoot
$runDir = Join-Path $root "data\run"
New-Item -ItemType Directory -Force $runDir | Out-Null
$supPid = Join-Path $runDir "supervisor.pid"
if (Test-Path $supPid) {
    $old = Get-Content $supPid -ErrorAction SilentlyContinue
    if ($old -and (Get-Process -Id $old -ErrorAction SilentlyContinue)) { Write-Host "Nikki já está rodando (supervisor PID $old)."; exit 0 }
}
Remove-Item (Join-Path $runDir "stop.flag") -ErrorAction SilentlyContinue
$sup = Start-Process -FilePath "powershell.exe" -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", ('"' + (Join-Path $PSScriptRoot 'supervisor.ps1') + '"') -WindowStyle Hidden -PassThru
Set-Content -Path $supPid -Value $sup.Id -Encoding ascii
$port = 8710
$envFile = Join-Path $root ".env"
if (Test-Path $envFile) { $m = Select-String -Path $envFile -Pattern '^NIKKI_PORT=(\d+)' ; if ($m) { $port = $m.Matches[0].Groups[1].Value } }
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    try { $r = Invoke-RestMethod "http://127.0.0.1:$port/api/status" -TimeoutSec 2; Write-Host "Nikki no ar: http://127.0.0.1:$port (publisher=$($r.publisher), worker=$($r.worker_alive))"; exit 0 } catch {}
}
Write-Host "Supervisor iniciado (PID $($sup.Id)), mas o servidor ainda não respondeu. Veja data\logs\server.err.log"
exit 1
