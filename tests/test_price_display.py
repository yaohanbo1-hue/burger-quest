"""餐品查询与价格展示专项测试（2026-10-17 价格审计新增）。

覆盖（对应审计要求 3/4/6/7/8）：
1. 金额单位转换：官方整数=分；小数/带点字符串=元；None/bool/空串不可解析；
   绝不出现 100 倍放大/缩小；
2. 价格语义区分：原价（originalPrice）/ 已核实优惠（discount）/ 配送费 /
   应付总价（price，用户最终支付）——只认 calculate-price 权威返回；
3. 行小计归一：quantity>1 时 subtotal ÷ quantity 为单价；0/负/缺失不猜价格；
4. 空价格/零价格/负价格：排除并明示，不进入组合、不充当预算空间；
5. 价格适用范围提示：卡片/命令行/Markdown 三处一致（门店+渠道+时间；演示数据明示）；
6. 预算边界：总价==预算算预算内（剩余 ¥0）；超 1 分算超预算；
7. 排序确定性：同分按总价与商品编码决胜，可复现。

真实服务证据（2026-10-17 17:01，门店 1420762，官方 Token 只读）：
- 3 件商品各 1 件：originalPrice=7960 == sum(subtotal)=7960，price=7960，
  discount=0，fee=None（到店）；金额单位为分（1590=¥15.90）；
- quantity=2：subtotal=3180=2×1590（行小计语义，归一化 1590 正确）。
"""
import pytest

from burger_quest.cards import render_html, render_markdown
from burger_quest.mcp_base import (
    OrderScene,
    adapt_price,
    build_calc_payload,
    extract_product_prices,
    yuan_to_cents,
    _to_cents_official,
)
from burger_quest.models import (
    Constraints,
    Coupon,
    MenuItem,
    PlanItem,
    PlanOption,
    PriceQuote,
    Store,
    format_yuan,
)
from burger_quest.planner import build_plan
from burger_quest.rpg import price_scope_lines
from burger_quest.service import run_quest
from test_http_client import make_client

SCENE = OrderScene(store_code="1420762", order_type=1, be_type=1)


def _resp(data):
    return {"success": True, "code": 200, "data": data}


# ---------------------------------------------------------------------------
# 1. 金额单位转换（分/元/字符串/None）——防 100 倍误差
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (2250, 2250),          # 官方整数 = 分（1590=¥15.90，联调实测）
        ("2250", 2250),        # 整数字符串 = 分
        (0, 0),                # 零（调用方按非正价格排除）
        (-500, -500),          # 异常负值（调用方按非正价格排除）
        (99999999, 99999999),  # 大整数仍是分，不会 100 倍膨胀
        (22.5, 2250),          # 小数 = 元 -> 分
        ("22.50", 2250),       # 带点字符串 = 元 -> 分
        ("15.9", 1590),
        (None, None),          # 缺失不可解析
        (True, None),          # bool 不是金额
        ("", None),
        ("abc", None),
    ],
)
def test_to_cents_official_unit_rules(raw, expected):
    assert _to_cents_official(raw) == expected


def test_yuan_to_cents_handles_currency_markers():
    """优惠券面额按「元」解析，容忍 ¥/元/逗号/引号。"""
    assert yuan_to_cents("¥25.5") == 2550
    assert yuan_to_cents("25.5元") == 2550
    assert yuan_to_cents("1,200") == 120000
    assert yuan_to_cents(None) is None


def test_format_yuan_roundtrip():
    assert format_yuan(2550) == "¥25.50"
    assert format_yuan(1590) == "¥15.90"
    assert format_yuan(0) == "¥0.00"
    assert format_yuan(-100) == "-¥1.00"
    assert format_yuan(None) == "价格待确认"


# ---------------------------------------------------------------------------
# 2. 官方返回适配：价格四段语义
# ---------------------------------------------------------------------------


def test_adapt_price_four_segments_from_official_shape():
    """官方返回 {price, originalPrice, discount, deliveryFee, productList} -> 四段。"""
    q = adapt_price(
        _resp(
            {
                "price": 7960,
                "originalPrice": 7960,
                "discount": 0,
                "productList": [
                    {"productCode": "A", "productName": "甲", "quantity": 1, "subtotal": 1590},
                    {"productCode": "B", "productName": "乙", "quantity": 1, "subtotal": 6370},
                ],
            }
        )
    )
    assert q.ok
    assert q.total_cents == 7960        # 应付（最终支付）
    assert q.original_cents == 7960     # 原价
    assert q.discount_cents == 0        # 已核实优惠
    assert q.delivery_fee_cents is None  # 到店无配送费字段
    assert extract_product_prices(q) == {"A": 1590, "B": 6370}


def test_adapt_price_missing_total_is_not_ok():
    """缺应付总价：ok=False——服务层据此停止，不用单价加总冒充。"""
    q = adapt_price(_resp({"productList": []}))
    assert q.ok is False and "应付总价" in q.message


def test_adapt_price_null_data_is_not_ok():
    assert adapt_price(_resp(None)).ok is False


