"""MCP 防御式适配器测试。

官方未公布每个工具的详细 JSON Schema，适配层必须容忍不同键名/单位/包装，
取不到就留空——绝不编造。这里固化这些行为。
"""
from burger_quest import mcp_base as mb


def test_adapt_meals_alternate_keys_and_missing_price():
    raw = {
        "list": [
            {"productCode": "A1", "productName": "麦辣鸡腿堡", "categoryName": "汉堡", "salePrice": "21.0", "labels": ["辣"]},
            {"code": "A2", "name": "薯条", "type": "小食"},  # 无价格
            {"mealName": "缺编码的餐品"},  # 无编码 -> 跳过
        ]
    }
    meals = mb.adapt_meals(raw)
    assert [m.code for m in meals] == ["A1", "A2"]
    assert meals[0].price_cents == 2100
    assert meals[0].tags == ("辣",)
    assert meals[1].price_cents is None  # 缺价格 -> None，不编造


def test_extract_price_cents_units():
    assert mb._extract_price_cents({"price": 25.0}) == 2500  # 元（浮点）
    assert mb._extract_price_cents({"price": "25.50"}) == 2550  # 元（字符串）
    assert mb._extract_price_cents({"price": 2500}) == 2500  # >500 的整数按「分」推断
    assert mb._extract_price_cents({"priceCents": 2500}) == 2500  # 显式分字段
    assert mb._extract_price_cents({"name": "x"}) is None  # 无价格字段
    assert mb._extract_price_cents({"price": "abc"}) is None  # 无法解析
    assert mb._extract_price_cents({"price": None}) is None


def test_adapt_coupons_variants():
    raw = [
        {"couponId": "C1", "couponName": "满30减5", "threshold": 30, "value": 5},
        {"couponId": "C2", "couponName": "薯条半价", "discount": 0.5, "applicableMeals": ["3001"]},
        {"couponId": "C3", "couponName": "无门槛券", "amount": 2},
        {"couponName": "缺ID的券"},  # 被跳过
    ]
    coupons = mb.adapt_coupons(raw, source="store")
    assert [c.id for c in coupons] == ["C1", "C2", "C3"]
    assert coupons[0].kind == "threshold"
    assert coupons[0].threshold_cents == 3000 and coupons[0].value_cents == 500
    assert coupons[1].kind == "percent" and coupons[1].percent == 0.5
    assert coupons[1].applicable_codes == ("3001",)
    assert coupons[2].value_cents == 200 and coupons[2].threshold_cents is None
    assert all(c.source == "store" for c in coupons)


def test_adapt_nutrition_chinese_and_english_keys():
    raw = [
        {"foodName": "巨无霸", "能量": 520, "蛋白质": 27, "脂肪": 28, "碳水化合物": 44, "钠": 1050, "钙": 300},
        {"name": "薯条", "energy": 340, "protein": 4},
    ]
    items = mb.adapt_nutrition(raw)
    assert items[0].calories_kcal == 520.0 and items[0].protein_g == 27.0 and items[0].carb_g == 44.0
    assert items[0].sodium_mg == 1050.0 and items[0].calcium_mg == 300.0
    assert items[1].calories_kcal == 340.0 and items[1].protein_g == 4.0


def test_adapt_price():
    ok = mb.adapt_price({"totalPrice": 27.0, "originalPrice": 30.0, "discountPrice": 3.0, "deliveryFee": 0})
    assert ok.ok is True and ok.total_cents == 2700
    assert ok.original_cents == 3000 and ok.discount_cents == 300 and ok.delivery_fee_cents == 0
    bad = mb.adapt_price({"message": "优惠券不可用"})
    assert bad.ok is False and "优惠券不可用" in bad.message
    failed = mb.adapt_price({"success": False, "totalPrice": 10})
    assert failed.ok is False


def test_as_list_unwraps_wrappers():
    assert mb._as_list({"items": [1, 2]}) == [1, 2]
    assert mb._as_list({"data": [3]}) == [3]
    assert mb._as_list([1]) == [1]
    assert mb._as_list(None) == []
    assert mb._as_list({"x": 1}) == [{"x": 1}]


def test_safe_args_strips_secrets():
    safe = mb._safe_args({"storeCode": "X", "token": "t", "Authorization": "Bearer x", "headers": {"a": 1}})
    assert safe == {"storeCode": "X"}


def test_adapt_stores_and_time():
    stores = mb.adapt_stores({"list": [{"storeId": "S1", "name": "甲店"}, {"noCode": 1}]})
    assert [s.code for s in stores] == ["S1"]
    t = mb.adapt_time({"currentTime": "2026-10-13 19:00:00", "period": "dinner"})
    assert t == {"datetime": "2026-10-13 19:00:00", "time_period": "dinner"}


def test_tools_lists_are_honest():
    # query-meal-detail 已从未接入清单移到预留清单（联调后再接入）
    assert "query-meal-detail" not in mb.TOOLS_USED
    assert "query-meal-detail" in mb.TOOLS_RESERVED
    assert "create-order" in mb.TOOLS_RESERVED
    assert len(mb.TOOLS_USED) == 7
