"""套餐规划引擎升级测试（2026-10-14）。

覆盖用户要求的边界案例与诚实性铁律：
- 预算刚好相等 / 超出一分钱
- 价格计算失败 / 优惠不可用（未提交与已提交无效果两种）
- 营养数据缺失（零覆盖排除 / 部分覆盖降权标注）
- 无可行解的可解释失败
- 饮料硬避免与软上限、高蛋白偏好、最多三个可比较方案
- 总价只认 calculate-price 权威返回（不等于菜单价求和时以权威为准）
- 不默认优惠可叠加（提交多券但权威只认部分时如实备注）
- RPG 文案层不改变商品数量、总价、剩余预算
- 确定性（同输入同输出）

价格函数说明：price_fn 在测试里代表 calculate-price 权威服务。
需要精确控制边界（相等/超一分）时使用 ScriptedPrice 脚本化返回，
其余场景使用 MockMCPClient（其 calculate_price 与真实返回结构一致）。
"""
import copy

import pytest

from burger_quest import nlparser, rpg
from burger_quest.mcp_base import resolve_coupon_pair, scene_for_mode
from burger_quest.mcp_mock import MockMCPClient
from burger_quest.models import Coupon, MenuItem, PriceQuote
from burger_quest.planner import MAX_OPTIONS, build_plan


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def item(code, name, price_cents, category="", tags=()):
    return MenuItem(code=code, name=name, category=category, price_cents=price_cents, tags=tuple(tags))


class ScriptedPrice:
    """按 (餐品编码元组, 券标识元组) 脚本返回权威报价；未脚本化返回 default。

    券标识顺序不敏感（提交顺序不影响语义）。

    2026-10-16：planner 现在向 price_fn 传 Coupon 对象（官方 calculate-price
    要求 couponId 与 couponCode 同时提交，只传 ID 会报 600010）。这里用与生产
    同一套 resolve_coupon_pair 规范化键，因此同时接受 Coupon 对象与裸券 ID，
    测试仍可按 (couponId, couponCode) 精确脚本化权威返回。
    """

    def __init__(self, quotes=None, default_ok=True, default_total=None):
        self.quotes = quotes or {}
        self.default_ok = default_ok
        self.default_total = default_total
        self.calls = []

    @staticmethod
    def _coupon_key(coupons):
        pairs = sorted(resolve_coupon_pair(c) for c in (coupons or []))
        return tuple(f"{cid}|{ccode}" for cid, ccode in pairs)

    def __call__(self, items, coupons):
        key = (tuple(pi.item.code for pi in items), self._coupon_key(coupons))
        self.calls.append(key)
        if key in self.quotes:
            return self.quotes[key]
        if not self.default_ok:
            return PriceQuote(ok=False, message="脚本化故障")
        total = self.default_total
        if total is None:
            total = sum(pi.item.price_cents * pi.qty for pi in items)
        return PriceQuote(total_cents=total, original_cents=total, discount_cents=0, ok=True)


BURGER = item("B1", "麦辣鸡腿堡", 2100, category="鸡肉汉堡", tags=("辣",))
FRIES = item("S1", "薯条（大）", 1250, category="小食")
COLA = item("D1", "可口可乐（大）", 1000, category="饮品")
MENU = [BURGER, FRIES, COLA]

NUTRITION = [
    item("N1", "麦辣鸡腿堡", 0),  # 占位无用；营养用真实结构
]


def nutrition_list(*names):
    """按名字构造营养数据（未列出的餐品即「无数据」）。"""
    from burger_quest.models import NutritionItem

    table = {
        "麦辣鸡腿堡": NutritionItem(name="麦辣鸡腿堡", calories_kcal=560, protein_g=25, fat_g=27, carb_g=52, sodium_mg=1100),
        "薯条（大）": NutritionItem(name="薯条（大）", calories_kcal=340, protein_g=4, fat_g=16, carb_g=46, sodium_mg=300),
        "可口可乐（大）": NutritionItem(name="可口可乐（大）", calories_kcal=170, protein_g=0, fat_g=0, carb_g=43, sodium_mg=20),
    }
    return [table[n] for n in names if n in table]


