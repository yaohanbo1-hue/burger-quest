"""真实 MCP 连接诊断（doctor，2026-10-16 Windows 支持新增）。

与 smoke 的分工：
- smoke：MCP 链路本身的只读冒烟（连接→工具→时间→门店→菜单→详情→算价）；
- doctor：**连通性前置诊断**——从 Python 环境到真实只读调用逐层排查，
  每步 PASS/FAIL + 简洁原因 + 下一步建议，失败分类明确
  （TOKEN_MISSING / 401 / 403 / 429 / NETWORK / SDK / TOOL_MISSING / BUSINESS /
  TIMEOUT_CONNECT / TIMEOUT_READ / RESPONSE）。

五层验证分级（2026-10-17 全链路体检新增，对应「分别输出状态」要求）：
- L1 服务可访问：TCP + TLS + HTTP 层响应（**无需 Token**，未认证探测即可验证）；
- L2 认证成功：带 Token 完成 MCP 会话初始化；
- L3 工具发现成功：list_tools 拿到官方工具清单且项目依赖工具在位；
- L4 单工具真实调用成功：now-time-info 只读调用返回；
- L5 业务流程真实跑通：本命令不覆盖，由 burger-quest smoke / quest 验证（需合法 Token）。
任一步失败即停止后续依赖步骤，但已完成的步骤全部报告；无 Token 时 L1 依然
给出证据，L2-L4 如实标记未执行——**绝不伪造 PASS、绝不偷偷切换 Demo**。

安全约定：
- Token 只查「存在性」，绝不打印值、长度、前缀；
- 不输出 Authorization 头、完整地址、手机号；
- HTTP 端点探针**绝不携带 Token**（未认证请求，只验证服务在 HTTP 层活着）；
- 只调用只读工具；缺 Token 直接失败（退出码 2），绝不偷偷切换 Demo。
"""
from __future__ import annotations

import os
import socket
import sys
from contextlib import suppress
from pathlib import Path

from .mcp_base import MCPClient, MCPError, TOOLS_USED
from .mcp_http import MCD_MCP_URL

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2

REQUIRED_PYTHON = (3, 10)
ENDPOINT_PROBE_TIMEOUT = 8.0  # HTTP 级探针超时（秒）

# 五层验证：级别 -> 名称（ doctor 覆盖 L1-L4，L5 由 smoke/quest 覆盖）
VERIFY_LEVELS = [
    ("L1", "服务可访问"),
    ("L2", "认证成功（会话初始化）"),
    ("L3", "工具发现成功"),
    ("L4", "单工具真实调用成功"),
    ("L5", "业务流程真实跑通"),
]


class DoctorReport:
    """分步诊断报告：PASS/FAIL + 原因 + 下一步建议（绝不在中途静默退出）。"""

    def __init__(self) -> None:
        self.steps: list[dict] = []

    def add(self, name: str, ok: bool, detail: str = "", hint: str = "", level: str = "") -> None:
        self.steps.append({"step": name, "ok": bool(ok), "detail": detail, "hint": hint, "level": level})
        mark = "✅ PASS" if ok else "❌ FAIL"
        prefix = f"[{level}] " if level else ""
        print(f"  {mark}  {prefix}{name}" + (f"  —— {detail}" if detail else ""))
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


def _probe_endpoint() -> tuple[bool, str, str]:
    """HTTP 级端点探针（2026-10-17 体检新增）。

    **绝不携带 Token/Authorization**：未认证 POST，只验证「服务在 HTTP 层活着」。
    实测（2026-10-17）：未认证 POST https://mcp.mcd.cn -> HTTP 403，TLS 1.4s 完成。
    因此 401/403 响应 = 服务可达 + 认证层正常，是 L1 的 PASS 证据而非失败。

    返回 (ok, detail, hint)。
    """
    import ssl
    import urllib.error
    import urllib.request

    if not MCD_MCP_URL.lower().startswith("https://"):
        # 端点探针只允许 https（MCD_MCP_URL 是模块常量，此处防御性校验 scheme）
        return False, f"端点 URL 非 https（{MCD_MCP_URL}），已拒绝探测", "请检查 MCD_MCP_URL 配置"
    request = urllib.request.Request(
        MCD_MCP_URL,
        data=b"{}",
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
    )
    try:
        # URL 为硬编码 https 常量且上方已校验 scheme，不打开本地文件/自定义协议
        with urllib.request.urlopen(request, timeout=ENDPOINT_PROBE_TIMEOUT) as response:  # nosec B310
            return True, f"HTTP {response.status}（未认证请求即被放行，服务正常）", ""
    except urllib.error.HTTPError as e:
        code = e.code
        if code in (400, 401, 403, 404, 405, 406, 415):
            # 服务可达且认证层按预期拒绝未带 Token 的请求
            return True, f"HTTP {code}（服务可达；未带 Token 的探测被预期响应，属正常）", ""
        if code >= 500:
            return (
                False,
                f"HTTP {code}（服务端异常）",
                "麦当劳 MCP 服务端暂时不可用：稍后重试，或经官方渠道确认服务状态",
            )
        return True, f"HTTP {code}（服务可达）", ""
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, ssl.SSLError):
            return (
                False,
                f"TLS 握手失败（{type(reason).__name__}）",
                "检查系统时间与证书链；代理/VPN 拦截 HTTPS 时也会导致此错误",
            )
        return (
            False,
            f"HTTP 请求失败（{type(reason).__name__}）",
            "检查网络/代理/VPN 能否访问 https://mcp.mcd.cn",
        )
    except (OSError, ValueError) as e:
        return False, f"HTTP 请求失败（{type(e).__name__}）", "检查网络/代理/VPN 能否访问 https://mcp.mcd.cn"


