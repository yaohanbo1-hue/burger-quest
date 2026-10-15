"""门店解析与场景流程测试（2026-10-13 联调后的官方工具链）。

核心规则：
- 到店场景用 query-nearby-stores（city/keyword）；外送场景走
  delivery-query-addresses -> delivery-query-stores 拿 beCode；
- 多候选门店全部列出，不编造、不默默选择；
- 真实模式没有位置信息 -> 明确要求补充，不发起任何调用；
- 菜单只有编码；名称优先取自行算价的 productList（2026-10-14 效率优化），
  productList 缺名称时才回退 query-meal-detail；价格只来自 calculate-price（分）。
"""
import dataclasses

import pytest

from burger_quest.mcp_base import TOOLS_USED, MCPClient, MCPError
from burger_quest.mcp_mock import MockMCPClient
from burger_quest.models import MenuItem, PriceQuote, Store
from burger_quest.service import run_quest

STUB_SKELETON = [
    MenuItem(code="M1", name="", category="鸡肉汉堡/卷", price_cents=None, tags=("辣",)),
    MenuItem(code="S1", name="", category="小食", price_cents=None, tags=("酥脆",)),
    MenuItem(code="R1", name="", category="饮品", price_cents=None, tags=()),
]
STUB_DETAILS = {
    "M1": {"name": "麦辣鸡腿堡", "price_cents": 2100},
    "S1": {"name": "薯条（大）", "price_cents": 1250},
    "R1": {"name": "可口可乐（大）", "price_cents": 1000},
}


class StubLiveClient(MCPClient):
    """模拟真实客户端（不是 MockMCPClient）：行为可控，遵循官方工具链语义。"""

    def __init__(self, stores=(), menus=None, stores_error=None, meals_error=None,
                 addresses=(), delivery_stores_map=None, delivery_error=None):
        super().__init__()
        self.stores = list(stores)
        self.menus = menus or {}
        self.stores_error = stores_error
        self.meals_error = meals_error
        self.addresses = list(addresses)
        self.delivery_stores_map = delivery_stores_map or {}
        self.delivery_error = delivery_error
        self.calls = []

    # -- 官方工具链 -----------------------------------------------------------

    def now_time(self):
        return {"datetime": "2026-10-13 20:00:00", "time_period": "dinner"}

    def list_tools(self):
        self.calls.append(("list_tools",))
        return [{"name": t, "description": "", "input_schema": None} for t in TOOLS_USED]

    def nearby_stores(self, keyword, city="", be_type=1):
        self.calls.append(("query-nearby-stores", keyword, city, be_type))
        if self.stores_error:
            raise MCPError("query-nearby-stores", self.stores_error)
        return list(self.stores)

    def delivery_addresses(self):
        self.calls.append(("delivery-query-addresses",))
        if self.delivery_error:
            raise MCPError("delivery-query-addresses", self.delivery_error)
        return list(self.addresses)

    def delivery_stores(self, address_id, be_type=2):
        self.calls.append(("delivery-query-stores", address_id, be_type))
        return list(self.delivery_stores_map.get(str(address_id), []))

    def query_meals(self, scene):
        self.calls.append(("query-meals", scene.store_code))
        if self.meals_error:
            raise MCPError("query-meals", self.meals_error)
        return list(self.menus.get(scene.store_code, []))

    def meal_detail(self, scene, code):
        self.calls.append(("query-meal-detail", code))
        d = STUB_DETAILS.get(code)
        if not d:
            raise MCPError("query-meal-detail", f"餐品 {code} 不存在", code=404)
        return {"code": code, "name": d["name"], "rounds": [], "is_combo": False}

    def store_coupons(self, scene):
        return []

    def my_coupons(self):
        return []

    def list_nutrition_foods(self):
        return []

    def calculate_price(self, scene, items, coupon_ids, purpose=""):
        self.calls.append(("calculate-price", tuple(pi.item.code for pi in items)))
        original = sum(STUB_DETAILS[pi.item.code]["price_cents"] * pi.qty for pi in items)
        fee = 900 if scene.order_type == 2 else 0
        product_list = [
            {
                "productCode": pi.item.code,
                "productName": STUB_DETAILS[pi.item.code]["name"],
                "quantity": pi.qty,
                "subtotal": STUB_DETAILS[pi.item.code]["price_cents"] * pi.qty,
            }
            for pi in items
        ]
        return PriceQuote(
            total_cents=original + fee,
            original_cents=original,
            discount_cents=0,
            delivery_fee_cents=fee,
            ok=True,
            raw={"productList": product_list},
        )


# ---------------------------------------------------------------- 到店场景


