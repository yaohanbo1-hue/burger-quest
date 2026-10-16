"""真实 MCP 连接诊断（doctor，2026-10-16 Windows 支持新增）。

与 smoke 的分工：
- smoke：MCP 链路本身的只读冒烟（连接→工具→时间→门店→菜单→详情→算价）；
- doctor：**连通性前置诊断**——从 Python 环境到真实只读调用逐层排查，
  每步 PASS/FAIL + 简洁原因 + 下一步建议，失败分类明确
  （TOKEN_MISSING / 401 / 403 / 429 / NETWORK / SDK / TOOL_MISSING / BUSINESS）。

安全约定：
- Token 只查「存在性」，绝不打印值、长度、前缀；
- 不输出 Authorization 头、完整地址、手机号；
- 只调用只读工具；缺 Token 直接失败（退出码 2），**绝不偷偷切换 Demo**。
"""
from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

from .mcp_base import MCPClient, MCPError, TOOLS_USED
from .mcp_http import MCD_MCP_URL

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2

REQUIRED_PYTHON = (3, 10)


class DoctorReport:
    """分步诊断报告：PASS/FAIL + 原因 + 下一步建议（绝不在中途静默退出）。"""

    def __init__(self) -> None:
        self.steps: list[dict] = []

    def add(self, name: str, ok: bool, detail: str = "", hint: str = "") -> None:
        self.steps.append({"step": name, "ok": bool(ok), "detail": detail, "hint": hint})
        mark = "✅ PASS" if ok else "❌ FAIL"
        print(f"  {mark}  {name}" + (f"  —— {detail}" if detail else ""))
        if not ok and hint:
            print(f"         💡 下一步：{hint}")

    @property
    def passed(self) -> bool:
        return all(s["ok"] for s in self.steps)

    @property
    def failed_count(self) -> int:
        return sum(1 for s in self.steps if not s["ok"])

    @property
    def exit_code(self) -> int:
        if self.passed:
            return EXIT_OK
        # 配置类失败（缺 Token/缺 SDK/Python 版本不符）用 2，其余用 1
        config_fail = any(
            (not s["ok"]) and any(k in (s["step"] + s["detail"]) for k in ("MCD_MCP_TOKEN", "mcp SDK", "Python"))
            for s in self.steps
        )
        return EXIT_CONFIG if config_fail else EXIT_FAILED

    def to_dict(self) -> dict:
        return {"passed": self.passed, "failed": self.failed_count, "steps": self.steps}


def _check_python(report: DoctorReport) -> bool:
    v = sys.version_info
    ok = (v.major, v.minor) >= REQUIRED_PYTHON
    report.add(
        "Python 版本",
        ok,
        f"{v.major}.{v.minor}.{v.micro}（要求 >= {REQUIRED_PYTHON[0]}.{REQUIRED_PYTHON[1]}）",
        "" if ok else f"请安装 Python {REQUIRED_PYTHON[0]}.{REQUIRED_PYTHON[1]}+（Windows 推荐 3.11/3.12，官网或 Microsoft Store）",
    )
    return ok


def _check_sdk(report: DoctorReport) -> bool:
    try:
        import mcp  # noqa: F401

        try:
            from importlib.metadata import version as _pkg_version

            version = _pkg_version("mcp")
        except Exception:
            version = "已安装"
        report.add("依赖：官方 mcp SDK", True, f"已安装（{version}）")
        return True
    except ImportError:
        report.add(
            "依赖：官方 mcp SDK",
            False,
            "未安装（真实模式需要）",
            "在虚拟环境中执行：pip install mcp（或 pip install -e \".[dev]\" 安装全部依赖）",
        )
        return False


def _check_token(report: DoctorReport) -> bool:
    # .env 只读补缺（不覆盖进程环境变量）；只报告存在性
    from .dotenv import load_dotenv

    applied = load_dotenv()
    token = os.environ.get("MCD_MCP_TOKEN")
    if token:
        source = "进程环境变量"
        if "MCD_MCP_TOKEN" in applied:
            source = ".env 文件"
        report.add("Token：MCD_MCP_TOKEN", True, f"已配置（来源：{source}；值不显示）")
        return True
    report.add(
        "Token：MCD_MCP_TOKEN",
        False,
        "未配置",
        "到 https://open.mcd.cn/mcp 手机号登录申请；PowerShell 当前会话：$env:MCD_MCP_TOKEN=\"你的Token\"；"
        "或写入仓库根 .env（已被 .gitignore 排除）。切勿把 Token 写进源码或提交 Git",
    )
    return False


def _check_network(report: DoctorReport) -> bool:
    host = MCD_MCP_URL.replace("https://", "").replace("http://", "").split("/")[0]
    try:
        with socket.create_connection((host, 443), timeout=5):
            report.add("网络：MCP 服务端口", True, f"{host}:443 可连接")
            return True
    except OSError as e:
        report.add(
            "网络：MCP 服务端口",
            False,
            f"{host}:443 不可达（{type(e).__name__}）",
            "检查网络/代理/VPN；公司网络可能拦截，请更换网络后重试",
        )
        return False


