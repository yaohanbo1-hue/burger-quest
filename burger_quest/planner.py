"""套餐规划引擎（业务逻辑层）——2026-10-14 升级版。

铁律（对应比赛要求「不得编造餐品、价格、优惠或库存状态」）：
1. 进入组合的餐品必须来自 query-meals 返回的真实菜单（或明确标记的演示数据）；
2. 价格未知的餐品不进入任何组合（而不是猜一个价格）；
3. 每个方案的「总价」只能来自 calculate-price 的权威返回；
   calculate-price 失败时该方案作废，绝不用菜单价自行加总冒充权威价；
4. 优惠券必须来自 query-store-coupons / query-my-coupons 的真实返回；
5. 不默认优惠可叠加：只有权威核验（calculate-price 的折扣字段）确认生效的
   优惠才计入方案效果；预期与实际不符时如实备注，仍按权威结果计价；
6. 营养偏好只用真实营养数据评估：部分缺失 -> 标注 + 降权（降低可信度）；
   完全缺失 -> 排除候选。绝不猜测营养数值；
7. 库存/供应状态以 MCP 菜单返回为准，引擎不做任何库存假设。

升级要点（2026-10-14，目标体验：「预算 35 元，想吃辣，两个人，希望尽量高蛋白，
不想点太多饮料」）：
- 品类硬避免（「不要饮料」）与饮料软上限（「不想点太多饮料」-> 人数-1 份）；
- 营养覆盖度跟踪（complete / missing），偏好场景下零覆盖排除、部分覆盖降权；
- 最多 3 个可比较方案；每个方案附「符合 / 不完全符合」的简洁说明；
- 失败诊断五分类（无价餐品 / 品类排除后无候选 / 算价全失败 / 预算筛除 /
  营养数据缺失），每类附可操作建议，不编造成功套餐；
- 金额全程 int 分；排序确定（同分按价格、编码），可复现。
"""
from __future__ import annotations

from typing import Callable, Optional

from .models import (
    Constraints,
    Coupon,
    MenuItem,
    NutritionItem,
    PlanItem,
    PlanOption,
    PriceQuote,
    TASTE_CN,
    format_calories,
    format_yuan,
)

MAX_OPTIONS = 3  # 最多三个可比较方案

# ---------------------------------------------------------------------------
# 餐品分类（启发式，用于搭配均衡）
# ---------------------------------------------------------------------------
CATEGORY_RULES = [
    # 主食优先：套餐/汉堡即使名字里带「麦乐鸡/鸡翅」等小食词也是主食
    ("main", ["汉堡", "堡", "套餐", "饭", "卷", "盒", "主食", "全餐", "四件套", "三件套", "随心选"]),
    ("drink", ["可乐", "雪碧", "芬达", "果汁", "咖啡", "茶", "奶", "饮料", "饮品", "冰旋"]),
    ("dessert", ["甜筒", "冰淇淋", "圣代", "麦旋风", "甜品"]),
    ("side", ["薯条", "薯", "鸡翅", "麦乐鸡", "玉米", "沙拉", "派", "洋葱圈", "小食", "薯饼"]),
]

CATEGORY_CN = {"main": "主食", "side": "小食", "drink": "饮料", "dessert": "甜品", "other": "其他"}

MOOD_AFFINITY = {
    "night": {"drink": 2, "soup": 2, "dessert": 1, "side": 1},
    "tired": {"main": 2, "side": 1, "dessert": 1},
    "celebrate": {"main": 2, "dessert": 2, "drink": 1},
    "refresh": {"veggie": 2, "soup": 2, "drink": 1, "dessert": -1},
    "happy": {"main": 1, "side": 1},
}

# 口味命中关键词（评分与「未满足」判定共用一张表，避免两处逻辑不一致）
TASTE_HIT_KEYWORDS = {
    "spicy": ("辣",),
    "sweet": ("甜",),
    "crispy": ("脆", "酥"),
    "burger": ("堡",),
    "chicken": ("鸡",),
    "beef": ("牛",),
    "fish": ("鱼",),
    "veggie": ("素", "菜", "沙拉"),
    "soup": ("汤",),
}
TASTE_HIT_SCORE = {"spicy": 4}  # 辣味权重更高；其余口味默认 3 分