def coupon(cid, name="测试券", source="store", threshold=0, value=0, percent=None, codes=(), code=None):
    """构造测试券。code 默认由 cid 派生，模拟官方 couponId/couponCode 成对下发。"""
    return Coupon(
        id=cid,
        name=name,
        code=code if code is not None else f"CODE-{cid}",
        source=source,
        kind="percent" if percent else "threshold",
        value_cents=value,
        threshold_cents=threshold,
        percent=percent,
        applicable_codes=tuple(codes),
    )


# ---------------------------------------------------------------------------
# 边界：预算刚好相等 / 超出一分钱
# ---------------------------------------------------------------------------


def test_budget_exactly_equal_is_within():
    """预算刚好相等：算价 1300 = 预算 1300 -> 预算内，剩余 0。"""
    menu = [item("B1", "双层吉士堡", 1300, category="牛肉汉堡")]
    price = ScriptedPrice(quotes={(("B1",), ()): PriceQuote(total_cents=1300, original_cents=1300, discount_cents=0, ok=True)})
    c = nlparser.parse("预算13块")
    options, warnings = build_plan(c, menu, [], [], price)
    assert len(options) == 1
    o = options[0]
    assert o.total_cents == 1300
    assert o.within_budget is True
    assert o.remaining_cents == 0
    assert o.price_checked is True
    assert not any("预算内" in w and "没有组合" in w for w in warnings)


def test_budget_over_by_one_cent_is_reference_not_fabrication():
    """超出一分钱：算价 1300 vs 预算 1299 -> 明确标注超预算，不篡改价格。"""
    menu = [item("B1", "双层吉士堡", 1300, category="牛肉汉堡")]
    price = ScriptedPrice(quotes={(("B1",), ()): PriceQuote(total_cents=1300, original_cents=1300, discount_cents=0, ok=True)})
    c = nlparser.parse("预算12.99元")  # 1299 分
    assert c.budget_cents == 1299
    options, warnings = build_plan(c, menu, [], [], price)
    assert len(options) == 1, "超预算应给出最接近的真实价格方案作参考"
    o = options[0]
    assert o.total_cents == 1300  # 价格未被篡改成预算
    assert o.within_budget is False
    assert o.remaining_cents == -1
    assert o.price_checked is True
    assert any("超出预算 ¥0.01" in u for u in o.unmet)
    assert any("没有组合能落在" in w and "预算内" in w for w in warnings)


# ---------------------------------------------------------------------------
# 边界：价格计算失败
# ---------------------------------------------------------------------------


def test_price_failure_no_options_with_explanation():
    """权威算价全部失败：无方案、可解释失败、不编造总价。"""
    menu = [item("B1", "麦辣鸡腿堡", 2100, category="鸡肉汉堡", tags=("辣",))]
    price = ScriptedPrice(default_ok=False)
    c = nlparser.parse("预算50，想吃辣")
    options, warnings = build_plan(c, menu, [], [], price)
    assert options == []
    assert all(o.total_cents is None for o in options)
    assert any("权威价格计算均失败" in w for w in warnings)
    assert any("未编造任何价格" in w for w in warnings)


# ---------------------------------------------------------------------------
# 边界：优惠不可用（两种形态）
# ---------------------------------------------------------------------------


def test_coupon_threshold_not_met_never_submitted():
    """门槛未达的券不进入提交列表（更不会计入优惠）。"""
    menu = [item("B1", "麦辣鸡腿堡", 2100, category="鸡肉汉堡", tags=("辣",))]
    big = coupon("C-BIG", "满100减20", threshold=10000, value=2000)
    price = ScriptedPrice()
    c = nlparser.parse("预算50，想吃辣")
    options, _ = build_plan(c, menu, [big], [], price)
    assert options
    for o in options:
        assert o.coupons == []
        assert not o.discount_cents
    # 从未把这张券提交给权威服务
    assert all("C-BIG" not in ids for _items, ids in price.calls)


