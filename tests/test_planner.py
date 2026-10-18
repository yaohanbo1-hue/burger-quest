"""规划引擎测试：重点验证「不编造」铁律。"""
import pytest

from burger_quest import nlparser
from burger_quest.mcp_base import scene_for_mode
from burger_quest.models import MenuItem, PriceQuote
from burger_quest.planner import build_plan


def price_fn_factory(client, scene=None, fail=False):
    scene = scene or scene_for_mode("takeaway", "DEMO-001")

    def fn(items, coupon_ids):
        if fail:
            return PriceQuote(ok=False, message="模拟 MCP 故障")
        return client.calculate_price(scene, items, coupon_ids)

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


# ===========================================================================
# 推荐多样性与早餐分类（2026-10-18 用户网页实测反馈回归）
#
# 背景：用户实测「预算 100，想吃辣」时，三个方案全是同一款「猪柳炒双蛋堡」，
# 标题也都叫「省钱精算阵型」。三层根因：
# 1) 菜单骨架阶段 item.name 为空，「麦满分系列」等分类名不含「堡」→ classify
#    返回 other → 短名单里主食只剩 1 个（早餐时段尤其明显）；
# 2) _build_candidates 的 side/drink 只取榜首、每个模板都用 mains[0] →
#    三个方案变成「同款 ×1 / +同款饮品 / ×2+同款饮品」；
# 3) _option_title 只看约束不看内容 → 同一阵型名在多个方案上重复。
# 以下用例锁死修复后的行为，防止回归。
# ===========================================================================


def test_classify_breakfast_series_is_main():
    """骨架阶段名称为空时，早餐主食系列必须按分类名识别为主食。"""
    from burger_quest.planner import classify

    for cat in ("麦满分系列", "厚松饼堡\n系列", "早安营养卷", "炒双蛋堡\n系列"):
        assert classify(MenuItem(code="X", name="", category=cat)) == "main", cat


def test_candidates_cover_multiple_main_series(client, menu, nutrition):
    """有多个系列主食时，候选不能只用 mains[0] 一种主食。"""
    from burger_quest.planner import _build_candidates, classify

    c = nlparser.parse("预算100，想吃辣，一个人")
    cands = _build_candidates(menu, c, nutrition)
    used = {pi.item.code for combo in cands for pi in combo if classify(pi.item) == "main"}
    assert len(used) >= 2, "候选主食必须覆盖至少两个不同餐品（否则必然同款堆量）"


def test_option_titles_are_distinct_and_show_main(client, menu, coupons, nutrition):
    """同一阵型名下多个方案时，标题必须可区分（含主食名）。"""
    c = nlparser.parse("预算100，想吃辣，一个人")
    options, _ = build_plan(c, menu, coupons, nutrition, price_fn_factory(client))
    assert len(options) >= 2
    titles = [o.title for o in options]
    assert len(set(titles)) == len(titles), f"标题重复：{titles}"
    assert all("省钱精算阵型" in t for t in titles)
    assert any("麦辣" in t or "堡" in t for t in titles), "标题应含主食名便于区分"


def test_taste_miss_warning_explains_menu_fact():
    """菜单里没有该口味时，全局 warnings 必须说明是菜单本身没有。"""
    no_spicy = [MenuItem(code="A1", name="薯饼", category="小食", price_cents=1000, tags=("小食",))]
    c = nlparser.parse("想吃辣，预算30，一个人")

    def fn(items, coupon_ids):
        cents = sum((pi.item.price_cents or 0) * pi.qty for pi in items)
        return PriceQuote(total_cents=cents, original_cents=cents, ok=True)

    _options, warnings = build_plan(c, no_spicy, [], [], fn)
    assert any("没有辣味口味的餐品" in w for w in warnings), warnings