def classify(item: MenuItem) -> str:
    text = item.name + " " + item.category + " " + " ".join(item.tags)
    for cat, kws in CATEGORY_RULES:
        if any(k in text for k in kws):
            return cat
    return "other"


def _item_text(item: MenuItem) -> str:
    return item.name + " " + item.category + " " + " ".join(item.tags)


def taste_hit(item: MenuItem, tastes: tuple) -> bool:
    """餐品是否命中任一目标口味（用于 reasons/unmet 的简洁说明）。"""
    if not tastes:
        return False
    text = _item_text(item)
    for t in tastes:
        if any(k in text for k in TASTE_HIT_KEYWORDS.get(t, ())):
            return True
    return False


# ---------------------------------------------------------------------------
# 评分
# ---------------------------------------------------------------------------


def taste_score(item: MenuItem, tastes: tuple) -> int:
    text = _item_text(item)
    score = 0
    for t in tastes:
        if not any(k in text for k in TASTE_HIT_KEYWORDS.get(t, ())):
            continue
        if t == "sweet" and classify(item) == "dessert":
            score += 3
        score += TASTE_HIT_SCORE.get(t, 3)
    return score


def mood_score(item: MenuItem, mood: Optional[str]) -> int:
    if not mood:
        return 0
    cat = classify(item)
    affinity = MOOD_AFFINITY.get(mood, {})
    score = affinity.get(cat, 0)
    text = item.name
    if "汤" in text:
        score += affinity.get("soup", 0)
    if "沙拉" in text or "素" in text:
        score += affinity.get("veggie", 0)
    return score


def nutrition_of(item: MenuItem, nutrition: list[NutritionItem]) -> Optional[NutritionItem]:
    if not nutrition:
        return None
    for n in nutrition:
        if n.name == item.name:
            return n
    for n in nutrition:
        if n.name and (n.name in item.name or item.name in n.name):
            return n
    return None


def item_score(item: MenuItem, c: Constraints, nutrition: list[NutritionItem]) -> float:
    s = float(taste_score(item, c.tastes) + mood_score(item, c.mood))
    if c.rich:
        # 丰盛偏好：套餐与高价主食优先
        if "套餐" in item.category or "套餐" in item.name:
            s += 2
        if item.price_cents and item.price_cents >= 2500:
            s += 1
    n = nutrition_of(item, nutrition)
    if c.max_calories is not None and n and n.calories_kcal is not None:
        if n.calories_kcal <= c.max_calories / max(c.people, 1):
            s += 2
        else:
            s -= 6
    if c.min_protein_g is not None and n and n.protein_g is not None:
        if n.protein_g >= c.min_protein_g / max(c.people, 1):
            s += 2
        else:
            s -= 3
    # 性价比：低于同类别中位价加分
    return s


# ---------------------------------------------------------------------------
# 营养汇总（只统计有真实数据的餐品；缺失如实标注，绝不猜测）
# ---------------------------------------------------------------------------


def nutrition_summary(option: PlanOption, nutrition: list[NutritionItem]) -> tuple[dict, bool, list]:
    """返回 (totals, complete, missing_names)。

    - totals 只累加「有真实营养数据」的餐品（按份数加权）；
    - complete=True 当且仅当全部餐品都有数据；
    - missing_names 是缺数据的餐品名（去重，保序）。
    """
    totals = {"calories_kcal": 0.0, "protein_g": 0.0, "fat_g": 0.0, "carb_g": 0.0, "sodium_mg": 0.0}
    missing: list[str] = []
    found_any = False
    for pi in option.items:
        n = nutrition_of(pi.item, nutrition)
        has_fields = n is not None and any(getattr(n, k, None) is not None for k in totals)
        if not has_fields:
            if pi.item.name not in missing:
                missing.append(pi.item.name)
            continue
        found_any = True
        for k in totals:
            v = getattr(n, k, None)
            if v is not None:
                totals[k] += v * pi.qty
    if not found_any:
        return {}, False, missing
    totals["calories_kcal"] = round(totals["calories_kcal"], 1)
    totals["protein_g"] = round(totals["protein_g"], 1)
    totals["fat_g"] = round(totals["fat_g"], 1)
    totals["carb_g"] = round(totals["carb_g"], 1)
    totals["sodium_mg"] = round(totals["sodium_mg"], 1)
    return totals, not missing, missing


