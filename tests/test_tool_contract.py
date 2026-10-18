"""工具契约审计测试（2026-10-17）。

权威来源：麦当劳官方 MCP Server 的实时 `list_tools` Schema 快照
（tests/fixtures/official_tools_schema.json，2026-10-17 用真实 Token 经
项目自有客户端拉取，**verbatim 落盘**，未做任何修改）+ 官方仓库 README
（端点/Streamable HTTP/Bearer/600 次每分钟/401/429）。

本文件锁死四类不变量：
1. 项目工具注册表（TOOLS_USED/TOOLS_RESERVED）是官方 35 工具的子集（不虚构工具名）；
2. 端到端传参契约：完整 quest 流程中每个工具的实际入参，对照官方 Schema——
   必填齐备、参数名在官方属性集内（防拼错）、枚举值合法、类型正确；
3. orderType/beCode 传递规则（官方明示：到店自取 beType=1 传 beCode 会报错；
   麦乐送 beType=2 必传 beCode）；
4. 三态台账守卫：每个在用工具都必须有「官方定义已核对/本地测试覆盖/真实服务
   已验证」的显式状态登记，新增工具不允许绕过审计。

不访问网络、不使用真实 Token（假会话驱动）；真实服务验证记录见
MCP_INTEGRATION.md §3.12。
"""
import json
import types
from pathlib import Path

import pytest

from burger_quest.mcp_base import (
    OrderScene,
    TOOLS_RESERVED,
    TOOLS_USED,
    build_calc_payload,
)
from burger_quest.models import Coupon, MenuItem, PlanItem
from burger_quest.planner import coupon_sets
from burger_quest.service import run_quest
from test_http_client import (
    CALC_RESP,
    DETAIL_RESP,
    MEALS_RESP,
    NOW_TIME_RESP,
    NUTRITION_RESP,
    STORES_RESP,
    make_client,
)

FIXTURE = Path(__file__).parent / "fixtures" / "official_tools_schema.json"

# 三态台账（与 MCP_INTEGRATION.md §3.12 契约清单保持一致；改工具必须同步改这里）：
#   官方定义已核对 = 实时 list_tools Schema 快照中有定义且本测试逐项核对；
#   本地测试覆盖 = 测试套件有用例覆盖（正常+异常响应）；
#   真实服务已验证 = 2026-10-17 体检/契约审计用真实 Token 实际调用过；
#   None = 受账户条件限制未真实调用（附原因）。
CONTRACT_STATUS = {
    "now-time-info": {"real": "doctor L4/smoke/quest 多次实测返回服务器时间"},
    "query-nearby-stores": {"real": "smoke/quest 实测 5 家真实候选门店"},
    "query-meals": {"real": "quest 实测 118 个菜单编码"},
    "query-meal-detail": {"real": "smoke 实测 9900016076 -> 鸡薯双全盒"},
    "query-store-coupons": {"real": "quest 实测调用成功，返回 0 张券（账户券池为空）"},
    "query-my-coupons": {"real": "quest 实测调用成功，返回空（账户无券）"},
    "list-nutrition-foods": {"real": "quest 实测返回 160 条营养数据"},
    "calculate-price": {"real": "quest 实测 ¥15.90/¥24.00 权威价；600010 双字段契约实测"},
    "delivery-query-addresses": {"real": "quest 外送路径实测调用成功，返回空（账户无保存地址）"},
    "delivery-query-stores": {"real": None, "blocked": "账户无保存地址，无法走到可配送门店查询"},
    # 2026-10-18 第一阶段新增（只读）
    "campaign-calendar": {"real": "2026-10-18 实测返回 2026-10 月 14 个日期段 / 28 条活动（Markdown 文本）"},
    "available-coupons": {
        "real": "2026-10-18 裸 HTTP 实测：无券时官方返回 code=499 + data=null（语义化空态，非错误），"
                "已按 empty 分类并展示；有券账户的列表结构仍未验证",
    },
    "query-my-account": {
        "real": "2026-10-18 实测返回 availablePoint=0 等积分字段（数值为字符串，已容错转换）",
    },
}


@pytest.fixture(scope="module")
def official() -> dict:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data["tool_count"] == len(data["tools"]) == 35, "官方 Schema 快照与记录的工具数不一致"
    return {t["name"]: t for t in data["tools"]}


