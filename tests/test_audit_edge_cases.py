"""深度审计回归测试（2026-10-16）。

只覆盖本次审计新发现的缺陷与加固点，不重复既有测试：
1. 价格发现坏商品隔离降级（传染失败修复 + 网络错误不拆分 + 调用上限）；
2. productList 异常形态：缺名称/缺小计/零价/负价/编码不匹配/quantity 归一；
3. 位置解析修复：「在X，到店自取」假阴性、「在X到店自取」吞用餐方式、
   「在X，想吃辣」从句、过捕获守卫。
"""
import sys
import types

import pytest

from burger_quest import nlparser
from burger_quest.mcp_base import MCPError, extract_product_prices
from burger_quest.mcp_http import HttpMCPClient
from burger_quest.service import (
    PRICE_DISCOVERY_MAX_CALLS,
    _discover_prices,
    _looks_like_item_error,
    _shortlist,
    run_quest,
)
from burger_quest.models import MenuItem
from test_http_client import fake_tools, wrap_api_text

STORES_RESP = {
    "success": True,
    "code": 200,
    "data": [
        {"storeCode": "1420762", "storeName": "测试门店", "address": "某路1号", "distance": 100, "businessStatus": True}
    ],
}
MEALS_RESP = {
    "success": True,
    "code": 200,
    "data": {
        "categories": [
            {"name": "汉堡", "meals": [{"code": "GOOD1", "tags": ["单品"]}, {"code": "BAD1", "tags": ["单品"]}]},
            {"name": "小食", "meals": [{"code": "GOOD2", "tags": ["单品"]}]},
        ]
    },
}


class _ScriptedSession:
    """按脚本响应的假会话：calc_script(items) -> payload 或 Exception。"""

    def __init__(self, calc_script, meals=None, stores=None):
        self.calc_script = calc_script
        self.meals = meals or MEALS_RESP
        self.stores = stores or STORES_RESP
        self.calls = []

    async def initialize(self):
        return None

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "query-nearby-stores":
            payload = self.stores
        elif name == "query-meals":
            payload = self.meals
        elif name == "calculate-price":
            codes = [i.get("productCode") for i in arguments.get("items", [])]
            result = self.calc_script(codes)
            if isinstance(result, Exception):
                raise result
            payload = result
        else:
            payload = {"success": True, "code": 200, "data": {}}
        return types.SimpleNamespace(content=[types.SimpleNamespace(text=wrap_api_text(payload))])

    async def list_tools(self):
        return types.SimpleNamespace(tools=fake_tools())


def _make_client(calc_script, meals=None):
    client = HttpMCPClient(token="test-token-not-real", timeout=10)
    session = _ScriptedSession(calc_script, meals=meals)

    async def fake_open(self):
        self._session = session

    client._open_session = types.MethodType(fake_open, client)
    client._check_sdk = types.MethodType(lambda self: None, client)
    client.connect()
    return client, session


def _ok_calc(codes):
    """正常算价：每件单价 2250。"""
    return {
        "success": True,
        "code": 200,
        "data": {
            "price": 2250 * len(codes),
            "originalPrice": 2250 * len(codes),
            "discount": 0,
            "productList": [
                {"productCode": c, "productName": f"餐品{c}", "quantity": 1, "subtotal": 2250, "originalSubtotal": 2250}
                for c in codes
            ],
        },
    }


def _bad_item_calc(codes):
    """含 BAD1 整单业务错误（官方可能的行为）。"""
    if "BAD1" in codes:
        return {"success": False, "code": 400, "message": "商品 BAD1 不存在或已下架", "data": None}
    return _ok_calc(codes)


# ---------------------------------------------------------------------------
# 1. 价格发现坏商品隔离降级
# ---------------------------------------------------------------------------


def test_bad_item_no_longer_kills_whole_task():
    """复现并固化：1 个坏商品不再废掉整个任务（2026-10-16 复现的问题）。"""
    client, session = _make_client(_bad_item_calc)
    r = run_quest("想吃汉堡，预算50，在深圳市南山区科技园，到店自取", client)
    assert r.chosen is not None, "坏商品被隔离后任务应成功"
    assert all("BAD1" not in pi.item.code for pi in r.chosen.items)
    assert any("已自动排查并排除" in w and "BAD1" in w for w in r.warnings)
    # 调用有界：全量 1 + 二分 2 + 逐个 1 = 4 次价格发现
    discovery = [c for c in session.calls if c[0] == "calculate-price" and c[1].get("_purpose") != "plan_verification"]
    assert len(discovery) <= PRICE_DISCOVERY_MAX_CALLS


def test_network_error_does_not_trigger_degradation():
    """网络/协议类错误不拆分降级（拆了也全败，浪费调用）——快速失败。"""
    def script(codes):
        return MCPError("calculate-price", "MCP 连接失败：网络不可达")

    client, session = _make_client(script)
    r = run_quest("想吃汉堡，预算50，在深圳市南山区科技园，到店自取", client)
    assert r.chosen is None
    assert any("价格查询失败" in w for w in r.warnings)
    calls = [c for c in session.calls if c[0] == "calculate-price"]
    assert len(calls) == 1, "网络错误应只调用 1 次（不拆分）"


