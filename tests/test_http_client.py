"""真实 MCP 客户端（同步门面）测试。

不访问网络、不依赖 mcp SDK：用假会话（FakeSession）替换 _open_session，
验证「同步接口 -> 后台事件循环 -> 工具调用 -> 防御式适配」整条链路，
以及 Token 缺失、401/429、超时、SDK 缺失等错误路径。
"""
import asyncio
import json
import sys
import types

import pytest

from burger_quest.mcp_base import MCPError
from burger_quest.mcp_http import HttpMCPClient
from burger_quest.models import PlanItem


class FakeSession:
    """假 MCP 会话：按工具名返回预制 JSON；可指定某工具抛异常或延迟。"""

    def __init__(self, responses=None, errors=None, delay=0.0):
        self.responses = responses or {}
        self.errors = errors or {}
        self.delay = delay
        self.calls = []

    async def initialize(self):
        return None

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.delay:
            await asyncio.sleep(self.delay)
        if name in self.errors:
            raise self.errors[name]
        text = json.dumps(self.responses.get(name, {}), ensure_ascii=False)
        return types.SimpleNamespace(content=[types.SimpleNamespace(text=text)])


def make_client(responses=None, errors=None, delay=0.0, timeout=60.0):
    """构造一个带假会话的 HttpMCPClient（不发起真实网络连接）。"""
    client = HttpMCPClient(token="test-token-not-real", timeout=timeout)
    session = FakeSession(responses, errors, delay)

    async def fake_open(self):
        self._session = session

    client._open_session = types.MethodType(fake_open, client)
    client._check_sdk = types.MethodType(lambda self: None, client)
    return client, session


MEALS_RESP = {
    "list": [
        {"mealCode": "SZ-001", "mealName": "麦辣鸡腿堡", "category": "汉堡", "currentPrice": 21.0, "tags": [{"name": "辣"}]},
        {"mealCode": "SZ-002", "mealName": "薯条（大）", "category": "小食", "salePrice": "12.5"},
        {"mealCode": "SZ-003", "mealName": "可口可乐（大）", "category": "饮品", "price": 10.0},
        {"mealCode": "SZ-004", "mealName": "无价幽灵堡", "category": "汉堡"},
    ]
}
STORES_RESP = [{"storeCode": "SZ-1001", "storeName": "麦当劳（深圳测试路店）", "address": "深圳市南山区测试路1号"}]
CALC_RESP = {"totalPrice": 27.0, "originalPrice": 30.0, "discountPrice": 3.0, "deliveryFee": 0}


def test_missing_token_raises_friendly_error(monkeypatch):
    monkeypatch.delenv("MCD_MCP_TOKEN", raising=False)
    with pytest.raises(MCPError) as ei:
        HttpMCPClient()
    msg = str(ei.value)
    assert "MCD_MCP_TOKEN" in msg
    assert ei.value.code == 401
    assert "demo" in msg  # 明示可用演示模式，但不会自动切换


def test_sync_facade_end_to_end():
    client, session = make_client(
        {
            "now-time-info": {"datetime": "2026-10-13 19:00:00", "timePeriod": "dinner"},
            "query-nearby-stores": STORES_RESP,
            "query-meals": MEALS_RESP,
            "query-store-coupons": {"list": [{"couponId": "C1", "couponName": "满30减3", "threshold": 30, "value": 3}]},
            "query-my-coupons": [],
            "list-nutrition-foods": {"list": [{"foodName": "麦辣鸡腿堡", "energy": 560, "protein": 25}]},
            "calculate-price": CALC_RESP,
        }
    )
    with client:
        assert client.now_time()["datetime"] == "2026-10-13 19:00:00"
        stores = client.nearby_stores("测试路")
        assert [s.code for s in stores] == ["SZ-1001"]
        assert stores[0].name == "麦当劳（深圳测试路店）"
        menu = client.query_meals("SZ-1001")
        assert len(menu) == 4
        assert menu[0].name == "麦辣鸡腿堡" and menu[0].price_cents == 2100
        assert menu[0].tags == ("辣",)  # dict 形式的 tag 也兼容
        assert menu[1].price_cents == 1250  # salePrice 字符串
        assert menu[3].price_cents is None  # 无价格 -> None，不编造
        coupons = client.store_coupons("SZ-1001")
        assert coupons[0].threshold_cents == 3000 and coupons[0].value_cents == 300
        nutrition = client.list_nutrition_foods()
        assert nutrition[0].calories_kcal == 560.0 and nutrition[0].protein_g == 25.0
        quote = client.calculate_price("SZ-1001", [PlanItem(menu[0], 1)], [], "takeaway")
        assert quote.ok and quote.total_cents == 2700
        assert quote.discount_cents == 300 and quote.delivery_fee_cents == 0
        # 审计记录不包含敏感字段
        for call in client.calls:
            assert "token" not in call["args"]
            assert "authorization" not in call["args"]
    # with 退出后会话已关闭，close 幂等
    client.close()
    client.close()


