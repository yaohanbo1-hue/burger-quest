"""只读冒烟测试（smoke）的自动化测试。

全部离线执行：
- 用假会话（FakeSession，来自 test_http_client）驱动真实 HttpMCPClient，
  覆盖七步全通过 / 某步失败 / 无门店时下游不编造 / 仅只读工具 / 连接失败；
- --demo 离线模式直接跑 MockMCPClient（可重复执行）；
- 缺 Token 时退出码 2，且提示中绝不出现 Token 本身。
"""
import sys
import types

import pytest

from burger_quest.mcp_base import MCPError
from burger_quest.mcp_http import HttpMCPClient
from burger_quest.smoke import READONLY_TOOLS, run_smoke
from test_http_client import fake_tools, make_client, wrap_api_text

from burger_quest import cli, smoke

# 冒烟全通过所需的响应集（按联调实测的真实信封结构）
SMOKE_RESPONSES = {
    "now-time-info": {"success": True, "code": 200, "data": {"datetime": "2026-10-13 21:00:00", "formatted": "2026-10-13 21:00:00"}},
    "query-nearby-stores": {
        "success": True,
        "code": 200,
        "data": [
            {
                "storeCode": "1420762",
                "storeName": "麦当劳深圳同方信息港餐厅",
                "address": "科技园北区朗山路11号同方信息港E栋G层5号商铺",
                "distance": 403,
                "businessStatus": True,
            }
        ],
    },
    "query-meals": {
        "success": True,
        "code": 200,
        "data": {"categories": [{"name": "鸡肉汉堡/卷", "meals": [{"code": "1440", "tags": ["单品"]}]}]},
    },
    "query-meal-detail": {
        "success": True,
        "code": 200,
        "data": {"code": "1440", "name": "麦辣鸡腿汉堡", "rounds": []},
    },
    "calculate-price": {
        "success": True,
        "code": 200,
        "data": {
            "price": 2250,
            "originalPrice": 2250,
            "discount": 0,
            "productList": [
                {"productCode": "1440", "productName": "麦辣鸡腿汉堡", "quantity": 1, "subtotal": 2250, "originalSubtotal": 2250}
            ],
        },
    },
}


def test_smoke_all_steps_pass(capsys):
    """七步全部 PASS；输出中不出现 Token。"""
    client, session = make_client(SMOKE_RESPONSES)
    report = run_smoke(client, location="深圳市南山区科技园")
    client.close()
    assert report.passed, [s for s in report.steps if not s["ok"]]
    assert len(report.steps) == 7  # run_smoke 七步；安全自检由 main() 追加为第 8 步
    assert all(s["ok"] for s in report.steps)
    out = capsys.readouterr().out
    assert "test-token-not-real" not in out  # Token 绝不打印
    assert "麦辣鸡腿汉堡" in out  # 详情名称可见
    # 调用过官方工具链
    called = [name for name, _ in session.calls]
    assert "list_tools" in called and "now-time-info" in called and "calculate-price" in called


