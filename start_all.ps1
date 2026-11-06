# 一键启动本地开发环境：平台后端(8000) + 前端(5173)；
# qa-agent(8100) 已改为随后端自动拉起（config.yaml agent.auto_start），不再单独开窗口。
# 重复执行安全（先清旧进程再拉起）。
# 用法：powershell -ExecutionPolicy Bypass -File .\start_all.ps1
$ErrorActionPreference = 'Stop'
$ROOT = $PSScriptRoot

function Stop-Port($port) {
  $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
  foreach ($c in $conns) { Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue }
  if ($conns) { Write-Host "  端口 $port 旧进程已清理（$($conns.OwningProcess | Select-Object -Unique)）" }
}

function Start-Svc($title, $workdir, $cmd) {
  Start-Process powershell -ArgumentList '-NoExit', '-ExecutionPolicy', 'Bypass', '-Command',
    "Set-Location '$workdir'; `$host.UI.RawUI.WindowTitle='$title'; $cmd"
}

Write-Host '[1/3] 清理旧监听端口…'
Stop-Port 8000; Stop-Port 8100; Stop-Port 5173
Start-Sleep -Seconds 1

Write-Host '[2/3] 启动平台后端 :8000（将自动带起 qa-agent :8100）…'
Start-Svc 'ia-backend' $ROOT `
  "`$env:PYTHONPATH='$ROOT'; & '$ROOT\.venv\Scripts\python.exe' -m uvicorn backend.main:app --host 127.0.0.1 --port 8000"

Write-Host '[3/3] 启动前端 :5173 …'
Start-Svc 'ia-web' (Join-Path $ROOT 'platform-web') 'npm run dev'

# 健康探测：三个服务就绪后给出访问入口（最多等 30 秒）
function Wait-Http($url, $name) {
  for ($i = 0; $i -lt 30; $i++) {
    try { Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 2 | Out-Null; Write-Host "  $name 就绪" -ForegroundColor Green; return }
    catch { Start-Sleep -Seconds 1 }
  }
  Write-Host "  $name 未在 30 秒内就绪，请查看对应窗口日志" -ForegroundColor Yellow
}
Write-Host '等待服务就绪…'
Wait-Http 'http://127.0.0.1:8000/api/health' '后端'
Wait-Http 'http://127.0.0.1:8100/health'     'qa-agent（后端自动拉起）'
Wait-Http 'http://localhost:5173'             '前端'
Write-Host "`n打开 http://localhost:5173 即可使用（关闭服务请运行 stop_all.ps1）" -ForegroundColor Cyan
