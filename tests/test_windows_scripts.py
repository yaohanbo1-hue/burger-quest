"""Windows PowerShell 脚本不变量测试（2026-10-16 新增，跨平台可执行）。

不断言脚本「跑起来」（那需要 Windows runner，由 CI 的 windows-latest 负责），
只验证跨平台可验的硬性不变量：
- UTF-8 BOM（PowerShell 5.1 无 BOM 会把中文按 ANSI 解析成乱码/语法错误）；
- 关键安全串存在（Token 不回显/不写日志、.env 明文风险披露、缺 Token 不回退 Demo）；
- 幂等标记（复用 .venv、不覆盖 .env）；
- 无硬编码 Token 形态；
- 控制台编码头在位。
"""
import re
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
SETUP = SCRIPTS_DIR / "setup-windows.ps1"
RUN = SCRIPTS_DIR / "run-windows.ps1"
CHECK = SCRIPTS_DIR / "check-mcp-windows.ps1"

ALL_SCRIPTS = [SETUP, RUN, CHECK]


@pytest.mark.parametrize("script", ALL_SCRIPTS, ids=lambda p: p.name)
def test_script_has_utf8_bom(script):
    """无 BOM 的 UTF-8 中文脚本在 PowerShell 5.1 下必出语法错误（实测踩坑）。"""
    raw = script.read_bytes()
    assert raw[:3] == b"\xef\xbb\xbf", f"{script.name} 缺 UTF-8 BOM"
    raw.decode("utf-8")  # 必须是合法 UTF-8


@pytest.mark.parametrize("script", ALL_SCRIPTS, ids=lambda p: p.name)
def test_script_has_console_encoding_header(script):
    text = script.read_bytes().decode("utf-8-sig")
    assert "PYTHONIOENCODING" in text
    assert "[Console]::OutputEncoding" in text


@pytest.mark.parametrize("script", ALL_SCRIPTS, ids=lambda p: p.name)
def test_script_never_echoes_token(script):
    text = script.read_bytes().decode("utf-8-sig")
    for pattern in ("Write-Host $plain", "Write-Output $plain", "Write-Host $token", "Write-Output $token", "echo $plain"):
        assert pattern not in text, f"{script.name} 出现 Token 输出模式：{pattern}"


def test_setup_security_invariants():
    text = SETUP.read_bytes().decode("utf-8-sig")
    assert "明文" in text  # .env 明文风险披露
    assert "Remove-Item .env" in text  # 清理方式
    assert "不回显" in text  # SecureString 输入
    assert "不覆盖" in text  # 已有 .env 不覆盖
    assert "不会偷偷使用演示数据" in text or "不会自动切换" in text  # 缺 Token 不回退 Demo
    assert "复用已存在的 .venv" in text  # 幂等


def test_run_and_check_no_demo_fallback():
    for script in (RUN, CHECK):
        text = script.read_bytes().decode("utf-8-sig")
        assert "不会自动切换到演示数据" in text or "不会偷偷使用演示数据" in text, f"{script.name} 缺少不回退声明"


@pytest.mark.parametrize("script", ALL_SCRIPTS, ids=lambda p: p.name)
def test_script_has_no_hardcoded_token(script):
    text = script.read_bytes().decode("utf-8-sig")
    # 形如真实 Token 的长随机串（排除占位/示例）
    for m in re.findall(r"MCD_MCP_TOKEN\s*=\s*[\"']?([A-Za-z0-9_\-]{16,})", text):
        assert any(w in m.lower() for w in ("your", "example", "test", "fake")), f"{script.name} 疑似硬编码 Token"


def test_scripts_exist_for_documented_commands():
    """README 引用的三个脚本必须存在。"""
    for name in ("setup-windows.ps1", "run-windows.ps1", "check-mcp-windows.ps1"):
        assert (SCRIPTS_DIR / name).is_file(), f"缺少 scripts/{name}"


# ---------------------------------------------------------------- 2026-10-17 Windows 启动体验增强
def test_run_help_and_exit_code_legend():
    """run-windows.ps1：帮助命令、退出码图例、安全退出指引（要求 8）。"""
    text = RUN.read_bytes().decode("utf-8-sig")
    assert "$Command = 'help'" in text  # 命令可选，缺省显示帮助（要求 8：查看帮助）
    assert "-eq 'help'" in text
    assert "0=成功" in text and "2=配置或连接错误" in text  # 退出码图例
    assert "Ctrl+C" in text  # 安全退出指引


def test_run_never_requires_admin_or_security_software():
    """不得要求管理员权限或关闭安全软件（要求 7）。"""
    for script in ALL_SCRIPTS:
        text = script.read_bytes().decode("utf-8-sig")
        for forbidden in ("Start-Process -Verb RunAs", "-Verb RunAs", "Set-MpPreference", "关闭 360", "关闭360", "禁用 Windows Defender"):
            assert forbidden not in text, f"{script.name} 出现提权/安全软件操作：{forbidden}"


def test_run_token_missing_message_is_actionable():
    """缺 Token 提示必须可操作（给申请地址与替代路径），且不回退 Demo（要求 3/5）。"""
    text = RUN.read_bytes().decode("utf-8-sig")
    assert "open.mcd.cn/mcp" in text  # 申请地址
    assert "不会自动切换到演示数据" in text  # 不回退 Demo


def test_setup_reads_token_only_from_user_input_or_env():
    """Token 只能来自进程环境变量、用户输入或 .env；不得有脚本默认值（要求 4）。"""
    text = SETUP.read_bytes().decode("utf-8-sig")
    assert "AsSecureString" in text  # 不回显输入
    assert "Read-Host" in text
    # 不允许出现赋值默认 Token 的形态（$env:MCD_MCP_TOKEN = '<literal>'）
    assert "MCD_MCP_TOKEN = '" not in text
    assert 'MCD_MCP_TOKEN = "' not in text
