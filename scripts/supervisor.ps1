# Mantém o servidor Nikki vivo: se o processo cair, reinicia com espera crescente.
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
$logDir = Join-Path $root "data\logs"
$runDir = Join-Path $root "data\run"
New-Item -ItemType Directory -Force $logDir, $runDir | Out-Null
$delay = 5
while ($true) {
    if (Test-Path (Join-Path $runDir "stop.flag")) { break }
    $started = Get-Date
    $p = Start-Process -FilePath $py -ArgumentList "-m", "nikki", "serve" -WorkingDirectory $root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logDir "server.out.log") -RedirectStandardError (Join-Path $logDir "server.err.log")
    Set-Content -Path (Join-Path $runDir "server.pid") -Value $p.Id -Encoding ascii
    $p.WaitForExit()
    if (Test-Path (Join-Path $runDir "stop.flag")) { break }
    Add-Content -Path (Join-Path $logDir "supervisor.log") -Value "$(Get-Date -Format s) servidor saiu (código $($p.ExitCode)); reiniciando em $delay s"
    if (((Get-Date) - $started).TotalMinutes -gt 10) { $delay = 5 } else { $delay = [Math]::Min($delay * 2, 300) }
    Start-Sleep -Seconds $delay
}
Remove-Item (Join-Path $runDir "supervisor.pid") -ErrorAction SilentlyContinue
