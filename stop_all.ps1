# 停止本地开发环境的三个服务（按端口找进程，不误伤其他应用）。
# 用法：powershell -ExecutionPolicy Bypass -File .\stop_all.ps1
foreach ($port in 8000, 8100, 5173) {
  $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
  foreach ($c in $conns) {
    Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
    Write-Host "端口 $port：进程 $($c.OwningProcess) 已停止"
  }
  if (-not $conns) { Write-Host "端口 $port：无监听进程" }
}
