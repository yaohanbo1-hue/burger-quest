"""官方 calculate-price 优惠券双字段契约回归（2026-10-16 真实联调发现）。

背景：真实联调官方 MCP 时，只提交 items[].couponId 会被整单拒绝：

    code=600010  使用优惠券需要couponId和couponCode

这意味着「有券账户的优惠永远不生效」——不是策略问题，是协议字段缺失。

本文件锁定三条不变量：
1. 券适配器能从官方响应里解出 couponCode；
2. 算价入参同时携带 couponId 与 couponCode（挂第一件商品）；
3. Mock 忠实还原官方 600010 ——只传 ID 必须失败，演示数据也不能比真实服务端宽松。

诚实性边界：本组测试全部是协议契约测试（mock/schema 层）。
真实有券账户的优惠实效仍未验证，见 MCP_INTEGRATION.md 待验证项。
"""
import pytest

from burger_quest.mcp_base import (
    OrderScene,
    adapt_coupons,
    build_calc_payload,
    resolve_coupon_pair,
)
from burger_quest.mcp_mock import MockMCPClient
from burger_quest.models import Coupon, MenuItem, PlanItem

STORE = "S-1001"
SCENE = OrderScene(store_code=STORE, order_type=1, be_type=1)

# 演示数据里的真实门店与商品编码（mock 只认这些，否则报「价格缺失」）
DEMO_STORE = "DEMO-001"
DEMO_SCENE = OrderScene(store_code=DEMO_STORE, order_type=1, be_type=1)
DEMO_BURGER = "D1002"  # 麦辣鸡腿堡
DEMO_FRIES = "D3001"  # 薯条（大）
DEMO_COUPON_ID = "C-DEMO-FRIES-HALF"
DEMO_COUPON_CODE = "CODE-C-DEMO-FRIES-HALF"


def burger():
    return MenuItem(code="B1", name="麦辣鸡腿堡", category="鸡肉汉堡", price_cents=2100)


def plan_item(qty=1):
    return PlanItem(item=burger(), qty=qty)


def demo_item(code, name, price_cents, qty=1):
    return PlanItem(item=MenuItem(code=code, name=name, category="演示", price_cents=price_cents), qty=qty)


# ---------------------------------------------------------------------------
# 券适配：必须解出 couponCode
# ---------------------------------------------------------------------------


def test_adapt_coupons_reads_coupon_code():
    """官方券响应带 couponCode 时必须被解析（否则算价永远 600010）。"""
    out = adapt_coupons(
        [
            {
                "couponId": "CP-1",
                "couponCode": "CODE-CP-1",
                "couponName": "满20减5",
                "threshold": "20",
                "discount": "5",
            }
        ],
        source="store",
    )
    assert len(out) == 1
    assert out[0].id == "CP-1"
    assert out[0].code == "CODE-CP-1"


def test_adapt_coupons_missing_code_is_empty_not_invented():
    """响应没有 couponCode 时留空——绝不伪造券码（伪造等于编造真实优惠）。"""
    out = adapt_coupons([{"couponId": "CP-2", "couponName": "赠品券"}], source="store")
    assert out[0].id == "CP-2"
    assert out[0].code == ""


def test_adapt_coupons_accepts_snake_case_alias():
    """字段命名容错：coupon_code 视为同义（不因命名差异丢码）。"""
    out = adapt_coupons([{"couponId": "CP-3", "couponName": "测试券", "coupon_code": "CODE-CP-3"}], source="my")
    assert out[0].code == "CODE-CP-3"


# ---------------------------------------------------------------------------
# resolve_coupon_pair：三种券形态统一
# ---------------------------------------------------------------------------


def test_resolve_pair_from_coupon_object():
    assert resolve_coupon_pair(Coupon(id="C1", name="券", code="CODE-C1")) == ("C1", "CODE-C1")


def test_resolve_pair_from_tuple():
    assert resolve_coupon_pair(("C1", "CODE-C1")) == ("C1", "CODE-C1")


def test_resolve_pair_from_plain_id_has_no_code():
    """纯 id 只能得到 id——这正是官方会拒绝的形态，不得凭空补码。"""
    assert resolve_coupon_pair("C1") == ("C1", "")


def test_resolve_pair_from_none():
    assert resolve_coupon_pair(None) == ("", "")


# ---------------------------------------------------------------------------
# build_calc_payload：双字段同时提交
# ---------------------------------------------------------------------------


def test_payload_carries_both_coupon_id_and_code():
    payload = build_calc_payload(SCENE, [plan_item()], [Coupon(id="C1", name="券", code="CODE-C1")])
    entry = payload["items"][0]
    assert entry["couponId"] == "C1"
    assert entry["couponCode"] == "CODE-C1"


def test_payload_coupon_attached_to_first_item_only():
    """券只挂第一件商品：第二件不得重复携带券字段。"""
    items = [plan_item(1), PlanItem(item=MenuItem(code="S1", name="薯条", category="小食", price_cents=1250), qty=1)]
    payload = build_calc_payload(SCENE, items, [Coupon(id="C1", name="券", code="CODE-C1")])
    assert "couponId" in payload["items"][0]
    assert "couponId" not in payload["items"][1]
    assert "couponCode" not in payload["items"][1]