def test_looks_like_item_error_classifier():
    assert _looks_like_item_error("商品 BAD1 不存在或已下架")
    assert _looks_like_item_error("product 9900 已停售")
    assert not _looks_like_item_error("MCP 连接失败：网络不可达")
    assert not _looks_like_item_error("")
    assert not _looks_like_item_error("MCP Token 无效或已过期（401）")


def test_all_items_bad_hits_cap_and_fails_honestly():
    """全部商品都坏：降级达到调用上限后诚实失败，不无限重试。"""
    def script(codes):
        return {"success": False, "code": 400, "message": "商品不存在", "data": None}

    client, session = _make_client(script)
    r = run_quest("想吃汉堡，预算50，在深圳市南山区科技园，到店自取", client)
    assert r.chosen is None
    calls = [c for c in session.calls if c[0] == "calculate-price"]
    assert len(calls) <= PRICE_DISCOVERY_MAX_CALLS + 3  # 发现上限 + 少量组合验价尝试
    assert any("没有获取到任何真实价格" in w or "价格查询失败" in w for w in r.warnings)


# ---------------------------------------------------------------------------
# 2. productList 异常形态
# ---------------------------------------------------------------------------


def _quote_from_product_list(product_list, price=2250):
    from burger_quest.mcp_base import adapt_price, extract_api_json

    data = {"price": price, "originalPrice": price, "discount": 0, "productList": product_list}
    return adapt_price(extract_api_json(wrap_api_text({"success": True, "code": 200, "data": data})))


def test_missing_productname_falls_back_to_detail():
    """缺 productName：价格可用，名称回退 query-meal-detail（不猜名称）。"""
    q = _quote_from_product_list([{"productCode": "A1", "quantity": 1, "subtotal": 2250}])
    prices = extract_product_prices(q)
    assert prices == {"A1": 2250}
    # 名称缺失由 service 回退详情（此处只验证提取层行为）
    from burger_quest.mcp_base import extract_product_names

    assert extract_product_names(q) == {}


def test_missing_subtotal_excluded():
    """缺 subtotal：不进入价格表（服务层按价格未知排除）。"""
    q = _quote_from_product_list([{"productCode": "A1", "productName": "堡", "quantity": 1}])
    assert extract_product_prices(q) == {}


def test_zero_and_negative_prices_excluded_in_service():
    """0 元/负价商品在服务层被排除（2026-10-16 复现：0 元会免费进组合）。"""
    def script(codes):
        pl = [
            {"productCode": "ZERO1", "productName": "零元堡", "quantity": 1, "subtotal": 0},
            {"productCode": "NEG1", "productName": "负数堡", "quantity": 1, "subtotal": -500},
            {"productCode": "GOOD1", "productName": "正常堡", "quantity": 1, "subtotal": 2250},
        ]
        return {"success": True, "code": 200, "data": {"price": 1750, "originalPrice": 2750, "discount": 1000, "productList": pl}}

    meals = {
        "success": True,
        "code": 200,
        "data": {"categories": [{"name": "汉堡", "meals": [
            {"code": "ZERO1", "tags": ["单品"]}, {"code": "NEG1", "tags": ["单品"]}, {"code": "GOOD1", "tags": ["单品"]}]}]},
    }
    client, _session = _make_client(script, meals=meals)
    r = run_quest("想吃汉堡，预算50，在深圳市南山区科技园，到店自取", client)
    assert r.chosen is not None
    codes = [pi.item.code for pi in r.chosen.items]
    assert "ZERO1" not in codes and "NEG1" not in codes
    assert "GOOD1" in codes
    assert any("非正价格" in w for w in r.warnings)


def test_code_mismatch_not_mis_mapped():
    """编码不匹配：价格挂在服务端返回的编码上，不误映射到请求的商品。"""
    q = _quote_from_product_list([{"productCode": "OTHER", "productName": "堡", "quantity": 1, "subtotal": 2250}])
    assert extract_product_prices(q) == {"OTHER": 2250}
    assert "GOOD1" not in extract_product_prices(q)


def test_quantity_normalized_to_unit_price():
    """quantity=2 的行小计归一为单价（防止把 2 件小计当单价高估）。"""
    q = _quote_from_product_list([{"productCode": "A1", "productName": "堡", "quantity": 2, "subtotal": 4500}])
    assert extract_product_prices(q) == {"A1": 2250}
    # quantity=0 除零保护
    q0 = _quote_from_product_list([{"productCode": "A1", "productName": "堡", "quantity": 0, "subtotal": 4500}])
    assert extract_product_prices(q0) == {"A1": 4500}  # 异常数量按原值，不除零崩溃