def _schema_of(official: dict, tool: str) -> dict:
    assert tool in official, f"{tool} 不在官方工具清单（疑似工具名虚构或官方已下线）"
    return official[tool].get("input_schema") or {}


def _quest_calls(responses: dict, text: str) -> list:
    """跑一次完整 quest（假会话），返回 [(tool, args), ...]（过滤 list_tools）。

    注意：必须进入客户端上下文（建立会话）——否则不会产生任何调用，
    校验循环空跑变成「假通过」。调用流非空由断言保证。
    """
    client, session = make_client(responses)
    with client:
        run_quest(text, client)
    calls = [(name, args) for name, args in session.calls if name != "list_tools"]
    assert calls, "未捕获到任何工具调用——测试替身失效，校验不可信"
    return calls


TAKEAWAY_RESPONSES = {
    "now-time-info": NOW_TIME_RESP,
    "query-nearby-stores": STORES_RESP,
    "query-meals": MEALS_RESP,
    "query-meal-detail": DETAIL_RESP,
    "query-store-coupons": {"success": True, "code": 200, "data": []},
    "query-my-coupons": {"success": True, "code": 200, "data": []},
    "list-nutrition-foods": NUTRITION_RESP,
    "calculate-price": CALC_RESP,
}
DELIVERY_RESPONSES = {
    **TAKEAWAY_RESPONSES,
    "delivery-query-addresses": {
        "success": True,
        "code": 200,
        "data": [{"addressId": "ADDR-1", "beCode": "BE-1", "address": "深圳市南山区科技园"}],
    },
    "delivery-query-stores": {
        "success": True,
        "code": 200,
        "data": [{"storeCode": "1420762", "storeName": "麦当劳深圳同方信息港餐厅", "beCode": "BE-1"}],
    },
}


# ---------------------------------------------------------------------------
# 1. 注册表与官方清单
# ---------------------------------------------------------------------------


def test_registry_is_subset_of_official(official):
    """项目使用的/预留的工具必须全部在官方 35 工具内（防虚构工具名）。"""
    assert set(TOOLS_USED) <= set(official)
    assert set(TOOLS_RESERVED) <= set(official)


def test_contract_ledger_covers_all_used_tools():
    """三态台账必须覆盖全部在用工具——新工具不允许绕过契约审计。"""
    missing = [t for t in TOOLS_USED if t not in CONTRACT_STATUS]
    assert not missing, f"以下工具缺少契约状态登记：{missing}"
    unblocked_without_evidence = [
        t for t, s in CONTRACT_STATUS.items() if s["real"] is None and "blocked" not in s
    ]
    assert not unblocked_without_evidence, "标记为未真实验证的工具必须写明阻塞原因"


def test_snapshot_provenance():
    """快照必须带出处元数据（服务器/SDK/时间），保证可追溯到官方定义。"""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data["server"] == "https://mcp.mcd.cn"
    assert data["sdk"].startswith("mcp ")
    assert data["captured_at"]


# ---------------------------------------------------------------------------
# 2. 端到端传参契约（到店 + 外送两条链路）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,responses",
    [
        ("我想吃辣，预算35，在深圳市南山区科技园，到店自取", TAKEAWAY_RESPONSES),
        ("外送到深圳市南山区科技园，预算35", DELIVERY_RESPONSES),
    ],
)
def test_end_to_end_params_match_official_schema(official, text, responses):
    """完整链路每个工具入参：必填齐备 / 无官方外参数 / 枚举合法 / 类型正确。"""
    for tool, args in _quest_calls(responses, text):
        schema = _schema_of(official, tool)
        props = schema.get("properties", {})
        required = schema.get("required", [])
        # a. 参数名必须是官方属性（防 mealCode/code 之类拼错）
        unknown = set(args) - set(props)
        assert not unknown, f"{tool} 传了官方 Schema 之外的参数：{unknown}"
        # b. 官方必填必须齐备
        missing = [k for k in required if k not in args]
        assert not missing, f"{tool} 缺少官方必填参数：{missing}"
        # c. 枚举与类型
        for key, value in args.items():
            spec = props.get(key) or {}
            if "enum" in spec:
                assert value in spec["enum"], f"{tool}.{key}={value} 不在官方枚举 {spec['enum']}"
            expected = spec.get("type")
            if expected == "integer":
                assert isinstance(value, int) and not isinstance(value, bool), f"{tool}.{key} 应为 integer"
            elif expected == "string":
                assert isinstance(value, str), f"{tool}.{key} 应为 string"
            elif expected == "boolean":
                assert isinstance(value, bool), f"{tool}.{key} 应为 boolean"
        # d. calculate-price 的 items 内部结构（官方商品级字段）
        if tool == "calculate-price":
            for entry in args["items"]:
                assert set(entry) <= {"productCode", "quantity", "couponId", "couponCode", "modification", "roundList"}, (
                    f"items 条目含官方未定义字段：{set(entry)}"
                )
                assert isinstance(entry["productCode"], str) and entry["productCode"]
                assert isinstance(entry["quantity"], int) and entry["quantity"] >= 1
                if "couponId" in entry:
                    assert isinstance(entry["couponId"], str) and entry["couponId"]