def test_smoke_main_appends_security_check(capsys):
    """main() 在七步之后追加「安全自检（仅只读工具）」共八步。"""
    rc = smoke.main(["--demo"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "安全自检" in out
    assert "全部只读" in out


def test_smoke_reports_failure_and_continues():
    """某一步失败：如实记 FAIL，其余步骤独立给出结论（不中途静默）。"""
    responses = dict(SMOKE_RESPONSES)
    responses["calculate-price"] = {"success": False, "code": 600048, "message": "外送场景下必传 beCode"}
    client, _ = make_client(responses)
    report = run_smoke(client)
    client.close()
    assert not report.passed
    failed = [s for s in report.steps if not s["ok"]]
    assert len(failed) == 1 and failed[0]["step"] == "calculate-price"
    assert "beCode" in failed[0]["detail"]


def test_smoke_no_store_stops_downstream_without_fabrication():
    """查不到门店：下游菜单/详情/算价明确跳过，且不发起这些调用（不编造）。"""
    responses = dict(SMOKE_RESPONSES)
    responses["query-nearby-stores"] = {"success": True, "code": 200, "data": []}
    client, session = make_client(responses)
    report = run_smoke(client, location="无人区")
    client.close()
    assert not report.passed
    steps = {s["step"]: s for s in report.steps}
    assert steps["query-nearby-stores"]["ok"] is False
    for name in ("query-meals", "query-meal-detail", "calculate-price"):
        assert steps[name]["ok"] is False
        assert "跳过" in steps[name]["detail"]
    # 关键：没有门店就不该调用菜单工具
    called = [name for name, _ in session.calls]
    assert "query-meals" not in called
    assert "calculate-price" not in called


def test_smoke_store_code_skips_store_query():
    """--store-code 直达：不调用 query-nearby-stores。"""
    client, session = make_client(SMOKE_RESPONSES)
    report = run_smoke(client, store_code="1420762")
    client.close()
    assert report.passed
    called = [name for name, _ in session.calls]
    assert "query-nearby-stores" not in called
    assert "query-meals" in called


def test_smoke_only_readonly_tools_called():
    """安全红线：冒烟全过程只允许只读工具。"""
    client, _ = make_client(SMOKE_RESPONSES)
    run_smoke(client)
    client.close()
    called = {c["tool"] for c in client.calls if isinstance(c, dict)}
    assert called, "应该有调用记录"
    assert called <= READONLY_TOOLS, f"出现非只读调用：{called - READONLY_TOOLS}"


def test_smoke_connection_failure_reported():
    """连接失败：第一步即 FAIL，后续步骤不再执行（不静默）。"""
    client = HttpMCPClient(token="test-token-not-real")

    async def failing_open(self):
        raise MCPError("mcd-mcp", "MCP 连接失败：网络不可达")

    client._open_session = types.MethodType(failing_open, client)
    client._check_sdk = types.MethodType(lambda self: None, client)
    report = run_smoke(client)
    client.close()
    assert not report.passed
    assert len(report.steps) == 1
    assert report.steps[0]["step"] == "连接 MCP Server"
    assert "网络不可达" in report.steps[0]["detail"]


def test_smoke_missing_token_exit_2(capsys, monkeypatch):
    """真实模式缺 Token：退出码 2，提示可读，且不出现任何 Token 值。"""
    monkeypatch.delenv("MCD_MCP_TOKEN", raising=False)
    rc = smoke.main([])
    out = capsys.readouterr().out
    assert rc == 2
    assert "MCD_MCP_TOKEN" in out
    assert "open.mcd.cn" in out


def test_smoke_demo_offline_passes(capsys):
    """--demo 离线冒烟：可重复执行，全部通过（退出码 0）。"""
    rc = smoke.main(["--demo"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "全部通过" in out
    assert "demo" in out


def test_smoke_cli_wiring(capsys):
    """CLI smoke 子命令接线正确（--demo 离线）。"""
    rc = cli.main(["smoke", "--demo"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "冒烟测试" in out


def test_smoke_json_output(tmp_path, capsys):
    """--json 输出机器可读结果。"""
    out_path = tmp_path / "smoke.json"
    rc = smoke.main(["--demo", "--json", str(out_path)])
    assert rc == 0
    import json

    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["passed"] is True
    assert len(payload["steps"]) == 8
    assert payload["mode"].startswith("demo")


# ---------------------------------------------------------------- 候选门店回退（2026-10-16 审计新增）


class _StoreRoutingSession:
    """按 storeCode 路由的假会话：第一家门店菜单报业务错误（打烊），第二家正常。"""

    def __init__(self):
        self.calls = []

    async def initialize(self):
        return None

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "query-meals" and arguments.get("storeCode") == "1111111":
            # 首家门店打烊：官方业务错误信封（联调实测形态 code=600057）
            payload = {"success": False, "code": 600057, "message": "门店可能已关闭或不在营业时间", "data": None}
            return types.SimpleNamespace(content=[types.SimpleNamespace(text=wrap_api_text(payload))])
        payload = SMOKE_RESPONSES.get(name, {"success": True, "code": 200, "data": {}})
        return types.SimpleNamespace(content=[types.SimpleNamespace(text=wrap_api_text(payload))])

    async def list_tools(self):
        return types.SimpleNamespace(tools=fake_tools())


def _client_with_closed_first_store():
    client = HttpMCPClient(token="test-token-not-real", timeout=10)
    session = _StoreRoutingSession()
    # 附近门店返回两家：1111111（打烊）+ 1420762（正常）
    stores_resp = {
        "success": True,
        "code": 200,
        "data": [
            {"storeCode": "1111111", "storeName": "已打烊门店", "address": "某路1号", "distance": 100, "businessStatus": False},
            {"storeCode": "1420762", "storeName": "营业中门店", "address": "某路2号", "distance": 200, "businessStatus": True},
        ],
    }
    session.call_tool_orig = session.call_tool

    async def routing_call_tool(name, arguments):
        if name == "query-nearby-stores":
            return types.SimpleNamespace(content=[types.SimpleNamespace(text=wrap_api_text(stores_resp))])
        return await session.call_tool_orig(name, arguments)

    session.call_tool = routing_call_tool

    async def fake_open(self):
        self._session = session

    client._open_session = types.MethodType(fake_open, client)
    client._check_sdk = types.MethodType(lambda self: None, client)
    return client, session


def test_smoke_falls_back_to_next_store_when_first_closed():
    """首家门店打烊（业务错误）时，冒烟有界回退到下一家候选门店（2026-10-16 实测场景）。"""
    from burger_quest.smoke import run_smoke

    client, session = _client_with_closed_first_store()
    report = run_smoke(client, location="深圳市南山区科技园")
    meals_step = next(s for s in report.steps if s["step"] == "query-meals")
    assert meals_step["ok"], f"应回退到第二家门店成功: {meals_step['detail']}"
    assert "1420762" in meals_step["detail"]
    assert "回退" in meals_step["detail"]  # 明示发生了回退
    # 调用有界：query-meals 最多 3 次（首家失败 + 第二家成功 = 2 次）
    meals_calls = [c for c in session.calls if c[0] == "query-meals"]
    assert len(meals_calls) == 2
