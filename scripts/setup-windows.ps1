<#
.SYNOPSIS
    Burger Quest Windows 一键安装（幂等，可重复执行）。
.DESCRIPTION
    步骤：检查 Python(>=3.10) -> 创建/复用 .venv -> 安装依赖 -> 引导配置
    MCD_MCP_TOKEN -> 验证 CLI 入口。

    安全约定：
    - 绝不覆盖已存在的 .env（存在时跳过写入并提示手动编辑）；
    - Token 只写入用户显式选择的 .env 或仅保留在当前进程，绝不进日志/输出；
    - 持久化到 .env 时明确披露风险（明文落盘）与清理方式（Remove-Item .env）。
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\setup-windows.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\setup-windows.ps1 -SkipDev
#>
[CmdletBinding()]
param(
    # 跳过测试依赖（只装运行时）
    [switch]$SkipDev,
    # 强制重建虚拟环境（默认复用已存在的 .venv）
    [switch]$RecreateVenv
)

$ErrorActionPreference = 'Stop'
# 控制台编码：子进程（Python）输出 UTF-8 中文，避免被系统 GBK 代码页显示为乱码
try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch {}
$env:PYTHONIOENCODING = 'utf-8'

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

function Write-Step([string]$msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok([string]$msg) { Write-Host "    [OK] $msg" -ForegroundColor Green }
function Write-Warn([string]$msg) { Write-Host "    [!] $msg" -ForegroundColor Yellow }
function Write-Err([string]$msg) { Write-Host "    [X] $msg" -ForegroundColor Red }

# ---------------------------------------------------------------- Python 检查
Write-Step "检查 Python（要求 >= 3.10，Windows 推荐 3.11/3.12）"

$pyCmd = $null
foreach ($candidate in @('python', 'python3', 'py')) {
    try {
        $v = & $candidate --version 2>&1
        if ($LASTEXITCODE -eq 0 -and $v -match 'Python (\d+)\.(\d+)') {
            $major = [int]$Matches[1]; $minor = [int]$Matches[2]
            if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 10)) {
                $pyCmd = $candidate
                Write-Ok "找到 $v（$candidate）"
                break
            }
            Write-Warn "$candidate 是 $v，低于 3.10，继续寻找其它解释器"
        }
    } catch { continue }
}
if (-not $pyCmd) {
    Write-Err "未找到 Python >= 3.10。请从 https://www.python.org/downloads/ 或 Microsoft Store 安装（勾选 Add to PATH）。"
    exit 2
}

# ---------------------------------------------------------------- 虚拟环境
$VenvDir = Join-Path $RepoRoot '.venv'
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
if ($RecreateVenv -and (Test-Path $VenvDir)) {
    Write-Step "重建虚拟环境（删除旧 .venv）"
    Remove-Item -Recurse -Force $VenvDir
}
if (-not (Test-Path $VenvPython)) {
    Write-Step "创建虚拟环境 .venv"
    & $pyCmd -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { Write-Err "venv 创建失败"; exit 2 }
    Write-Ok ".venv 已创建"
} else {
    Write-Ok "复用已存在的 .venv（幂等：不重建；如需重建加 -RecreateVenv）"
}

# ---------------------------------------------------------------- 依赖安装
Write-Step "安装依赖（单一事实来源 pyproject.toml）"
if ($SkipDev) {
    & $VenvPython -m pip install -q --upgrade pip
    & $VenvPython -m pip install -q .
} else {
    & $VenvPython -m pip install -q --upgrade pip
    & $VenvPython -m pip install -q -e '.[dev]'
}
if ($LASTEXITCODE -ne 0) { Write-Err "依赖安装失败（pip 退出码 $LASTEXITCODE）"; exit 2 }
Write-Ok "依赖安装完成"

# ---------------------------------------------------------------- Token 配置
Write-Step "配置 MCD_MCP_TOKEN（真实模式必需）"
$EnvFile = Join-Path $RepoRoot '.env'

if ($env:MCD_MCP_TOKEN) {
    Write-Ok "当前 PowerShell 进程已设置 MCD_MCP_TOKEN（仅本进程有效，值不显示）"
} elseif ((Test-Path $EnvFile) -and ((Get-Content $EnvFile -Raw) -match 'MCD_MCP_TOKEN\s*=')) {
    Write-Ok ".env 中已存在 MCD_MCP_TOKEN（不覆盖；如需更新请手动编辑 .env）"
} else {
    Write-Host "    真实模式需要麦当劳 MCP Token：https://open.mcd.cn/mcp （手机号登录申请）" -ForegroundColor Gray
    Write-Host "    选择配置方式：" -ForegroundColor Gray
    Write-Host "      [1] 仅当前 PowerShell 进程（关闭窗口即失效，最安全）【默认】"
    Write-Host "      [2] 持久化到 .env（明文落盘；.env 已被 .gitignore 排除，不会进 Git）"
    Write-Host "      [3] 暂不配置（之后可随时重跑本脚本；demo 演示模式无需 Token）"
    $choice = Read-Host "    请输入 1 / 2 / 3（回车默认 1）"
    if ($choice -eq '3') {
        Write-Warn "已跳过 Token 配置。真实模式命令将明确报错，不会偷偷使用演示数据。"
    } else {
        $secure = Read-Host "    请粘贴 Token（输入不回显）" -AsSecureString
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringBOM(
            [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
        if ([string]::IsNullOrWhiteSpace($plain)) {
            Write-Warn "未输入 Token，跳过配置。"
        } elseif ($choice -eq '2') {
            # 不覆盖：仅当 .env 不存在或无该键时追加
            Add-Content -Path $EnvFile -Value "MCD_MCP_TOKEN=$plain" -Encoding utf8
            Write-Ok "已写入 .env（该文件在 .gitignore 中；清理方式：Remove-Item .env）"
            Write-Warn "安全提醒：.env 是明文文件，任何能读你磁盘的程序都能看到；共享屏幕/远程协助时请注意遮挡。"
            $env:MCD_MCP_TOKEN = $plain
        } else {
            $env:MCD_MCP_TOKEN = $plain
            Write-Ok "已设置到当前进程环境变量（关闭窗口即失效，最安全）"
        }
        $plain = $null
    }
}

# ---------------------------------------------------------------- 验证
Write-Step "验证安装"
& $VenvPython -m burger_quest --version
if ($LASTEXITCODE -ne 0) { Write-Err "CLI 入口验证失败"; exit 2 }
& $VenvPython -c "import burger_quest, pathlib; p = pathlib.Path(burger_quest.__file__).parent / 'data' / 'demo_data.json'; print('    [OK] demo data shipped with wheel:', p.exists())"
if ($LASTEXITCODE -ne 0) { Write-Err "包数据验证失败"; exit 2 }

Write-Host "`n安装完成。常用命令：" -ForegroundColor Cyan
Write-Host "  .\.venv\Scripts\Activate.ps1                     # 激活虚拟环境"
Write-Host "  powershell -File scripts\check-mcp-windows.ps1   # 真实连接诊断（需 Token）"
Write-Host "  burger-quest demo `"我想吃辣，预算35`"             # 离线演示（无需 Token）"
Write-Host "  burger-quest quest `"我想吃辣，预算35，到店自取`"   # 真实模式（需 Token）"
exit 0
