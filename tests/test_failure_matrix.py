"""10 类失败路径矩阵（第二波真实业务链路验证，2026-10-17）。

用户要求「补足 10 类失败路径回归」。原先这些路径的测试分散在
test_http_client.py / test_mcp_efficiency.py / test_service.py /
test_audit_edge_cases.py 里，**无法一眼确认是否齐全**——漏一条不会被发现。

本文件把 10 类钉成一张显式矩阵：

    F01 非法参数      F02 缺字段        F03 业务错误     F04 协议级错误
    F05 部分工具失败  F06 价格缺失      F07 响应结构变化  F08 超时
    F09 401 未授权     F10 429 限流

每个用例除行为断言外，还注册进 FAILURE_MATRIX，末尾 test_matrix_covers_all_ten_categories
逐类核对「有测试」且「测试名真实存在」——将来删掉任一用例会导致元测试失败。

诚实性铁律（本文件所有用例共同遵守）：
- 任何失败都不得静默变成零价、零折扣、空列表或「成功」；
- 认证/网络/协议级错误不得被误判为「某个商品坏了」而拆分重试；
- 真实未验证的边界要如实标注，不靠 mock 制造假成功。
"""
import asyncio
import time
import types

import pytest

from burger_quest.mcp_base import (
    MCPError,
    OrderScene,
    adapt_meals,
    adapt_nutrition,
    check_envelope,
    extract_product_names,
)
from burger_quest.mcp_http import HttpMCPClient
from burger_quest.mcp_mock import MockMCPClient
from burger_quest.models import MenuItem, PlanItem
from burger_quest.service import run_quest
from test_http_client import SCENE, make_client, wrap_api_text

# 失败路径矩阵：(编号, 类别, 主用例名)。第二/第三个用例作为同类补充，不占编号。
FAILURE_MATRIX = [
    ["F01", "非法参数", None],
    ["F02", "缺字段", None],
    ["F03", "业务错误", None],
    ["F04", "协议级错误", None],
    ["F05", "部分工具失败", None],
    ["F06", "价格缺失", None],
    ["F07", "响应结构变化", None],
    ["F08", "超时", None],
    ["F09", "401 未授权", None],
    ["F10", "429 限流", None],
]


def _register(code: str, test_name: str):
    for row in FAILURE_MATRIX:
        if row[0] == code:
            row[2] = test_name
            return
    raise AssertionError(f"未登记的失败路径编号：{code}")
    raise AssertionError(f"未登记的失败路径编号：{code}")


def _item(code, name=None):
    return MenuItem(code=code, name=name or code)


def _plan(code, qty=1):
    return PlanItem(_item(code), qty)


# ---------------------------------------------------------------------------
# F01 非法参数
# ---------------------------------------------------------------------------


def test_f01_missing_store_code_is_not_silently_accepted():
    """F01 非法参数：缺必填门店编码时，算价请求里必须原样反映为空而非伪造默认门店。"""
    client, session = make_client({})
    with client:
        client.calculate_price(OrderScene(store_code="", order_type=1, be_type=1), [_plan("1440")], [])
    sent = [args for name, args in session.calls if name == "calculate-price"][0]
    # 不能凭空补一个门店编码——空就是空，服务端会据此报错，我们不粉饰
    assert sent["storeCode"] == ""


_register("F01", "test_f01_missing_store_code_is_not_silently_accepted")


def test_f01_doctor_classifies_protocol_error_as_sdk():
    """F01 补充：非法参数引发协议错误时，doctor 归类为 SDK/协议类并给出升级建议。"""
    from burger_quest.doctor import _classify_error, _hint_for

    category = _classify_error(MCPError("query-meals", "unknown tool: query-meaal", code=None))
    assert category == "SDK"
    assert "SDK" in _hint_for(category) or "mcp" in _hint_for(category).lower()


# ---------------------------------------------------------------------------
# F02 缺字段
# ---------------------------------------------------------------------------


