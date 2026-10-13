"""服务编排层：把 解析 -> MCP 检索 -> 规划 -> RPG 文案 -> 分享卡 串成一次任务。

错误处理约定（对应比赛要求）：
- 任何 MCP 失败都不伪造数据：失败步骤记入 warnings，明确告诉用户哪一步不可用；
- 门店无法确定（无位置/查不到/多候选未选）= 无法生成方案，不猜门店；
- 菜单查询失败 = 无法生成方案（没有菜单就没有真实餐品）；
- 价格计算失败 = 对应方案作废（没有权威总价就不给方案）；
- 没有符合预算的组合 = 如实说明，并给出最接近的真实价格方案作参考。
"""
from __future__ import annotations

from typing import Optional

from . import cards, nlparser, rpg
from .mcp_base import MCPClient, MCPError
from .mcp_mock import MockMCPClient
from .models import Constraints, PlanOption, PriceQuote, QuestResult, Store


def _is_mock(client: MCPClient) -> bool:
    """是否演示客户端（决定无位置时能否回退默认演示门店）。"""
    return isinstance(client, MockMCPClient)


def _demo_note(client: MCPClient) -> str:
    return "演示数据（MockMCPClient，非实时菜单）" if _is_mock(client) else "麦当劳 MCP 实时数据"


def _resolve_store_and_menu(c: Constraints, client: MCPClient, result: QuestResult):
    """确定门店并取回菜单。

    返回 (store, menu)；无法确定时返回 (None, []) 并把原因写进 result.warnings。
    规则：
    1. 用户显式给出门店编码 → 直接验证并使用（只查一次菜单并复用，避免重复调用）；
    2. 否则用官方 query-nearby-stores 按位置查询：
       - 0 家 → 明确告知，请补充地址或提供编码；
       - 1 家 → 使用；
       - 多家 → 列出全部候选（result.store_candidates），不编造、不默默选择；
    3. 真实模式下没有位置信息 → 明确要求补充；演示模式回退默认演示门店并声明。
    """
    # 1) 显式门店编码优先
    if c.store_code:
        try:
            menu = client.query_meals(c.store_code)
        except MCPError as e:
            result.warnings.append(
                f"门店编码 {c.store_code} 查询失败：{e}；请确认编码是否正确"
                "（演示模式仅有 DEMO-001 / DEMO-002，真实编码请用 stores 子命令查询）"
            )
            return None, []
        if not menu:
            result.warnings.append(f"门店编码 {c.store_code} 未返回任何可售餐品；请确认编码是否正确")
            return None, []
        return Store(code=c.store_code, name="指定门店"), menu

    # 2) 按位置查询附近门店（官方 MCP 能力）
    address = c.location or ""
    if not address and not _is_mock(client):
        result.warnings.append(
            "未提供位置信息：请说明地址（如「外送到南山科技园」），"
            "或使用 --store-code 指定门店编码（可先运行 burger-quest stores \"<地址>\" 查询门店）"
        )
        return None, []
    try:
        stores = client.nearby_stores(address)
    except MCPError as e:
        result.warnings.append(f"附近门店查询失败：{e}；请补充更具体的地址或使用 --store-code 指定门店编码")
        return None, []
    if not stores:
        if address:
            result.warnings.append(
                f"未在「{address}」找到附近门店；请补充更具体的地址（建议包含城市/商圈），"
                "或使用 --store-code 指定门店编码（麦当劳 MCP 服务范围不含港澳台）"
            )
        else:
            result.warnings.append("未找到可用门店")
        return None, []
    if len(stores) > 1:
        # 多候选：不编造门店、不默默选择——列出候选，等用户指定
        result.store_candidates = list(stores)
        result.warnings.append(f"找到 {len(stores)} 家候选门店：请选择一家，用 --store-code <编码> 重新运行")
        return None, []
    store = stores[0]
    if not address:
        result.warnings.append(
            f"未提供位置信息，已使用默认演示门店「{store.name}」（真实模式必须提供位置或门店编码）"
        )
    try:
        menu = client.query_meals(store.code)
    except MCPError as e:
        result.warnings.append(f"菜单查询失败：{e}；无法生成方案（未编造任何餐品）")
        return None, []
    if not menu:
        result.warnings.append(f"门店「{store.name}」未返回可售餐品，无法生成方案")
        return None, []
    return store, menu


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

    # 2. 门店 + 菜单（必须成功，否则没有真实餐品）
    store, menu = _resolve_store_and_menu(c, client, result)
    result.store = store
    if store is None:
        if result.store_candidates:
            result.warnings.append("已暂停：等待你选择门店（未查询菜单、未生成任何价格）")
        else:
            result.warnings.append("未确定门店，无法查询真实菜单；已停止生成方案（未编造任何餐品）")
        return result

    # 3. 优惠券（尽力而为）
    coupons = []
    for label, fn in (("门店优惠券", lambda: client.store_coupons(store.code)), ("我的卡包", client.my_coupons)):
        try:
            coupons.extend(fn())
        except MCPError as e:
            result.warnings.append(f"{label}查询失败：{e}；将按无券方案计算")
    if not coupons:
        result.warnings.append("未获取到可用优惠券（不排除有券，只是当前查询未返回）")

    # 4. 营养数据（尽力而为）
    nutrition = []
    try:
        nutrition = client.list_nutrition_foods()
    except MCPError as e:
        result.warnings.append(f"营养数据查询失败：{e}；方案将不展示营养信息")

    # 5. 权威价格计算函数（失败只计数，最后汇总为一条提示，避免刷屏）
    price_failures: list[str] = []

    def price_fn(items, coupon_ids) -> PriceQuote:
        try:
            return client.calculate_price(store.code, items, coupon_ids, c.dining_mode)
        except MCPError as e:
            price_failures.append(str(e))
            return PriceQuote(ok=False, message=str(e))

    # 6. 规划
    from .planner import build_plan

    options, plan_warnings = build_plan(c, menu, coupons, nutrition, price_fn)
    result.options = options
    result.warnings.extend(plan_warnings)
    if price_failures and not options:
        result.warnings.append(
            f"calculate-price 调用失败 {len(price_failures)} 次（{price_failures[0]}）；"
            "无法给出权威总价（未编造任何价格）"
        )

    # 7. RPG 任务书（以最优方案为推荐装备）
    demo = _is_mock(client)
    if options:
        best = options[0]
        result.chosen = best
        result.quest_name = rpg.quest_name(c, best)
        result.story = rpg.story(c, store.name, demo=demo)
        result.gear_lines = rpg.gear_lines(best)
        result.gear_lines.extend(rpg.nutrition_lines(best, nutrition))

    # 8. 数据来源审计
    result.data_sources = {
        "server": client.source_note(),
        "store": (f"query-nearby-stores @ {c.location}" if not c.store_code else f"指定门店编码 {c.store_code}"),
        "menu": f"query-meals @ {store.code}（{len(menu)} 个餐品）",
        "coupons": f"query-store-coupons + query-my-coupons（{len(coupons)} 张）",
        "nutrition": f"list-nutrition-foods（{len(nutrition)} 条）" if nutrition else "未获取",
        "price": "calculate-price（权威总价来源）",
        "mode": _demo_note(client),
    }
    result.mcp_calls = list(client.calls)

    # 9. 分享卡
    if result.chosen is not None:
        result.card_html = cards.render_html(
            c, result.chosen, store, result.time_info, _demo_note(client), generated_at or "（未提供）", demo=demo
        )
        result.card_markdown = cards.render_markdown(
            c, result.chosen, store, result.time_info, _demo_note(client), generated_at or "（未提供）", demo=demo
        )
    return result
