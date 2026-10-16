"""真实 MCP 连接诊断（doctor）测试（2026-10-16 Windows 支持新增）。

覆盖：缺 Token 直接失败（exit 2，不回退 Demo）/ 错误分类（401/403/429/
NETWORK/SDK/TOOL_MISSING/BUSINESS）/ 全通过 / JSON 输出不含敏感值 /
CLI 接线。用假会话驱动，不访问网络。
"""
import json
import sys
import types

import pytest

from burger_quest import cli, doctor
from burger_quest.mcp_base import MCPError
from burger_quest.mcp_http import HttpMCPClient
from test_http_client import fake_tools, wrap_api_text


def _patch_client(monkeypatch, script):
    """把 HttpMCPClient 的会话换成按脚本响应的假会话（不访问网络）。"""
    calls = []

    class FakeSession:
        async def initialize(self):
            return None

        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            result = script(name, arguments)
            if isinstance(result, Exception):
                raise result
            return types.SimpleNamespace(content=[types.SimpleNamespace(text=wrap_api_text(result))])

        async def list_tools(self):
            return types.SimpleNamespace(tools=fake_tools())

    session = FakeSession()

    def factory(token=None, url=None, timeout=60.0):
        client = HttpMCPClient(token=token or "test-token-not-real", timeout=timeout)

        async def fake_open(self):
            self._session = session

        client._open_session = types.MethodType(fake_open, client)
        client._check_sdk = types.MethodType(lambda self: None, client)
        return client

    monkeypatch.setattr("burger_quest.mcp_http.HttpMCPClient", factory)
    return calls


def test_doctor_missing_token_fails_with_exit_2(monkeypatch, capsys):
    """缺 Token：明确失败，退出码 2，绝不偷偷切换 Demo。"""
    monkeypatch.delenv("MCD_MCP_TOKEN", raising=False)
    monkeypatch.setattr(doctor, "_check_network", lambda r: True)  # 跳过真实网络
    report = doctor.run_doctor()
    out = capsys.readouterr().out
    token_step = next(s for s in report.steps if s["step"].startswith("Token"))
    assert token_step["ok"] is False
    assert "申请" in token_step["hint"]  # 有明确下一步
    assert report.exit_code == 2
    assert "演示" not in out or "不会自动切换" in out  # 不诱导用 demo 冒充


def test_doctor_all_pass(monkeypatch, capsys):
    monkeypatch.setenv("MCD_MCP_TOKEN", "test-token-not-real")
    monkeypatch.setattr(doctor, "_check_network", lambda r: True)

    def script(name, args):
        if name == "list_tools":
            return {"success": True, "code": 200, "data": {}}
        return {"success": True, "code": 200, "data": {"datetime": "2026-10-16 21:00:00"}}

    _patch_client(monkeypatch, script)
    report = doctor.run_doctor()
    assert report.passed, [s for s in report.steps if not s["ok"]]
    assert report.exit_code == 0


@pytest.mark.parametrize(
    "error_msg,expect_cat",
    [
        ("MCP Token 无效或已过期（401）", "401"),
        ("无权限访问该资源（403）", "403"),
        ("请求过于频繁（429）", "429"),
        ("MCP 连接失败：网络不可达", "NETWORK"),
        ("[mcp] 未知工具 unknown tool", "SDK"),
        ("门店可能已关闭或不在营业时间", "BUSINESS"),
    ],
)
def test_error_classification(error_msg, expect_cat):
    e = MCPError("t", error_msg)
    assert doctor._classify_error(e) == expect_cat