def _sum_nutrition(opt: PlanOption, nutrition: list[NutritionItem]) -> dict:
    """兼容入口（rpg 等使用）：无任何数据时返回 {}。"""
    totals, _complete, _missing = nutrition_summary(opt, nutrition)
    return totals


# ---------------------------------------------------------------------------
# 候选组合生成
# ---------------------------------------------------------------------------


def _rank(items: list[MenuItem], c: Constraints, nutrition: list[NutritionItem], cat: str) -> list[MenuItem]:
    same_cat = [i for i in items if classify(i) == cat and i.price_cents is not None]
    same_cat.sort(key=lambda i: (-item_score(i, c, nutrition), i.price_cents, i.code))
    return same_cat


def _templates(c: Constraints) -> list[tuple[str, dict]]:
    """返回 [(模板名, 每类数量)]。

    模板选择同时考虑人数、预算档位与饮料偏好：
    - 「不要饮料」-> 所有模板 drink=0；
    - 「不想点太多饮料」-> drink 槽位封顶（软上限优先满足，不能满足时由
      unmet 标注，不静默忽略偏好）；
    - 预算越紧，组合越精简，保证「预算内一定有候选」而不是直接失败。
    """
    p = max(c.people, 1)
    avoid = set(c.avoid_categories)
    cap = c.max_drink_qty
    budget = c.budget_cents

    def drink_slots(n: int) -> int:
        if "drink" in avoid:
            return 0
        if cap is not None:
            return max(0, min(n, cap))
        return n

    if p >= 2:
        tight = budget is not None and budget <= 4500
        if tight:
            # 紧预算多人：先给「轻量分享」（1 主食 + 小食），再给标准多人餐
            return [
                ("share_light", {"main": 1, "side": 1, "drink": drink_slots(1)}),
                ("share_value", {"main": p, "side": 1, "drink": drink_slots(1)}),
                ("share_lean", {"main": p, "side": 1, "drink": 0}),
            ]
        return [
            ("share_feast", {"main": p, "side": p, "drink": drink_slots(p)}),
            ("share_value", {"main": p, "side": 1, "drink": drink_slots(1)}),
            ("share_lean", {"main": p, "side": 1, "drink": 0}),
        ]
    if c.max_calories is not None:
        return [
            ("light_meal", {"main": 1, "side": 0, "drink": drink_slots(1)}),
            ("light_plus", {"main": 1, "side": 1, "drink": drink_slots(1)}),
            ("solo_main", {"main": 1}),
        ]
    if budget is not None and budget <= 2500:
        return [
            ("mini_meal", {"main": 1, "side": 0, "drink": drink_slots(1)}),
            ("solo_main", {"main": 1}),
        ]
    if budget is not None and budget <= 4500:
        return [
            ("value_meal", {"main": 1, "side": 1, "drink": drink_slots(1)}),
            ("mini_meal", {"main": 1, "side": 0, "drink": drink_slots(1)}),
            ("solo_main", {"main": 1}),
        ]
    return [
        ("full_meal", {"main": 1, "side": 1, "drink": drink_slots(1)}),
        ("double_main", {"main": 2, "side": 1, "drink": drink_slots(1)}),
        ("solo_main", {"main": 1}),
    ]


def _drink_qty(combo: list[PlanItem]) -> int:
    return sum(pi.qty for pi in combo if classify(pi.item) == "drink")


