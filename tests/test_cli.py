"""CLI 测试：demo / quest / stores / tools 的行为、退出码与错误提示。

真实模式用「假会话」替换 HttpMCPClient（不访问网络），
验证 quest 全流程、多候选门店、无效编码等路径。
"""
import json
import types

import pytest

from burger_quest import cli
from burger_quest.mcp_http import HttpMCPClient


class FakeSession:
    def __init__(self, responses=None, errors=None):
        self.responses = responses or {}
        self.errors = errors or {}
        self.calls = []

    async def initialize(self):
        return None

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name in self.errors:
            raise self.errors[name]
        text = json.dumps(self.responses.get(name, {}), ensure_ascii=False)
        return types.SimpleNamespace(content=[types.SimpleNamespace(text=text)])


LIVE_RESPONSES = {
    "now-time-info": {"datetime": "2026-10-13 19:00:00", "timePeriod": "dinner"},
    "query-nearby-stores": [
        {"storeCode": "SZ-1001", "storeName": "麦当劳（深圳测试路店）", "address": "深圳市南山区测试路1号"}
    ],
    "query-meals": {
        "list": [
            {"mealCode": "SZ-001", "mealName": "麦辣鸡腿堡", "category": "汉堡", "price": 21.0, "tags": ["辣"]},
            {"mealCode": "SZ-002", "mealName": "薯条（大）", "category": "小食", "price": 12.5},
            {"mealCode": "SZ-003", "mealName": "可口可乐（大）", "category": "饮品", "price": 10.0},
        ]
    },
    "query-store-coupons": {"list": [{"couponId": "C1", "couponName": "满30减3", "threshold": 30, "value": 3}]},
    "query-my-coupons": [],
    "list-nutrition-foods": {"list": [{"foodName": "麦辣鸡腿堡", "energy": 560, "protein": 25}]},
    "calculate-price": {"totalPrice": 40.5, "originalPrice": 43.5, "discountPrice": 3.0, "deliveryFee": 0},
}


@pytest.fixture()
def fake_http(monkeypatch):
    """把 burger_quest.mcp_http.HttpMCPClient 替换为带假会话的工厂（不访问网络）。"""
    holder = {}

    def install(responses=None, errors=None):
        responses = LIVE_RESPONSES if responses is None else responses
        errors = errors or {}

        def factory(token=None, url=None, timeout=60.0):
            client = HttpMCPClient(token=token or "test-token-not-real", timeout=timeout)
            session = FakeSession(responses, errors)
            holder["session"] = session

            async def fake_open(self):
                self._session = session

            client._open_session = types.MethodType(fake_open, client)
            client._check_sdk = types.MethodType(lambda self: None, client)
            return client

        monkeypatch.setattr("burger_quest.mcp_http.HttpMCPClient", factory)
        return holder

    return install


# ---------------------------------------------------------------- demo 模式


def test_demo_happy_path(capsys):
    code = cli.main(["demo", "我想吃辣，预算35，外送到南山科技园，一个人"])
    out = capsys.readouterr().out
    assert code == 0
    assert "麦辣鸡腿堡" in out
    assert "calculate-price 权威结果" in out
    assert "演示数据" in out  # 明确标注数据来源


def test_demo_budget_too_small(capsys):
    code = cli.main(["demo", "预算5块，想吃汉堡"])
    out = capsys.readouterr().out
    assert code == 1
    assert "预算内" in out
    assert "不会编造" in out


def test_demo_missing_fixture_is_friendly(capsys):
    code = cli.main(["demo", "测试", "--fixture", "no_such_file.json"])
    out = capsys.readouterr().out
    assert code == 2
    assert "演示数据不存在" in out
    assert "Traceback" not in out  # 不是裸堆栈


def test_demo_multi_candidates(capsys):
    code = cli.main(["demo", "外送到演示门店，预算35"])
    out = capsys.readouterr().out
    assert code == 1
    assert "候选门店" in out
    assert "DEMO-001" in out and "DEMO-002" in out
    assert "--store-code" in out
    assert "不会替你默认选择" in out


def test_demo_invalid_store_code(capsys):
    code = cli.main(["demo", "预算35", "--store-code", "DEMO-999"])
    out = capsys.readouterr().out
    assert code == 1
    assert "DEMO-999" in out
    assert "查询失败" in out


# ---------------------------------------------------------------- quest 模式


def test_quest_without_token_never_falls_back_to_mock(capsys, monkeypatch):
    monkeypatch.delenv("MCD_MCP_TOKEN", raising=False)
    code = cli.main(["quest", "我想吃辣，预算35"])
    out = capsys.readouterr().out
    assert code == 2
    assert "MCD_MCP_TOKEN" in out
    assert "不会自动切换到演示数据" in out
    # 没有静默使用演示数据：菜单内容与 Mock 客户端都不会出现
    assert "麦辣鸡腿堡" not in out
    assert "MockMCPClient" not in out