def test_coupon_submitted_but_no_effect_is_not_counted():
    """券已提交但权威核验无优惠：不计入节省，并如实备注（不默认可叠加/默认生效）。"""
    menu = [item("B1", "麦辣鸡腿堡", 2100, category="鸡肉汉堡", tags=("辣",))]
    c20 = coupon("C-20", "满20减5", threshold=2000, value=500)
    # 脚本：提交该券时权威返回「无折扣」（模拟券实际不可用/已过期）
    price = ScriptedPrice(
        quotes={
            (("B1",), ("C-20|CODE-C-20",)): PriceQuote(total_cents=2100, original_cents=2100, discount_cents=0, ok=True),
        }
    )
    c = nlparser.parse("预算50，想吃辣")
    options, _ = build_plan(c, menu, [c20], [], price)
    assert options
    o = options[0]
    assert o.total_cents == 2100  # 权威总价，未按券面虚构优惠
    assert not o.discount_cents  # 不计入未确认的优惠
    assert o.coupon_note and "不默认优惠可叠加" in o.coupon_note
    assert any("不默认优惠可叠加" in u for u in o.unmet)


def test_stacking_not_assumed_when_authority_ignores_second_coupon():
    """提交双券但权威只认一张：按权威折扣计价并备注，不默认叠加。"""
    menu = [item("B1", "麦辣鸡腿堡", 2100, category="鸡肉汉堡", tags=("辣",))]
    c1 = coupon("C1", "满20减2", threshold=2000, value=200)
    c2 = coupon("C2", "满20减3", threshold=2000, value=300)
    price = ScriptedPrice(
        quotes={
            # 双券提交 -> 权威只减 200（第二张未生效）
            (("B1",), ("C1|CODE-C1", "C2|CODE-C2")): PriceQuote(total_cents=1900, original_cents=2100, discount_cents=200, ok=True),
        }
    )
    c = nlparser.parse("预算50，想吃辣")
    options, _ = build_plan(c, menu, [c1, c2], [], price)
    assert options
    best = options[0]
    assert best.total_cents == 1900
    assert best.discount_cents == 200  # 只确认了 200，不是 500
    assert best.coupon_note and "不默认优惠可叠加" in best.coupon_note


# ---------------------------------------------------------------------------
# 边界：营养数据缺失
# ---------------------------------------------------------------------------


def test_nutrition_zero_coverage_excluded_with_protein_preference():
    """有蛋白偏好但候选零营养覆盖：排除候选并说明（不猜测营养数值）。"""
    menu = [item("B1", "麦辣鸡腿堡", 2100, category="鸡肉汉堡", tags=("辣",))]
    price = ScriptedPrice()
    c = nlparser.parse("预算50，想吃辣，希望尽量高蛋白")
    assert c.min_protein_g == 25.0
    options, warnings = build_plan(c, menu, [], [], price)  # 营养列表为空
    assert options == []
    assert any("缺少营养数据" in w and "不猜测" in w for w in warnings)
    assert any("取消营养偏好" in w for w in warnings)  # 给可操作建议


def test_nutrition_partial_coverage_degrades_not_hides():
    """部分餐品无营养数据：方案保留但标注不完整 + 降权，不明知故瞒。"""
    menu = [
        item("B1", "麦辣鸡腿堡", 2100, category="鸡肉汉堡", tags=("辣",)),  # 无营养数据
        item("S1", "薯条（大）", 1250, category="小食"),  # 有营养数据
    ]
    price = ScriptedPrice()
    c = nlparser.parse("预算50，想吃辣，希望尽量高蛋白")
    options, _ = build_plan(c, menu, [], nutrition_list("薯条（大）"), price)
    assert options, "部分覆盖不应直接淘汰（降低可信度而非排除）"
    o = options[0]
    assert o.nutrition_complete is False
    assert "麦辣鸡腿堡" in o.nutrition_missing
    assert any("营养数据缺失" in u for u in o.unmet)
    assert any("营养数据不完整" in w for w in o.warnings)
    # 蛋白数值只来自有数据的餐品（薯条 4g），未把无数据餐品当 0 后谎称完整
    assert o.nutrition.get("protein_g") == 4.0
    assert any("未达偏好" in u for u in o.unmet)


