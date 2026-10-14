"""真实 MCP 客户端（同步门面）测试。

不访问网络、不依赖 mcp SDK：用假会话（FakeSession）替换 _open_session，
假会话按**真实联调实测的文本信封格式**返回（字段说明 + JSON + 传参规则），
验证「同步接口 -> 后台事件循环 -> 信封提取 -> 防御式适配」整条链路，
以及 Token 缺失、401/429、超时、SDK 缺失、业务错误、协议级 isError、
非法返回结构（非 JSON 文本/空 content/错误类型字段）等路径。

2026-10-13 阶段 5 补充：SDK 2.3.0 复核（list_tools 返回 ListToolsResult）、
协议级错误通道、金额单位防御（整数=分；带小数=元）。
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


def tool_names() -> list:
    """联调实测：项目使用的 8 个工具（冒烟/工具发现测试用）。"""
    from burger_quest.mcp_base import TOOLS_USED

    return list(TOOLS_USED)


def fake_tools(names=None) -> list:
    return [
        types.SimpleNamespace(name=n, description="（测试用）", input_schema={"type": "object"})
        for n in (names or tool_names())
    ]


class FakeSession:
    """假 MCP 会话：按工具名返回预制 payload（包装成真实文本格式）。

    - responses: {tool: payload dict} -> 包装成文本信封返回；
    - raw_results: {tool: 任意对象} -> 原样返回（用于构造非法/异常结构）；
    - errors: {tool: Exception} -> 抛出；
    - tools_shape: list_tools 返回形态（"result"=SDK2.x， "list"=SDK1.x）；
    - delay/timeout: 超时路径。
    """

    def __init__(self, responses=None, errors=None, delay=0.0, raw_results=None, tools_shape="result"):
        self.responses = responses or {}
        self.errors = errors or {}
        self.raw_results = raw_results or {}
        self.tools_shape = tools_shape
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
        if name in self.raw_results:
            return self.raw_results[name]
        payload = self.responses.get(name, {"success": True, "code": 200, "data": {}})
        return types.SimpleNamespace(content=[types.SimpleNamespace(text=wrap_api_text(payload))])

    async def list_tools(self):
        self.calls.append(("list_tools", {}))
        tools = fake_tools()
        if self.tools_shape == "result":  # SDK 2.x：ListToolsResult(.tools)
            return types.SimpleNamespace(tools=tools)
        return tools  # SDK 1.x：裸 list


def make_client(responses=None, errors=None, delay=0.0, timeout=60.0, raw_results=None, tools_shape="result"):
    """构造一个带假会话的 HttpMCPClient（不发起真实网络连接）。"""
    client = HttpMCPClient(token="test-token-not-real", timeout=timeout)
    session = FakeSession(responses, errors, delay, raw_results, tools_shape)

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


# ---------------------------------------------------------------- 阶段 5 补充
# 非法 / 异常返回结构：解析路径必须「明确失败或明确为空」，
# 绝不允许「未知格式猜一个值」后静默放过。


def test_is_error_response_raises_mcp_error():
    """协议级 isError=True（SDK 2.x 字段名）必须转 MCPError，不能流入适配器。"""
    client, _ = make_client(
        raw_results={
            "query-meals": types.SimpleNamespace(
                is_error=True,
                content=[types.SimpleNamespace(text="Unknown tool: query-meals")],
            )
        }
    )
    with client:
        with pytest.raises(MCPError) as ei:
            client.query_meals(SCENE)
    assert "协议错误" in str(ei.value)
    assert "Unknown tool" in str(ei.value)
    # 审计记录必须留下失败痕迹
    assert any(c["ok"] is False and c.get("note") == "isError" for c in client.calls)


def test_is_error_alias_isError_sdk1x():
    """SDK 1.x 的字段名 isError 同样必须被接住。"""
    client, _ = make_client(
        raw_results={
            "query-meals": types.SimpleNamespace(
                isError=True,
                content=[types.SimpleNamespace(text="denied")],
            )
        }
    )
    with client:
        with pytest.raises(MCPError) as ei:
            client.query_meals(SCENE)
    assert "denied" in str(ei.value)


def test_is_error_without_text_still_fails():
    """isError=True 但 content 无文本：也必须失败（不允许当成功放过）。"""
    client, _ = make_client(
        raw_results={"query-meals": types.SimpleNamespace(is_error=True, content=[])}
    )
    with client:
        with pytest.raises(MCPError) as ei:
            client.query_meals(SCENE)
    assert "协议错误" in str(ei.value)


def test_non_json_text_is_graceful_not_guessed():
    """content 是不含信封的纯文本：不猜值，原样交给适配器得到空结果。"""
    client, _ = make_client(
        raw_results={
            "now-time-info": types.SimpleNamespace(
                content=[types.SimpleNamespace(text="服务暂时不可用（无 JSON 信封）")]
            )
        }
    )
    with client:
        t = client.now_time()
    assert t == {"datetime": "", "time_period": ""}  # 空，而不是编造的时间


def test_empty_content_list_is_graceful():
    """content 为空列表：不崩溃、不编造。"""
    client, _ = make_client(raw_results={"now-time-info": types.SimpleNamespace(content=[])})
    with client:
        t = client.now_time()
    assert t["datetime"] == ""


def test_plain_string_content_is_parsed():
    """content 直接是字符串（部分 SDK/代理形态）：按文本解析。"""
    client, _ = make_client(
        raw_results={"now-time-info": types.SimpleNamespace(content=wrap_api_text(NOW_TIME_RESP))}
    )
    with client:
        assert client.now_time()["datetime"] == "2026-10-13 20:34:27"


def test_price_yuan_defense_when_decimal():
    """算价金额单位防御：带小数的数值/字符串按「元」解释（防单位变更 100 倍误差）。"""
    for raw_price, expected in (("22.50", 2250), (22.5, 2250)):
        client, _ = make_client(
            {
                "calculate-price": {
                    "success": True,
                    "code": 200,
                    "data": {"price": raw_price, "originalPrice": raw_price, "discount": 0},
                }
            }
        )
        with client:
            quote = client.calculate_price(SCENE, [PlanItem(_item("1440"), 1)], [])
        assert quote.ok and quote.total_cents == expected, raw_price


def test_price_int_string_stays_cents():
    """整数字符串按「分」解释（官方单位，联调实测）。"""
    client, _ = make_client(
        {
            "calculate-price": {
                "success": True,
                "code": 200,
                "data": {"price": "2250", "originalPrice": "2250", "discount": "0"},
            }
        }
    )
    with client:
        quote = client.calculate_price(SCENE, [PlanItem(_item("1440"), 1)], [])
    assert quote.ok and quote.total_cents == 2250


def test_calculate_price_missing_total_is_not_ok():
    """算价未返回应付总价：ok=False（服务层据此停止出方案，不猜价格）。"""
    client, _ = make_client(
        {"calculate-price": {"success": True, "code": 200, "data": {"productList": []}}}
    )
    with client:
        quote = client.calculate_price(SCENE, [PlanItem(_item("1440"), 1)], [])
    assert quote.ok is False
    assert "应付总价" in quote.message


def test_envelope_without_data_returns_payload():
    """信封没有 data 键：返回信封本体（由适配器决定能否用），不抛异常。"""
    from burger_quest.mcp_base import check_envelope

    out = check_envelope("t", {"success": True, "code": 200, "message": "ok"})
    assert out == {"success": True, "code": 200, "message": "ok"}


def test_business_error_without_message():
    """success=false 且无 message：给出可读的兜底文案（不是静默 None）。"""
    from burger_quest.mcp_base import check_envelope

    with pytest.raises(MCPError) as ei:
        check_envelope("query-meals", {"success": False, "code": 500})
    assert "未知业务错误" in str(ei.value)


def test_wrong_field_types_dropped_not_guessed():
    """返回结构字段类型错误：丢弃该条，不用默认值编造。"""
    client, _ = make_client(
        {
            "query-nearby-stores": {
                "success": True,
                "code": 200,
                "data": [
                    "不是对象",
                    {"storeName": "缺编码的门店"},  # 无 storeCode -> 丢弃
                    {"storeCode": "1420762", "storeName": "合法门店"},
                ],
            }
        }
    )
    with client:
        stores = client.nearby_stores("科技园", city="深圳市")
    assert [s.code for s in stores] == ["1420762"]


def test_meals_entries_without_code_dropped():
    """菜单骨架里缺 code 的条目直接丢弃，不生成占位餐品。"""
    client, _ = make_client(
        {
            "query-meals": {
                "success": True,
                "code": 200,
                "data": {
                    "categories": [
                        {"name": "主食", "meals": [{"tags": ["单品"]}, {"code": "1440", "tags": ["单品"]}]}
                    ]
                },
            }
        }
    )
    with client:
        menu = client.query_meals(SCENE)
    assert [m.code for m in menu] == ["1440"]


def test_list_tools_sdk2_result_shape():
    """SDK 2.x：list_tools() 返回 ListToolsResult（.tools 属性）。"""
    client, session = make_client(tools_shape="result")
    with client:
        tools = client.list_tools()
    assert len(tools) == len(tool_names())
    assert tools[0]["name"] == tool_names()[0]
    assert ("list_tools", {}) in session.calls


def test_list_tools_sdk1_list_shape():
    """SDK 1.x：list_tools() 返回裸 list（兼容旧版）。"""
    client, _ = make_client(tools_shape="list")
    with client:
        tools = client.list_tools()
    assert [t["name"] for t in tools] == tool_names()


def test_nutrition_malformed_rows_skipped():
    """营养文本行列数与表头不符：跳过该行，不用 0 编造。"""
    client, _ = make_client(
        {
            "list-nutrition-foods": {
                "success": True,
                "code": 200,
                "data": "[2]{productName,energyKcal}:\n  麦辣鸡腿汉堡,560\n  坏行,多了一列,560",
            }
        }
    )
    with client:
        nutrition = client.list_nutrition_foods()
    assert [n.name for n in nutrition] == ["麦辣鸡腿汉堡"]
    assert nutrition[0].calories_kcal == 560.0
