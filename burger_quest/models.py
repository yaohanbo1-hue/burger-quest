"""数据模型层。

设计原则：
1. 金额一律使用「分」(int) 表示，杜绝浮点误差；只有展示时才格式化为元。
2. 所有模型与 MCP 原始返回解耦，原始数据保留在 raw 字段中供审计。
3. 价格未知就是 None —— 引擎任何位置都禁止编造价格。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# ---------------------------------------------------------------------------
# 金额工具
# ---------------------------------------------------------------------------


def yuan_to_cents(value: Any) -> Optional[int]:
    """把 "25.5" / "¥25.5" / 25.5 之类的输入按「元」转成 int 分；无法解析返回 None。

    注意：若数据源以「分」为单位，请由适配层先转换，不要直接传进来。
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, str):
            value = value.strip().replace("¥", "").replace("元", "").replace(",", "")
            if not value:
                return None
        f = float(value)
    except (TypeError, ValueError):
        return None
    return int(round(f * 100))


def format_yuan(cents: Optional[int]) -> str:
    """2550 -> '¥25.50'；None -> '价格待确认'。"""
    if cents is None:
        return "价格待确认"
    sign = "-" if cents < 0 else ""
    c = abs(int(cents))
    return f"{sign}¥{c // 100}.{c % 100:02d}"


def format_calories(kcal: Optional[float]) -> str:
    if kcal is None:
        return "热量未知"
    return f"约 {kcal:.0f} 千卡"


# 口味标签 -> 中文（供 describe / RPG 文案共用，避免循环依赖集中定义于此）
TASTE_CN = {
    "spicy": "辣味", "sweet": "甜味", "crispy": "酥脆", "burger": "汉堡",
    "chicken": "鸡肉", "beef": "牛肉", "fish": "鱼肉", "veggie": "清爽",
    "soup": "暖汤",
}


# ---------------------------------------------------------------------------
# 门店 / 菜单 / 优惠券 / 营养
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Store:
    code: str
    name: str = ""
    address: str = ""
    city: str = ""
    be_code: str = ""  # 业务编码（外送/得来速场景必传，联调实测）
    raw: dict = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class MenuItem:
    code: str
    name: str
    category: str = ""
    price_cents: Optional[int] = None  # None = MCP 未返回真实价格，禁止编造
    tags: tuple = ()
    raw: dict = field(default_factory=dict, compare=False)

    def price_text(self) -> str:
        return format_yuan(self.price_cents)


@dataclass(frozen=True)
class Campaign:
    """官方活动日历中的一条活动（campaign-calendar，只读）。

    status：live=进行中（官方「今日」段）｜upcoming=未来｜past=往期。
    仅展示官方返回的名称/内容/日期/图片；不派生奖品、优惠金额或资格。
    """

    date: str          # yyyy-MM-dd（官方分段日期）
    title: str
    content: str = ""
    image_url: str = ""
    status: str = "upcoming"


@dataclass(frozen=True)
class Coupon:
    id: str
    name: str
    # couponCode：官方 calculate-price 要求 couponId 与 couponCode 同时提交
    # （2026-10-16 真实联调实测：只传 couponId 报业务错误 600010）
    code: str = ""
    source: str = "store"  # store=门店可用券 | my=我的卡包 | available=麦麦省可领
    kind: str = "threshold"  # threshold=满减 | percent=折扣 | item=指定商品
    value_cents: Optional[int] = None  # 满减：减免金额（分）；折扣：忽略
    threshold_cents: Optional[int] = None  # 满减：门槛（分）
    percent: Optional[float] = None  # 折扣：如 0.5 表示五折
    applicable_codes: tuple = ()  # 指定商品券适用餐品编码；空=全场
    raw: dict = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class PointsAccount:
    """麦享会积分账户（query-my-account，只读）。

    **只保留积分数值**：官方返回的 accountId 属于账户标识，绝不进入模型、
    日志、CLI/Web 输出。字段缺失或无法解析时为 None（上层显示「未知」），
    不得显示成 0。
    """

    available: Optional[int] = None
    accumulative: Optional[int] = None
    used: Optional[int] = None
    expiring_next_month: Optional[int] = None
    currency: str = ""
    unknown_fields: tuple = ()


