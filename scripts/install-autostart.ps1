# Opcional: inicia a Nikki automaticamente ao fazer login no Windows (atalho na pasta Inicializar do usuário).
$startup = [Environment]::GetFolderPath("Startup")
$lnk = Join-Path $startup "Nikki Automation.lnk"
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut($lnk)
$s.TargetPath = "powershell.exe"
$s.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$(Join-Path $PSScriptRoot 'start.ps1')`""
$s.WorkingDirectory = Split-Path -Parent $PSScriptRoot
$s.WindowStyle = 7
$s.Save()
Write-Host "Autostart instalado: $lnk (remova o atalho para desfazer)"