def _build_candidates(
    menu: list[MenuItem],
    c: Constraints,
    nutrition: list[NutritionItem],
    max_candidates: int = 6,
) -> list[list[PlanItem]]:
    avoid = set(c.avoid_categories)
    avoid_tastes = set(c.avoid_tastes)
    # 硬避免品类与口味直接出局；价格未知同样出局（见 build_plan 的说明）
    # 注意：「不吃辣」是硬约束——任何辣味餐品都不进候选，
    # 不会被「想吃脆的」等其他偏好覆盖（用户 2026-10-14 要求）。
    pool = [
        m
        for m in menu
        if m.price_cents is not None
        and classify(m) not in avoid
        and not (avoid_tastes and taste_hit(m, tuple(avoid_tastes)))
    ]
    ranked = {cat: _rank(pool, c, nutrition, cat) for cat in ("main", "side", "drink", "dessert")}
    candidates: list[list[PlanItem]] = []
    seen = set()

    for _tname, slots in _templates(c):
        main_n = slots.get("main", 1)
        side_n = slots.get("side", 0)
        drink_n = slots.get("drink", 0)
        mains = ranked["main"]
        if not mains:
            continue
        # 多人分享：优先两个不同主食各一份；单人双主食：同一主食两份。
        # 紧预算时额外纳入最便宜主食（价格也是候选维度），并标注「主食少于人数」。
        main_combos: list[list[PlanItem]] = []
        if main_n >= 2 and c.people >= 2 and len(mains) >= 2:
            main_combos.append([PlanItem(mains[0], 1), PlanItem(mains[1], 1)])
        main_combos.append([PlanItem(mains[0], main_n)])
        if len(mains) > 1 and main_n == 1:
            main_combos.append([PlanItem(mains[1], 1)])
        if c.budget_cents is not None and c.budget_cents <= 4500 and len(mains) > 2:
            cheapest = min(mains, key=lambda m: (m.price_cents or 0, m.code))
            if all(pi.item.code != cheapest.code for combo in main_combos for pi in combo):
                main_combos.append([PlanItem(cheapest, main_n)])
        # 品类池为空时跳过该槽位（不是丢弃整个组合）
        side_pool = ranked["side"][:1] if side_n and ranked["side"] else [None]
        drink_pool = ranked["drink"][:1] if drink_n and ranked["drink"] else [None]
        for main_combo in main_combos:
            for s in side_pool:
                for d in drink_pool:
                    combo = list(main_combo)
                    if s is not None:
                        combo.append(PlanItem(s, side_n))
                    if d is not None:
                        combo.append(PlanItem(d, drink_n))
                    key = tuple(sorted((pi.item.code, pi.qty) for pi in combo))
                    if key in seen:
                        continue
                    seen.add(key)
                    candidates.append(combo)
                    if len(candidates) >= max_candidates:
                        return candidates
    # 兜底：菜单里没有主食（或全被避免）时，用池内任意有价餐品凑「单人尝鲜」
    if not candidates:
        any_items = pool[:2]
        if any_items:
            candidates.append([PlanItem(i, 1) for i in any_items])
    return candidates


# ---------------------------------------------------------------------------
# 优惠券匹配与效力核验
# ---------------------------------------------------------------------------


def coupon_applies(coupon: Coupon, combo: list[PlanItem], estimated_original: int) -> bool:
    if coupon.threshold_cents and estimated_original < coupon.threshold_cents:
        return False
    if coupon.applicable_codes:
        codes = {pi.item.code for pi in combo}
        if not codes & set(coupon.applicable_codes):
            return False
    return True