def test_quest_full_flow_with_fake_session(capsys, fake_http, monkeypatch):
    monkeypatch.setenv("MCD_MCP_TOKEN", "test-token-not-real")
    holder = fake_http()
    code = cli.main(["quest", "我想吃辣，预算50，外送到测试路1号"])
    out = capsys.readouterr().out
    assert code == 0
    assert "麦当劳（深圳测试路店）" in out
    assert "麦辣鸡腿堡" in out
    assert "麦当劳 MCP 实时数据" in out
    assert "演示数据" not in out
    tools = {name for name, _ in holder["session"].calls}
    assert {"now-time-info", "query-nearby-stores", "query-meals", "calculate-price"} <= tools


def test_quest_multiple_stores_lists_candidates(capsys, fake_http, monkeypatch):
    monkeypatch.setenv("MCD_MCP_TOKEN", "test-token-not-real")
    responses = dict(LIVE_RESPONSES)
    responses["query-nearby-stores"] = [
        {"storeCode": "SZ-1001", "storeName": "麦当劳（甲店）"},
        {"storeCode": "SZ-1002", "storeName": "麦当劳（乙店）"},
        {"storeCode": "SZ-1003", "storeName": "麦当劳（丙店）"},
    ]
    holder = fake_http(responses)
    code = cli.main(["quest", "预算50，外送到测试路"])
    out = capsys.readouterr().out
    assert code == 1
    assert "候选门店" in out
    for c in ("SZ-1001", "SZ-1002", "SZ-1003"):
        assert c in out
    assert "--store-code" in out
    # 没有查询菜单（不悄悄选店）
    assert not any(name == "query-meals" for name, _ in holder["session"].calls)


def test_quest_invalid_store_code(capsys, fake_http, monkeypatch):
    monkeypatch.setenv("MCD_MCP_TOKEN", "test-token-not-real")
    fake_http(errors={"query-meals": Exception("门店不存在或已停售")})
    code = cli.main(["quest", "预算50", "--store-code", "BAD-999"])
    out = capsys.readouterr().out
    assert code == 1
    assert "BAD-999" in out
    assert "查询失败" in out
    assert "Traceback" not in out


def test_quest_connection_failure(capsys, fake_http, monkeypatch):
    """connect 阶段失败（如网络不可达）也要友好报错。"""
    monkeypatch.setenv("MCD_MCP_TOKEN", "test-token-not-real")

    def broken_factory(token=None, url=None, timeout=60.0):
        from burger_quest.mcp_base import MCPError

        raise MCPError("mcd-mcp", "MCP 连接失败：模拟网络不可达；请检查网络能否访问 https://mcp.mcd.cn")

    monkeypatch.setattr("burger_quest.mcp_http.HttpMCPClient", broken_factory)
    code = cli.main(["quest", "预算50，外送到测试路"])
    out = capsys.readouterr().out
    assert code == 2
    assert "连接失败" in out
    assert "Traceback" not in out


# ---------------------------------------------------------------- stores 模式


def test_stores_real_mode(capsys, fake_http, monkeypatch):
    monkeypatch.setenv("MCD_MCP_TOKEN", "test-token-not-real")
    fake_http()
    code = cli.main(["stores", "测试路1号"])
    out = capsys.readouterr().out
    assert code == 0
    assert "SZ-1001" in out
    assert "--store-code" in out


def test_stores_demo_mode(capsys):
    code = cli.main(["stores", "南山科技园", "--demo"])
    out = capsys.readouterr().out
    assert code == 0
    assert "DEMO-001" in out


def test_stores_without_token(capsys, monkeypatch):
    monkeypatch.delenv("MCD_MCP_TOKEN", raising=False)
    code = cli.main(["stores", "南山科技园"])
    out = capsys.readouterr().out
    assert code == 2
    assert "MCD_MCP_TOKEN" in out


# ---------------------------------------------------------------- tools / help


def test_tools_command(capsys):
    code = cli.main(["tools"])
    out = capsys.readouterr().out
    assert code == 0
    assert "calculate-price" in out
    assert "query-meals" in out
    assert "query-meal-detail" in out  # 现在列在预留区，不再宣称已调用


def test_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as ei:
        cli.main(["--help"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "demo" in out and "quest" in out and "stores" in out


def test_quest_help_has_no_fixture_and_mentions_token(capsys):
    with pytest.raises(SystemExit) as ei:
        cli.main(["quest", "--help"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "--fixture FIXTURE" not in out  # 真实模式不接受演示数据参数（选项行中不存在）
    assert "MCD_MCP_TOKEN" in out


def test_demo_help_mentions_fixture_and_offline(capsys):
    with pytest.raises(SystemExit) as ei:
        cli.main(["demo", "--help"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "--fixture" in out
    assert "离线" in out
