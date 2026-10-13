"""真实 MCP 客户端（同步门面）测试。

不访问网络、不依赖 mcp SDK：用假会话（FakeSession）替换 _open_session，
假会话按**真实联调实测的文本信封格式**返回（字段说明 + JSON + 传参规则），
验证「同步接口 -> 后台事件循环 -> 信封提取 -> 防御式适配」整条链路，
以及 Token 缺失、401/429、超时、SDK 缺失、业务错误等路径。
"""
import asyncio
import json
import sys
import types

import pytest

from burger_quest.mcp_base import MCPError, OrderScene
from burger_quest.mcp_http import HttpMCPClient
from burger_quest.models import PlanItem


def wrap_api_text(payload: dict) -> str:
    """按联调实测格式包装：字段说明 + ## Original Response + JSON + 传参规则说明。"""
    return (
        "# API Response Information\n\n"
        "Below is the response from an API call.\n\n"
        "## Response Structure\n\n"
        "> Content-Type: application/json\n\n"
        "- **data**: 业务数据 (Type: object)\n"
        "- **success**: 是否成功 (Type: boolean)\n\n"
        "## Original Response\n\n"
        + json.dumps(payload, ensure_ascii=False)
        + "\n\n## 数据传参规则\n\n1. 严禁省略字段。\n"
    )


class FakeSession:
    """假 MCP 会话：按工具名返回预制 payload（包装成真实文本格式）；可指定异常/延迟。"""

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
        payload = self.responses.get(name, {"success": True, "code": 200, "data": {}})
        return types.SimpleNamespace(content=[types.SimpleNamespace(text=wrap_api_text(payload))])


def make_client(responses=None, errors=None, delay=0.0, timeout=60.0):
    """构造一个带假会话的 HttpMCPClient（不发起真实网络连接）。"""
    client = HttpMCPClient(token="test-token-not-real", timeout=timeout)
    session = FakeSession(responses, errors, delay)

    async def fake_open(self):
        self._session = session

    client._open_session = types.MethodType(fake_open, client)
    client._check_sdk = types.MethodType(lambda self: None, client)
    return client, session


SCENE = OrderScene(store_code="1420762", order_type=1, be_type=1)

# 联调实测格式的响应样本
NOW_TIME_RESP = {"success": True, "code": 200, "data": {"datetime": "2026-10-13 20:34:27", "formatted": "2026-10-13 20:34:27"}}
STORES_RESP = {
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
}
MEALS_RESP = {
    "success": True,
    "code": 200,
    "data": {
        "categories": [
            {"name": "鸡肉汉堡/卷", "meals": [{"code": "1440", "tags": ["单品"]}, {"code": "9900005462", "tags": ["套餐"]}]},
            {"name": "小食", "meals": [{"code": "4810", "tags": []}]},
            {"name": "饮品", "meals": [{"code": "3101", "tags": []}]},
        ]
    },
}
DETAIL_RESP = {
    "success": True,
    "code": 200,
    "data": {
        "code": "1440",
        "name": "麦辣鸡腿汉堡",
        "rounds": [],
        "supportModify": True,
    },
}
CALC_RESP = {
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
}
NUTRITION_RESP = {
    "success": True,
    "code": 200,
    "data": "[2]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:\n"
    "  麦辣鸡腿汉堡,null,2345,560,25,27,52,1100,120\n  中薯条,null,1210,289,4,12,38,165,18",
}


def test_missing_token_raises_friendly_error(monkeypatch):
    monkeypatch.delenv("MCD_MCP_TOKEN", raising=False)
    with pytest.raises(MCPError) as ei:
        HttpMCPClient()
    msg = str(ei.value)
    assert "MCD_MCP_TOKEN" in msg
    assert ei.value.code == 401
    assert "demo" in msg  # 明示可用演示模式，但不会自动切换