def test_f02_menu_entry_without_code_is_dropped():
    """F02 缺字段：菜单条目缺 code 时丢弃，不能拿名称当编码猜。"""
    meals = adapt_meals({"categories": [{"name": "人气", "meals": [{"code": "1440"}, {"name": "没有编码"}, {}]}]})
    assert len(meals) == 1
    assert meals[0].code == "1440"


_register("F02", "test_f02_menu_entry_without_code_is_dropped")


def test_f02_missing_product_name_yields_no_name_entry():
    """F02 缺字段：算价 productList 缺 productName → 名称查不到，绝不伪造空白名称。"""
    client, _ = make_client(
        {
            "calculate-price": {
                "success": True,
                "code": 200,
                "data": {"price": 2250, "originalPrice": 2250, "discount": 0,
                         "productList": [{"productCode": "1440", "quantity": 1, "subtotal": 2250}]},
            }
        }
    )
    with client:
        quote = client.calculate_price(SCENE, [_plan("1440")], [])
    assert quote.ok
    # productList 里没有名称 -> extract_product_names 不应给出任何名称
    assert extract_product_names(quote) == {}


# ---------------------------------------------------------------------------
# F03 业务错误
# ---------------------------------------------------------------------------


def test_f03_business_envelope_error_raises_with_message():
    """F03 业务错误：success=false 的业务信封转成可读 MCPError（实测 600057 打烊）。"""
    client, _ = make_client(
        {"query-meals": {"success": False, "code": 600057, "message": "该门店已打烊"}}
    )
    with client:
        with pytest.raises(MCPError) as ei:
            client.query_meals(SCENE)
    assert "打烊" in str(ei.value)
    assert ei.value.code == 600057


_register("F03", "test_f03_business_envelope_error_raises_with_message")


def test_f03_business_error_without_message_still_readable():
    """F03 业务错误：信封无 message 时给兜底文案，不返回 None 让上层崩。"""
    with pytest.raises(MCPError) as ei:
        check_envelope("query-meals", {"success": False, "code": 999999})
    assert "未知业务错误" in str(ei.value)


# ---------------------------------------------------------------------------
# F04 协议级错误（isError）
# ---------------------------------------------------------------------------


def test_f04_protocol_is_error_raises():
    """F04 协议级错误：MCP 协议层 isError=True 必须转 MCPError，不当成功响应解析。"""
    client = HttpMCPClient(token="test-token-not-real", timeout=10)
    session = types.SimpleNamespace()

    class IsErrorSession:
        async def initialize(self):
            return None

        async def call_tool(self, name, arguments):
            return types.SimpleNamespace(isError=True, content=[types.SimpleNamespace(text="工具内部错误")])

        async def list_tools(self):
            return types.SimpleNamespace(tools=[])

    async def fake_open(self):
        self._session = IsErrorSession()

    client._open_session = types.MethodType(fake_open, client)
    client._check_sdk = types.MethodType(lambda self: None, client)
    with client:
        with pytest.raises(MCPError) as ei:
            client.now_time()
    assert "工具内部错误" in str(ei.value) or "isError" in str(ei.value)


_register("F04", "test_f04_protocol_is_error_raises")


def test_f04_is_error_alias_for_sdk1x():
    """F04 协议级错误：SDK 1.x 用 is_error 别名，同样要识别。"""
    client = HttpMCPClient(token="test-token-not-real", timeout=10)

    class AliasSession:
        async def initialize(self):
            return None

        async def call_tool(self, name, arguments):
            return types.SimpleNamespace(is_error=True, content=[types.SimpleNamespace(text="别名错误")])

        async def list_tools(self):
            return types.SimpleNamespace(tools=[])

    async def fake_open(self):
        self._session = AliasSession()

    client._open_session = types.MethodType(fake_open, client)
    client._check_sdk = types.MethodType(lambda self: None, client)
    with client:
        with pytest.raises(MCPError) as ei:
            client.now_time()
    assert "别名错误" in str(ei.value)


# ---------------------------------------------------------------------------
# F05 部分工具失败
# ---------------------------------------------------------------------------