def _check_network(report: DoctorReport) -> bool:
    """L1 服务可访问：先 TCP 握手，再 HTTP 级未认证探测（两步都无需 Token）。"""
    host = MCD_MCP_URL.replace("https://", "").replace("http://", "").split("/")[0]
    try:
        with socket.create_connection((host, 443), timeout=5):
            pass
    except OSError as e:
        report.add(
            "网络：MCP 服务端点",
            False,
            f"{host}:443 TCP 不可达（{type(e).__name__}）",
            "检查网络/代理/VPN；公司网络可能拦截，请更换网络后重试",
            level="L1",
        )
        return False
    ok, detail, hint = _probe_endpoint()
    report.add("网络：MCP 服务端点", ok, f"TCP/TLS 正常；{detail}", hint, level="L1")
    return ok


def _classify_error(e: Exception) -> str:
    """把异常归类为明确类别（供报告与下一步建议）。

    2026-10-17 体检新增：TIMEOUT_CONNECT / TIMEOUT_READ / RESPONSE——
    连接超时（换网络）与读取超时（服务端慢，稍后重试）的排查方向相反，
    无信封响应（RESPONSE）要把协议故障与业务空结果区分开。
    """
    msg = str(e)
    low = msg.lower()
    if isinstance(e, MCPError):
        if getattr(e, "code", None) == 401 or "401" in msg:
            return "401"
        if "403" in msg or "无权限" in msg or "拒绝访问" in msg:
            return "403"
        if "429" in msg or "限流" in msg:
            return "429"
        if "未检测到 MCD_MCP_TOKEN" in msg:
            return "TOKEN_MISSING"
        if "读取超时" in msg or "连接池超时" in msg or "写入超时" in msg or "ReadTimeout" in low:
            return "TIMEOUT_READ"
        if "连接超时" in msg or "ConnectTimeout" in low:
            return "TIMEOUT_CONNECT"
        if "响应解析失败" in msg or "响应协议错误" in msg:
            return "RESPONSE"
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
        "403": "无权限访问该资源（也可能是网络代理/WAF 拦截）：确认 Token 账户权限，或更换网络环境",
        "429": "触发限流（600 次/分钟）：稍等一分钟后重试；脚本化调用请降低频率",
        "NETWORK": "网络不可达：检查连接/代理/VPN 后重试",
        "TIMEOUT_CONNECT": "连接超时（TCP/TLS 握手未完成）：更换网络/代理后重试；持续出现请联系网络管理员放行 mcp.mcd.cn:443",
        "TIMEOUT_READ": "读取超时（已连接但服务端迟迟不返回）：稍后重试；持续出现可能是服务端过载或被限速，请反馈官方渠道",
        "RESPONSE": "服务端返回了非预期格式（网关错误页/服务异常）：可先用 curl 直接 POST https://mcp.mcd.cn 对比 HTTP 状态码；持续出现请反馈",
        "SDK": "SDK/协议层错误：执行 pip install -U mcp 升级官方 SDK 后重试；仍失败请附诊断输出反馈",
        "TOKEN_MISSING": "配置 MCD_MCP_TOKEN（环境变量或 .env），详见上方 Token 步骤建议",  # nosec B105：操作提示文案，非凭证
        "TOOL_MISSING": "官方工具清单缺少项目依赖的工具：可能是服务端变更，请到仓库 Issue 反馈",
        "BUSINESS": "官方返回业务错误：按消息提示调整（如更换门店/位置），或稍后重试",
    }.get(category, "查看上方原因，修复后重试；仍失败请带诊断输出到仓库 Issue 反馈")


