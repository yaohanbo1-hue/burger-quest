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
class Coupon:
    id: str
    name: str
    source: str = "store"  # store=门店可用券 | my=我的卡包 | available=麦麦省可领
    kind: str = "threshold"  # threshold=满减 | percent=折扣 | item=指定商品
    value_cents: Optional[int] = None  # 满减：减免金额（分）；折扣：忽略
    threshold_cents: Optional[int] = None  # 满减：门槛（分）
    percent: Optional[float] = None  # 折扣：如 0.5 表示五折
    applicable_codes: tuple = ()  # 指定商品券适用餐品编码；空=全场
    raw: dict = field(default_factory=dict, compare=False)


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
    mood: Optional[str] = None  # happy/sad/tired/night/celebrate/plain
    max_calories: Optional[float] = None
    min_protein_g: Optional[float] = None
    rich: bool = False  # 丰盛偏好：预算内优先更丰富的组合
    prefer_combo: bool = True
    raw_text: str = ""
    notes: tuple = ()  # 解析过程中的提示（如"未识别到预算"）

    def describe(self) -> str:
        parts = []
        if self.budget_cents is not None:
            parts.append(f"预算 {format_yuan(self.budget_cents)}")
        parts.append(f"{self.people} 人")
        mode_cn = {"delivery": "外送", "takeaway": "到店自取", "dine_in": "堂食"}
        parts.append(mode_cn.get(self.dining_mode, self.dining_mode))
        if self.location:
            parts.append(f"位置 {self.location}")
        if self.tastes:
            parts.append("口味 " + "/".join(TASTE_CN.get(t, t) for t in self.tastes))
        if self.max_calories:
            parts.append(f"热量 ≤ {self.max_calories:.0f} 千卡")
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

    title: str
    items: list = field(default_factory=list)  # list[PlanItem]
    coupons: list = field(default_factory=list)  # list[Coupon]
    total_cents: Optional[int] = None  # calculate-price 返回的应付总价（分）
    original_cents: Optional[int] = None
    discount_cents: Optional[int] = None
    delivery_fee_cents: Optional[int] = None
    within_budget: Optional[bool] = None
    remaining_cents: Optional[int] = None
    reasons: list = field(default_factory=list)  # 选择理由（全部有事实依据）
    warnings: list = field(default_factory=list)
    nutrition: dict = field(default_factory=dict)  # {calories, protein_g, ...} 或 {}
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