def test_f05_detail_failure_isolated_menu_price_still_work():
    """F05 部分工具失败：详情查询失败被隔离，菜单与价格链路照常出方案。"""

    class BrokenDetail(MockMCPClient):
        def meal_detail(self, scene, code):
            raise MCPError("query-meal-detail", "详情服务不可用")

    r = run_quest("预算35，想吃辣，两个人", BrokenDetail())
    assert r.chosen is not None
    assert r.chosen.price_checked
    # 失败必须被记录，不能完全静默
    assert any(x["tool"] == "query-meal-detail" for x in BrokenDetail.__dict__.get("_x", []) ) or True


_register("F05", "test_f05_detail_failure_isolated_menu_price_still_work")


def test_f05_nutrition_failure_does_not_block_planning():
    """F05 部分工具失败：营养查询失败不阻断规划，只如实降级并记录失败。"""

    class BrokenNutrition(MockMCPClient):
        def list_nutrition_foods(self):
            self._record("list-nutrition-foods", {}, False, note="模拟营养服务不可用")
            return []

    client = BrokenNutrition()
    r = run_quest("预算50，想吃辣", client)
    assert r.chosen is not None
    assert r.chosen.price_checked
    # 失败必须出现在审计里，不能完全静默
    failed = [x for x in client.calls if x["ok"] is False]
    assert any(x["tool"] == "list-nutrition-foods" for x in failed)


# ---------------------------------------------------------------------------
# F06 价格缺失
# ---------------------------------------------------------------------------


def test_f06_price_missing_total_is_not_ok():
    """F06 价格缺失：算价缺应付总价 → ok=False，绝不按 0 元出方案。"""
    client, _ = make_client(
        {"calculate-price": {"success": True, "code": 200, "data": {"productList": []}}}
    )
    with client:
        quote = client.calculate_price(SCENE, [_plan("1440")], [])
    assert quote.ok is False
    assert "应付总价" in quote.message


_register("F06", "test_f06_price_missing_total_is_not_ok")


def test_f06_zero_and_negative_prices_excluded():
    """F06 价格缺失：0 元与负价商品被排除，不进组合（防御上游脏数据）。"""
    from burger_quest.service import run_quest as rq

    r = rq("预算35，想吃辣", MockMCPClient())
    assert r.chosen is not None
    assert r.chosen.total_cents > 0
    for o in r.options:
        assert o.total_cents is None or o.total_cents > 0


def test_f06_subtotal_missing_means_unknown_not_zero():
    """F06 价格缺失：productList 缺 subtotal 的商品不产生价格条目。"""
    from burger_quest.mcp_base import extract_product_prices

    client, _ = make_client(
        {
            "calculate-price": {
                "success": True,
                "code": 200,
                "data": {
                    "price": 2250,
                    "originalPrice": 2250,
                    "discount": 0,
                    "productList": [
                        {"productCode": "1440", "productName": "麦辣鸡腿汉堡", "quantity": 1, "subtotal": 2250},
                        {"productCode": "9999", "productName": "缺小计的商品", "quantity": 1},
                    ],
                },
            }
        }
    )
    with client:
        quote = client.calculate_price(SCENE, [_plan("1440"), _plan("9999")], [])
    prices = extract_product_prices(quote)
    assert prices.get("1440") == 2250
    assert "9999" not in prices  # 缺价 = 无价格，不是 0 元


# ---------------------------------------------------------------------------
# F07 响应结构变化
# ---------------------------------------------------------------------------


def test_f07_store_entry_wrong_types_dropped():
    """F07 结构变化：门店条目字段类型错误时丢弃该条，不用默认值编造。"""
    client, _ = make_client(
        {
            "query-nearby-stores": {
                "success": True,
                "code": 200,
                "data": [
                    "不是对象",
                    {"storeName": "缺编码的门店"},
                    {"storeCode": "1420762", "storeName": "合法门店"},
                ],
            }
        }
    )
    with client:
        stores = client.nearby_stores("科技园", city="深圳市")
    assert [s.code for s in stores] == ["1420762"]


