"""RPG 游戏主持人层（游戏文案）。

设计原则：
- 文案是「包装」，事实是「内核」：所有数字（价格、热量、优惠）都来自 MCP 真实数据，
  本层只负责叙事风格，绝不生成或修改任何数值；
- 模板 + 输入哈希种子，保证同样输入得到同样任务书（可测试、可复现）；
- 装备绰号是趣味映射，真实餐品名与真实价格始终并列展示，不遮挡事实；
- 世界观/剧情线/成就叙事的单一事实来源在 lore.py，本层只做组装。

称号体系（2026-10-17 统一，两个正交维度，勿混淆）：
- **身份**（BUDGET_TIERS）：按预算档位的趣味身份，任务名/卡片装饰用；
- **称号**（achievements.level_for_xp）：按累计经验的等级称号，成就进度用。
两者都是纯虚构装饰，不代表任何真实权益。
"""
from __future__ import annotations

import hashlib
from typing import Optional

from .achievements import level_for_xp
from .models import Constraints, PlanOption, TASTE_CN, format_yuan
from .planner import _sum_nutrition, classify

# 心情 -> 任务名场景
MOOD_SCENES = {
    "night": "午夜黄金拱门试炼",
    "tired": "疲惫勇者的治愈圣殿",
    "celebrate": "庆典之王的加冕宴",
    "refresh": "清爽晨曦的净化之旅",
    "happy": "快乐行者的日常巡礼",
    None: "无名冒险者的第一道门槛",
}

# 预算档位 -> 身份
BUDGET_TIERS = [
    (2000, "节俭骑士"),
    (3500, "精打细算的游侠"),
    (6000, "出手阔绰的冒险者"),
    (10**9, "挥金如土的领主"),
]

DINING_SUFFIX = {
    "delivery": "麦乐送远征",
    "takeaway": "到店取餐密令",
    "dine_in": "堂食据点集结",
}

# 装备绰号（趣味映射，真实名称始终并列展示）
EPITHET_MAP = {
    "巨无霸": "双层面包的永恒经典",
    "麦辣鸡腿堡": "烈焰骑士的招牌巨剑",
    "板烧鸡腿堡": "炭火秘术烤炉出品",
    "双层吉士堡": "双倍干酪壁垒",
    "麦香鱼": "深海波纹三明治",
    "薯条": "黄金竖琴",
    "麦乐鸡": "欢愉小块护符",
    "麦辣鸡翅": "火焰双翼",
    "可乐": "气泡生命药水",
    "雪碧": "清凉薄荷护盾",
    "甜筒": "倒置的奶油尖塔",
    "菠萝派": "热带榴弹",
    "玉米": "金色田野图腾",
    "麦辣鸡腿堡套餐": "烈焰骑士的全套铠甲",
    "巨无霸套餐": "王者的完整冠冕",
    "板烧鸡腿堡套餐": "炭火秘术的仪式套装",
}
EPITHET_FALLBACK = {
    "main": "饱腹战刃",
    "side": "能量副武器",
    "drink": "补给药水",
    "dessert": "甜蜜圣物",
    "other": "神秘补给",
}


def _seed(text: str) -> int:
    return int(hashlib.md5(text.encode("utf-8")).hexdigest()[:8], 16)


def quest_name(c: Constraints, option: Optional[PlanOption]) -> str:
    scene = MOOD_SCENES.get(c.mood, MOOD_SCENES[None])
    tier = "冒险者"
    if c.budget_cents is not None:
        for limit, name in BUDGET_TIERS:
            if c.budget_cents <= limit:
                tier = name
                break
    mode = DINING_SUFFIX.get(c.dining_mode, "")
    taste = ""
    if c.tastes:
        taste = "·" + TASTE_CN.get(c.tastes[0], c.tastes[0]) + "偏好"
    return f"{scene} · {tier}{taste}（{mode}）"


