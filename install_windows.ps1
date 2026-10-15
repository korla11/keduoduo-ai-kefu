# 客多多AI客服系统 - Windows 安装脚本
# 双击 install_windows.bat 即可；也可: powershell -ExecutionPolicy Bypass -File install_windows.ps1 [-Dest 目录]
param(
  [string]$Dest = "$env:USERPROFILE\KeDuoDuo",
  [switch]$RegisterOnly
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$TaskName = 'KeDuoDuoGateway'

function Register-KddTask([string]$D) {
  $pyw = Join-Path $D 'venv\Scripts\pythonw.exe'
  $run = Join-Path $D 'kdd_run.py'
  $action  = New-ScheduledTaskAction -Execute $pyw -Argument "`"$run`"" -WorkingDirectory $D
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
     -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
     -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -Hidden
  $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
     -Principal $principal -Description '客多多AI客服系统（登录时后台启动）' -Force | Out-Null
  Write-Host "==> 已注册任务计划: $TaskName（登录时启动，失败每分钟重启）"
}

if ($RegisterOnly) { Register-KddTask $Dest; exit 0 }

$Src = Split-Path -Parent $MyInvocation.MyCommand.Path
function Find-Python {
  foreach ($c in @(@('py','-3.12'), @('py','-3.13'), @('py','-3.11'), @('py','-3.10'), @('python'), @('python3'))) {
    try {
      $exe = $c[0]; $args2 = @(); if ($c.Count -gt 1) { $args2 = $c[1..($c.Count-1)] }
      $v = & $exe @args2 -c "import sys;print(1 if sys.version_info>=(3,10) else 0);print(sys.executable)" 2>$null
      if ($LASTEXITCODE -eq 0 -and $v -and $v[0] -eq '1' -and $v[1] -notmatch 'WindowsApps') { return $v[1] }
    } catch {}
  }
  return $null
}
$py = Find-Python
if (-not $py) {
  Write-Host '==> 未找到 Python 3.10+，尝试用 winget 安装 Python 3.12 ...'
  if (Get-Command winget -ErrorAction SilentlyContinue) {
    winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
    $env:Path = [Environment]::GetEnvironmentVariable('Path','User') + ';' + [Environment]::GetEnvironmentVariable('Path','Machine')
    $py = Find-Python
  }
  if (-not $py) {
    Write-Host '!! 请先从 https://www.python.org/downloads/ 安装 Python 3.12（勾选 "Add python.exe to PATH"），然后重新双击 install_windows.bat'
    Read-Host '按回车退出'; exit 1
  }
}
Write-Host "==> 使用 Python: $py"
& $py (Join-Path $Src 'install.py') install --dest $Dest
if ($LASTEXITCODE -ne 0) { Write-Host '!! 安装失败，请把上面的报错截图发给我们'; Read-Host '按回车退出'; exit 1 }
Read-Host '安装完成，按回车关闭此窗口'
