"""MCP 适配层测试（对齐 2026-10-13 联调实测格式）。

固化：文本信封提取、data 解包、业务错误抛出、菜单骨架（只有编码）、
算价金额单位（分）、营养自定义文本解析、算价入参构造。
"""
import pytest

from burger_quest import mcp_base as mb
from burger_quest.mcp_base import MCPError, OrderScene
from burger_quest.models import MenuItem, PlanItem


# ------------------------------------------------------------ 信封提取


def test_extract_api_json_from_real_text_format():
    """联调实测：字段说明 + ## Original Response + JSON + 传参规则说明。"""
    text = (
        "# API Response Information\n\n"
        "## Response Structure\n\n"
        "- **data**: 业务数据 (Type: object)\n\n"
        "## Original Response\n\n"
        '{"success":true,"code":200,"message":"请求成功","data":{"datetime":"2026-10-13 20:34:27"}}\n\n'
        "## 数据传参规则\n\n1. 严禁省略字段。\n"
    )
    env = mb.extract_api_json(text)
    assert env is not None
    assert env["success"] is True
    assert env["data"]["datetime"] == "2026-10-13 20:34:27"


def test_extract_api_json_skips_non_json_braces():
    """字段说明里的 {productName,...} 不是合法 JSON，必须被跳过。"""
    text = (
        "[160]{productName,nutritionDescription,energyKcal}:\n"
        "  巨无霸,null,520\n\n"
        '{"success":true,"code":200,"data":"ok"}'
    )
    env = mb.extract_api_json(text)
    assert env is not None and env["data"] == "ok"


def test_extract_api_json_returns_none_without_json():
    assert mb.extract_api_json("您的优惠券列表\n\n暂无可用优惠券") is None


def test_check_envelope_business_error():
    with pytest.raises(MCPError) as ei:
        mb.check_envelope("calculate-price", {"success": False, "code": 600048, "message": "外送场景下，beType=2/5/6时必传beCode"})
    assert "beCode" in str(ei.value)
    assert ei.value.code == 600048


def test_check_envelope_success_returns_data():
    assert mb.check_envelope("t", {"success": True, "data": [1, 2]}) == [1, 2]
    # 非信封对象原样返回
    assert mb.check_envelope("t", "纯文本") == "纯文本"



# ------------------------------------------------------------ 菜单骨架


def test_adapt_meals_categories_only_codes():
    """query-meals 只返回编码/分类/标签：无名称、无价格（联调实测）。"""
    raw = {
        "categories": [
            {"name": "鸡肉汉堡/卷", "meals": [{"code": "1440", "tags": ["单品"]}, {"code": "9900005462", "tags": ["套餐"]}]},
            {"name": "小食", "meals": [{"code": "4810", "tags": []}]},
            {"name": "重复分类", "meals": [{"code": "1440", "tags": ["单品"]}]},  # 去重
        ]
    }
    meals = mb.adapt_meals(raw)
    assert [m.code for m in meals] == ["1440", "9900005462", "4810"]
    assert meals[0].name == "" and meals[0].price_cents is None
    assert meals[0].category == "鸡肉汉堡/卷" and meals[0].tags == ("单品",)


def test_adapt_meal_detail():
    raw = {"code": "9900005462", "name": "板烧鸡腿堡三件套", "rounds": [{"id": 1, "name": "板烧鸡腿堡"}]}
    d = mb.adapt_meal_detail({"data": raw})
    assert d["code"] == "9900005462"
    assert d["name"] == "板烧鸡腿堡三件套"
    assert d["is_combo"] is True
    assert d["rounds"][0]["name"] == "板烧鸡腿堡"


# ------------------------------------------------------------ 算价（单位：分）


def test_adapt_price_cents_unit():
    """联调实测：官方返回整数分（2250 = ¥22.50），总价字段是 price。"""
    q = mb.adapt_price({"price": 2250, "originalPrice": 2250, "discount": 0, "productList": []})
    assert q.ok is True
    assert q.total_cents == 2250
    assert q.original_cents == 2250
    assert q.discount_cents == 0


def test_adapt_price_missing_total():
    q = mb.adapt_price({"message": "优惠券不可用"})
    assert q.ok is False and "优惠券不可用" in q.message


def test_to_cents_official():
    assert mb._to_cents_official(2250) == 2250      # 整数分（官方）
    assert mb._to_cents_official("2250") == 2250
    assert mb._to_cents_official("22.50") == 2250   # 带小数按元（防御单位变更）
    assert mb._to_cents_official(22.5) == 2250
    assert mb._to_cents_official(None) is None
    assert mb._to_cents_official("abc") is None


def test_extract_product_prices_and_names():
    q = mb.adapt_price(
        {
            "price": 4350,
            "productList": [
                {"productCode": "1440", "productName": "麦辣鸡腿汉堡", "quantity": 2, "subtotal": 4500},
                {"productCode": "4810", "productName": "中薯条", "quantity": 1, "subtotal": 1250},
            ],
        }
    )
    assert mb.extract_product_prices(q) == {"1440": 4500, "4810": 1250}
    assert mb.extract_product_names(q) == {"1440": "麦辣鸡腿汉堡", "4810": "中薯条"}