def story(c: Constraints, store_name: str = "", demo: bool = False, chapters: Optional[list] = None) -> str:
    """任务背景故事（确定性：种子为用户输入哈希；chapters 为已解锁剧情章节）。"""
    where = f"『{store_name}』" if store_name else "附近的黄金拱门据点"
    loc = f"，坐标 {c.location}" if c.location else ""
    budget = f"，兜里只剩 {format_yuan(c.budget_cents)}" if c.budget_cents is not None else ""
    people = "独自" if c.people == 1 else f"与 {c.people - 1} 位同伴"
    taste_cn = "、".join(TASTE_CN.get(t, t) for t in c.tastes)
    want = f"，渴望着{taste_cn}的味道" if taste_cn else ""
    seed = _seed(c.raw_text)
    openers = [
        "夜色像打翻的可乐一样漫过街道",
        "午后的阳光把招牌晒得发烫",
        "饥饿感如同迟到的debuff悄然生效",
        "城市的霓虹与拱门灯牌遥相呼应",
    ]
    opener = openers[seed % len(openers)]
    menu_desc = (
        "柜台后的屏幕闪烁着一行密文——那是演示菜单（Mock 数据，非实时菜单）"
        if demo
        else "柜台后的屏幕闪烁着一行密文——那是今日真实在售的菜单"
    )
    oracle = (
        "智囊团当前运行在演示模式，价格与优惠均为演示数值"
        if demo
        else "智囊团已接入麦当劳 MCP 水晶球，价格与优惠以官方实时数据为准"
    )
    text = (
        f"{opener}，你{people}站在{where}{loc}{budget}{want}。"
        f"{menu_desc}。"
        "作为麦门异世界的见习冒险者，你需要在预算与口味的双重封印下，"
        "从这份菜单中组出属于自己的传说装备。"
        f"{oracle}。祝你好运，冒险者。"
    )
    # 剧情章节（虚构叙事，仅追加不修改事实；无章节时不追加）
    for chapter in chapters or []:
        text += f"【异世界见闻】{chapter.name}——{chapter.summary}"
    return text


def gear_lines(option: PlanOption) -> list[str]:
    lines = []
    for pi in option.items:
        cat = classify(pi.item)
        epithet = EPITHET_MAP.get(pi.item.name, EPITHET_FALLBACK.get(cat, "补给"))
        qty = f" ×{pi.qty}" if pi.qty > 1 else ""
        lines.append(f"⚔️ {epithet} —— {pi.item.name}{qty}（{pi.item.price_text()}）")
    for c in option.coupons:
        lines.append(f"🎟️ 券灵加持 —— {c.name}")
    return lines


def price_lines(option: PlanOption, c: Constraints) -> list[str]:
    lines = []
    if option.original_cents is not None:
        lines.append(f"原价合计：{format_yuan(option.original_cents)}")
    if option.discount_cents is not None:
        lines.append(f"优惠减免：-{format_yuan(option.discount_cents)}")
    if option.delivery_fee_cents:
        lines.append(f"配送费：{format_yuan(option.delivery_fee_cents)}")
    if option.total_cents is not None:
        lines.append(f"应付总价：{format_yuan(option.total_cents)}（calculate-price 权威结果）")
    if c.budget_cents is not None and option.remaining_cents is not None:
        if option.remaining_cents >= 0:
            lines.append(f"剩余预算：{format_yuan(option.remaining_cents)}")
        else:
            lines.append(f"超出预算：{format_yuan(-option.remaining_cents)}")
    return lines


# 用餐渠道 -> 中文（价格适用范围标注用，与 cards._CHANNEL_CN 一致）
_CHANNEL_CN = {"delivery": "外送（麦乐送）", "takeaway": "到店自取", "dine_in": "堂食"}


def price_scope_lines(store, c: Constraints, demo: bool = False) -> list[str]:
    """价格适用范围说明（2026-10-17 价格专项审计新增）。

    价格只代表「该门店 + 该渠道 + 该次查询」的官方返回；官方价格随门店/
    时间/渠道/规格变化。命令行与分享卡给出一致口径。
    """
    if demo:
        return ["📌 价格适用范围：演示数据（非实时菜单）——不构成任何真实价格"]
    channel = _CHANNEL_CN.get(c.dining_mode, c.dining_mode)
    code = store.code if store is not None else "未知"
    return [
        f"📌 价格适用范围：门店编码 {code} · {channel} · 官方实时返回，随门店/时间/渠道/规格变化",
        "💱 「单价」为官方按商品行返回的行小计归一（数量>1 时 ÷数量）；套餐类餐品（三件套/双全盒/单人餐等）该单价即套餐组合价",
    ]


def nutrition_lines(option: PlanOption, nutrition: list) -> list[str]:
    n = _sum_nutrition(option, nutrition)
    if not n:
        if option.nutrition_missing:
            return ["🧪 属性面板：营养数据缺失（" + "、".join(option.nutrition_missing) + "），未展示数值——不猜测"]
        return []
    out = []
    if n.get("calories_kcal") is not None:
        out.append(f"热量约 {n['calories_kcal']:.0f} 千卡")
    if n.get("protein_g") is not None:
        out.append(f"蛋白质约 {n['protein_g']:.0f}g")
    if n.get("fat_g") is not None:
        out.append(f"脂肪约 {n['fat_g']:.0f}g")
    if not option.nutrition_complete:
        out.append("（部分餐品无营养数据，数值可能偏低——未猜测）")
    return ["🧪 属性面板：" + "，".join(out)] if out else []
