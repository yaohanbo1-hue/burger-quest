<#
.SYNOPSIS
    Burger Quest Windows 运行入口（幂等，透传参数给 burger-quest CLI）。
.DESCRIPTION
    自动使用仓库 .venv 的解释器（无需手动激活），把全部参数透传给
    `python -m burger_quest`。真实模式未配置 Token 时给出明确提示，
    **不会**自动切换到 demo。

    不带参数运行 = 显示本帮助（查看帮助）。
    退出码透传自 CLI：0=成功；1=未生成方案（含等待选择门店）；2=配置或连接错误。
    随时可按 Ctrl+C 安全退出（脚本无后台任务，终止即干净退出）。

.EXAMPLE
    powershell -File scripts\run-windows.ps1
    # 不带参数：显示帮助
.EXAMPLE
    powershell -File scripts\run-windows.ps1 demo "我想吃辣，预算35"
.EXAMPLE
    powershell -File scripts\run-windows.ps1 quest "我想吃辣，预算35，在深圳市南山区科技园，到店自取"
.EXAMPLE
    powershell -File scripts\run-windows.ps1 quest "我想吃辣，预算35" --store-code 1420762
.EXAMPLE
    powershell -File scripts\run-windows.ps1 progress --progress .\burger-quest-progress.json
.EXAMPLE
    powershell -File scripts\run-windows.ps1 doctor
.EXAMPLE
    powershell -File scripts\run-windows.ps1 smoke --demo
.EXAMPLE
    powershell -File scripts\run-windows.ps1 help
#>
[CmdletBinding()]
param(
    # 子命令（可选；缺省/ help 显示帮助）
    [Parameter(Position = 0)]
    [string]$Command = 'help',
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args
)

$ErrorActionPreference = 'Stop'
# 控制台编码：子进程（Python）输出 UTF-8 中文，避免被系统 GBK 代码页显示为乱码
try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch {}
$env:PYTHONIOENCODING = 'utf-8'

function Show-Help {
    Write-Host ""
    Write-Host "=== Burger Quest Windows 运行入口 ===" -ForegroundColor Cyan
    Write-Host "用法： powershell -File scripts\run-windows.ps1 <命令> [参数...]"
    Write-Host ""
    Write-Host "常用命令："
    Write-Host "  demo ""我想吃辣，预算35""                       离线演示（无需 Token）"
    Write-Host "  quest ""我想吃辣，预算35，到店自取""              真实模式（需 Token）"
    Write-Host "  quest ""..."" --store-code 1420762              指定门店的真实模式"
    Write-Host "  progress --progress .\burger-quest-progress.json   查看本地虚拟进度/成就图鉴"
    Write-Host "  doctor                                       真实 MCP 连接诊断（只读）"
    Write-Host "  smoke --demo                                 离线只读冒烟"
    Write-Host "  stores ""南山科技园""                           查询附近门店与编码"
    Write-Host "  tools                                        查看引擎调用的官方工具清单"
    Write-Host ""
    Write-Host "退出码： 0=成功  1=未生成方案（含等待选择门店）  2=配置或连接错误"
    Write-Host "随时 Ctrl+C 安全退出；本脚本不保留任何后台任务。"
    Write-Host "Token 配置： 见 scripts\check-mcp-windows.ps1 的重跑提示，或手动设置环境变量。"
    Write-Host ""
}

if ($Command -eq 'help' -or $Command -eq '--help' -or $Command -eq '-h') {
    Show-Help
    exit 0
}

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$VenvPython = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $VenvPython)) {
    Write-Host "[X] 未找到 .venv。请先运行安装脚本：" -ForegroundColor Red
    Write-Host "    powershell -ExecutionPolicy Bypass -File scripts\setup-windows.ps1" -ForegroundColor Yellow
    exit 2
}

# 真实模式命令但未配置 Token：明确提示（不偷偷切 demo）
$needsToken = @('quest', 'smoke', 'stores', 'doctor') -contains $Command
$hasToken = $env:MCD_MCP_TOKEN
if (-not $hasToken) {
    $envFile = Join-Path $RepoRoot '.env'
    if ((Test-Path $envFile) -and ((Get-Content $envFile -Raw) -match 'MCD_MCP_TOKEN\s*=\s*\S')) {
        $hasToken = $true  # .env 由 Python 侧加载
    }
}
if ($needsToken -and -not $hasToken -and $Command -ne 'smoke') {
    Write-Host "[!] 未检测到 MCD_MCP_TOKEN（进程环境变量或 .env）。" -ForegroundColor Yellow
    Write-Host "    申请：https://open.mcd.cn/mcp ；配置：重跑 scripts\setup-windows.ps1" -ForegroundColor Yellow
    Write-Host "    真实模式不会自动切换到演示数据；离线体验请用：run-windows.ps1 demo ""我想吃辣，预算35""" -ForegroundColor Yellow
}

& $VenvPython -m burger_quest $Command @Args
$code = $LASTEXITCODE
if ($code -ne 0) {
    Write-Host ""
    Write-Host "[退出码 $code] 0=成功 1=未生成方案 2=配置或连接错误；按上方提示处理后重试。" -ForegroundColor DarkGray
}
exit $code
