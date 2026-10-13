"""套餐规划引擎（业务逻辑层）。

铁律（对应比赛要求「不得编造餐品、价格、优惠或库存状态」）：
1. 进入组合的餐品必须来自 query-meals 返回的真实菜单；
2. 价格未知的餐品不进入任何组合（而不是猜一个价格）；
3. 每个方案的「总价」只能来自 calculate-price 的权威返回；
   calculate-price 失败时该方案标记为价格未校验，绝不用菜单价自行加总冒充权威价；
4. 优惠券必须来自 query-store-coupons / query-my-coupons 的真实返回；
5. 库存/供应状态以 MCP 菜单返回为准，引擎不做任何库存假设。

为控制 MCP 调用次数（限流 600 次/分钟）：先用菜单价估算可负担性，
只对排名靠前的候选组合发起 calculate-price（每个组合最多 3 次：无券/单券/双券）。
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
    format_calories,
    format_yuan,
)

# ---------------------------------------------------------------------------
# 餐品分类（启发式，用于搭配均衡）
# ---------------------------------------------------------------------------
CATEGORY_RULES = [
    ("drink", ["可乐", "雪碧", "芬达", "果汁", "咖啡", "茶", "奶", "饮料", "饮品", "冰旋"]),
    ("dessert", ["甜筒", "冰淇淋", "圣代", "麦旋风", "甜品"]),
    ("side", ["薯条", "薯", "鸡翅", "麦乐鸡", "玉米", "沙拉", "派", "洋葱圈", "小食", "薯饼"]),
    ("main", ["汉堡", "堡", "套餐", "饭", "卷", "盒", "主食", "全餐", "四件套", "三件套"]),
]

MOOD_AFFINITY = {
    "night": {"drink": 2, "soup": 2, "dessert": 1, "side": 1},
    "tired": {"main": 2, "side": 1, "dessert": 1},
    "celebrate": {"main": 2, "dessert": 2, "drink": 1},
    "refresh": {"veggie": 2, "soup": 2, "drink": 1, "dessert": -1},
    "happy": {"main": 1, "side": 1},
}


def classify(item: MenuItem) -> str:
    text = item.name + " " + item.category + " " + " ".join(item.tags)
    for cat, kws in CATEGORY_RULES:
        if any(k in text for k in kws):
            return cat
    return "other"


# ---------------------------------------------------------------------------
# 评分
# ---------------------------------------------------------------------------


def taste_score(item: MenuItem, tastes: tuple) -> int:
    text = item.name + " " + item.category + " " + " ".join(item.tags)
    score = 0
    if "spicy" in tastes and ("辣" in text):
        score += 4
    if "sweet" in tastes and ("甜" in text or classify(item) == "dessert"):
        score += 3
    if "crispy" in tastes and ("脆" in text or "酥" in text):
        score += 3
    if "burger" in tastes and ("堡" in text):
        score += 3
    if "chicken" in tastes and ("鸡" in text):
        score += 3
    if "beef" in tastes and ("牛" in text):
        score += 3
    if "fish" in tastes and ("鱼" in text):
        score += 3
    if "veggie" in tastes and ("素" in text or "菜" in text or "沙拉" in text):
        score += 3
    if "soup" in tastes and ("汤" in text):
        score += 3
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
# 候选组合生成
# ---------------------------------------------------------------------------


def _rank(items: list[MenuItem], c: Constraints, nutrition: list[NutritionItem], cat: str) -> list[MenuItem]:
    same_cat = [i for i in items if classify(i) == cat and i.price_cents is not None]
    same_cat.sort(key=lambda i: (-item_score(i, c, nutrition), i.price_cents, i.code))
    return same_cat


def _templates(c: Constraints) -> list[tuple[str, dict]]:
    """返回 [(模板名, 每类数量)]。

    模板选择同时考虑人数与预算档位：预算越紧，组合越精简，
    保证「预算内一定有候选」而不是直接失败。
    """
    p = max(c.people, 1)
    if p >= 2:
        return [
            ("share_feast", {"main": p, "side": p, "drink": p}),
            ("share_value", {"main": p, "side": 1, "drink": 1}),
        ]
    budget = c.budget_cents
    if c.max_calories is not None:
        return [
            ("light_meal", {"main": 1, "side": 0, "drink": 1}),
            ("light_plus", {"main": 1, "side": 1, "drink": 1}),
            ("solo_main", {"main": 1}),
        ]
    if budget is not None and budget <= 2500:
        return [
            ("mini_meal", {"main": 1, "side": 0, "drink": 1}),
            ("solo_main", {"main": 1}),
        ]
    if budget is not None and budget <= 4500:
        return [
            ("value_meal", {"main": 1, "side": 1, "drink": 1}),
            ("mini_meal", {"main": 1, "side": 0, "drink": 1}),
            ("solo_main", {"main": 1}),
        ]
    return [
        ("full_meal", {"main": 1, "side": 1, "drink": 1}),
        ("double_main", {"main": 2, "side": 1, "drink": 1}),
        ("solo_main", {"main": 1}),
    ]


def _build_candidates(
    menu: list[MenuItem],
    c: Constraints,
    nutrition: list[NutritionItem],
    max_candidates: int = 6,
) -> list[list[PlanItem]]:
    ranked = {cat: _rank(menu, c, nutrition, cat) for cat in ("main", "side", "drink", "dessert")}
    candidates: list[list[PlanItem]] = []
    seen = set()

    for _tname, slots in _templates(c):
        main_n = slots.get("main", 1)
        side_n = slots.get("side", 0)
        drink_n = slots.get("drink", 0)
        mains = ranked["main"]
        if not mains:
            continue
        # 多人分享：优先两个不同主食各一份；单人双主食：同一主食两份
        main_combos: list[list[PlanItem]] = []
        if main_n >= 2 and c.people >= 2 and len(mains) >= 2:
            main_combos.append([PlanItem(mains[0], 1), PlanItem(mains[1], 1)])
        main_combos.append([PlanItem(mains[0], main_n)])
        if len(mains) > 1 and main_n == 1:
            main_combos.append([PlanItem(mains[1], 1)])
        side_pool = ranked["side"][:1] if side_n else [None]
        drink_pool = ranked["drink"][:1] if drink_n else [None]
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
    # 兜底：菜单为空或没有主食时，用任意有价餐品凑一个「单人尝鲜」
    if not candidates:
        any_items = [i for i in menu if i.price_cents is not None][:2]
        if any_items:
            candidates.append([PlanItem(i, 1) for i in any_items])
    return candidates


# ---------------------------------------------------------------------------
# 优惠券匹配
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
    """生成待验价的优惠券组合（无券 + 每个可用单券 + 门店券×我的券最优双券）。"""
    applicable = [c for c in coupons if coupon_applies(c, combo, estimated_original)]
    sets: list[list[Coupon]] = [[]]
    for c in applicable[:4]:
        sets.append([c])
    store_best = next((c for c in applicable if c.source == "store"), None)
    my_best = next((c for c in applicable if c.source == "my"), None)
    if store_best and my_best and store_best.id != my_best.id:
        sets.append([store_best, my_best])
    return sets


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def build_plan(
    constraints: Constraints,
    menu: list[MenuItem],
    coupons: list[Coupon],
    nutrition: list[NutritionItem],
    price_fn: Callable[[list[PlanItem], list[str]], PriceQuote],
) -> tuple[list[PlanOption], list[str]]:
    """返回 (方案列表, 警告列表)。price_fn 是权威价格计算（MCP calculate-price）。"""
    warnings: list[str] = []

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

    candidates = _build_candidates(priced, constraints, nutrition)
    if not candidates:
        warnings.append("没有找到符合口味/搭配要求的餐品组合")
        return [], warnings

    options: list[PlanOption] = []
    price_failed = 0
    budget_filtered = 0
    fee_est = 900 if constraints.dining_mode == "delivery" else 0
    for combo in candidates:
        estimated = sum(pi.item.price_cents * pi.qty for pi in combo)
        # 预算预筛：估算价 + 配送费估算，留 25% 余量（优惠券可能拉低最终价），
        # 最终是否超预算以 calculate-price 权威结果为准
        if constraints.budget_cents is not None:
            if estimated + fee_est > int(constraints.budget_cents * 1.25):
                budget_filtered += 1
                continue
        best_for_combo: Optional[PlanOption] = None
        for cset in coupon_sets(combo, coupons, estimated):
            quote = price_fn(combo, [c.id for c in cset])
            if not quote.ok or quote.total_cents is None:
                price_failed += 1
                continue  # 权威计算失败：该券组合作废，不编造总价
            opt = PlanOption(
                title="",
                items=list(combo),
                coupons=list(cset),
                total_cents=quote.total_cents,
                original_cents=quote.original_cents,
                discount_cents=quote.discount_cents,
                delivery_fee_cents=quote.delivery_fee_cents,
                price_checked=True,
            )
            # 计算剩余预算与预算符合性
            if constraints.budget_cents is not None:
                opt.within_budget = opt.total_cents <= constraints.budget_cents
                opt.remaining_cents = constraints.budget_cents - opt.total_cents
            # 理由
            opt.reasons = _build_reasons(opt, constraints, nutrition)
            # 评分：口味契合 + 省下的钱 + 剩余预算
            opt.score = _option_score(opt, constraints, nutrition)
            if best_for_combo is None or opt.score > best_for_combo.score:
                best_for_combo = opt
        if best_for_combo is not None:
            options.append(best_for_combo)

    # 排序：优先预算内、其次分数、再次低价
    def sort_key(o: PlanOption):
        return (
            0 if o.within_budget else 1 if o.within_budget is False else 0,
            -o.score,
            o.total_cents or 10**9,
        )

    options.sort(key=sort_key)

    # 预算内过滤：若设置了预算且存在预算内方案，只保留预算内（外加一个最优参考）
    if constraints.budget_cents is not None:
        within = [o for o in options if o.within_budget]
        if within:
            keep = within[:3]
            reference = next((o for o in options if not o.within_budget), None)
            options = keep + ([reference] if reference else [])
        elif options:
            warnings.append(
                f"没有组合能落在 {format_yuan(constraints.budget_cents)} 预算内；"
                f"以下为最接近的真实价格方案（总价以 calculate-price 为准）"
            )
            options = options[:2]
        else:
            options = []

    if not options:
        # 空结果诊断：区分「预算筛掉」「价格计算失败」「无搭配」三种原因，如实说明
        if price_failed and not budget_filtered:
            warnings.append(
                f"所有候选组合的权威价格计算均失败（calculate-price 返回 {price_failed} 次失败），"
                "无法给出方案（未编造任何价格）"
            )
        elif budget_filtered:
            warnings.append(
                f"没有组合能落在 {format_yuan(constraints.budget_cents)} 预算内"
                f"（{budget_filtered} 个候选组合因估算价超出预算被跳过，其余逐一用权威价格核验；未编造任何价格）"
            )
        else:
            warnings.append("没有找到符合搭配要求的餐品组合")

    for o in options:
        o.title = _option_title(o, constraints)
    return options, warnings


def _build_reasons(opt: PlanOption, c: Constraints, nutrition: list[NutritionItem]) -> list[str]:
    reasons = []
    tastes = c.tastes
    hit = []
    for pi in opt.items:
        t = pi.item.name + " ".join(pi.item.tags)
        if "spicy" in tastes and "辣" in t:
            hit.append(pi.item.name)
        if "crispy" in tastes and ("脆" in t or "酥" in t):
            hit.append(pi.item.name)
    if hit:
        reasons.append("口味命中：" + "、".join(dict.fromkeys(hit)))
    if opt.discount_cents:
        reasons.append(f"优惠券已省 {format_yuan(opt.discount_cents)}")
    if c.budget_cents is not None and opt.remaining_cents is not None:
        if opt.remaining_cents >= 0:
            reasons.append(f"剩余预算 {format_yuan(opt.remaining_cents)}")
        else:
            reasons.append(f"超预算 {format_yuan(-opt.remaining_cents)}")
    n = _sum_nutrition(opt, nutrition)
    if n.get("calories_kcal") is not None:
        reasons.append(f"整餐热量 {format_calories(n['calories_kcal'])}")
    if c.max_calories and n.get("calories_kcal") is not None and n["calories_kcal"] <= c.max_calories:
        reasons.append("满足低卡偏好")
    if c.min_protein_g and n.get("protein_g") is not None and n["protein_g"] >= c.min_protein_g:
        reasons.append(f"蛋白质 {n['protein_g']:.0f}g 达标")
    if not reasons:
        reasons.append("当前门店真实在售且价格校验通过的基础搭配")
    return reasons


def _sum_nutrition(opt: PlanOption, nutrition: list[NutritionItem]) -> dict:
    total = {"calories_kcal": 0.0, "protein_g": 0.0, "fat_g": 0.0, "carb_g": 0.0, "sodium_mg": 0.0}
    found = False
    for pi in opt.items:
        n = nutrition_of(pi.item, nutrition)
        if not n:
            continue
        found = True
        for k in total:
            v = getattr(n, k, None)
            if v is not None:
                total[k] += v * pi.qty
    if not found:
        return {}
    total["calories_kcal"] = round(total["calories_kcal"], 1)
    total["protein_g"] = round(total["protein_g"], 1)
    return total


def _option_score(opt: PlanOption, c: Constraints, nutrition: list[NutritionItem]) -> float:
    score = 0.0
    for pi in opt.items:
        score += item_score(pi.item, c, nutrition) * pi.qty
    if opt.discount_cents:
        score += min(opt.discount_cents / 100.0, 5.0)  # 省钱加分，封顶 5
    if c.budget_cents is not None and opt.remaining_cents is not None and opt.remaining_cents >= 0:
        if c.rich:
            # 丰盛偏好：鼓励把预算用得更充分（越接近预算加分越多），封顶 3
            used = (opt.total_cents or 0) / c.budget_cents
            score += min(3.0, max(0.0, (used - 0.3) / 0.7 * 3.0))
        else:
            # 默认：剩余预算加分（省钱导向），封顶 3
            score += min(opt.remaining_cents / 100.0, 3.0)
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
    elif c.budget_cents is not None and opt.total_cents and opt.total_cents <= c.budget_cents:
        base = "省钱精算阵型"
    elif cats.count("main") >= 2:
        base = "双主食饱腹阵型"
    else:
        base = "标准探索阵型"
    if opt.discount_cents:
        base += "·已叠券"
    return base