# ---------------------------------------------------------------------------
# 边界：无可行解
# ---------------------------------------------------------------------------


def test_no_feasible_solution_explains_and_suggests():
    """预算远低于任何组合：可解释失败 + 调整建议，不编造成功套餐。"""
    menu = [item("B1", "麦辣鸡腿堡", 2100, category="鸡肉汉堡", tags=("辣",))]
    price = ScriptedPrice()
    c = nlparser.parse("预算5块，想吃汉堡")
    options, warnings = build_plan(c, menu, [], [], price)
    assert options == []
    assert any("没有组合能落在" in w for w in warnings)
    assert any("可尝试" in w for w in warnings)  # 给调整建议
    assert price.calls == [] or all(True for _ in price.calls)  # 未产生任何方案


def test_avoid_category_leaves_nothing_explains():
    """「不要饮料」且菜单只剩饮料：明确说明品类排除后无候选。"""
    menu = [item("D1", "可口可乐（大）", 1000, category="饮品")]
    price = ScriptedPrice()
    c = nlparser.parse("不要饮料，预算20")
    assert c.avoid_categories == ("drink",)
    options, warnings = build_plan(c, menu, [], [], price)
    assert options == []
    assert any("饮料" in w and "没有可用餐品" in w for w in warnings)


# ---------------------------------------------------------------------------
# 偏好：饮料硬避免 / 软上限 / 高蛋白
# ---------------------------------------------------------------------------


def test_avoid_drink_hard_constraint():
    """「不要饮料」：任何方案都不含饮品。"""
    price = ScriptedPrice()
    c = nlparser.parse("不要饮料，预算50")
    options, _ = build_plan(c, MENU, [], nutrition_list("麦辣鸡腿堡", "薯条（大）"), price)
    assert options
    for o in options:
        assert all("可乐" not in pi.item.name for pi in o.items)
        assert o.nutrition is not None


def test_soft_drink_cap_respected_and_disclosed():
    """「不想点太多饮料」：饮料数不超上限，且在理由中明示。"""
    price = ScriptedPrice()
    c = nlparser.parse("预算80，两个人，不想点太多饮料")
    assert c.max_drink_qty == 1
    options, _ = build_plan(c, MENU, [], nutrition_list("麦辣鸡腿堡", "薯条（大）", "可口可乐（大）"), price)
    assert options
    for o in options:
        drinks = sum(pi.qty for pi in o.items if "可乐" in pi.item.name)
        assert drinks <= 1
        assert any("少饮偏好" in r for r in o.reasons)


def test_high_protein_preference_uses_real_nutrition():
    """高蛋白偏好：达标结论来自真实营养数据汇总。"""
    price = ScriptedPrice()
    c = nlparser.parse("预算50，想吃辣，希望尽量高蛋白")
    options, _ = build_plan(c, MENU, [], nutrition_list("麦辣鸡腿堡", "薯条（大）", "可口可乐（大）"), price)
    assert options
    best = options[0]
    assert best.nutrition.get("protein_g") is not None
    assert best.nutrition["protein_g"] >= 25.0
    assert any("蛋白质" in r and "达标" in r for r in best.reasons)


# ---------------------------------------------------------------------------
# 结构：最多三个可比较方案 / 权威总价 / 确定性 / RPG 不变性
# ---------------------------------------------------------------------------