def test_doctor_init_failure_reports_category(monkeypatch, capsys):
    """连接失败：分类明示 + 下一步建议，不静默。"""
    monkeypatch.setenv("MCD_MCP_TOKEN", "test-token-not-real")
    monkeypatch.setattr(doctor, "_check_network", lambda r: True)

    def script(name, args):
        return MCPError("connect", "MCP Token 无效或已过期（401）")

    # 连接级失败：initialize 阶段即抛错
    import burger_quest.doctor as _d

    _orig_patch = _patch_client

    def _patch_connect_fail(monkeypatch, script):
        calls = []

        class FailingSession:
            async def initialize(self):
                raise RuntimeError("HTTP 401")

            async def call_tool(self, name, arguments):
                calls.append((name, arguments))
                raise RuntimeError("HTTP 401")

            async def list_tools(self):
                raise RuntimeError("HTTP 401")

        session = FailingSession()

        def factory(token=None, url=None, timeout=60.0):
            client = HttpMCPClient(token=token or "test-token-not-real", timeout=timeout)

            async def fake_open(self):
                # 镜像真实 _open_session：建会话后立即 initialize（401 在这一步暴露）
                self._session = session
                await session.initialize()

            client._open_session = types.MethodType(fake_open, client)
            client._check_sdk = types.MethodType(lambda self: None, client)
            return client

        monkeypatch.setattr("burger_quest.mcp_http.HttpMCPClient", factory)
        return calls

    _patch_connect_fail(monkeypatch, script)
    report = doctor.run_doctor()
    init_step = next(s for s in report.steps if s["step"].startswith("MCP：初始化"))
    assert init_step["ok"] is False
    assert "[401]" in init_step["detail"]
    assert "重新申请" in init_step["hint"]
    # 401=Token 无效，属配置类错误（exit 2）：与「缺 Token」同类的凭据问题
    assert report.exit_code == 2


def test_doctor_json_output_has_no_sensitive_values(monkeypatch, tmp_path, capsys):
    """JSON 诊断输出不含 Token 值/Authorization。"""
    monkeypatch.setenv("MCD_MCP_TOKEN", "super-secret-token-value")
    monkeypatch.setattr(doctor, "_check_network", lambda r: True)
    def _fake_token_fail(r):
        r.add("Token：MCD_MCP_TOKEN", False, "未配置（测试）", "测试建议")
        return False

    monkeypatch.setattr(doctor, "_check_token", _fake_token_fail)  # 直接失败，快

    out = tmp_path / "doctor.json"
    rc = doctor.main(["--json", str(out)])
    payload = out.read_text(encoding="utf-8")
    assert "super-secret-token-value" not in payload
    assert "Authorization" not in payload
    data = json.loads(payload)
    assert data["passed"] is False
    assert rc == 2


def test_cli_doctor_wiring(monkeypatch, capsys):
    """CLI doctor 子命令接线（缺 Token 快速失败路径）。"""
    monkeypatch.delenv("MCD_MCP_TOKEN", raising=False)
    monkeypatch.setattr(doctor, "_check_network", lambda r: True)
    rc = cli.main(["doctor"])
    assert rc == 2
    assert "MCD_MCP_TOKEN" in capsys.readouterr().out


def test_cli_output_survives_narrow_codepage(monkeypatch, capsys):
    """窄代码页（cp1252）控制台下 CLI 输出不崩溃（2026-10-16 Windows CI 实测回归）。

    GitHub Windows runner 默认 cp1252，print emoji/中文会 UnicodeEncodeError；
    main() 入口的 _ensure_utf8_output() 把标准流切到 UTF-8+replace。
    """
    import io

    class _FakeStream(io.TextIOWrapper):
        """模拟会抛 UnicodeEncodeError 的窄代码页流（无 reconfigure 时必崩）。"""

        def __init__(self):
            super().__init__(io.BytesIO(), encoding="cp1252")

        def write(self, text):
            # 窄代码页真实行为：当前编码无法表示的字符直接抛错
            # （reconfigure 切到 UTF-8 后 self.encoding 随之改变，不再抛错）
            text.encode(self.encoding)
            return super().write(text)

        def flush(self):
            pass

    fake_out, fake_err = _FakeStream(), _FakeStream()
    monkeypatch.setattr(sys, "stdout", fake_out)
    monkeypatch.setattr(sys, "stderr", fake_err)
    monkeypatch.delenv("MCD_MCP_TOKEN", raising=False)
    monkeypatch.setattr(doctor, "_check_network", lambda r: True)
    # 不抛 UnicodeEncodeError 即通过（退出码仍为 2=缺 Token）
    rc = cli.main(["doctor"])
    assert rc == 2
    # reconfigure 后流已切 UTF-8，写中文/emoji 不再抛错
    sys.stdout.write("🩺 中文测试")
    sys.stdout.flush()