def _classify_error(e: Exception) -> str:
    """把异常归类为明确类别（供报告与下一步建议）。"""
    msg = str(e)
    low = msg.lower()
    if isinstance(e, MCPError):
        if getattr(e, "code", None) == 401 or "401" in msg:
            return "401"
        if "403" in msg or "无权限" in msg:
            return "403"
        if "429" in msg or "限流" in msg:
            return "429"
        if "未检测到 MCD_MCP_TOKEN" in msg:
            return "TOKEN_MISSING"
        if "isError" in msg or "unknown tool" in low or "协议" in msg:
            return "SDK"
        if "不存在" in msg or "业务错误" in msg or "已关闭" in msg:
            return "BUSINESS"
        if any(h in low for h in ("connect", "ssl", "timed out", "unreachable", "reset", "closed", "连接", "网络", "不可达", "超时")):
            return "NETWORK"
        return "SDK"
    if any(h in low for h in ("timed out", "timeout", "连接", "网络", "不可达", "超时")):
        return "NETWORK"
    return "SDK"


def _hint_for(category: str) -> str:
    return {
        "401": "Token 无效或已过期：到 https://open.mcd.cn/mcp 重新申请并更新环境变量/.env",
        "403": "无权限访问该资源：确认 Token 对应的账户权限，或更换网络环境",
        "429": "触发限流（600 次/分钟）：稍等一分钟后重试；脚本化调用请降低频率",
        "NETWORK": "网络不可达：检查连接/代理/VPN 后重试",
        "SDK": "SDK/协议层错误：执行 pip install -U mcp 升级官方 SDK 后重试；仍失败请附诊断输出反馈",
        "TOKEN_MISSING": "配置 MCD_MCP_TOKEN（环境变量或 .env），详见上方 Token 步骤建议",
        "TOOL_MISSING": "官方工具清单缺少项目依赖的工具：可能是服务端变更，请到仓库 Issue 反馈",
        "BUSINESS": "官方返回业务错误：按消息提示调整（如更换门店/位置），或稍后重试",
    }.get(category, "查看上方原因，修复后重试；仍失败请带诊断输出到仓库 Issue 反馈")


def _check_mcp_live(report: DoctorReport) -> None:
    """连接 + 工具发现 + 一次只读调用（now-time-info）。任何失败都分类明示。"""
    from .mcp_http import HttpMCPClient

    client = None
    try:
        client = HttpMCPClient()
        client.connect()
        report.add("MCP：初始化会话", True, "Streamable HTTP 会话已建立")
    except MCPError as e:
        cat = _classify_error(e)
        report.add("MCP：初始化会话", False, f"[{cat}] {e}", _hint_for(cat))
        return
    except Exception as e:  # SDK 缺失等
        cat = _classify_error(e)
        report.add("MCP：初始化会话", False, f"[{cat}] {e}", _hint_for(cat))
        return

    try:
        tools = client.list_tools()
        names = {t.get("name") for t in tools}
        missing = [t for t in TOOLS_USED if t not in names]
        if missing:
            report.add(
                "MCP：工具发现",
                False,
                f"[TOOL_MISSING] 官方清单缺 {len(missing)} 个依赖工具：{', '.join(missing)}",
                _hint_for("TOOL_MISSING"),
            )
        else:
            report.add("MCP：工具发现", True, f"官方 {len(names)} 个工具，项目依赖的 {len(TOOLS_USED)} 个全部在位")
    except Exception as e:
        cat = _classify_error(e)
        report.add("MCP：工具发现", False, f"[{cat}] {e}", _hint_for(cat))
        return
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    # 只读调用（用独立会话，避免上面的 finally 已关闭）
    try:
        with HttpMCPClient() as c2:
            t = c2.now_time()
            when = (t.get("datetime") or "").strip()
            report.add("MCP：只读调用 now-time-info", True, f"服务器时间 {when}" if when else "已返回时间信息")
    except Exception as e:
        cat = _classify_error(e)
        report.add("MCP：只读调用 now-time-info", False, f"[{cat}] {e}", _hint_for(cat))


def run_doctor() -> DoctorReport:
    """完整诊断：Python -> 依赖 -> Token -> 网络 -> MCP 初始化 -> 工具发现 -> 只读调用。

    任一步失败即停止后续依赖步骤（缺 Token 就不连网），但已完成的步骤全部报告。
    """
    report = DoctorReport()
    print("🩺 Burger Quest 真实 MCP 连接诊断（只读，不调用任何写操作）")
    print(f"   目标服务：{MCD_MCP_URL}")
    if not _check_python(report):
        return report
    if not _check_sdk(report):
        return report
    if not _check_token(report):
        return report
    if not _check_network(report):
        return report
    _check_mcp_live(report)
    return report


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="burger-quest-doctor",
        description="真实 MCP 连接诊断：Python/依赖/Token/网络/初始化/工具发现/只读调用，逐步 PASS/FAIL + 下一步建议",
    )
    parser.add_argument("--json", default=None, help="诊断结果 JSON 输出路径（不含任何敏感值）")
    args = parser.parse_args(argv)

    report = run_doctor()
    if report.passed:
        print("\n🎉 诊断全部通过：真实 MCP 连接就绪。可运行：burger-quest quest \"你的需求\"")
    else:
        print(f"\n💥 诊断未通过：{report.failed_count} 项失败。请按上方「下一步」建议逐项修复后重试。")
    if args.json:
        import json

        Path(args.json).write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"📄 诊断结果已保存：{args.json}")
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