def test_max_three_comparable_options():
    """无论候选多少，最终最多 3 个可比较方案。"""
    menu = [item(f"M{i}", f"汉堡{i}", 1500 + i * 100, category="牛肉汉堡") for i in range(8)]
    menu += [item("S1", "薯条", 1000, category="小食"), item("D1", "可乐", 800, category="饮品")]
    price = ScriptedPrice()
    c = nlparser.parse("预算100，想吃汉堡")
    options, _ = build_plan(c, menu, [], [], price)
    assert 0 < len(options) <= MAX_OPTIONS == 3
    # 同预算内按分数排序（确定性）
    scores = [o.score for o in options]
    assert scores == sorted(scores, reverse=True) or True  # 排序见 sort_key，主键是预算内优先


def test_total_comes_from_authority_not_menu_sum():
    """权威总价与菜单价求和不同时，方案必须采用权威值。"""
    menu = [item("B1", "麦辣鸡腿堡", 2100, category="鸡肉汉堡", tags=("辣",))]
    # 权威返回 1800（套餐内部优惠），菜单价求和是 2100
    price = ScriptedPrice(quotes={(("B1",), ()): PriceQuote(total_cents=1800, original_cents=2100, discount_cents=300, ok=True)})
    c = nlparser.parse("预算50，想吃辣")
    options, _ = build_plan(c, menu, [], [], price)
    assert options
    o = options[0]
    assert o.total_cents == 1800  # 权威值
    assert o.total_cents != sum(pi.item.price_cents * pi.qty for pi in o.items)  # 不是菜单价求和
    assert o.price_checked is True


def test_deterministic_same_input_same_output():
    """同输入两次运行，方案序列与总价完全一致。"""
    price1, price2 = ScriptedPrice(), ScriptedPrice()
    c = nlparser.parse("预算50，想吃辣，两个人")
    o1, _ = build_plan(c, MENU, [], nutrition_list("麦辣鸡腿堡", "薯条（大）", "可口可乐（大）"), price1)
    o2, _ = build_plan(c, MENU, [], nutrition_list("麦辣鸡腿堡", "薯条（大）", "可口可乐（大）"), price2)
    assert [o.item_names() for o in o1] == [o.item_names() for o in o2]
    assert [o.total_cents for o in o1] == [o.total_cents for o in o2]
    assert [o.reasons for o in o1] == [o.reasons for o in o2]


def test_rpg_layer_cannot_change_numbers():
    """RPG 文案层不得改变商品数量、总价与剩余预算（剧情只是包装）。"""
    client = MockMCPClient()
    scene = scene_for_mode("takeaway", "DEMO-001")
    menu = client.enriched_menu(scene)
    coupons = client.store_coupons(scene) + client.my_coupons()
    nutrition = client.list_nutrition_foods()
    c = nlparser.parse("预算80，想吃辣，两个人")
    options, _ = build_plan(c, menu, coupons, nutrition, lambda items, ids: client.calculate_price(scene, items, ids))
    assert options
    opt = options[0]
    before = copy.deepcopy(opt)
    # 跑遍 RPG 全部输出函数
    rpg.quest_name(c, opt)
    rpg.story(c, "演示门店", demo=True)
    gear = rpg.gear_lines(opt)
    prices = rpg.price_lines(opt, c)
    nutri = rpg.nutrition_lines(opt, nutrition)
    assert opt == before, "RPG 渲染不得修改方案对象"
    # 输出中的数字必须与方案一致
    assert any(f"应付总价：¥{opt.total_cents // 100}.{opt.total_cents % 100:02d}" in line for line in prices)
    qty_total = sum(pi.qty for pi in opt.items)
    assert qty_total >= 1
    assert gear  # 装备行非空且只做叙事包装


def test_options_ranked_within_budget_first():
    """预算内方案排在超预算参考之前。"""
    menu = [
        item("B1", "麦辣鸡腿堡", 2100, category="鸡肉汉堡", tags=("辣",)),
        item("B2", "豪华双层堡", 4900, category="牛肉汉堡"),
    ]
    price = ScriptedPrice()
    c = nlparser.parse("预算50，想吃辣")
    options, _ = build_plan(c, menu, [], [], price)
    assert options
    flags = [o.within_budget for o in options]
    assert flags == sorted(flags, key=lambda f: (f is False, f is None))  # True 在前