def test_sync_facade_end_to_end():
    """完整链路：文本信封提取 -> data 解包 -> 适配（金额单位：分）。"""
    client, session = make_client(
        {
            "now-time-info": NOW_TIME_RESP,
            "query-nearby-stores": STORES_RESP,
            "query-meals": MEALS_RESP,
            "query-meal-detail": DETAIL_RESP,
            "query-store-coupons": {"success": True, "code": 200, "data": []},
            "query-my-coupons": {"success": True, "code": 200, "data": []},
            "list-nutrition-foods": NUTRITION_RESP,
            "calculate-price": CALC_RESP,
        }
    )
    with client:
        assert client.now_time()["datetime"] == "2026-10-13 20:34:27"
        stores = client.nearby_stores("科技园", city="深圳市")
        assert [s.code for s in stores] == ["1420762"]
        assert stores[0].name == "麦当劳深圳同方信息港餐厅"
        # 门店查询参数：beType + searchType=2 + city/keyword（联调实测）
        args = dict(session.calls[1][1])
        assert args["beType"] == 1 and args["searchType"] == 2 and args["city"] == "深圳市"
        # 菜单骨架：只有编码/分类/标签，无名称无价格
        menu = client.query_meals(SCENE)
        assert [m.code for m in menu] == ["1440", "9900005462", "4810", "3101"]
        assert menu[0].name == "" and menu[0].price_cents is None
        assert menu[0].category == "鸡肉汉堡/卷" and menu[0].tags == ("单品",)
        # 餐品详情：名称来自这里
        detail = client.meal_detail(SCENE, "1440")
        assert detail["name"] == "麦辣鸡腿汉堡" and detail["is_combo"] is False
        # 算价：金额单位是分（2250 = ¥22.50）
        quote = client.calculate_price(SCENE, [PlanItem(menu[0], 1)], [])
        assert quote.ok and quote.total_cents == 2250
        assert quote.original_cents == 2250 and quote.discount_cents == 0
        # 单品价格/名称提取（价格发现用）
        from burger_quest.mcp_base import extract_product_names, extract_product_prices

        assert extract_product_prices(quote) == {"1440": 2250}
        assert extract_product_names(quote) == {"1440": "麦辣鸡腿汉堡"}
        # 营养：自定义文本解析
        nutrition = client.list_nutrition_foods()
        assert nutrition[0].name == "麦辣鸡腿汉堡"
        assert nutrition[0].calories_kcal == 560.0 and nutrition[0].protein_g == 25.0
        assert nutrition[1].calories_kcal == 289.0
        # 审计记录不包含敏感字段
        for call in client.calls:
            assert "token" not in call["args"]
            assert "authorization" not in call["args"]
    # with 退出后会话已关闭，close 幂等
    client.close()
    client.close()


def test_business_error_becomes_mcp_error():
    """success=false 的业务错误（如缺 beCode）必须转成可读 MCPError。"""
    client, _ = make_client(
        {
            "calculate-price": {
                "success": False,
                "code": 600048,
                "message": "外送场景下，beType=2/5/6时必传beCode",
            }
        }
    )
    with client:
        with pytest.raises(MCPError) as ei:
            client.calculate_price(SCENE, [PlanItem(_item("1440"), 1)], [])
    assert "beCode" in str(ei.value)
    assert ei.value.code == 600048


def _item(code):
    from burger_quest.models import MenuItem

    return MenuItem(code=code, name=code)


def test_error_mapping_401():
    client, _ = make_client(errors={"query-meals": Exception("HTTP 401 Unauthorized")})
    with client:
        with pytest.raises(MCPError) as ei:
            client.query_meals(SCENE)
    assert ei.value.code == 401
    assert "Token" in str(ei.value)


def test_error_mapping_429():
    client, _ = make_client(errors={"query-meals": Exception("429 Too Many Requests")})
    with client:
        with pytest.raises(MCPError) as ei:
            client.query_meals(SCENE)
    assert ei.value.code == 429


def test_error_mapping_generic():
    client, _ = make_client(errors={"query-meals": Exception("boom")})
    with client:
        with pytest.raises(MCPError) as ei:
            client.query_meals(SCENE)
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