def test_real_mode_requires_location():
    client = StubLiveClient()
    r = run_quest("想吃辣，预算35", client)  # 无位置、无编码
    assert r.chosen is None
    assert any("未提供位置信息" in w for w in r.warnings)
    assert client.calls == []  # 没有发起任何 MCP 调用（不猜门店）


def test_real_mode_multiple_candidates_not_silently_picked():
    client = StubLiveClient(
        stores=[
            Store(code="A-1", name="麦当劳（甲店）"),
            Store(code="A-2", name="麦当劳（乙店）"),
            Store(code="A-3", name="麦当劳（丙店）"),
        ]
    )
    r = run_quest("在测试路吃，到店自取，预算35", client)
    assert r.chosen is None
    assert [s.code for s in r.store_candidates] == ["A-1", "A-2", "A-3"]
    assert any("候选门店" in w for w in r.warnings)
    # 没有悄悄查菜单/选店
    assert not any(c[0] == "query-meals" for c in client.calls)


def test_real_mode_nearby_empty():
    client = StubLiveClient(stores=[])
    r = run_quest("在火星基地吃，到店自取，预算35", client)
    assert r.chosen is None
    assert any("未在「火星基地」找到附近门店" in w for w in r.warnings)
    assert any("未确定门店" in w for w in r.warnings)


def test_real_mode_nearby_error():
    client = StubLiveClient(stores_error="模拟 MCP 无响应")
    r = run_quest("在测试路吃，到店自取，预算35", client)
    assert r.chosen is None
    assert any("附近门店查询失败" in w for w in r.warnings)


def test_real_mode_invalid_store_code():
    client = StubLiveClient(meals_error="门店不存在或已停售")
    r = run_quest("预算35", client, store_code="BAD-999")
    assert r.chosen is None
    assert any("BAD-999" in w and "查询失败" in w for w in r.warnings)


def test_real_mode_empty_menu_for_valid_store():
    client = StubLiveClient(stores=[Store(code="A-1", name="麦当劳（甲店）")], menus={"A-1": []})
    r = run_quest("在测试路吃，到店自取，预算35", client)
    assert r.chosen is None
    assert any("未返回可售餐品" in w for w in r.warnings)


def test_real_mode_single_store_full_flow():
    """完整官方工具链：附近门店 -> 菜单骨架 -> 算价探测（名称+单价）-> 组合验价。

    2026-10-14 效率优化后：名称与单价来自同一次 calculate-price 的 productList
    （官方权威返回），query-meal-detail 只在 productList 缺名称时回退调用。
    """
    client = StubLiveClient(
        stores=[Store(code="A-1", name="麦当劳（甲店）")],
        menus={"A-1": list(STUB_SKELETON)},
    )
    r = run_quest("在测试路吃，到店自取，预算50，想吃辣", client)
    assert r.chosen is not None
    assert r.store.code == "A-1"
    assert r.chosen.price_checked is True
    assert r.chosen.total_cents is not None
    tools = [c[0] for c in client.calls]
    assert "query-nearby-stores" in tools
    assert "query-meals" in tools
    assert "calculate-price" in tools
    # 价格发现：一次调用传入全部短名单商品（拿名称+单价）
    discovery = [c for c in client.calls if c[0] == "calculate-price"]
    assert any(len(c[1]) == len(STUB_SKELETON) for c in discovery)
    # 名称来自 productList（Stub 的算价返回自带 productName）-> 无需详情调用
    assert "query-meal-detail" not in tools
    assert r.chosen.items and all(pi.item.name for pi in r.chosen.items)


def test_explicit_store_code_queries_menu_once():
    client = StubLiveClient(menus={"A-1": list(STUB_SKELETON)})
    r = run_quest("预算50，想吃辣", client, store_code="A-1")
    assert r.chosen is not None
    meal_calls = [c for c in client.calls if c[0] == "query-meals"]
    assert len(meal_calls) == 1  # 恰好一次


def test_detail_fallback_when_product_list_has_no_names():
    """productList 不含名称时，回退 query-meal-detail 补名（不猜名称）。"""

    class NoNamePriceClient(StubLiveClient):
        def calculate_price(self, scene, items, coupon_ids, purpose=""):
            quote = super().calculate_price(scene, items, coupon_ids, purpose=purpose)
            # 抹掉 productList 里的名称，迫使服务层回退查详情
            return dataclasses.replace(
                quote,
                raw={**quote.raw, "productList": [
                    {k: v for k, v in e.items() if k != "productName"} for e in quote.raw.get("productList", [])
                ]},
            )

    client = NoNamePriceClient(
        stores=[Store(code="A-1", name="麦当劳（甲店）")],
        menus={"A-1": list(STUB_SKELETON)},
    )
    r = run_quest("在测试路吃，到店自取，预算50，想吃辣", client)
    assert r.chosen is not None
    tools = [c[0] for c in client.calls]
    assert "query-meal-detail" in tools  # 回退发生了
    assert all(pi.item.name for pi in r.chosen.items)


