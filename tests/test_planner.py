"""规划引擎测试：重点验证「不编造」铁律。"""
import pytest

from burger_quest import nlparser
from burger_quest.models import MenuItem, PriceQuote
from burger_quest.planner import build_plan


def price_fn_factory(client, store_code="DEMO-001", fail=False):
    def fn(items, coupon_ids):
        if fail:
            return PriceQuote(ok=False, message="模拟 MCP 故障")
        return client.calculate_price(store_code, items, coupon_ids, "takeaway")
    return fn


def test_menu_without_price_is_excluded(client, menu, coupons, nutrition):
    """价格未知的餐品不得进入组合。"""
    # 构造一个无价格餐品插到菜单最前
    broken = MenuItem(code="9999", name="幽灵汉堡", category="汉堡", price_cents=None)
    menu_with_broken = [broken] + menu
    c = nlparser.parse("想吃汉堡")
    options, warnings = build_plan(c, menu_with_broken, coupons, nutrition, price_fn_factory(client))
    assert any("幽灵汉堡" in w for w in warnings)
    for o in options:
        assert all(pi.item.code != "9999" for pi in o.items)


def test_price_failure_produces_no_option(client, menu, coupons, nutrition):
    """calculate-price 失败时不给方案（不编造总价）。"""
    c = nlparser.parse("预算50")
    options, warnings = build_plan(c, menu, coupons, nutrition, price_fn_factory(client, fail=True))
    assert options == []
    assert any("权威价格计算均失败" in w for w in warnings)
    assert all(o.total_cents is None for o in options)


def test_budget_filtering(client, menu, coupons, nutrition):
    """预算内方案必须真的在预算内；预算过低时如实失败。"""
    c = nlparser.parse("预算35，想吃辣")
    options, warnings = build_plan(c, menu, coupons, nutrition, price_fn_factory(client))
    assert options, "¥35 应能买到麦辣鸡腿堡"
    for o in options:
        if o.within_budget:
            assert o.total_cents <= 3500
            assert o.remaining_cents == 3500 - o.total_cents

    c2 = nlparser.parse("预算5块")
    options2, warnings2 = build_plan(c2, menu, coupons, nutrition, price_fn_factory(client))
    assert options2 == []
    assert any("预算内" in w for w in warnings2)


def test_over_budget_returns_closest_reference(client, menu, coupons, nutrition):
    """超预算时给出最接近的真实价格方案作参考，并明确标注（参考分支必须真实执行）。"""
    # ¥12：最便宜主食（双层吉士堡 ¥13）可过预筛但超过预算 -> 触发「最接近方案」分支
    c = nlparser.parse("预算12块")
    options, warnings = build_plan(c, menu, coupons, nutrition, price_fn_factory(client))
    assert options, "应给出最接近的真实价格方案作参考"
    assert all(o.within_budget is False for o in options)
    assert all(o.price_checked for o in options)
    assert any("预算内" in w for w in warnings)
    assert options[0].total_cents is not None and options[0].total_cents > 1200


def test_coupon_applied_comes_from_real_pool(client, menu, coupons, nutrition):
    """优惠券必须来自真实券池，且效果体现在权威总价上。"""
    c = nlparser.parse("预算40，想吃辣")
    options, _ = build_plan(c, menu, coupons, nutrition, price_fn_factory(client))
    assert options
    best = options[0]
    assert best.price_checked is True
    assert best.total_cents is not None
    if best.coupons:
        assert best.discount_cents and best.discount_cents > 0
        # 总价 = 原价 - 优惠 + 配送费
        assert best.total_cents == best.original_cents - best.discount_cents + (best.delivery_fee_cents or 0)


def test_two_people_share(client, menu, coupons, nutrition):
    c = nlparser.parse("两个人，预算80")
    options, _ = build_plan(c, menu, coupons, nutrition, price_fn_factory(client))
    assert options
    best = options[0]
    main_qty = sum(pi.qty for pi in best.items if "堡" in pi.item.name or "套餐" in pi.item.name)
    assert main_qty >= 2, "多人方案应包含多人份主食"


def test_low_calorie_preference(client, menu, coupons, nutrition):
    c = nlparser.parse("低卡，预算40")
    options, _ = build_plan(c, menu, coupons, nutrition, price_fn_factory(client))
    assert options
    best = options[0]
    total_cal = sum(
        (next((n.calories_kcal for n in nutrition if n.name == pi.item.name), 0) or 0) * pi.qty
        for pi in best.items
    )
    assert total_cal <= 600


def test_deterministic(client, menu, coupons, nutrition):
    """同样输入必须得到同样输出（可复现）。"""
    c = nlparser.parse("想吃辣，预算35")
    fn = price_fn_factory(client)
    o1, _ = build_plan(c, menu, coupons, nutrition, fn)
    o2, _ = build_plan(c, menu, coupons, nutrition, fn)
    assert [o.item_names() for o in o1] == [o.item_names() for o in o2]
    assert [o.total_cents for o in o1] == [o.total_cents for o in o2]
