"""服务编排层：把 解析 -> MCP 检索 -> 规划 -> RPG 文案 -> 分享卡 串成一次任务。

错误处理约定（对应比赛要求）：
- 任何 MCP 失败都不伪造数据：失败步骤记入 warnings，明确告诉用户哪一步不可用；
- 门店无法确定（无位置/查不到/多候选未选）= 无法生成方案，不猜门店；
- 菜单查询失败 = 无法生成方案（没有菜单就没有真实餐品）；
- 价格计算失败 = 无法生成方案（没有权威总价就不给方案）；
- 没有符合预算的组合 = 如实说明，并给出最接近的真实价格方案作参考。

真实数据流（2026-10-13 联调实测后的官方工具链）：
1. 门店：到店场景 query-nearby-stores（searchType=2 + city/keyword）；
   外送场景 delivery-query-addresses -> delivery-query-stores（拿 beCode）。
2. 菜单骨架：query-meals 只返回「编码+分类+标签」，没有名称和价格。
3. 名称：对短名单商品逐个 query-meal-detail。
4. 价格：一次 calculate-price 传入全部短名单商品（各 1 件），
   从返回的 productList 拿到每件商品的真实单价（分）——权威来源，不是估算。
5. 组合验价：每个候选组合再走 calculate-price（含券）得到权威应付总价。
"""
from __future__ import annotations

from typing import Optional

from . import cards, nlparser, rpg
from .mcp_base import (
    MCPClient,
    MCPError,
    OrderScene,
    extract_product_names,
    extract_product_prices,
    scene_for_mode,
)
from .mcp_mock import MockMCPClient
from .models import Constraints, MenuItem, PlanItem, PlanOption, PriceQuote, QuestResult, Store
from .planner import build_plan

SHORTLIST_PER_CATEGORY = 3  # 每个分类最多取几个商品查详情
SHORTLIST_MAX = 10           # 短名单上限（控制 query-meal-detail 调用次数）


def _is_mock(client: MCPClient) -> bool:
    """是否演示客户端（决定无位置时能否回退默认演示门店）。"""
    return isinstance(client, MockMCPClient)


def _demo_note(client: MCPClient) -> str:
    return "演示数据（MockMCPClient，非实时菜单）" if _is_mock(client) else "麦当劳 MCP 实时数据"


def _split_city(location: str) -> tuple[str, str]:
    """把位置文本拆成 (城市, 关键词)。「深圳市南山区科技园」-> ("深圳市", "南山区科技园")。"""
    loc = (location or "").strip()
    if not loc:
        return "", ""
    if "市" in loc:
        i = loc.find("市")
        city = loc[: i + 1]
        keyword = loc[i + 1 :].strip() or city
        return city, keyword
    return "", loc


def _pick_store(stores: list[Store], result: QuestResult, source: str) -> Optional[Store]:
    """从门店列表中选择：0 家明确告知；多家列出候选（不默默选择）；1 家直接使用。"""
    if not stores:
        result.warnings.append(
            f"未在{source}找到附近门店；请补充更具体的地址（建议包含城市/商圈），"
            "或使用 --store-code 指定门店编码（麦当劳 MCP 服务范围不含港澳台）"
        )
        return None
    if len(stores) > 1:
        result.store_candidates = list(stores)
        result.warnings.append(f"找到 {len(stores)} 家候选门店：请选择一家，用 --store-code <编码> 重新运行")
        return None
    return stores[0]


