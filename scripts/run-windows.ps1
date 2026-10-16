<#
.SYNOPSIS
    Burger Quest Windows 运行入口（幂等，透传参数给 burger-quest CLI）。
.DESCRIPTION
    自动使用仓库 .venv 的解释器（无需手动激活），把全部参数透传给
    `python -m burger_quest`。真实模式未配置 Token 时给出明确提示，
    **不会**自动切换到 demo。

.EXAMPLE
    powershell -File scripts\run-windows.ps1 demo "我想吃辣，预算35"
.EXAMPLE
    powershell -File scripts\run-windows.ps1 quest "我想吃辣，预算35，在深圳市南山区科技园，到店自取"
.EXAMPLE
    powershell -File scripts\run-windows.ps1 doctor
.EXAMPLE
    powershell -File scripts\run-windows.ps1 smoke --demo
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Command,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args
)

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
    Write-Host "    真实模式不会自动切换到演示数据；离线体验请用：run-windows.ps1 demo `"我想吃辣，预算35`"" -ForegroundColor Yellow
}

& $VenvPython -m burger_quest $Command @Args
exit $LASTEXITCODE
