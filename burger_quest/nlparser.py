"""自然语言解析层：从用户的一句话里提取结构化约束。

设计原则：
- 纯规则 + 关键词表，确定性输出，可测试、可复现；
- 只提取「用户明确表达或强烈暗示」的信息，识别不到就留空并记入 notes，
  绝不猜测编造（例如预算没提就不设预算上限）；
- 本层不做任何 MCP 调用。
"""
from __future__ import annotations

import re
from typing import Optional

from .models import Constraints, yuan_to_cents

# ---------------------------------------------------------------------------
# 口味关键词表（键 = 内部标签，值 = 同义词）
# ---------------------------------------------------------------------------
TASTE_KEYWORDS = {
    "spicy": ["辣", "麻辣", "香辣", "麦辣", "变态辣"],
    "sweet": ["甜", "甜的", "甜品", "冰淇淋", "甜筒"],
    "crispy": ["脆", "酥", "咔嘣脆"],
    "burger": ["汉堡", "堡", "汉堡包"],
    "chicken": ["鸡", "鸡肉", "鸡翅", "麦乐鸡"],
    "beef": ["牛", "牛肉", "牛堡"],
    "fish": ["鱼", "鱼柳", "鳕鱼"],
    "veggie": ["素", "素食", "蔬菜", "清爽"],
    "soup": ["汤", "暖汤", "玉米汤"],
}

# 心情关键词 -> 内部心情标签
MOOD_KEYWORDS = {
    "celebrate": ["庆祝", "获奖", "升职", "生日", "发工资", "开心", "高兴", "爽"],
    "tired": ["累", "疲惫", "加班", "熬夜", "困", "emo", "难过", "不开心", "失落", "丧"],
    "night": ["深夜", "午夜", "凌晨", "夜宵", "睡不着"],
    "refresh": ["清爽", "解腻", "提神", "醒脑"],
}

DINING_KEYWORDS = {
    "delivery": ["外卖", "外送", "配送", "送到", "送过来", "麦乐送", "点外卖"],
    "dine_in": ["堂食", "店里吃", "坐着吃", "在店里", "堂吃"],
    "takeaway": ["自取", "到店", "取餐", "带走", "外带", "打包"],
}

# 低卡 / 高蛋白等营养偏好
RICH_PAT = re.compile(r"(吃好的|吃点儿好的|吃点好的|丰盛|大餐|豪华|奢侈|丰盛一点|好一点|犒劳)")
LOW_CAL_PAT = re.compile(r"(低卡|减肥|减脂|轻食|不长胖|瘦身|低热量|控制热量|低脂)")
HIGH_PROTEIN_PAT = re.compile(r"(高蛋白|增肌|健身|补充蛋白|蛋白质)")
CAL_LIMIT_PAT = re.compile(r"(?:热量|卡路里)?\s*(?:不超过?|低于?|控制在?|最多)\s*(\d{2,5})\s*(?:千卡|大卡|kcal|卡)")
PROTEIN_MIN_PAT = re.compile(r"蛋白质?\s*(?:至少|不低于|超过?|大于)\s*(\d+(?:\.\d+)?)\s*(?:g|克)")

# 饮料偏好（2026-10-14 规划引擎升级）
# 软上限（优先判定）：「不想点太多饮料 / 别点太多饮料 / 少点饮料 / 饮料少一点 / 多主食少饮料」
LESS_DRINK_PAT = re.compile(
    r"(?:不想点太多饮料|别点太多饮料|不要点太多饮料|点太多饮料|太多饮料|"
    r"少(?:点|一些|一点)?饮料|饮料少(?:点|一些|一点)|多主食少饮料|饮料适量)"
)
# 硬避免：「不要饮料 / 不点饮料 / 不想喝饮料 / 饮料去掉 / 无饮料 / 免饮料」
AVOID_DRINK_PAT = re.compile(
    r"(?:不要|不点|不想喝?|去掉|免了?|无)(?:点|些|任何)?饮料"
    r"|饮料(?:就?)?(?:不要|免|去掉|不用)"
)