def test_adapt_price_unit_defense_decimal():
    """金额单位防御：带小数按元（防官方改口径导致 100 倍误差）。"""
    q = adapt_price(_resp({"price": "79.60", "originalPrice": "79.60"}))
    assert q.ok and q.total_cents == 7960


def test_extract_product_prices_normalizes_line_subtotals():
    """行小计归一：quantity>=2 的 subtotal ÷ quantity；异常数量不猜。"""
    q = adapt_price(
        _resp(
            {
                "price": 3180,
                "originalPrice": 3180,
                "discount": 0,
                "productList": [
                    {"productCode": "A", "quantity": 2, "subtotal": 3180},   # 2×1590
                    {"productCode": "B", "quantity": 1, "subtotal": 1000},
                    {"productCode": "C", "quantity": None, "subtotal": 777},  # 缺数量按 1 件
                    {"productCode": "D", "quantity": 0, "subtotal": 500},     # 除零保护：原样
                    {"productCode": "E", "quantity": 1, "subtotal": None},    # 缺小计：跳过
                ],
            }
        )
    )
    assert extract_product_prices(q) == {"A": 1590, "B": 1000, "C": 777, "D": 500}


def test_extract_product_prices_keeps_nonpositive_for_caller_filtering():
    """0/负小计在此处不过滤（忠实提取）——由 service 结合语境排除并明示。"""
    q = adapt_price(
        _resp(
            {
                "price": 0,
                "originalPrice": 500,
                "discount": 500,
                "productList": [{"productCode": "Z", "quantity": 1, "subtotal": 0}],
            }
        )
    )
    assert extract_product_prices(q) == {"Z": 0}


# ---------------------------------------------------------------------------
# 3. 服务层：空/零/负价格不进入组合（要求 6/7）
# ---------------------------------------------------------------------------


def _discovery_responses(product_list, price=2250):
    return {
        "now-time-info": {"success": True, "code": 200, "data": {"datetime": "T"}},
        "query-nearby-stores": {
            "success": True,
            "code": 200,
            "data": [{"storeCode": "1420762", "storeName": "测试门店"}],
        },
        "query-meals": {
            "success": True,
            "code": 200,
            "data": {
                "categories": [
                    {"name": "主食", "meals": [{"code": "A", "tags": ["单品"]}, {"code": "Z0", "tags": []}]},
                    {"name": "小食", "meals": [{"code": "S", "tags": []}]},
                ]
            },
        },
        "query-meal-detail": {"success": True, "code": 200, "data": {"code": "A", "name": "甲"}},
        "query-store-coupons": {"success": True, "code": 200, "data": []},
        "query-my-coupons": {"success": True, "code": 200, "data": []},
        "list-nutrition-foods": {"success": True, "code": 200, "data": ""},
        "calculate-price": _resp({"price": price, "originalPrice": price, "discount": 0, "productList": product_list}),
    }


def test_zero_priced_item_excluded_with_warning():
    """0 元商品：排除 + 明示（0 元不该免费进组合、也不该充当预算空间）。"""
    responses = _discovery_responses(
        [
            {"productCode": "A", "productName": "甲", "quantity": 1, "subtotal": 2250},
            {"productCode": "Z0", "productName": "零元怪", "quantity": 1, "subtotal": 0},
        ]
    )
    client, _ = make_client(responses)
    with client:
        result = run_quest("想吃汉堡，预算50，在深圳市南山区科技园，到店自取", client)
    assert any("非正价格" in w for w in result.warnings)
    assert result.chosen is not None
    assert all(pi.item.code != "Z0" for opt in result.options for pi in opt.items)


def test_all_items_unpriced_fails_with_price_unverifiable_message():
    """全部无价格：诚实失败，措辞明示「价格暂不可验证」而非笼统无价格。"""
    responses = _discovery_responses([])
    client, _ = make_client(responses)
    with client:
        result = run_quest("想吃汉堡，预算50，在深圳市南山区科技园，到店自取", client)
    assert result.chosen is None
    assert any("价格暂不可验证" in w for w in result.warnings)
    # 不编造任何价格：没有任何带总价的方案
    assert not result.options


def test_partial_unpriced_items_excluded_not_estimated():
    """部分无价格：排除 + 明示排除了几件（不估算价格）。"""
    responses = _discovery_responses(
        [
            {"productCode": "A", "productName": "甲", "quantity": 1, "subtotal": 2250},
            # S 无价格行
        ]
    )
    client, _ = make_client(responses)
    with client:
        result = run_quest("想吃汉堡，预算50，在深圳市南山区科技园，到店自取", client)
    assert any("未返回真实价格" in w and "已从组合中排除" in w for w in result.warnings)
    assert result.chosen is not None


# ---------------------------------------------------------------------------
# 4. 预算边界（要求 8）
# ---------------------------------------------------------------------------


def _menu_with_prices(*pairs):
    return [MenuItem(code=code, name=name, category=cat, price_cents=cents) for code, name, cat, cents in pairs]