def coupon_sets(combo: list[PlanItem], coupons: list[Coupon], estimated_original: int) -> list[list[Coupon]]:
    """生成待验价的优惠券试探序列：最优单券 -> 最优双券（无可用券时只有无券）。

    2026-10-14 效率优化（每个组合最多 3 次算价，原来最多 8 次）：
    - 有可用券时不再无条件先探「无券基线」——同餐品下无券方案严格被
      叠券方案支配（更贵），先探券；
    - 取预期折扣最高的券优先试探；是否真的可叠加由 calculate-price
      权威核验决定，引擎不默认优惠可叠加（见 coupon_expected_discount /
      coupon_note）；全部券试探失败时由调用方回退无券核验。
    """
    applicable = [c for c in coupons if coupon_applies(c, combo, estimated_original)]
    if not applicable:
        return [[]]
    applicable.sort(key=lambda c: (-coupon_expected_discount(c, combo, estimated_original), c.id))
    sets: list[list[Coupon]] = [[applicable[0]]]
    if len(applicable) >= 2:
        sets.append([applicable[0], applicable[1]])
    return sets


def coupon_expected_discount(coupon: Coupon, combo: list[PlanItem], estimated_original: int) -> int:
    """按券定义估算可省金额（分）——仅用于与权威折扣比对，从不进入总价。"""
    if not coupon_applies(coupon, combo, estimated_original):
        return 0
    if coupon.kind == "percent" and coupon.percent:
        base = estimated_original
        if coupon.applicable_codes:
            applicable = {pi.item.code for pi in combo} & set(coupon.applicable_codes)
            base = sum(pi.item.price_cents * pi.qty for pi in combo if pi.item.code in applicable)
        return int(round(base * (1.0 - coupon.percent)))
    return int(coupon.value_cents or 0)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def _make_option(
    combo: list[PlanItem],
    cset: list,
    quote: PriceQuote,
    c: Constraints,
    estimated: int,
    nutrition: list,
    totals: dict,
    complete: bool,
    missing: list,
) -> PlanOption:
    """把一次权威核验结果组装成方案（总价只认 quote；优惠效力如实核验）。"""
    opt = PlanOption(
        title="",
        items=list(combo),
        coupons=list(cset),
        total_cents=quote.total_cents,
        original_cents=quote.original_cents,
        discount_cents=quote.discount_cents,
        delivery_fee_cents=quote.delivery_fee_cents,
        price_checked=True,
        nutrition=totals,
        nutrition_complete=complete,
        nutrition_missing=list(missing),
    )
    if c.budget_cents is not None:
        opt.within_budget = opt.total_cents <= c.budget_cents
        opt.remaining_cents = c.budget_cents - opt.total_cents
    # 优惠核验：预期折扣 vs 权威折扣，不默认可叠加
    expected = sum(coupon_expected_discount(c2, combo, estimated) for c2 in cset)
    actual = opt.discount_cents or 0
    if cset and expected != actual:
        opt.coupon_note = (
            f"提交 {len(cset)} 张券，官方核验实际优惠 {format_yuan(actual)}"
            f"（按券面预估 {format_yuan(expected)}）——不默认优惠可叠加，已按权威结果计价"
        )
        if actual == 0:
            # 权威核验确认优惠未生效：不把这张券算作本方案的优惠
            opt.coupons = []
    if c.has_nutrition_preference() and not complete:
        opt.warnings.append(
            "营养数据不完整：" + "、".join(missing) + " 无营养记录，蛋白/热量结论可能偏低（未猜测数值）"
        )
    opt.reasons = _build_reasons(opt, c, nutrition)
    opt.unmet = _build_unmet(opt, c)
    opt.score = _option_score(opt, c, nutrition)
    return opt