def _resolve_scene(c: Constraints, client: MCPClient, result: QuestResult):
    """确定订单场景（门店 + orderType/beType/beCode）。

    返回 (scene, store, source_desc)；无法确定时 scene 为 None，
    并把原因写进 warnings。source_desc 用于数据来源审计。
    """
    # A. 外送场景：必须走「配送地址 -> 可配送门店」流程拿 beCode
    if c.dining_mode == "delivery":
        if c.store_code:
            result.warnings.append(
                "外送场景需要从已保存的配送地址获取业务编码（beCode），暂不支持直接指定门店编码；已按该门店的「到店自取」场景查询"
            )
            c.dining_mode = "takeaway"
        else:
            try:
                addresses = client.delivery_addresses()
            except MCPError as e:
                result.warnings.append(f"配送地址查询失败：{e}；外送需要已保存的配送地址")
                return None, None, ""
            if not addresses:
                result.warnings.append(
                    "外送（麦乐送）需要先在麦当劳官方 App/小程序保存配送地址；当前账户没有可用地址。"
                    "请到官方渠道添加地址后重试，或改为「到店自取 / 堂食」"
                )
                return None, None, ""
            addr = addresses[0]
            if len(addresses) > 1:
                result.warnings.append(
                    f"默认使用第一个配送地址「{addr.get('address', addr.get('addressId', ''))}」"
                )
            try:
                stores = client.delivery_stores(str(addr.get("addressId", "")), be_type=2)
            except MCPError as e:
                result.warnings.append(f"可配送门店查询失败：{e}")
                return None, None, ""
            store = _pick_store(stores, result, f"配送地址「{addr.get('address', '')}」附近")
            if store is None:
                return None, None, ""
            be_code = store.be_code or str(addr.get("beCode", "") or "")
            if not be_code:
                result.warnings.append("可配送门店未返回业务编码（beCode），无法进行外送算价")
                return None, None, ""
            source = f"delivery-query-stores @ 地址 {addr.get('addressId', '')}"
            return OrderScene(store.code, order_type=2, be_type=2, be_code=be_code), store, source

    # B. 显式门店编码（到店场景）
    if c.store_code:
        return scene_for_mode("takeaway", c.store_code), Store(code=c.store_code, name="指定门店"), f"指定门店编码 {c.store_code}"

    # C. 按位置查询附近门店（到店场景）
    city, keyword = _split_city(c.location or "")
    if not keyword and not city and not _is_mock(client):
        result.warnings.append(
            "未提供位置信息：请说明地址（如「外送到深圳市南山区科技园」），"
            "或使用 --store-code 指定门店编码（可先运行 burger-quest stores \"<地址>\" 查询门店）"
        )
        return None, None, ""
    try:
        stores = client.nearby_stores(keyword, city=city, be_type=1)
    except MCPError as e:
        result.warnings.append(f"附近门店查询失败：{e}；请补充更具体的地址或使用 --store-code 指定门店编码")
        return None, None, ""
    if not stores and not keyword and not city:
        result.warnings.append("未找到可用门店")
        return None, None, ""
    if not stores:
        result.warnings.append(
            f"未在「{c.location}」找到附近门店；请补充更具体的地址（建议包含城市/商圈），"
            "或使用 --store-code 指定门店编码（麦当劳 MCP 服务范围不含港澳台）"
        )
        return None, None, ""
    if len(stores) > 1:
        result.store_candidates = list(stores)
        result.warnings.append(f"找到 {len(stores)} 家候选门店：请选择一家，用 --store-code <编码> 重新运行")
        return None, None, ""
    store = stores[0]
    if not keyword and not city:
        result.warnings.append(
            f"未提供位置信息，已使用默认演示门店「{store.name}」（真实模式必须提供位置或门店编码）"
        )
        return scene_for_mode(c.dining_mode, store.code), store, "默认演示门店（未提供位置）"
    return scene_for_mode(c.dining_mode, store.code), store, f"query-nearby-stores @ {c.location}"


def _menu_item_score(m: MenuItem, c: Constraints) -> float:
    """短名单打分：只用 query-meals 能拿到的 分类/标签 信息（此时还没有名称和价格）。"""
    text = m.category + " " + " ".join(m.tags)
    s = 0.0
    if "套餐" in text:
        s += 3
    if c.rich and "套餐" in text:
        s += 2
    taste_hits = {
        "spicy": ["辣"], "sweet": ["甜"], "crispy": ["脆", "酥"], "burger": ["堡", "卷"],
        "chicken": ["鸡"], "beef": ["牛"], "fish": ["鱼"], "veggie": ["素", "清爽", "沙拉"], "soup": ["汤"],
    }
    for taste, kws in taste_hits.items():
        if taste in c.tastes and any(k in text for k in kws):
            s += 3
    if c.mood == "night" and any(k in text for k in ("暖饮", "汤", "热")):
        s += 2
    if c.max_calories is not None and any(k in text for k in ("零卡", "无糖", "清爽", "素")):
        s += 1
    return s