def test_becode_transfer_rules(official):
    """官方规则：到店自取(beType=1)不传 beCode（传了报错）；麦乐送(orderType=2)必传。"""
    # 到店链路：任何工具的入参都不得出现 beCode
    for tool, args in _quest_calls(TAKEAWAY_RESPONSES, "我想吃辣，预算35，在深圳市南山区科技园，到店自取"):
        assert "beCode" not in args, f"到店自提场景 {tool} 不得传 beCode（官方：无效参数，传了报错）"
        props = _schema_of(official, tool).get("properties", {})
        if "beType" in props:
            assert args.get("beType") == 1, f"到店链路 {tool}.beType 应为 1，实际 {args.get('beType')}"
        if "orderType" in props:
            assert args.get("orderType") == 1, f"到店链路 {tool}.orderType 应为 1，实际 {args.get('orderType')}"
    # 外送链路（2026-10-18 起）：delivery-query-stores 未经真实验证已移除。
    # 外送输入只调用 delivery-query-addresses；有地址则明确提示能力未接入，
    # 不再进入 菜单/详情/算价 链路，因此本项目不存在 beCode 传递场景。
    menu_tools = {"query-meals", "query-meal-detail", "query-store-coupons", "calculate-price"}
    delivery_called = {tool for tool, _ in _quest_calls(DELIVERY_RESPONSES, "外送到深圳市南山区科技园，预算35")}
    assert "delivery-query-addresses" in delivery_called
    assert not (menu_tools & delivery_called), "外送不再进入菜单/详情/算价链路"


def test_nearby_stores_enum_values(official):
    """query-nearby-stores：beType 官方枚举只有 [1,5]（到店自提/得来速），外送不能走它。"""
    props = _schema_of(official, "query-nearby-stores")["properties"]
    assert props["beType"]["enum"] == [1, 5]
    assert props["searchType"]["enum"] == [1, 2]
    for tool, args in _quest_calls(TAKEAWAY_RESPONSES, "我想吃辣，预算35，在深圳市南山区科技园，到店自取"):
        if tool == "query-nearby-stores":
            assert args["beType"] in (1, 5) and args["searchType"] == 2


# ---------------------------------------------------------------------------
# 3. calculate-price 券挂载契约（2026-10-17 审计修复验证）
# ---------------------------------------------------------------------------


def _coupon(cid: str, code: str = "CODE") -> Coupon:
    return Coupon(id=cid, name=f"券{cid}", code=code)


def test_single_coupon_attached_with_both_fields():
    scene = OrderScene(store_code="S1", order_type=1, be_type=1)
    items = [PlanItem(MenuItem(code="A", name="甲"), 1)]
    payload = build_calc_payload(scene, items, [_coupon("C1")])
    assert payload["items"][0]["couponId"] == "C1"
    assert payload["items"][0]["couponCode"] == "CODE"


def test_two_coupons_attach_to_distinct_items():
    """官方 Schema：couponId/couponCode 是商品级字段——两张券挂两件不同商品。"""
    scene = OrderScene(store_code="S1", order_type=1, be_type=1)
    items = [PlanItem(MenuItem(code="A", name="甲"), 1), PlanItem(MenuItem(code="B", name="乙"), 1)]
    payload = build_calc_payload(scene, items, [_coupon("C1"), _coupon("C2")])
    assert payload["items"][0]["couponId"] == "C1"
    assert payload["items"][1]["couponId"] == "C2"
    assert payload["items"][0]["couponCode"] == "CODE" and payload["items"][1]["couponCode"] == "CODE"