BUDGET_PATTERNS = [
    # ¥30 / ￥30 / 30元 / 30块 / 30 RMB
    re.compile(r"[¥￥]\s*(\d+(?:\.\d+)?)"),
    re.compile(r"(\d+(?:\.\d+)?)\s*(?:元|块|rmb|人民币)(?:钱)?(?:以内|左右|上下|之内)?"),
    # 30元 / 30块（无单位前缀关键词）
    re.compile(r"(?:预算|身上只有|只有|最多|不超过?|控制在?|大概|左右|大约|约)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(?:元|块|rmb|人民币|¥)?"),
]
BUDGET_NEG_PAT = re.compile(r"(?:不超过?|最多|控制在?|预算)\s*(\d+(?:\.\d+)?)")
PEOPLE_PAT = re.compile(r"(\d+|[一二两三四五])\s*(?:个)?人(?:吃|份|的餐)?|(\d+)\s*份")
STORE_CODE_PAT = re.compile(r"(?:门店|店铺|餐厅)?\s*(?:编码|code|编号)\s*[:：]?\s*([A-Za-z0-9\-_]{3,})", re.I)
LOCATION_PATTERNS = [
    re.compile(r"(?:送到|送至|配送|外卖)(?:到|至)?\s*([^，。,.!！?？;；\s]{2,20})"),
    re.compile(r"(?:在|于)\s*([^，。,.!！?？;；\s]{2,20})\s*(?:的?麦当劳|吃|点|下单|取餐)"),
    # 句尾位置：「……，在深圳市南山区科技园」（2026-10-14 补充：尾部「在X」）
    re.compile(r"(?:在|于)\s*([^，。,.!！?？;；\s]{2,20})\s*$"),
    re.compile(r"附近\s*([^，。,.!！?？;；\s]{2,20})"),
]
CHINESE_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

# 常见语气词/停用字，位置名词里去掉
LOCATION_STOP = ("麦当劳", "麦门", "门店", "餐厅", "店铺", "附近", "这里", "那边", "店里")
# 疑问片段：不是位置（「附近有什么」「在哪」等）
LOCATION_QUESTION = ("什么", "哪些", "哪家", "哪里", "哪儿", "多少", "怎么")


def _match_any(text: str, keywords) -> Optional[str]:
    for kw in keywords:
        if kw in text:
            return kw
    return None


def parse_budget(text: str) -> tuple[Optional[int], Optional[str]]:
    """返回 (预算分, 命中方式说明)。识别不到返回 (None, None)。"""
    m = BUDGET_NEG_PAT.search(text)
    if m:
        return yuan_to_cents(m.group(1)), "上限表述"
    for pat in BUDGET_PATTERNS:
        m = pat.search(text)
        if m:
            return yuan_to_cents(m.group(1)), "金额表述"
    return None, None


def parse_people(text: str) -> tuple[int, Optional[str]]:
    m = PEOPLE_PAT.search(text)
    if m:
        raw = m.group(1) or m.group(2)
        if raw is None:
            return 1, None
        if raw.isdigit():
            n = int(raw)
        else:
            n = CHINESE_NUM.get(raw, 1)
        return max(1, min(n, 20)), f"{n} 人"
    return 1, None


def parse_tastes(text: str) -> tuple:
    hits = []
    for tag, kws in TASTE_KEYWORDS.items():
        if _match_any(text, kws):
            hits.append(tag)
    return tuple(hits)


def parse_mood(text: str) -> Optional[str]:
    # night 优先于 tired（深夜加班）
    if _match_any(text, MOOD_KEYWORDS["night"]):
        return "night"
    for mood in ("celebrate", "tired", "refresh"):
        if _match_any(text, MOOD_KEYWORDS[mood]):
            return mood
    return None


def parse_dining_mode(text: str) -> str:
    for mode in ("delivery", "dine_in", "takeaway"):
        if _match_any(text, DINING_KEYWORDS[mode]):
            return mode
    return "takeaway"  # 默认到店自取（最保守，不假设配送费）