def build_plan(
    constraints: Constraints,
    menu: list[MenuItem],
    coupons: list[Coupon],
    nutrition: list[NutritionItem],
    price_fn: Callable[[list[PlanItem], list[str]], PriceQuote],
) -> tuple[list[PlanOption], list[str]]:
    """返回 (方案列表, 警告列表)。price_fn 是权威价格计算（MCP calculate-price）。"""
    warnings: list[str] = []
    c = constraints

    priced = [m for m in menu if m.price_cents is not None]
    unpriced = [m for m in menu if m.price_cents is None]
    if unpriced:
        warnings.append(
            f"{len(unpriced)} 个餐品未返回真实价格（如：{'、'.join(m.name for m in unpriced[:5])}），"
            "已从组合中排除，而不是估算价格"
        )
    if not priced:
        warnings.append("当前门店没有带价格的餐品数据，无法生成方案")
        return [], warnings

    candidates = _build_candidates(priced, c, nutrition)
    if not candidates:
        if c.avoid_categories:
            cats = "、".join(CATEGORY_CN.get(x, x) for x in c.avoid_categories)
            warnings.append(
                f"排除「{cats}」品类后没有可用餐品，无法生成方案；"
                "可尝试取消该限制（例如改为「少点饮料」而非「不要饮料」）"
            )
        elif c.avoid_tastes:
            tastes = "、".join(TASTE_CN.get(t, t) for t in c.avoid_tastes)
            warnings.append(
                f"排除「不吃{tastes}」后没有可用餐品，无法生成方案；"
                "可尝试放宽口味限制（避免口味是硬约束，不会被其他偏好覆盖）"
            )
        else:
            warnings.append("没有找到符合口味/搭配要求的餐品组合")
        return [], warnings

    options: list[PlanOption] = []
    price_failed = 0
    budget_filtered = 0
    nutrition_excluded = 0
    fee_est = 900 if c.dining_mode == "delivery" else 0
    for combo in candidates:
        estimated = sum(pi.item.price_cents * pi.qty for pi in combo)
        # 预算预筛：估算价 + 配送费估算，留 25% 余量（优惠券可能拉低最终价），
        # 最终是否超预算以 calculate-price 权威结果为准
        if c.budget_cents is not None:
            if estimated + fee_est > int(c.budget_cents * 1.25):
                budget_filtered += 1
                continue
        # 营养预判（与券无关，避免浪费权威核验调用）：
        # 有营养偏好且零覆盖 -> 整个组合作废（无法评估，不猜测）
        probe = PlanOption(items=list(combo))
        totals, complete, missing = nutrition_summary(probe, nutrition)
        if c.has_nutrition_preference() and not totals:
            nutrition_excluded += 1
            continue
        best_for_combo: Optional[PlanOption] = None
        any_verified = False
        for cset in coupon_sets(combo, coupons, estimated):
            quote = price_fn(combo, [c2.id for c2 in cset])
            if not quote.ok or quote.total_cents is None:
                price_failed += 1
                continue  # 权威计算失败：该券组合作废，不编造总价
            any_verified = True
            opt = _make_option(combo, cset, quote, c, estimated, nutrition, totals, complete, missing)
            rank = (opt.score, -(opt.total_cents or 0))
            if best_for_combo is None or rank > (best_for_combo.score, -(best_for_combo.total_cents or 0)):
                best_for_combo = opt
        if not any_verified:
            # 所有券试探都被权威核验拒绝 -> 回退无券核验（保证有方案可出）
            quote = price_fn(combo, [])
            if quote.ok and quote.total_cents is not None:
                best_for_combo = _make_option(combo, [], quote, c, estimated, nutrition, totals, complete, missing)
            else:
                price_failed += 1
        if best_for_combo is not None:
            options.append(best_for_combo)

    # 排序：优先预算内、其次分数、再次低价（同分按总价与餐品编码，保证确定性）
    def sort_key(o: PlanOption):
        return (
            0 if o.within_budget else 1 if o.within_budget is False else 0,
            -o.score,
            o.total_cents or 10**9,
            tuple(sorted((pi.item.code, pi.qty) for pi in o.items)),
        )

    options.sort(key=sort_key)

    # 最多三个可比较方案：预算内有则取预算内 Top3；否则给至多 2 个最接近参考
    if c.budget_cents is not None:
        within = [o for o in options if o.within_budget]
        if within:
            options = within[:MAX_OPTIONS]
        elif options:
            warnings.append(
                f"没有组合能落在 {format_yuan(c.budget_cents)} 预算内；"
                f"以下 {min(len(options), 2)} 个为最接近的真实价格方案（总价以 calculate-price 为准，未编造任何价格）"
            )
            options = options[:2]
        else:
            options = []

    if len(options) > MAX_OPTIONS:
        options = options[:MAX_OPTIONS]

    if not options:
        # 空结果诊断：分类说明原因，每类附可操作建议——不编造成功套餐
        if price_failed and not budget_filtered:
            warnings.append(
                f"所有候选组合的权威价格计算均失败（calculate-price 返回 {price_failed} 次失败），"
                "无法给出方案（未编造任何价格）；请稍后重试或检查网络"
            )
        elif budget_filtered:
            warnings.append(
                f"没有组合能落在 {format_yuan(c.budget_cents)} 预算内"
                f"（{budget_filtered} 个候选组合因估算价超出预算被跳过，其余逐一用权威价格核验；未编造任何价格）。"
                "可尝试：适当提高预算，或减少人数/品类要求"
            )
        elif nutrition_excluded:
            warnings.append(
                "候选组合缺少营养数据，无法按营养偏好（蛋白/热量）评估，已全部排除（不猜测营养数值）。"
                "可尝试：取消营养偏好，或更换门店"
            )
        else:
            warnings.append("没有找到符合搭配要求的餐品组合")

    for o in options:
        o.title = _option_title(o, c)
    return options, warnings