def test_extra_coupons_beyond_items_are_not_attached():
    """券多于商品时只挂得下的——不产生挂到不存在商品上的幽灵券字段。"""
    scene = OrderScene(store_code="S1", order_type=1, be_type=1)
    items = [PlanItem(MenuItem(code="A", name="甲"), 1)]
    payload = build_calc_payload(scene, items, [_coupon("C1"), _coupon("C2")])
    assert payload["items"][0]["couponId"] == "C1"
    assert sum(1 for e in payload["items"] if "couponId" in e) == 1


def test_coupon_sets_respect_item_capacity():
    """试探生成受商品数约束：1 件商品只探单券（挂不下的券不生成试探）。"""
    one = [PlanItem(MenuItem(code="A", name="甲"), 1)]
    two = one + [PlanItem(MenuItem(code="B", name="乙"), 1)]
    pool = [_coupon("C1"), _coupon("C2")]
    assert len(coupon_sets(one, pool, 2000)) == 1
    sets = coupon_sets(two, pool, 2000)
    assert len(sets) == 2 and [len(s) for s in sets] == [1, 2]


def test_mock_discount_matches_payload_not_input_list():
    """Mock 必须按 payload 实际提交的券算折扣——演示不得比真实宽松（契约审计修复）。"""
    from burger_quest.mcp_mock import MockMCPClient

    mock = MockMCPClient()
    scene = OrderScene(store_code="DEMO-001", order_type=1, be_type=1)
    items = [
        PlanItem(MenuItem(code="D1002", name="麦辣鸡腿堡", category="鸡肉汉堡", price_cents=2100), 1),
        PlanItem(MenuItem(code="D3001", name="薯条（大）", category="小食", price_cents=1250), 1),
    ]
    coupons = list(mock.store_coupons(scene)) + list(mock.my_coupons())
    c30 = next(c for c in coupons if c.id == "C-DEMO-30-5")  # 满30减5（有门槛）
    cspicy = next(c for c in coupons if c.id == "C-DEMO-SPICY")  # 立减3（无门槛）
    # 两张券两件商品：都挂上 -> mock 按两张算
    q2 = mock.calculate_price(scene, items, [c30, cspicy])
    assert q2.ok and q2.discount_cents == 800  # 500 + 300
    # 券多于商品：只有第一张（无门槛券）挂得上 -> 只认第一张，第二张不生效
    q_overflow = mock.calculate_price(scene, items[:1], [cspicy, c30])
    assert q_overflow.ok and q_overflow.discount_cents == 300  # 只算挂上的立减3
    # 挂上的券门槛不达 -> 官方语义整单拒绝（与真实服务一致），不偷偷给折扣
    q_threshold = mock.calculate_price(scene, items[:1], [c30, cspicy])
    assert not q_threshold.ok and "未达使用门槛" in q_threshold.message


# ---------------------------------------------------------------------------
# 4. 响应防御性解析（缺失字段 / null / 空列表 / 类型变化）
# ---------------------------------------------------------------------------


def _resp(data):
    return {"success": True, "code": 200, "data": data}


@pytest.mark.parametrize(
    "container",
    [
        [{"addressId": "A1"}],  # 裸列表
        {"list": [{"addressId": "A1"}]},  # list 容器
        {"items": [{"addressId": "A1"}]},  # items 容器
        {"data": [{"addressId": "A1"}]},  # data 容器
        {"records": [{"addressId": "A1"}]},  # records 容器
        {"addresses": [{"addressId": "A1"}]},  # addresses 容器（联调推测名）
    ],
)
def test_delivery_addresses_container_variants(container):
    """delivery-query-addresses 容器结构官方未定义——兼容多种形态，取不到即空。"""
    from burger_quest.mcp_base import _as_list

    out = _as_list(container)
    assert [a.get("addressId") for a in out if isinstance(a, dict)] == ["A1"]


@pytest.mark.parametrize("data", [None, [], {}, "暂无可用地址", 123, [None, "坏条目", {"addressId": "A1"}]])
def test_delivery_addresses_degenerate_responses(data):
    """null/空/纯文本/数字/坏条目：不崩溃、不猜测地址。"""
    from test_http_client import wrap_api_text

    client, _ = make_client(
        raw_results={
            "delivery-query-addresses": types.SimpleNamespace(
                content=[types.SimpleNamespace(text=wrap_api_text(_resp(data)))]
            )
        }
    )
    with client:
        out = client.delivery_addresses()
    assert isinstance(out, list)
    assert all(isinstance(a, dict) for a in out)
    if isinstance(data, list) and any(isinstance(x, dict) for x in data):
        assert [a.get("addressId") for a in out] == ["A1"]
    else:
        assert out == []