def _shortlist(menu: list[MenuItem], c: Constraints) -> list[MenuItem]:
    """从菜单骨架挑出要查详情的短名单（分类覆盖 + 偏好加权）。"""
    by_cat: dict[str, list[MenuItem]] = {}
    for m in menu:
        by_cat.setdefault(m.category or "未分类", []).append(m)
    picked: list[MenuItem] = []
    for _cat, items in by_cat.items():
        items.sort(key=lambda m: (-_menu_item_score(m, c), m.code))
        picked.extend(items[:SHORTLIST_PER_CATEGORY])
    picked.sort(key=lambda m: (-_menu_item_score(m, c), m.code))
    return picked[:SHORTLIST_MAX]


def run_quest(
    text: str,
    client: MCPClient,
    store_code: Optional[str] = None,
    generated_at: str = "",
    card_snapshot: bool = False,
) -> QuestResult:
    result = QuestResult(constraints=nlparser.parse(text))
    c = result.constraints
    result.warnings.extend(c.notes)
    if store_code:
        c.store_code = store_code
        c.location = None

    # 1. 时间上下文（失败不阻塞）
    try:
        t = client.now_time()
        result.time_info = f"{t.get('datetime', '')} {t.get('time_period', '')}".strip()
    except MCPError as e:
        result.warnings.append(f"时间信息获取失败（{e}），将按通用时段处理")

    # 2. 订单场景（门店 + orderType/beType）
    scene, store, store_source = _resolve_scene(c, client, result)
    result.store = store
    if scene is None:
        if result.store_candidates:
            result.warnings.append("已暂停：等待你选择门店（未查询菜单、未生成任何价格）")
        else:
            result.warnings.append("未确定门店，无法查询真实菜单；已停止生成方案（未编造任何餐品）")
        return result

    # 3. 菜单骨架（只有编码/分类/标签）
    try:
        skeleton = client.query_meals(scene)
    except MCPError as e:
        result.warnings.append(
            f"门店 {store.code} 菜单查询失败：{e}；无法生成方案（未编造任何餐品）"
        )
        return result
    if not skeleton:
        result.warnings.append("当前门店未返回可售餐品，无法生成方案")
        return result

    # 4. 短名单 + 价格发现（一次算价同时拿「名称 + 单价」——2026-10-14 效率优化）
    # 原来：先逐个 query-meal-detail 拿名称（≤10 次），再算价（1 次）。
    # 现在：先对短名单算价（1 次），calculate-price 的 productList 自带 productName
    # 与 subtotal（官方权威返回），只有 productList 没给名称的商品才回退查详情
    # （实测通常 0 次）。总调用从 ≤11 次降到 1-3 次，且名称/价格仍全部来自官方。
    shortlist = _shortlist(skeleton, c)
    try:
        probe = client.calculate_price(scene, [PlanItem(m, 1) for m in shortlist], [], purpose="price_discovery")
    except MCPError as e:
        result.warnings.append(
            f"价格查询失败：{e}；没有权威价格就无法生成方案（未编造任何价格）"
        )
        return result
    if not probe.ok:
        result.warnings.append(
            f"价格查询失败：{probe.message or 'calculate-price 未返回应付总价'}；无法生成方案（未编造任何价格）"
        )
        return result
    unit_prices = extract_product_prices(probe)
    price_names = extract_product_names(probe)

    named: list[MenuItem] = []
    detail_failures = 0
    detail_skipped = 0
    for m in shortlist:
        name = price_names.get(m.code) or ""
        if not name:
            # productList 没给名称：回退 query-meal-detail（不猜名称）
            try:
                detail = client.meal_detail(scene, m.code)
            except MCPError:
                detail_failures += 1
                continue
            name = detail.get("name") or ""
        else:
            detail_skipped += 1
        if not name:
            continue
        named.append(
            MenuItem(code=m.code, name=name, category=m.category, price_cents=None, tags=m.tags)
        )
    if detail_failures:
        result.warnings.append(f"{detail_failures} 个餐品详情查询失败，已跳过")
    if detail_skipped:
        result.warnings.append(
            f"{detail_skipped} 个餐品名称直接取自行算价返回（productList.productName），省下 {detail_skipped} 次详情查询"
        )
    if not named:
        result.warnings.append("未能获取任何餐品名称，无法生成方案（未编造任何餐品）")
        return result

    # 5. 价格汇总：单品实付来自同一次算价的 productList.subtotal（权威，非估算）
    priced: list[MenuItem] = []
    unpriced = 0
    for m in named:
        cents = unit_prices.get(m.code)
        if cents is None:
            unpriced += 1
            continue
        priced.append(
            MenuItem(
                code=m.code,
                name=price_names.get(m.code) or m.name,
                category=m.category,
                price_cents=cents,
                tags=m.tags,
            )
        )
    if unpriced:
        result.warnings.append(f"{unpriced} 个餐品未返回真实价格，已从组合中排除（而不是估算价格）")
    if not priced:
        result.warnings.append("没有获取到任何真实价格，无法生成方案")
        return result

    # 6. 优惠券（尽力而为）
    coupons = []
    for label, fn in (("门店优惠券", lambda: client.store_coupons(scene)), ("我的卡包", client.my_coupons)):
        try:
            coupons.extend(fn())
        except MCPError as e:
            result.warnings.append(f"{label}查询失败：{e}；将按无券方案计算")
    if not coupons:
        result.warnings.append("未获取到可用优惠券（不排除有券，只是当前查询未返回）")

    # 7. 营养数据（尽力而为）
    nutrition = []
    try:
        nutrition = client.list_nutrition_foods()
    except MCPError as e:
        result.warnings.append(f"营养数据查询失败：{e}；方案将不展示营养信息")

    # 8. 权威价格计算函数（失败只计数，最后汇总为一条提示，避免刷屏）
    price_failures: list[str] = []

    def price_fn(items, coupon_ids) -> PriceQuote:
        try:
            return client.calculate_price(scene, items, coupon_ids, purpose="plan_verification")
        except MCPError as e:
            price_failures.append(str(e))
            return PriceQuote(ok=False, message=str(e))

    # 9. 规划
    options, plan_warnings = build_plan(c, priced, coupons, nutrition, price_fn)
    result.options = options
    result.warnings.extend(plan_warnings)
    if price_failures and not options:
        result.warnings.append(
            f"calculate-price 调用失败 {len(price_failures)} 次（{price_failures[0]}）；"
            "无法给出权威总价（未编造任何价格）"
        )

    # 10. RPG 任务书（以最优方案为推荐装备）
    demo = _is_mock(client)
    if options:
        best = options[0]
        result.chosen = best
        result.quest_name = rpg.quest_name(c, best)
        result.story = rpg.story(c, store.name, demo=demo)
        result.gear_lines = rpg.gear_lines(best)
        result.gear_lines.extend(rpg.nutrition_lines(best, nutrition))

    # 11. 数据来源审计
    nutrition_note = f"list-nutrition-foods（{len(nutrition)} 条）" if nutrition else "未获取"
    if result.chosen is not None and result.chosen.nutrition_missing:
        nutrition_note += (
            "；推荐方案中 " + "、".join(result.chosen.nutrition_missing) + " 无营养数据（未猜测数值）"
        )
    result.data_sources = {
        "server": client.source_note(),
        "store": store_source,
        "menu": f"query-meals @ {store.code}（{len(skeleton)} 个编码，详情 {len(named)} 个，有价 {len(priced)} 个）",
        "coupons": f"query-store-coupons + query-my-coupons（{len(coupons)} 张）",
        "nutrition": nutrition_note,
        "price": "calculate-price（价格发现 + 组合验价，金额单位：分）",
        "mode": _demo_note(client),
    }
    result.mcp_calls = list(client.calls)

    # 12. 分享卡
    if result.chosen is not None:
        result.card_html = cards.render_html(
            c, result.chosen, store, result.time_info, _demo_note(client), generated_at or "（未提供）",
            demo=demo, snapshot=card_snapshot,
        )
        result.card_markdown = cards.render_markdown(
            c, result.chosen, store, result.time_info, _demo_note(client), generated_at or "（未提供）",
            demo=demo, snapshot=card_snapshot,
        )
    return result
