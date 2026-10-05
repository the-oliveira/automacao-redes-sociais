# Para a automação Nikki (supervisor e servidor).
$root = Split-Path -Parent $PSScriptRoot
$runDir = Join-Path $root "data\run"
New-Item -ItemType Directory -Force $runDir | Out-Null
Set-Content -Path (Join-Path $runDir "stop.flag") -Value "stop" -Encoding ascii
foreach ($f in "supervisor.pid", "server.pid") {
    $pidFile = Join-Path $runDir $f
    if (Test-Path $pidFile) {
        $procId = Get-Content $pidFile -ErrorAction SilentlyContinue
        if ($procId) { Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue }
        Remove-Item $pidFile -ErrorAction SilentlyContinue
    }
}
Write-Host "Nikki parada."