@pytest.mark.parametrize(
    "tool,data,expect",
    [
        ("query-nearby-stores", None, []),  # null -> 空门店（上层明示未找到）
        ("query-nearby-stores", "文本", []),  # 非 JSON 信封已被 _call 拦截，这里测已解包 data 的边界
        ("query-store-coupons", None, []),  # null 券池
        ("query-my-coupons", [], []),  # 空列表
        ("query-meals", None, []),  # null 菜单
        ("query-meals", {"categories": None}, []),  # categories 为 null
        ("query-meals", {"categories": {"不是": "列表"}}, []),  # categories 类型变化
        ("query-meals", {"categories": [None, 42, {"name": "主食", "meals": None}]}, []),  # 坏分类条目
        ("list-nutrition-foods", "没有表头的营养文本", []),  # 自定义文本表头缺失
        ("list-nutrition-foods", "", []),  # 空文本
    ],
)
def test_null_and_type_change_responses_are_graceful(tool, data, expect):
    """缺失/null/类型变化/空容器：明确为空或丢弃坏条目，绝不猜测填空。"""
    client, _ = make_client({tool: _resp(data)})
    with client:
        if tool == "query-nearby-stores":
            assert client.nearby_stores("科技园", city="深圳市") == expect
        elif tool == "query-store-coupons":
            scene = OrderScene(store_code="S1", order_type=1, be_type=1)
            assert client.store_coupons(scene) == expect
        elif tool == "query-my-coupons":
            assert client.my_coupons() == expect
        elif tool == "query-meals":
            scene = OrderScene(store_code="S1", order_type=1, be_type=1)
            assert client.query_meals(scene) == expect
        elif tool == "list-nutrition-foods":
            assert client.list_nutrition_foods() == expect


def test_meal_detail_null_data_yields_no_name():
    """详情返回 null：名称为空串（上层计数跳过，不编造餐品名）。"""
    client, _ = make_client({"query-meal-detail": _resp(None)})
    with client:
        scene = OrderScene(store_code="S1", order_type=1, be_type=1)
        detail = client.meal_detail(scene, "1440")
    assert detail["name"] == "" and detail["rounds"] == []


def test_calculate_price_null_data_is_not_ok():
    """算价返回 null：ok=False（服务层据此停止出方案，不猜价格）。"""
    from burger_quest.models import PlanItem as _PI

    client, _ = make_client({"calculate-price": _resp(None)})
    with client:
        scene = OrderScene(store_code="S1", order_type=1, be_type=1)
        quote = client.calculate_price(scene, [_PI(MenuItem(code="A", name="甲"), 1)], [])
    assert quote.ok is False


def test_service_counts_detail_without_name():
    """详情成功但无名称：服务层如实计数并警告，随后诚实停止（2026-10-17 契约审计新增）。

    流程事实：productList 不含名称、详情也返回空名称 -> 4 个短名单商品全部被
    跳过并计数警告，named 为空 -> 停止出方案。两个事实同时成立：跳过≠静默，
    停止≠编造。
    """
    responses = dict(TAKEAWAY_RESPONSES)
    # 让算价 productList 只含 1440 且不给名称 -> 全部回退详情；详情返回无名称
    responses["calculate-price"] = {
        "success": True,
        "code": 200,
        "data": {
            "price": 2250,
            "originalPrice": 2250,
            "discount": 0,
            "productList": [{"productCode": "1440", "quantity": 1, "subtotal": 2250}],
        },
    }
    responses["query-meal-detail"] = _resp({"code": "1440", "rounds": [], "name": ""})
    client, _ = make_client(responses)
    with client:
        result = run_quest("我想吃辣，预算35，在深圳市南山区科技园，到店自取", client)
    assert any("未返回名称" in w for w in result.warnings)
    # 全部无名称 -> 诚实停止（不编造餐品名硬出方案）
    assert result.chosen is None
    assert any("未能获取任何餐品名称" in w for w in result.warnings)