def test_build_calc_payload_with_scene():
    scene = OrderScene(store_code="1420762", order_type=2, be_type=2, be_code="BE-1")
    items = [PlanItem(MenuItem(code="1440", name="麦辣鸡腿汉堡"), 2), PlanItem(MenuItem(code="4810", name="中薯条"), 1)]
    payload = mb.build_calc_payload(scene, items, ["C1"])
    assert payload["storeCode"] == "1420762"
    assert payload["orderType"] == 2 and payload["beType"] == 2
    assert payload["beCode"] == "BE-1"
    assert payload["items"][0] == {"productCode": "1440", "quantity": 2, "couponId": "C1"}
    assert payload["items"][1] == {"productCode": "4810", "quantity": 1}


def test_scene_for_mode():
    s = mb.scene_for_mode("delivery", "S1", be_code="BE-1")
    assert (s.order_type, s.be_type, s.be_code) == (2, 2, "BE-1")
    s2 = mb.scene_for_mode("takeaway", "S1")
    assert (s2.order_type, s2.be_type) == (1, 1)
    s3 = mb.scene_for_mode("dine_in", "S1")
    assert (s3.order_type, s3.be_type) == (1, 1)


# ------------------------------------------------------------ 门店 / 优惠券 / 营养


def test_adapt_stores_with_be_code():
    stores = mb.adapt_stores(
        {
            "list": [
                {"storeCode": "1420762", "storeName": "麦当劳深圳同方信息港餐厅", "address": "朗山路11号", "distance": 403},
                {"storeCode": "1420888", "storeName": "麦当劳深圳科技园长园得来速餐厅", "beCode": "BE-9"},
            ]
        }
    )
    assert [s.code for s in stores] == ["1420762", "1420888"]
    assert stores[0].name == "麦当劳深圳同方信息港餐厅"
    assert stores[1].be_code == "BE-9"


def test_adapt_coupons_variants():
    raw = [
        {"couponId": "C1", "couponName": "满30减5", "threshold": 30, "value": 5},
        {"couponId": "C2", "couponName": "薯条半价", "discount": 0.5, "applicableMeals": ["4810"]},
        {"couponName": "缺ID的券"},  # 被跳过
    ]
    coupons = mb.adapt_coupons(raw, source="store")
    assert [c.id for c in coupons] == ["C1", "C2"]
    assert coupons[0].kind == "threshold" and coupons[0].threshold_cents == 3000 and coupons[0].value_cents == 500
    assert coupons[1].kind == "percent" and coupons[1].percent == 0.5
    assert coupons[1].applicable_codes == ("4810",)


def test_adapt_nutrition_text_format():
    """联调实测：data 是自定义分隔文本，表头定义列顺序。"""
    text = (
        "[2]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:\n"
        "  猪柳麦满分,null,1288,308,16,16,24,781,213\n"
        "  中薯条,null,1210,289,4,12,38,165,18"
    )
    items = mb.adapt_nutrition(text)
    assert len(items) == 2
    assert items[0].name == "猪柳麦满分"
    assert items[0].calories_kcal == 308.0 and items[0].protein_g == 16.0
    assert items[0].fat_g == 16.0 and items[0].carb_g == 24.0
    assert items[0].sodium_mg == 781.0 and items[0].calcium_mg == 213.0
    assert items[1].calories_kcal == 289.0


def test_adapt_nutrition_ignores_doc_lines():
    text = (
        "# 说明\n- **energyKcal**: 热量\n"
        "[1]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:\n"
        "  巨无霸,null,2176,520,27,28,44,1050,300\n"
        "结尾说明文字"
    )
    items = mb.adapt_nutrition(text)
    assert len(items) == 1 and items[0].calories_kcal == 520.0


def test_adapt_time():
    t = mb.adapt_time({"datetime": "2026-10-13T20:34:27.471", "formatted": "2026-10-13 20:34:27"})
    assert t["datetime"] == "2026-10-13T20:34:27.471"


def test_as_list_unwraps_wrappers():
    assert mb._as_list({"items": [1, 2]}) == [1, 2]
    assert mb._as_list({"data": [3]}) == [3]
    assert mb._as_list([1]) == [1]
    assert mb._as_list(None) == []
    assert mb._as_list({"x": 1}) == [{"x": 1}]


def test_safe_args_strips_secrets():
    safe = mb._safe_args({"storeCode": "X", "token": "t", "Authorization": "Bearer x", "headers": {"a": 1}})
    assert safe == {"storeCode": "X"}


def test_tools_lists():
    # query-meal-detail 在联调后已真实接入（菜单无名称，必须靠详情补全）
    assert "query-meal-detail" in mb.TOOLS_USED
    assert "create-order" in mb.TOOLS_RESERVED
    assert "delivery-query-addresses" in mb.TOOLS_RESERVED
    assert len(mb.TOOLS_USED) == 8