# ---------------------------------------------------------------------------
# 方案说明（符合 / 不完全符合）
# ---------------------------------------------------------------------------


def _build_reasons(opt: PlanOption, c: Constraints, nutrition: list[NutritionItem]) -> list[str]:
    reasons = []
    tastes = c.tastes
    hit = []
    for pi in opt.items:
        if taste_hit(pi.item, tastes):
            hit.append(pi.item.name)
    if hit:
        reasons.append("口味命中：" + "、".join(dict.fromkeys(hit)))
    if opt.discount_cents:
        reasons.append(f"优惠券已省 {format_yuan(opt.discount_cents)}（权威核验）")
    if c.budget_cents is not None and opt.remaining_cents is not None and opt.remaining_cents >= 0:
        reasons.append(f"预算内，剩余 {format_yuan(opt.remaining_cents)}")
    n = opt.nutrition or {}
    if n.get("calories_kcal") is not None:
        reasons.append(f"整餐热量 {format_calories(n['calories_kcal'])}")
    if c.max_calories and n.get("calories_kcal") is not None and n["calories_kcal"] <= c.max_calories:
        reasons.append("满足低卡偏好")
    if c.min_protein_g and n.get("protein_g") is not None and n["protein_g"] >= c.min_protein_g:
        reasons.append(f"蛋白质 {n['protein_g']:.0f}g 达标（偏好 ≥{c.min_protein_g:.0f}g）")
    if c.max_drink_qty is not None:
        drinks = _drink_qty(opt.items)
        if drinks <= c.max_drink_qty:
            reasons.append(f"饮料 {drinks} 份，符合少饮偏好（≤{c.max_drink_qty}）")
    if c.avoid_tastes:
        reasons.append(
            "已排除避免口味：" + "/".join(TASTE_CN.get(t, t) for t in c.avoid_tastes)
        )
    if c.people >= 2:
        main_qty = sum(pi.qty for pi in opt.items if classify(pi.item) == "main")
        if main_qty >= c.people:
            reasons.append(f"主食 {main_qty} 份，够 {c.people} 人分食")
    if not reasons:
        reasons.append("当前门店真实在售且价格校验通过的基础搭配")
    return reasons


