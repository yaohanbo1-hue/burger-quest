"""门店解析流程测试（对应审计 D-2 与任务 4-8）。

核心规则：
- 真实模式没有位置信息 -> 明确要求补充，不发起任何调用；
- 多候选门店 -> 全部列出，不编造、不默默选择；
- 查无门店 / 查询失败 / 无效编码 -> 明确提示原因，不生成价格；
- 演示模式无位置 -> 回退默认演示门店并明确声明。
"""
import pytest

from burger_quest.mcp_base import MCPClient, MCPError
from burger_quest.mcp_mock import MockMCPClient
from burger_quest.models import MenuItem, PriceQuote, Store
from burger_quest.service import run_quest


class StubLiveClient(MCPClient):
    """模拟真实客户端（不是 MockMCPClient）：行为可控，不提供无地址回退。"""

    def __init__(self, stores=(), menus=None, stores_error=None, meals_error=None):
        super().__init__()
        self.stores = list(stores)
        self.menus = menus or {}
        self.stores_error = stores_error
        self.meals_error = meals_error
        self.calls = []

    def now_time(self):
        return {"datetime": "2026-10-13 19:00:00", "time_period": "dinner"}

    def nearby_stores(self, address):
        self.calls.append(("query-nearby-stores", address))
        if self.stores_error:
            raise MCPError("query-nearby-stores", self.stores_error)
        return list(self.stores)

    def query_meals(self, store_code):
        self.calls.append(("query-meals", store_code))
        if self.meals_error:
            raise MCPError("query-meals", self.meals_error)
        return self.menus.get(store_code, [])

    def meal_detail(self, meal_code):
        return {}

    def store_coupons(self, store_code):
        return []

    def my_coupons(self):
        return []

    def list_nutrition_foods(self):
        return []

    def calculate_price(self, store_code, items, coupon_ids, dining_mode):
        raise MCPError("calculate-price", "测试不验价")


def test_real_mode_requires_location():
    client = StubLiveClient()
    r = run_quest("想吃辣，预算35", client)
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
    r = run_quest("外送到测试路，预算35", client)
    assert r.chosen is None
    assert [s.code for s in r.store_candidates] == ["A-1", "A-2", "A-3"]
    assert any("候选门店" in w for w in r.warnings)
    # 没有悄悄查菜单/选店
    assert not any(name == "query-meals" for name, _ in client.calls)


def test_real_mode_nearby_empty():
    client = StubLiveClient(stores=[])
    r = run_quest("外送到火星基地，预算35", client)
    assert r.chosen is None
    assert any("未在「火星基地」找到附近门店" in w for w in r.warnings)
    assert any("未确定门店" in w for w in r.warnings)


def test_real_mode_nearby_error():
    client = StubLiveClient(stores_error="模拟 MCP 无响应")
    r = run_quest("外送到测试路，预算35", client)
    assert r.chosen is None
    assert any("附近门店查询失败" in w for w in r.warnings)


def test_real_mode_invalid_store_code():
    client = StubLiveClient(meals_error="门店不存在或已停售")
    r = run_quest("预算35", client, store_code="BAD-999")
    assert r.chosen is None
    assert any("BAD-999" in w and "查询失败" in w for w in r.warnings)


def test_real_mode_empty_menu_for_valid_store():
    client = StubLiveClient(
        stores=[Store(code="A-1", name="麦当劳（甲店）")],
        menus={"A-1": []},
    )
    r = run_quest("外送到测试路，预算35", client)
    assert r.chosen is None
    assert any("未返回可售餐品" in w for w in r.warnings)


def test_real_mode_single_store_full_flow():
    client = StubLiveClient(
        stores=[Store(code="A-1", name="麦当劳（甲店）")],
        menus={
            "A-1": [
                MenuItem(code="M1", name="麦辣鸡腿堡", category="汉堡", price_cents=2100, tags=("辣",)),
                MenuItem(code="S1", name="薯条（大）", category="小食", price_cents=1250),
            ]
        },
    )

    def calc(store_code, items, coupon_ids, dining_mode):
        total = sum(pi.item.price_cents * pi.qty for pi in items)
        return PriceQuote(total_cents=total, original_cents=total, discount_cents=0, delivery_fee_cents=0, ok=True)

    client.calculate_price = calc
    r = run_quest("外送到测试路，预算50，想吃辣", client)
    assert r.chosen is not None
    assert r.store.code == "A-1"
    assert r.chosen.price_checked is True
    assert r.chosen.total_cents is not None
    assert "query-nearby-stores" in {name for name, _ in client.calls}


def test_explicit_store_code_queries_menu_once():
    """显式编码只查一次菜单（复用为菜单数据，不重复调用）。"""
    client = StubLiveClient(
        menus={
            "A-1": [
                MenuItem(code="M1", name="麦辣鸡腿堡", category="汉堡", price_cents=2100, tags=("辣",)),
                MenuItem(code="S1", name="薯条（大）", category="小食", price_cents=1250),
            ]
        }
    )

    def calc(store_code, items, coupon_ids, dining_mode):
        total = sum(pi.item.price_cents * pi.qty for pi in items)
        return PriceQuote(total_cents=total, original_cents=total, discount_cents=0, delivery_fee_cents=0, ok=True)

    client.calculate_price = calc
    r = run_quest("预算50，想吃辣", client, store_code="A-1")
    assert r.chosen is not None
    meal_calls = [args for name, args in client.calls if name == "query-meals"]
    assert meal_calls == ["A-1"]  # 恰好一次


def test_demo_mode_falls_back_to_default_store_with_note():
    r = run_quest("预算35", MockMCPClient())  # 无位置
    assert r.chosen is not None
    assert any("默认演示门店" in w for w in r.warnings)
    assert any("真实模式必须提供位置或门店编码" in w for w in r.warnings)


def test_mock_multi_candidates_lists_both():
    r = run_quest("外送到演示门店，预算35", MockMCPClient())
    assert r.chosen is None
    assert [s.code for s in r.store_candidates] == ["DEMO-001", "DEMO-002"]


def test_mock_location_filters_to_one_store():
    r = run_quest("外送到南山科技园，预算35", MockMCPClient())
    assert r.chosen is not None
    assert r.store.code == "DEMO-001"
    assert not r.store_candidates


def test_dining_mode_default_is_disclosed():
    """未说用餐方式时，明确告知默认按到店自取（不假设配送费）。"""
    r = run_quest("预算35，想吃辣", MockMCPClient())
    assert any("未识别用餐方式" in w for w in r.warnings)
    assert r.constraints.dining_mode == "takeaway"