def test_server_dropped_item_excluded():
    """服务端丢弃商品：只对返回了价格与名称的商品出方案。"""
    def script(codes):
        # 请求 3 个，只返回 1 个
        return _ok_calc(["GOOD1"])

    meals = {
        "success": True,
        "code": 200,
        "data": {"categories": [{"name": "汉堡", "meals": [
            {"code": "GOOD1", "tags": ["单品"]}, {"code": "GONE1", "tags": ["单品"]}, {"code": "GONE2", "tags": ["单品"]}]}]},
    }
    client, _s = _make_client(script, meals=meals)
    r = run_quest("想吃汉堡，预算50，在深圳市南山区科技园，到店自取", client)
    assert r.chosen is not None
    assert all(pi.item.code == "GOOD1" for pi in r.chosen.items)


# ---------------------------------------------------------------------------
# 3. 位置解析修复
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expect",
    [
        ("在深圳市南山区科技园，到店自取", "深圳市南山区科技园"),  # 假阴性修复
        ("在深圳市南山区科技园到店自取", "深圳市南山区科技园"),  # 吞用餐方式修复
        ("在深圳市南山区科技园，想吃辣", "深圳市南山区科技园"),  # 从句
        ("预算35，在深圳市南山区科技园，想吃辣", "深圳市南山区科技园"),
        ("于中关村大街，堂食", "中关村大街"),
        ("在购物公园", "购物公园"),  # 原有行为
        ("在南山科技园吃", "南山科技园"),
        ("外送到南山科技园，一个人", "南山科技园"),
        ("我在想美食，去吃麦当劳", None),  # 过捕获守卫
        ("我在想，吃麦当劳", None),
    ],
)
def test_location_parsing_audit_fixes(text, expect):
    assert nlparser.parse(text).location == expect


def test_discover_prices_returns_excluded_codes():
    """_discover_prices 直接单测：坏商品编码出现在 excluded 中。"""
    from burger_quest.models import Constraints

    client, _s = _make_client(_bad_item_calc)
    scene = type("S", (), {"store_code": "1420762", "order_type": 1, "be_type": 1, "be_code": ""})()
    shortlist = [MenuItem(code="GOOD1", name="好堡", price_cents=None), MenuItem(code="BAD1", name="坏堡", price_cents=None)]

    class _R:
        warnings = []

    prices, names, excluded = _discover_prices(scene, client, shortlist, _R())
    assert prices.get("GOOD1") == 2250
    assert excluded == ("BAD1",)


# ---------------------------------------------------------------------------
# 4. 营养匹配：仅精确匹配（2026-10-16 审计移除了子串回退）
# ---------------------------------------------------------------------------


def test_nutrition_exact_match_only_no_combo_mislabel():
    """套餐不得匹配单品营养（「麦辣鸡腿堡套餐」≠「麦辣鸡腿堡」——实测误映射）。"""
    from burger_quest.models import NutritionItem
    from burger_quest.planner import nutrition_of

    nutri = [
        NutritionItem(name="麦辣鸡腿堡", calories_kcal=400, protein_g=18, fat_g=20),
        NutritionItem(name="巨无霸", calories_kcal=520, protein_g=27, fat_g=28),
    ]
    combo = MenuItem(code="C1", name="麦辣鸡腿堡套餐", price_cents=3200)
    single = MenuItem(code="S1", name="麦辣鸡腿堡", price_cents=2100)
    assert nutrition_of(combo, nutri) is None, "套餐匹配到单品营养=虚假结论"
    assert nutrition_of(single, nutri) is not None
    assert nutrition_of(single, nutri).calories_kcal == 400


def test_nutrition_unknown_not_faked():
    """匹配不到返回 None（上层标注「未知」，不给虚假低卡/高蛋白结论）。"""
    from burger_quest.models import NutritionItem
    from burger_quest.planner import nutrition_of

    nutri = [NutritionItem(name="巨无霸", calories_kcal=520, protein_g=27, fat_g=28)]
    assert nutrition_of(MenuItem(code="X", name="龙焰鸡腿堡三件套", price_cents=3900), nutri) is None
    assert nutrition_of(MenuItem(code="Y", name="巨无霸套餐", price_cents=3600), nutri) is None


def test_demo_low_cal_scenario_still_resolves_exact_nutrition():
    """低卡演示场景：精确匹配的营养仍正常汇总（不因收紧匹配而丢失真实数据）。"""
    from burger_quest.mcp_mock import MockMCPClient
    from burger_quest.service import run_quest

    r = run_quest("减肥中，想吃低卡的，预算40，到店自取", MockMCPClient())
    assert r.chosen is not None
    # 精确匹配汇总值（2026-10-18 短名单补配餐后推荐构成变化：麦香鱼+可乐 550）
    assert r.chosen.nutrition.get("calories_kcal") == 550.0
    assert r.chosen.nutrition_complete is True