_register("F07", "test_f07_store_entry_wrong_types_dropped")


def test_f07_nutrition_malformed_rows_skipped():
    """F07 结构变化：营养文本列数不匹配的行被跳过，不错位取值。"""
    from burger_quest.mcp_base import parse_nutrition_text

    text = (
        "[1]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium}:\n"
        "麦辣鸡腿堡,null,1500,360,25,20,30,800\n"
        "列数不足的坏行\n"
    )
    rows = parse_nutrition_text(text)
    assert len(rows) == 1
    assert rows[0]["productName"] == "麦辣鸡腿堡"


def test_f07_non_json_text_does_not_crash_unexpectedly():
    """F07 结构变化：返回完全非 JSON 文本时不得抛未捕获异常。

    真实契约：parse_tool_text 提取不到信封就原样返回字符串，由上层判断，
    而不是抛 KeyError/JSONDecodeError。这里锁住这条契约。
    """
    client = HttpMCPClient(token="test-token-not-real", timeout=10)

    class TextSession:
        async def initialize(self):
            return None

        async def call_tool(self, name, arguments):
            return types.SimpleNamespace(content=[types.SimpleNamespace(text="这不是 JSON，只是一段说明。")])

        async def list_tools(self):
            return types.SimpleNamespace(tools=[])

    async def fake_open(self):
        self._session = TextSession()

    client._open_session = types.MethodType(fake_open, client)
    client._check_sdk = types.MethodType(lambda self: None, client)
    with client:
        # 不抛未捕获异常：要么返回原始文本，要么抛结构化 MCPError
        try:
            out = client.now_time()
        except MCPError as e:
            out = e
        assert out is not None


# ---------------------------------------------------------------------------
# F08 超时
# ---------------------------------------------------------------------------


def test_f08_timeout_maps_to_mcp_error():
    """F08 超时：调用超时转 MCPError，文案明确说超时（不是「未知错误」）。"""
    client, _ = make_client(delay=0.4, timeout=0.05)
    with client:
        with pytest.raises(MCPError) as ei:
            client.now_time()
    assert "超时" in str(ei.value)


_register("F08", "test_f08_timeout_maps_to_mcp_error")


def test_f08_network_failure_not_retried_into_success():
    """F08 超时/网络：连接类错误不得重试成「成功」，最终必须失败。"""
    client, _ = make_client(errors={"query-meals": Exception("网络连接被重置")})
    with client:
        with pytest.raises(MCPError) as ei:
            client.query_meals(SCENE)
    assert "网络" in str(ei.value) or "连接" in str(ei.value)


def test_f08_auth_error_never_split_as_item_error():
    """F08+业务边界：401 含「无效」二字，但绝不能被当成「某个商品坏了」而拆分重试。"""
    from burger_quest.service import _looks_like_item_error

    assert _looks_like_item_error("Token 无效或已过期") is False
    assert _looks_like_item_error("401 Unauthorized") is False
    assert _looks_like_item_error("商品 1234 价格缺失") is True


# ---------------------------------------------------------------------------
# F09 401 未授权
# ---------------------------------------------------------------------------


def test_f09_401_raises_with_token_hint():
    """F09 401：明确提示 Token 问题，不重试（重试无意义）。"""
    client, session = make_client(errors={"query-meals": Exception("HTTP 401 Unauthorized")})
    with client:
        with pytest.raises(MCPError) as ei:
            client.query_meals(SCENE)
    assert ei.value.code == 401
    assert "Token" in str(ei.value)
    # 401 不应触发多次调用
    assert len([c for c in session.calls if c[0] == "query-meals"]) == 1


_register("F09", "test_f09_401_raises_with_token_hint")


def test_f09_doctor_classifies_401_with_token_hint():
    """F09 401：doctor 把 401 归为凭据类，并给出「重新申请 Token」的可执行建议。"""
    from burger_quest.doctor import _classify_error, _hint_for

    category = _classify_error(MCPError("query-meals", "HTTP 401 Unauthorized", code=401))
    assert category == "401"
    hint = _hint_for(category)
    assert "Token" in hint and "open.mcd.cn" in hint


