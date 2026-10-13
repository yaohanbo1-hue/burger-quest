"""服务编排层：把 解析 -> MCP 检索 -> 规划 -> RPG 文案 -> 分享卡 串成一次任务。

错误处理约定（对应比赛要求）：
- 任何 MCP 失败都不伪造数据：失败步骤记入 warnings，明确告诉用户哪一步不可用；
- 菜单查询失败 = 无法生成方案（没有菜单就没有真实餐品）；
- 价格计算失败 = 对应方案作废（没有权威总价就不给方案）；
- 没有符合预算的组合 = 如实说明，并给出最接近的真实价格方案作参考。
"""
from __future__ import annotations

from typing import Optional

from . import cards, nlparser, rpg
from .mcp_base import MCPClient, MCPError
from .models import Constraints, PlanOption, QuestResult, Store


def _demo_note(client: MCPClient) -> str:
    return "演示数据（MockMCPClient，非实时菜单）" if type(client).__name__ == "MockMCPClient" else "麦当劳 MCP 实时数据"


def run_quest(
    text: str,
    client: MCPClient,
    store_code: Optional[str] = None,
    generated_at: str = "",
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

    # 2. 门店解析
    store: Optional[Store] = None
    try:
        if c.store_code:
            store = Store(code=c.store_code, name="指定门店")
            # 用一次菜单查询验证门店可用性
            probe = client.query_meals(c.store_code)
            if not probe:
                result.warnings.append(f"指定门店 {c.store_code} 未返回可售餐品")
        else:
            address = c.location or ""
            stores = client.nearby_stores(address)
            if not stores:
                result.warnings.append(
                    f"附近门店查询未返回结果（地址：{address or '未提供'}），请补充位置或指定门店编码"
                )
            else:
                store = stores[0]
                if len(stores) > 1:
                    result.warnings.append(f"默认选择最近门店「{store.name}」，可在需求中指定其他门店")
    except MCPError as e:
        result.warnings.append(f"门店查询失败：{e}")
    result.store = store
    if store is None:
        result.warnings.append("未确定门店，无法查询真实菜单；已停止生成方案（未编造任何餐品）")
        return result

    # 3. 菜单（必须成功，否则没有真实餐品）
    try:
        menu = client.query_meals(store.code)
    except MCPError as e:
        result.warnings.append(f"菜单查询失败：{e}；无法生成方案（未编造任何餐品）")
        return result
    if not menu:
        result.warnings.append("当前门店未返回可售餐品，无法生成方案")
        return result

    # 4. 优惠券（尽力而为）
    coupons = []
    for label, fn in (("门店优惠券", lambda: client.store_coupons(store.code)), ("我的卡包", client.my_coupons)):
        try:
            coupons.extend(fn())
        except MCPError as e:
            result.warnings.append(f"{label}查询失败：{e}；将按无券方案计算")
    if not coupons:
        result.warnings.append("未获取到可用优惠券（不排除有券，只是当前查询未返回）")

    # 5. 营养数据（尽力而为）
    nutrition = []
    try:
        nutrition = client.list_nutrition_foods()
    except MCPError as e:
        result.warnings.append(f"营养数据查询失败：{e}；方案将不展示营养信息")

    # 6. 权威价格计算函数
    def price_fn(items, coupon_ids) -> PlanOption:
        try:
            return client.calculate_price(store.code, items, coupon_ids, c.dining_mode)
        except MCPError as e:
            result.warnings.append(f"calculate-price 调用失败：{e}")
            from .models import PriceQuote

            return PriceQuote(ok=False, message=str(e))

    # 7. 规划
    from .planner import build_plan

    options, plan_warnings = build_plan(c, menu, coupons, nutrition, price_fn)
    result.options = options
    result.warnings.extend(plan_warnings)

    # 8. RPG 任务书（以最优方案为推荐装备）
    demo = type(client).__name__ == "MockMCPClient"
    if options:
        best = options[0]
        result.chosen = best
        result.quest_name = rpg.quest_name(c, best)
        result.story = rpg.story(c, store.name, demo=demo)
        result.gear_lines = rpg.gear_lines(best)
        result.gear_lines.extend(rpg.nutrition_lines(best, nutrition))

    # 9. 数据来源审计
    result.data_sources = {
        "server": client.source_note(),
        "menu": f"query-meals @ {store.code}（{len(menu)} 个餐品）",
        "coupons": f"query-store-coupons + query-my-coupons（{len(coupons)} 张）",
        "nutrition": f"list-nutrition-foods（{len(nutrition)} 条）" if nutrition else "未获取",
        "price": "calculate-price（权威总价来源）",
        "mode": _demo_note(client),
    }
    result.mcp_calls = list(client.calls)

    # 10. 分享卡
    if result.chosen is not None:
        result.card_html = cards.render_html(
            c, result.chosen, store, result.time_info, _demo_note(client), generated_at or "（未提供）", demo=demo
        )
        result.card_markdown = cards.render_markdown(
            c, result.chosen, store, result.time_info, _demo_note(client), generated_at or "（未提供）", demo=demo
        )
    return result