def _check_mcp_live(report: DoctorReport) -> None:
    """L2-L4：连接 + 工具发现 + 一次只读调用（now-time-info）。任何失败都分类明示。"""
    from .mcp_http import HttpMCPClient

    client = None
    try:
        client = HttpMCPClient()
        client.connect()
        report.add("MCP：初始化会话", True, "Streamable HTTP 会话已建立（Token 通过认证）", level="L2")
    except MCPError as e:
        cat = _classify_error(e)
        report.add("MCP：初始化会话", False, f"[{cat}] {e}", _hint_for(cat), level="L2")
        return
    except Exception as e:  # SDK 缺失等
        cat = _classify_error(e)
        report.add("MCP：初始化会话", False, f"[{cat}] {e}", _hint_for(cat), level="L2")
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
                level="L3",
            )
        else:
            report.add(
                "MCP：工具发现", True, f"官方 {len(names)} 个工具，项目依赖的 {len(TOOLS_USED)} 个全部在位", level="L3"
            )
    except Exception as e:
        cat = _classify_error(e)
        report.add("MCP：工具发现", False, f"[{cat}] {e}", _hint_for(cat), level="L3")
        return
    finally:
        if client is not None:
            # 尽力而为：close 失败不影响已产出的诊断结论
            with suppress(Exception):
                client.close()

    # 只读调用（用独立会话，避免上面的 finally 已关闭）
    try:
        with HttpMCPClient() as c2:
            t = c2.now_time()
            when = (t.get("datetime") or "").strip()
            report.add(
                "MCP：只读调用 now-time-info",
                True,
                f"服务器时间 {when}" if when else "已返回时间信息",
                level="L4",
            )
    except Exception as e:
        cat = _classify_error(e)
        report.add("MCP：只读调用 now-time-info", False, f"[{cat}] {e}", _hint_for(cat), level="L4")


def _print_level_summary(report: DoctorReport) -> None:
    """五层验证状态汇总：未执行的层级如实标记，不伪造 PASS。"""
    print("\n📊 五层验证状态（L5 需合法 Token，由 burger-quest smoke / quest 覆盖）：")
    for level, name in VERIFY_LEVELS:
        steps = [s for s in report.steps if s.get("level") == level]
        if level == "L5":
            print(f"  ⬜ {level} {name}：未执行（本命令覆盖 L1-L4；请用 smoke/quest 验证）")
        elif not steps:
            print(f"  ⬜ {level} {name}：未执行（前置步骤失败，无法到达本层）")
        elif all(s["ok"] for s in steps):
            print(f"  ✅ {level} {name}：PASS")
        else:
            print(f"  ❌ {level} {name}：FAIL")


def run_doctor() -> DoctorReport:
    """完整诊断：Python -> 依赖 -> L1 服务可访问 -> Token -> L2-L4 MCP 层。

    顺序说明（2026-10-17 体检调整）：L1 服务可达性**不需要 Token**，提前到
    Token 检查之前——没有 Token 的用户也能拿到「服务通不通」的实证结论，
    而不是只看到一句「未配置 Token」。
    任一步失败即停止后续依赖步骤（缺 Token 就不建会话），已完成的步骤全部报告。
    """
    report = DoctorReport()
    print("🩺 Burger Quest 真实 MCP 连接诊断（只读，不调用任何写操作）")
    print(f"   目标服务：{MCD_MCP_URL}")
    if not _check_python(report):
        return report
    if not _check_sdk(report):
        return report
    if not _check_network(report):
        return report
    if not _check_token(report):
        return report
    _check_mcp_live(report)
    return report


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="burger-quest-doctor",
        description=(
            "真实 MCP 连接诊断（只读）：Python/依赖/L1 服务可访问/Token/认证/工具发现/单工具调用，"
            "逐步 PASS/FAIL + 下一步建议 + 五层验证状态汇总。\n"
            "失败分类：TOKEN_MISSING / 401 / 403 / 429 / NETWORK / TIMEOUT_CONNECT / TIMEOUT_READ / "
            "RESPONSE / SDK / TOOL_MISSING / BUSINESS。\n"
            "退出码：0=全部通过；1=有失败；2=配置类失败（缺 Token/缺 SDK/Python 版本不符）。"
        ),
    )
    parser.add_argument("--json", default=None, help="诊断结果 JSON 输出路径（不含任何敏感值）")
    args = parser.parse_args(argv)

    report = run_doctor()
    if report.passed:
        print("\n🎉 诊断全部通过：真实 MCP 连接就绪。可运行：burger-quest quest \"你的需求\"")
    else:
        print(f"\n💥 诊断未通过：{report.failed_count} 项失败。请按上方「下一步」建议逐项修复后重试。")
    _print_level_summary(report)
    print("退出码：0=全部通过；1=有失败；2=配置类失败（缺 Token/缺 SDK/Python 版本不符）")
    if args.json:
        import json

        Path(args.json).write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"📄 诊断结果已保存：{args.json}")
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