def test_error_mapping_401():
    client, _ = make_client(errors={"query-meals": Exception("HTTP 401 Unauthorized")})
    with client:
        with pytest.raises(MCPError) as ei:
            client.query_meals("X")
    assert ei.value.code == 401
    assert "Token" in str(ei.value)


def test_error_mapping_429():
    client, _ = make_client(errors={"query-meals": Exception("429 Too Many Requests")})
    with client:
        with pytest.raises(MCPError) as ei:
            client.query_meals("X")
    assert ei.value.code == 429


def test_error_mapping_generic():
    client, _ = make_client(errors={"query-meals": Exception("boom")})
    with client:
        with pytest.raises(MCPError) as ei:
            client.query_meals("X")
    assert "boom" in str(ei.value)
    assert ei.value.code is None


def test_timeout_maps_to_mcp_error():
    client, _ = make_client(delay=0.4, timeout=0.05)
    with client:
        with pytest.raises(MCPError) as ei:
            client.now_time()
    assert "超时" in str(ei.value)


def test_connect_without_sdk(monkeypatch):
    monkeypatch.setitem(sys.modules, "mcp", None)
    client = HttpMCPClient(token="x")
    with pytest.raises(MCPError) as ei:
        client.connect()
    assert "pip install mcp" in str(ei.value)
    assert client._loop is None  # 没有留下后台线程


def test_call_without_session():
    client = HttpMCPClient(token="x")
    with pytest.raises(MCPError) as ei:
        client.now_time()
    assert "会话未建立" in str(ei.value)


def test_close_without_connect_is_noop():
    client = HttpMCPClient(token="x")
    client.close()  # 不报错


def test_map_sdk_error_with_http_status():
    """新版 SDK 会吞掉 HTTP 状态码；event hook 记录的状态必须能还原 401/429 语义。"""
    from burger_quest.mcp_http import _map_sdk_error

    e = _map_sdk_error("tool", Exception("Server returned an error response"), http_status=401)
    assert e.code == 401
    assert "Token" in str(e)
    e2 = _map_sdk_error("tool", Exception("Server returned an error response"), http_status=429)
    assert e2.code == 429
    e3 = _map_sdk_error("tool", Exception("boom"), http_status=None)
    assert e3.code is None and "boom" in str(e3)


def test_import_stream_client_with_sdk():
    """已安装 mcp SDK 时，双版本兼容导入必须拿到可调用的工厂。"""
    pytest.importorskip("mcp")
    from burger_quest.mcp_http import _import_stream_client

    factory = _import_stream_client()
    assert callable(factory)


def test_transport_kwargs_version_branches():
    """_transport_kwargs 按工厂签名选择 headers / http_client 两种传法。"""
    pytest.importorskip("mcp")
    from burger_quest.mcp_http import _import_stream_client

    client = HttpMCPClient(token="x")
    factory = _import_stream_client()
    kwargs = client._transport_kwargs(factory, {"Authorization": "Bearer x"})
    assert ("headers" in kwargs) or ("http_client" in kwargs)
    if "http_client" in kwargs:
        assert kwargs["http_client"].headers.get("authorization") == "Bearer x"
        assert client._http_client is kwargs["http_client"]

    # 旧版签名（直接收 headers）
    def old_factory(url, headers=None):  # noqa: ARG001
        return None

    assert client._transport_kwargs(old_factory, {"Authorization": "Bearer x"}) == {
        "headers": {"Authorization": "Bearer x"}
    }

    # 未知签名：按旧版习惯传 headers
    def unknown_factory(url):  # noqa: ARG001
        return None

    assert client._transport_kwargs(unknown_factory, {"Authorization": "Bearer x"}) == {
        "headers": {"Authorization": "Bearer x"}
    }
