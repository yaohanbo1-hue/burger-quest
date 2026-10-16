<#
.SYNOPSIS
    Burger Quest Windows 真实 MCP 连接检查（只读诊断）。
.DESCRIPTION
    运行 `burger-quest doctor`：Python -> 依赖 -> Token -> 网络 -> MCP 初始化
    -> 工具发现 -> 只读调用，逐步 PASS/FAIL + 下一步建议。
    退出码透传：0=全部通过；1=有失败；2=配置错误（缺 Token/缺 SDK）。

    本脚本不打印、不写入任何 Token 值。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\check-mcp-windows.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
# 控制台编码：子进程（Python）输出 UTF-8 中文，避免被系统 GBK 代码页显示为乱码
try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch {}
$env:PYTHONIOENCODING = 'utf-8'

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$VenvPython = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $VenvPython)) {
    Write-Host "[X] 未找到 .venv。请先运行安装脚本：" -ForegroundColor Red
    Write-Host "    powershell -ExecutionPolicy Bypass -File scripts\setup-windows.ps1" -ForegroundColor Yellow
    exit 2
}

if (-not $env:MCD_MCP_TOKEN) {
    $envFile = Join-Path $RepoRoot '.env'
    if (-not ((Test-Path $envFile) -and ((Get-Content $envFile -Raw) -match 'MCD_MCP_TOKEN\s*=\s*\S'))) {
        Write-Host "[!] 未检测到 MCD_MCP_TOKEN（进程环境变量或 .env）。" -ForegroundColor Yellow
        Write-Host "    申请：https://open.mcd.cn/mcp ；配置：重跑 scripts\setup-windows.ps1" -ForegroundColor Yellow
    }
}

& $VenvPython -m burger_quest doctor
$code = $LASTEXITCODE
if ($code -eq 0) {
    Write-Host "`n[OK] 真实 MCP 连接就绪，可以运行 quest 命令了。" -ForegroundColor Green
} elseif ($code -eq 2) {
    Write-Host "`n[X] 配置类失败：按上方「下一步」建议修复后重试（不会偷偷使用演示数据）。" -ForegroundColor Red
} else {
    Write-Host "`n[X] 连接失败：按上方「下一步」建议逐项排查。" -ForegroundColor Red
}
exit $code