def _build_unmet(opt: PlanOption, c: Constraints) -> list[str]:
    """未完全满足的条件——简洁、如实，不隐瞒（对应「说明为什么不完全符合」）。"""
    unmet = []
    if c.budget_cents is not None and opt.within_budget is False and opt.remaining_cents is not None:
        unmet.append(f"超出预算 {format_yuan(-opt.remaining_cents)}")
    if c.people >= 2:
        main_qty = sum(pi.qty for pi in opt.items if classify(pi.item) == "main")
        if main_qty < c.people:
            unmet.append(f"主食 {main_qty} 份，少于 {c.people} 人（轻量分享，可能吃不饱）")
    if c.max_drink_qty is not None:
        drinks = _drink_qty(opt.items)
        if drinks > c.max_drink_qty:
            unmet.append(f"饮料 {drinks} 份，超过少饮偏好上限 {c.max_drink_qty} 份")
    if c.min_protein_g is not None:
        protein = (opt.nutrition or {}).get("protein_g")
        if protein is None:
            unmet.append("营养数据不足，无法确认蛋白质是否达标")
        elif protein < c.min_protein_g:
            unmet.append(f"蛋白质 {protein:.0f}g，未达偏好 {c.min_protein_g:.0f}g")
    if c.max_calories is not None:
        cal = (opt.nutrition or {}).get("calories_kcal")
        if cal is None:
            unmet.append("营养数据不足，无法确认热量是否达标")
        elif cal > c.max_calories:
            unmet.append(f"热量 {cal:.0f} 千卡，超过上限 {c.max_calories:.0f} 千卡")
    if c.tastes and not any(taste_hit(pi.item, c.tastes) for pi in opt.items):
        unmet.append("口味偏好（" + "/".join(TASTE_CN.get(t, t) for t in c.tastes) + "）未被任何餐品命中")
    if c.has_nutrition_preference() and not opt.nutrition_complete:
        unmet.append(
            "营养数据缺失：" + "、".join(opt.nutrition_missing) + "（结论可能不完整，未猜测数值）"
        )
    if opt.coupon_note:
        unmet.append(opt.coupon_note)
    return unmet


def _option_score(opt: PlanOption, c: Constraints, nutrition: list[NutritionItem]) -> float:
    score = 0.0
    for pi in opt.items:
        score += item_score(pi.item, c, nutrition) * pi.qty
    if opt.discount_cents:
        score += min(opt.discount_cents / 100.0, 5.0)  # 省钱加分，封顶 5（仅权威折扣）
    if c.budget_cents is not None and opt.remaining_cents is not None and opt.remaining_cents >= 0:
        if c.rich:
            # 丰盛偏好：鼓励把预算用得更充分（越接近预算加分越多），封顶 3
            used = (opt.total_cents or 0) / c.budget_cents
            score += min(3.0, max(0.0, (used - 0.3) / 0.7 * 3.0))
        else:
            # 默认：剩余预算加分（省钱导向），封顶 3
            score += min(opt.remaining_cents / 100.0, 3.0)
    # 营养偏好：蛋白/热量达标注加成；数据不完整降权（降低可信度）
    n = opt.nutrition or {}
    if c.min_protein_g is not None and n.get("protein_g") is not None and n["protein_g"] >= c.min_protein_g:
        score += 2.0
    if c.max_calories is not None and n.get("calories_kcal") is not None and n["calories_kcal"] <= c.max_calories:
        score += 2.0
    if c.has_nutrition_preference() and not opt.nutrition_complete:
        score -= 2.0
    # 软偏好未满足：降权
    if any("超过少饮偏好上限" in u for u in opt.unmet):
        score -= 3.0
    if c.tastes and not any(taste_hit(pi.item, c.tastes) for pi in opt.items):
        score -= 2.0
    # 丰盛偏好：品类多样性加分
    if c.rich:
        cats = {classify(pi.item) for pi in opt.items}
        score += 0.5 * len(cats)
    return score


def _option_title(opt: PlanOption, c: Constraints) -> str:
    cats = [classify(pi.item) for pi in opt.items]
    n = c.people
    if c.rich:
        base = "丰盛飨宴阵型"
    elif n >= 2:
        base = f"{n} 人分享阵型"
    elif c.max_calories is not None:
        base = "轻装低卡阵型"
    elif c.min_protein_g is not None:
        base = "高能蛋白阵型"
    elif c.budget_cents is not None and opt.total_cents and opt.total_cents <= c.budget_cents:
        base = "省钱精算阵型"
    elif cats.count("main") >= 2:
        base = "双主食饱腹阵型"
    else:
        base = "标准探索阵型"
    if opt.discount_cents:
        base += "·已叠券"
    return base