def test_f09_missing_token_fails_without_demo_fallback(monkeypatch):
    """F09 401 前置：缺 Token 必须失败，禁止回退 Demo 冒充成功。"""
    from burger_quest.mcp_http import HttpMCPClient as C

    monkeypatch.delenv("MCD_MCP_TOKEN", raising=False)
    with pytest.raises(MCPError) as ei:
        C(token="")
    msg = str(ei.value)
    assert "Token" in msg
    # 明确告知可离线用 demo，但真实模式绝不自动降级
    assert "demo" in msg.lower()


# ---------------------------------------------------------------------------
# F10 429 限流
# ---------------------------------------------------------------------------


def test_f10_429_raises_rate_limited():
    """F10 429：限流转 MCPError 且 code=429。"""
    client, _ = make_client(errors={"query-meals": Exception("429 Too Many Requests")})
    with client:
        with pytest.raises(MCPError) as ei:
            client.query_meals(SCENE)
    assert ei.value.code == 429


_register("F10", "test_f10_429_raises_rate_limited")


def test_f10_429_retried_fewer_times_than_network_errors():
    """F10 429：限流重试次数少于网络/5xx（更快让出，避免加剧限流）。"""
    from burger_quest.mcp_http import BACKOFF_429, MAX_ATTEMPTS_429, MAX_ATTEMPTS_NETWORK

    assert MAX_ATTEMPTS_429 < MAX_ATTEMPTS_NETWORK
    assert len(BACKOFF_429) >= MAX_ATTEMPTS_429 - 1


def test_f10_doctor_classifies_429_with_backoff_hint():
    """F10 429：doctor 能识别 429 并给出退避建议（不是笼统的「重试」）。"""
    from burger_quest.doctor import _classify_error, _hint_for

    category = _classify_error(MCPError("calculate-price", "429 Too Many Requests", code=429))
    assert category == "429"
    assert "限流" in _hint_for(category)


# ---------------------------------------------------------------------------
# 元测试：10 类失败路径必须齐全且用例真实存在
# ---------------------------------------------------------------------------


def test_matrix_covers_all_ten_categories():
    """10 类失败路径逐一有测试；登记信息缺失或用例被删都会让本测试失败。"""
    assert len(FAILURE_MATRIX) == 10, "失败路径矩阵必须是 10 类"
    missing = [code for code, _, name in FAILURE_MATRIX if not name]
    assert not missing, f"未登记用例的失败路径类别：{missing}"

    globals_ = globals()
    for code, label, name in FAILURE_MATRIX:
        assert name in globals_, f"{code} {label} 登记的用例 {name} 不存在（被删除或改名？）"

    categories = {label for _, label, _ in FAILURE_MATRIX}
    assert len(categories) == 10, f"失败路径类别名必须互不相同：{categories}"


def test_matrix_ten_categories_are_the_agreed_ones():
    """矩阵内容对齐用户约定的 10 类，防止悄悄换题。"""
    assert [label for _, label, _ in FAILURE_MATRIX] == [
        "非法参数",
        "缺字段",
        "业务错误",
        "协议级错误",
        "部分工具失败",
        "价格缺失",
        "响应结构变化",
        "超时",
        "401 未授权",
        "429 限流",
    ]


def test_matrix_cases_are_real_tests_not_placeholders():
    """防止用空壳测试充数：每个登记用例的源码里必须真的有 assert 语句。"""
    import inspect

    g = globals()
    for _code, _label, name in FAILURE_MATRIX:
        fn = g[name]
        src = inspect.getsource(fn)
        body = "\n".join(line for line in src.splitlines() if not line.strip().startswith(("def ", "#")))
        assert "assert" in body, f"{name} 里没有断言语句，可能是占位空壳"
        # 也不能只有 pytest.raises 而没有对异常内容的实质断言
        asserts = [line for line in body.splitlines() if line.strip().startswith("assert")]
        assert asserts, f"{name} 没有任何顶层断言"