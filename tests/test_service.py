"""服务编排与错误处理测试。"""
from burger_quest.mcp_base import MCPError
from burger_quest.mcp_mock import MockMCPClient
from burger_quest.service import run_quest


class BrokenMenuClient(MockMCPClient):
    """菜单查询直接失败的客户端。"""

    def query_meals(self, scene):
        raise MCPError("query-meals", "模拟 MCP 无响应", code=None)


class BrokenPriceClient(MockMCPClient):
    """calculate-price 直接失败的客户端。"""

    def calculate_price(self, scene, items, coupon_ids, purpose=""):
        raise MCPError("calculate-price", "模拟价格服务不可用", code=429)


def test_happy_path():
    r = run_quest("我想吃辣，预算35，外送到南山科技园，一个人", MockMCPClient(), generated_at="2026-10-13")
    assert r.chosen is not None
    assert r.chosen.price_checked
    assert r.store is not None and r.store.code == "DEMO-001"
    assert r.quest_name and r.story and r.gear_lines
    assert r.card_html and r.card_markdown
    # 审计链路完整
    tools = {c["tool"] for c in r.mcp_calls}
    assert {"query-meals", "calculate-price", "query-store-coupons", "list-nutrition-foods"} <= tools


def test_menu_failure_no_fabrication():
    r = run_quest("想吃辣，预算35", BrokenMenuClient())
    assert r.chosen is None
    assert r.options == []
    assert any("菜单查询失败" in w for w in r.warnings)


def test_price_failure_no_fabrication():
    r = run_quest("想吃辣，预算35", BrokenPriceClient())
    assert r.chosen is None
    assert any("calculate-price" in w or "权威价格计算均失败" in w for w in r.warnings)
    # 没有任何方案带着编造的总价
    assert all(o.total_cents is None for o in r.options)


def test_no_store_resolvable():
    """有位置但查无门店（演示数据中不存在的地址）-> 明确停止，不猜门店。"""
    r = run_quest("在火星基地吃，预算35", MockMCPClient())
    assert r.chosen is None
    assert any("未在「火星基地」找到附近门店" in w for w in r.warnings)
    assert any("未确定门店" in w for w in r.warnings)


def test_data_sources_audit():
    r = run_quest("想吃辣，预算35", MockMCPClient())
    src = r.data_sources
    assert "query-meals" in src["menu"]
    assert "calculate-price" in src["price"]
    assert "演示数据" in src["mode"]