def test_payload_without_coupon_has_no_coupon_fields():
    payload = build_calc_payload(SCENE, [plan_item()], [])
    entry = payload["items"][0]
    assert "couponId" not in entry
    assert "couponCode" not in entry


def test_payload_tuple_form_also_emits_code():
    payload = build_calc_payload(SCENE, [plan_item()], [("C1", "CODE-C1")])
    assert payload["items"][0]["couponCode"] == "CODE-C1"


def test_payload_no_code_field_when_code_absent():
    """code 为空时不能提交空 couponCode 字段——官方对空串会再次报错。"""
    payload = build_calc_payload(SCENE, [plan_item()], [Coupon(id="C1", name="券", code="")])
    assert payload["items"][0]["couponId"] == "C1"
    assert "couponCode" not in payload["items"][0]


def test_payload_preserves_scene_and_quantity():
    payload = build_calc_payload(SCENE, [plan_item(2)], [Coupon(id="C1", name="券", code="CODE-C1")])
    assert payload["storeCode"] == STORE
    assert payload["orderType"] == 1
    assert payload["beType"] == 1
    assert payload["items"][0]["quantity"] == 2


def test_payload_delivery_scene_includes_be_code():
    scene = OrderScene(store_code=STORE, order_type=2, be_type=2, be_code="BC-77")
    payload = build_calc_payload(scene, [plan_item()], [Coupon(id="C1", name="券", code="CODE-C1")])
    assert payload["beCode"] == "BC-77"


# ---------------------------------------------------------------------------
# Mock 忠实还原官方 600010
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_client():
    return MockMCPClient()


def test_mock_rejects_coupon_id_without_code(mock_client):
    """只给 couponId：mock 必须像官方一样报 600010，而不是偷偷给折扣。"""
    quote = mock_client.calculate_price(DEMO_SCENE, [demo_item(DEMO_BURGER, "麦辣鸡腿堡", 2100)], [DEMO_COUPON_ID])
    assert not quote.ok
    assert "600010" in (quote.message or "") or "couponCode" in (quote.message or "")


def test_mock_accepts_coupon_id_with_code(mock_client):
    """双字段齐备：mock 应正常算价（真实优惠幅度仍以官方为准）。"""
    quote = mock_client.calculate_price(
        DEMO_SCENE,
        [demo_item(DEMO_FRIES, "薯条（大）", 1250)],
        [(DEMO_COUPON_ID, DEMO_COUPON_CODE)],
    )
    assert quote.ok


def test_mock_accepts_coupon_object(mock_client):
    coupon = Coupon(id=DEMO_COUPON_ID, name="薯条半价尝鲜券（演示券）", code=DEMO_COUPON_CODE, percent=0.5)
    quote = mock_client.calculate_price(DEMO_SCENE, [demo_item(DEMO_FRIES, "薯条（大）", 1250)], [coupon])
    assert quote.ok


def test_mock_discount_uses_authority_not_face_value(mock_client):
    """券面额只是线索，最终金额以 mock 权威返回为准，恒满足 原价-优惠=现价。"""
    quote = mock_client.calculate_price(
        DEMO_SCENE,
        [demo_item(DEMO_FRIES, "薯条（大）", 1250)],
        [(DEMO_COUPON_ID, DEMO_COUPON_CODE)],
    )
    assert quote.ok
    assert quote.original_cents and quote.total_cents
    assert quote.discount_cents >= 0
    assert quote.total_cents + quote.discount_cents == quote.original_cents


def test_mock_delivery_scene_charges_fee_only_when_order_type_2(mock_client):
    """到店不应产生配送费；券照走双字段契约。"""
    dine_in = mock_client.calculate_price(
        DEMO_SCENE, [demo_item(DEMO_BURGER, "麦辣鸡腿堡", 2100)], [(DEMO_COUPON_ID, DEMO_COUPON_CODE)]
    )
    assert dine_in.ok
    assert not dine_in.delivery_fee_cents

    delivery = OrderScene(store_code=DEMO_STORE, order_type=2, be_type=2, be_code="BE-DEMO-001")
    home = mock_client.calculate_price(
        delivery, [demo_item(DEMO_BURGER, "麦辣鸡腿堡", 2100)], [(DEMO_COUPON_ID, DEMO_COUPON_CODE)]
    )
    assert home.ok
    assert home.delivery_fee_cents


# ---------------------------------------------------------------------------
# 演示数据契约：Demo 券也必须带 couponCode
# ---------------------------------------------------------------------------


def test_demo_coupons_all_carry_code(mock_client):
    """离线演示若有券，券必须带 couponCode ——否则演示会撞上官方同款600010。"""
    all_coupons = list(mock_client.store_coupons(DEMO_SCENE)) + list(mock_client.my_coupons())
    if not all_coupons:
        pytest.skip("演示数据当前无券")
    for c in all_coupons:
        assert c.code, f"演示券 {c.id} 缺少 couponCode，演示与真实协议不一致"