def test_budget_boundary_equal_is_within():
    """总价 == 预算：算预算内（剩余 ¥0），不算超预算。"""
    menu = _menu_with_prices(("A", "甲", "主食", 1300))
    c = Constraints(budget_cents=1300)
    price = lambda items, coupons: PriceQuote(ok=True, total_cents=1300, original_cents=1300, discount_cents=0)
    options, _ = build_plan(c, menu, [], [], price)
    assert options and options[0].within_budget is True and options[0].remaining_cents == 0


def test_budget_boundary_one_cent_over():
    """总价 = 预算 + 1 分：超预算 ¥0.01，如实标注。"""
    menu = _menu_with_prices(("A", "甲", "主食", 1301))
    c = Constraints(budget_cents=1300)
    price = lambda items, coupons: PriceQuote(ok=True, total_cents=1301, original_cents=1301, discount_cents=0)
    options, warnings = build_plan(c, menu, [], [], price)
    assert options  # 无预算内方案时给最接近参考
    assert options[0].within_budget is False and options[0].remaining_cents == -1
    assert any("超出预算" in u for u in options[0].unmet)


def test_unpriced_item_never_enters_combination():
    """无价格餐品不进任何组合（要求 7：不能把无价餐品当符合预算）。"""
    menu = [
        MenuItem(code="A", name="甲", category="主食", price_cents=2100),
        MenuItem(code="N", name="无价美食", category="主食", price_cents=None),
    ]
    c = Constraints(budget_cents=5000)
    price = lambda items, coupons: PriceQuote(ok=True, total_cents=2100, original_cents=2100, discount_cents=0)
    options, warnings = build_plan(c, menu, [], [], price)
    assert options
    for opt in options:
        assert all(pi.item.code != "N" for pi in opt.items)
    assert any("未返回真实价格" in w for w in warnings)


def test_price_sorting_deterministic():
    """同分方案按总价与商品编码决胜——同样输入同样输出。"""
    menu = _menu_with_prices(("A", "甲", "主食", 2000), ("B", "乙", "主食", 2000))
    c = Constraints(budget_cents=9000)
    price = lambda items, coupons: PriceQuote(
        ok=True, total_cents=sum(pi.item.price_cents * pi.qty for pi in items),
        original_cents=sum(pi.item.price_cents * pi.qty for pi in items), discount_cents=0,
    )
    o1, _ = build_plan(c, menu, [], [], price)
    o2, _ = build_plan(c, menu, [], [], price)
    sig = lambda opts: [tuple(sorted((pi.item.code, pi.qty) for pi in o.items)) for o in opts]
    assert sig(o1) == sig(o2) and len(o1) >= 2


# ---------------------------------------------------------------------------
# 5. 价格适用范围提示（要求 5）
# ---------------------------------------------------------------------------


def _option(total=2400):
    return PlanOption(
        title="t",
        items=[PlanItem(MenuItem(code="A", name="龙焰鸡腿堡", category="主食", price_cents=total), 1)],
        total_cents=total,
        original_cents=total,
        price_checked=True,
    )


def test_card_html_has_price_scope_and_unit_notes():
    c = Constraints(budget_cents=3500, dining_mode="takeaway")
    html = render_html(c, _option(), Store(code="1420762", name="同方信息港"), "", "麦当劳 MCP 实时数据", "2026-10-17 17:00")
    assert "适用范围" in html and "1420762" in html
    assert "到店自取" in html and "2026-10-17 17:00" in html
    assert "行小计归一" in html and "套餐组合价" in html


def test_card_markdown_has_price_scope_and_unit_notes():
    c = Constraints(budget_cents=3500, dining_mode="delivery")
    md = render_markdown(c, _option(), Store(code="1420762", name="同方信息港"), "", "麦当劳 MCP 实时数据", "2026-10-17 17:00")
    assert "**适用范围**" in md and "1420762" in md and "外送（麦乐送）" in md
    assert "行小计归一" in md


def test_demo_card_states_demo_prices_not_real():
    """演示数据必须明示非实时——不能把演示价当真实价格。"""
    c = Constraints(budget_cents=3500)
    html = render_html(c, _option(), Store(code="DEMO-001"), "", "演示数据", "", demo=True)
    assert "演示数据" in html and "不构成任何真实价格" in html
    md = render_markdown(c, _option(), Store(code="DEMO-001"), "", "演示数据", "", demo=True)
    assert "非实时菜单" in md


def test_cli_scope_lines_cover_store_and_channel():
    c = Constraints(dining_mode="takeaway")
    lines = price_scope_lines(Store(code="1420762", name="x"), c)
    assert any("1420762" in ln and "到店自取" in ln for ln in lines)
    assert any("行小计归一" in ln for ln in lines)
    demo_lines = price_scope_lines(None, c, demo=True)
    assert any("演示数据" in ln for ln in demo_lines)


def test_coupon_pairing_still_required_in_payload():
    """券字段双提交契约（600010）在 payload 层保持：只传 ID 不传 code。"""
    scene = OrderScene(store_code="S", order_type=1, be_type=1)
    items = [PlanItem(MenuItem(code="A", name="甲"), 1)]
    payload = build_calc_payload(scene, items, [Coupon(id="C1", name="券", code="")])
    assert payload["items"][0]["couponId"] == "C1" and "couponCode" not in payload["items"][0]