def dining_mode_matched(text: str) -> bool:
    """用户是否明确表达了用餐方式（用于决定是否追加默认口径提示）。"""
    return any(_match_any(text, kws) for kws in DINING_KEYWORDS.values())


def parse_location(text: str) -> Optional[str]:
    for pat in LOCATION_PATTERNS:
        m = pat.search(text)
        if m:
            loc = m.group(1).strip().strip("的了呀吧呢")
            for stop in LOCATION_STOP:
                loc = loc.replace(stop, "")
            loc = loc.strip()
            if len(loc) >= 2 and not any(q in loc for q in LOCATION_QUESTION):
                return loc
    return None


def parse_store_code(text: str) -> Optional[str]:
    m = STORE_CODE_PAT.search(text)
    return m.group(1) if m else None


def parse(text: str) -> Constraints:
    """主入口：一句话 -> Constraints。"""
    notes: list[str] = []
    text = text.strip()
    if not text:
        notes.append("输入为空，使用默认约束")

    budget, budget_how = parse_budget(text)
    people, people_how = parse_people(text)
    dining = parse_dining_mode(text)
    location = parse_location(text)
    store_code = parse_store_code(text)
    tastes = parse_tastes(text)
    mood = parse_mood(text)

    max_cal = None
    if RICH_PAT.search(text):
        rich = True
        notes.append("识别到丰盛偏好，将在预算内优先更丰富的组合")
    else:
        rich = False
    if LOW_CAL_PAT.search(text):
        max_cal = 600.0  # 经验阈值：单人单餐低卡线（非官方标准，仅作筛选偏好）
        notes.append("识别到低卡偏好，按单人单餐 ≤600 千卡筛选（偏好而非硬性医学建议）")
    m = CAL_LIMIT_PAT.search(text)
    if m:
        max_cal = float(m.group(1))
        notes.append(f"按用户指定热量上限 {max_cal:.0f} 千卡筛选")

    min_protein = None
    if HIGH_PROTEIN_PAT.search(text):
        min_protein = 25.0  # 经验阈值：高蛋白偏好线
        notes.append("识别到高蛋白偏好，按蛋白质 ≥25g 筛选（偏好而非硬性医学建议）")
    m = PROTEIN_MIN_PAT.search(text)
    if m:
        min_protein = float(m.group(1))
        notes.append(f"按用户指定蛋白质下限 {min_protein:.0f}g 筛选")

    # 饮料偏好：软上限（「不想点太多」）优先于硬避免（「不要饮料」）
    avoid_categories: tuple = ()
    max_drink_qty: Optional[int] = None
    if LESS_DRINK_PAT.search(text):
        max_drink_qty = max(1, people - 1)
        notes.append(
            f"识别到「少点饮料」偏好，饮料数量上限 {max_drink_qty} 份（尽量满足，无法满足时会明示）"
        )
    elif AVOID_DRINK_PAT.search(text):
        avoid_categories = ("drink",)
        notes.append("识别到「不要饮料」，候选组合将不含饮品")

    if budget is None:
        notes.append("未识别到预算，将不设预算上限（仍会标注真实总价）")
    if not dining_mode_matched(text):
        notes.append(
            "未识别用餐方式，默认按「到店自取」计算（不含配送费）；如需外送请说明「外送/外卖」"
        )
    if location is None and store_code is None and dining == "delivery":
        notes.append("外送场景但未提供地址/门店，需要用户补充或改用就近门店")
    if store_code:
        notes.append(f"使用指定门店编码 {store_code}")

    return Constraints(
        budget_cents=budget,
        people=people,
        dining_mode=dining,
        location=location,
        store_code=store_code,
        tastes=tastes,
        mood=mood,
        max_calories=max_cal,
        min_protein_g=min_protein,
        rich=rich,
        avoid_categories=avoid_categories,
        max_drink_qty=max_drink_qty,
        raw_text=text,
        notes=tuple(notes),
    )