def test_menu_without_names_stops():
    """算价不返名称且详情全部失败 -> 停止，不编造餐品名。"""

    class NoNameNoDetailClient(StubLiveClient):
        def calculate_price(self, scene, items, coupon_ids, purpose=""):
            quote = super().calculate_price(scene, items, coupon_ids, purpose=purpose)
            return dataclasses.replace(
                quote,
                raw={**quote.raw, "productList": [
                    {k: v for k, v in e.items() if k != "productName"} for e in quote.raw.get("productList", [])
                ]},
            )

        def meal_detail(self, scene, code):
            raise MCPError("query-meal-detail", "详情服务不可用")

    client = NoNameNoDetailClient(stores=[Store(code="A-1", name="甲店")], menus={"A-1": list(STUB_SKELETON)})
    r = run_quest("在测试路吃，到店自取，预算50", client)
    assert r.chosen is None
    assert any("餐品名称" in w for w in r.warnings)


# ---------------------------------------------------------------- 外送场景


def test_delivery_without_addresses_explains():
    client = StubLiveClient(addresses=[])
    r = run_quest("外送到测试路，预算35", client)
    assert r.chosen is None
    assert any("配送地址" in w for w in r.warnings)
    assert any("麦当劳官方 App" in w for w in r.warnings)


def test_delivery_with_address_full_flow():
    client = StubLiveClient(
        addresses=[{"addressId": "ADDR-1", "address": "深圳市南山区测试路1号", "beCode": "BE-1"}],
        delivery_stores_map={"ADDR-1": [Store(code="A-1", name="麦当劳（甲店）", be_code="BE-1")]},
        menus={"A-1": list(STUB_SKELETON)},
    )
    r = run_quest("外送到测试路，预算50，想吃辣", client)
    assert r.chosen is not None
    assert r.store.code == "A-1"
    assert r.chosen.price_checked
    # 外送场景：算价带配送费（场景 orderType=2）
    assert r.chosen.delivery_fee_cents == 900
    tools = [c[0] for c in client.calls]
    assert "delivery-query-addresses" in tools and "delivery-query-stores" in tools


def test_delivery_multiple_stores_listed():
    client = StubLiveClient(
        addresses=[{"addressId": "ADDR-1", "address": "深圳市南山区测试路1号"}],
        delivery_stores_map={
            "ADDR-1": [
                Store(code="A-1", name="麦当劳（甲店）", be_code="BE-1"),
                Store(code="A-2", name="麦当劳（乙店）", be_code="BE-2"),
            ]
        },
    )
    r = run_quest("外送到测试路，预算35", client)
    assert r.chosen is None
    assert [s.code for s in r.store_candidates] == ["A-1", "A-2"]


def test_delivery_with_explicit_store_code_falls_back_to_takeaway():
    """外送 + 显式编码：明确说明并落到店场景（不假装能外送）。"""
    client = StubLiveClient(menus={"A-1": list(STUB_SKELETON)})
    r = run_quest("外送到测试路，预算50，想吃辣", client, store_code="A-1")
    assert r.chosen is not None
    assert any("暂不支持直接指定门店编码" in w for w in r.warnings)
    assert any("到店自取" in w for w in r.warnings)
    assert r.chosen.delivery_fee_cents == 0  # 按到店场景算价，无配送费


# ---------------------------------------------------------------- 演示模式


def test_demo_mode_falls_back_to_default_store_with_note():
    r = run_quest("预算35", MockMCPClient())  # 无位置
    assert r.chosen is not None
    assert any("默认演示门店" in w for w in r.warnings)
    assert any("真实模式必须提供位置或门店编码" in w for w in r.warnings)


def test_mock_multi_candidates_lists_both():
    r = run_quest("在演示门店吃，到店自取，预算35", MockMCPClient())
    assert r.chosen is None
    assert [s.code for s in r.store_candidates] == ["DEMO-001", "DEMO-002"]


def test_mock_location_filters_to_one_store():
    r = run_quest("在南山科技园吃，到店自取，预算35", MockMCPClient())
    assert r.chosen is not None
    assert r.store.code == "DEMO-001"
    assert not r.store_candidates


def test_dining_mode_default_is_disclosed():
    """未说用餐方式时，明确告知默认按到店自取（不假设配送费）。"""
    r = run_quest("预算35，想吃辣", MockMCPClient())
    assert any("未识别用餐方式" in w for w in r.warnings)
    assert r.constraints.dining_mode == "takeaway"