@dataclass(frozen=True)
class CouponQuery:
    """可领券查询结果：显式区分「真实列表 / 官方明确无券 / 语义未知」。

    2026-10-18 裸 HTTP 实测（绕过 SDK 校验）：
      available-coupons 无券时返回 {"success": false, "code": 499,
        "message": "No coupons available", "data": null}
      query-my-coupons 无券时返回 {"success": true, "data": {"coupons": []}}
    两者语义都是「无券」，但一个用业务错误码、一个用空数组——必须区分，
    不能把「语义未知」显示成「0 张券」。
    """

    state: str  # ok=有列表｜empty=官方明确无券｜unknown=有返回但语义无法确认
    coupons: list["Coupon"] = field(default_factory=list)
    note: str = ""
    raw_summary: dict = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class NutritionItem:
    name: str
    calories_kcal: Optional[float] = None
    protein_g: Optional[float] = None
    fat_g: Optional[float] = None
    carb_g: Optional[float] = None
    sodium_mg: Optional[float] = None
    calcium_mg: Optional[float] = None
    raw: dict = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class PriceQuote:
    """calculate-price 的权威返回。total_cents=None 表示计算失败/未返回。"""

    total_cents: Optional[int] = None
    original_cents: Optional[int] = None
    discount_cents: Optional[int] = None
    delivery_fee_cents: Optional[int] = None
    ok: bool = False
    message: str = ""
    raw: dict = field(default_factory=dict, compare=False)


# ---------------------------------------------------------------------------
# 需求约束（自然语言解析结果）
# ---------------------------------------------------------------------------


@dataclass
class Constraints:
    budget_cents: Optional[int] = None
    people: int = 1
    dining_mode: str = "takeaway"  # delivery=外送 | takeaway=到店自取 | dine_in=堂食
    location: Optional[str] = None
    store_code: Optional[str] = None
    tastes: tuple = ()  # spicy/sweet/crispy/burger/chicken/beef/fish/veggie/soup
    avoid_tastes: tuple = ()  # 明确不吃的口味（「不吃辣」——避免性约束，优先于正向偏好）
    spicy_level: Optional[str] = None  # mild=微辣 | medium=中辣/香辣 | extra=特辣/变态辣（仅记录用户表述）
    mood: Optional[str] = None  # happy/sad/tired/night/celebrate/plain
    max_calories: Optional[float] = None
    min_protein_g: Optional[float] = None
    rich: bool = False  # 丰盛偏好：预算内优先更丰富的组合
    prefer_side: bool = False  # 明确想要小食/配餐（2026-10-18）：模板保证 side 槽位 ≥1
    prefer_combo: bool = True
    avoid_categories: tuple = ()  # 明确不要的品类（如 ("drink",)——「不要饮料」）
    max_drink_qty: Optional[int] = None  # 饮料数量软上限（「不想点太多饮料」；None=不限制）
    fuzzy_budget: bool = False  # 预算来自模糊区间（如「二三十元」按上限处理）
    sources: dict = field(default_factory=dict)  # 约束来源：explicit=用户明确给出 / inferred=系统推断 / default=系统默认
    raw_text: str = ""
    notes: tuple = ()  # 解析过程中的提示（如"未识别到预算"）

    def has_nutrition_preference(self) -> bool:
        """用户是否表达了营养偏好（决定营养数据缺失时是否降权/排除候选）。"""
        return self.max_calories is not None or self.min_protein_g is not None

    def spicy_level_cn(self) -> str:
        return {"mild": "微辣", "medium": "中辣/香辣", "extra": "特辣/变态辣"}.get(self.spicy_level or "", "")

    def to_debug_dict(self, include_raw: bool = False) -> dict:
        """结构化调试表示（默认不含原始输入，避免不必要地回显用户文本）。"""
        d = {
            "budget_cents": self.budget_cents,
            "fuzzy_budget": self.fuzzy_budget,
            "people": self.people,
            "dining_mode": self.dining_mode,
            "location": self.location,
            "store_code": self.store_code,
            "tastes": list(self.tastes),
            "avoid_tastes": list(self.avoid_tastes),
            "spicy_level": self.spicy_level,
            "mood": self.mood,
            "max_calories": self.max_calories,
            "min_protein_g": self.min_protein_g,
            "rich": self.rich,
            "avoid_categories": list(self.avoid_categories),
            "max_drink_qty": self.max_drink_qty,
            "sources": dict(self.sources),
            "notes": list(self.notes),
        }
        if include_raw:
            d["raw_text"] = self.raw_text
        else:
            d["raw_text_length"] = len(self.raw_text)
        return d

    def describe(self) -> str:
        parts = []
        if self.budget_cents is not None:
            budget = format_yuan(self.budget_cents) + ("（模糊区间上限）" if self.fuzzy_budget else "")
            parts.append(f"预算 {budget}")
        parts.append(f"{self.people} 人")
        mode_cn = {"delivery": "外送", "takeaway": "到店自取", "dine_in": "堂食"}
        parts.append(mode_cn.get(self.dining_mode, self.dining_mode))
        if self.location:
            parts.append(f"位置 {self.location}")
        if self.tastes:
            parts.append("口味 " + "/".join(TASTE_CN.get(t, t) for t in self.tastes))
        if self.avoid_tastes:
            parts.append("不吃 " + "/".join(TASTE_CN.get(t, t) for t in self.avoid_tastes))
        if self.max_calories:
            parts.append(f"热量 ≤ {self.max_calories:.0f} 千卡")
        if self.min_protein_g:
            parts.append(f"蛋白质 ≥ {self.min_protein_g:.0f}g")
        if self.prefer_side:
            parts.append("要小食")
        if "drink" in self.avoid_categories:
            parts.append("不要饮料")
        elif self.max_drink_qty is not None:
            parts.append(f"饮料 ≤ {self.max_drink_qty} 份")
        if "side" in self.avoid_categories:
            parts.append("不要小食")
        return "，".join(parts)


# ---------------------------------------------------------------------------
# 规划结果
# ---------------------------------------------------------------------------


@dataclass
class PlanItem:
    item: MenuItem
    qty: int = 1


@dataclass
class PlanOption:
    """一个可执行的点餐方案。total_cents 只能来自 calculate-price 权威结果。"""

    title: str = ""
    items: list = field(default_factory=list)  # list[PlanItem]
    coupons: list = field(default_factory=list)  # list[Coupon]（提交核验的券）
    total_cents: Optional[int] = None  # calculate-price 返回的应付总价（分）
    original_cents: Optional[int] = None
    discount_cents: Optional[int] = None
    delivery_fee_cents: Optional[int] = None
    within_budget: Optional[bool] = None
    remaining_cents: Optional[int] = None
    reasons: list = field(default_factory=list)  # 选择理由（全部有事实依据）
    unmet: list = field(default_factory=list)  # 未完全满足的条件（简洁说明，不隐瞒）
    warnings: list = field(default_factory=list)
    nutrition: dict = field(default_factory=dict)  # {calories, protein_g, ...} 或 {}
    nutrition_complete: bool = True  # 营养数据是否覆盖全部餐品（缺失时 False）
    nutrition_missing: list = field(default_factory=list)  # 缺营养数据的餐品名
    coupon_note: str = ""  # 优惠核验备注（如「提交的券未产生优惠，已按权威结果计价」）
    score: float = 0.0
    price_checked: bool = False  # 是否经过 calculate-price 校验

    def item_names(self) -> str:
        out = []
        for pi in self.items:
            if pi.qty > 1:
                out.append(f"{pi.item.name}×{pi.qty}")
            else:
                out.append(pi.item.name)
        return " + ".join(out)


@dataclass
class QuestResult:
    """一次完整任务的输出（RPG 任务书 + 数据审计）。"""

    constraints: Constraints
    store: Optional[Store] = None
    store_candidates: list = field(default_factory=list)  # 多门店候选：等待用户选择（不默默挑选）
    time_info: str = ""
    quest_name: str = ""
    story: str = ""
    gear_lines: list = field(default_factory=list)  # 装备行（含 RPG 前缀 + 真实餐品）
    options: list = field(default_factory=list)  # list[PlanOption]
    chosen: Optional[PlanOption] = None
    card_html: str = ""
    card_markdown: str = ""
    warnings: list = field(default_factory=list)  # 全局提示（MCP 失败等）
    data_sources: dict = field(default_factory=dict)  # 每个数据块的来源说明
    mcp_calls: list = field(default_factory=list)  # 审计：实际发起的 MCP 调用
    # 真实数据计数（2026-10-17 成就系统新增）：menu/nutrition/coupons/stores 的真实返回条数
    counts: dict = field(default_factory=dict)
    # 预算三态统计（2026-10-17 推荐算法完善新增）：
    # within=确定符合预算（权威总价 ≤ 预算）/ over=超过预算的参考方案 /
    # unknown=价格缺失无法确认的餐品数（已排除，不猜价格）/ priced=有权威价的餐品数
    price_states: dict = field(default_factory=dict)
