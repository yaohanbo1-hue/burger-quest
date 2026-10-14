"""自然语言解析层：从用户的一句话里提取结构化约束。

设计原则：
- 纯规则 + 关键词表，确定性输出，可测试、可复现（2026-10-14 评估结论：
  目标表达全集均可由更清晰的规则覆盖，无需引入大模型/NLP 依赖）；
- 只提取「用户明确表达或强烈暗示」的信息，识别不到就留空并记入 notes，
  绝不猜测编造（例如预算没提就不设预算上限）；
- 明确区分三类约束来源（Constraints.sources）：
  explicit=用户明确给出（硬限制）｜inferred=系统从软偏好推断（可解释的经验值）
  ｜default=系统默认（用户未表达）；
- 冲突与歧义不静默裁决：要么给出可解释的处理（取更严格值/避免优先），
  要么明示无法可靠解析——绝不假装理解成功；
- 本层不做任何 MCP 调用。

2026-10-14 升级修复的实测问题：
1. 「热量控制在400千卡以内」的 400 被误读为预算（¥400）并污染地点
   -> 预算模式增加营养单位/营养词守卫；
2. 「不吃辣」被解析成想吃辣 -> 新增否定口味（avoid_tastes），
   避免性约束优先于正向偏好，冲突时明示；
3. 「大概二三十元」等模糊预算无法识别 -> 中文数字区间解析，
   按上限设置预算并注明区间；
4. 「少糖」静默丢失 -> 识别但明示营养数据无糖字段、无法按此筛选；
5. 门店名称（「麦当劳深圳同方信息港餐厅」）无法作为地点 -> 新增门店名模式。
"""
from __future__ import annotations

import re
from typing import Optional

from .models import Constraints, TASTE_CN, format_yuan, yuan_to_cents

# ---------------------------------------------------------------------------
# 口味关键词表（键 = 内部标签，值 = 同义词）
# ---------------------------------------------------------------------------
TASTE_KEYWORDS = {
    "spicy": ["辣", "麻辣", "香辣", "麦辣", "变态辣"],
    "sweet": ["甜", "甜品", "冰淇淋", "甜筒"],
    "crispy": ["脆", "酥", "咔嘣脆"],
    "burger": ["汉堡", "堡", "汉堡包"],
    "chicken": ["鸡", "鸡肉", "鸡翅", "麦乐鸡"],
    "beef": ["牛", "牛肉", "牛堡"],
    "fish": ["鱼", "鱼柳", "鳕鱼"],
    "veggie": ["素", "素食", "蔬菜", "清爽", "清淡"],
    "soup": ["汤", "暖汤", "玉米汤"],
}

# 心情关键词 -> 内部心情标签
MOOD_KEYWORDS = {
    "celebrate": ["庆祝", "获奖", "升职", "生日", "发工资", "开心", "高兴", "爽", "周五"],
    "tired": ["累", "疲惫", "加班", "熬夜", "困", "emo", "难过", "不开心", "失落", "丧", "回血"],
    "night": ["深夜", "午夜", "凌晨", "夜宵", "睡不着"],
    "refresh": ["清爽", "解腻", "提神", "醒脑", "放松"],
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
# 少糖/低糖：识别到但不可执行（官方营养数据无糖分字段）——明示而非静默丢弃
LESS_SUGAR_PAT = re.compile(r"(少糖|低糖|减糖|糖分少|无糖|不加糖)")

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

# ---------------------------------------------------------------------------
# 预算（2026-10-14 修复：营养单位守卫 + 模糊区间 + 多值取严）
# ---------------------------------------------------------------------------
# 预算数字后不允许紧跟的营养/重量单位（防止「400千卡」被当成 400 元）
_NUTRI_UNIT_AFTER = r"(?!\s*(?:千卡|大卡|kcal|卡|克|g|G|%|ml|毫升|毫克|mg))"
# 预算触发词前不允许出现的营养主语（防止「热量控制在400」被当成预算）
_NUTRI_SUBJECT_BEFORE = re.compile(r"(热量|卡路里|蛋白质|蛋白|糖|脂肪|钠|碳水|热量控制|卡路里控制)\s*$")

BUDGET_NEG_PAT = re.compile(r"(?:不超过?|最多|控制在?|预算)\s*[:：]?\s*(\d+(?:\.\d+)?)" + _NUTRI_UNIT_AFTER)
BUDGET_PATTERNS = [
    # ¥30 / ￥30
    re.compile(r"[¥￥]\s*(\d+(?:\.\d+)?)" + _NUTRI_UNIT_AFTER),
    # 30元 / 30块 / 30 RMB（可带「以内/左右/上下/之内」后缀）
    re.compile(r"(\d+(?:\.\d+)?)\s*(?:元|块|rmb|人民币)(?:钱)?(?:以内|左右|上下|之内)?"),
    # 「预算/只有/大概/约 + 数字」（单位可选）
    re.compile(
        r"(?:预算|身上只有|只有|最多|不超过?|控制在?|大概|左右|大约|约)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*"
        r"(?:元|块|rmb|人民币)?" + _NUTRI_UNIT_AFTER
    ),
]

# 中文数字（0-99）三种结构：
#   X十Y（三十五=35，Y 在十后）｜X[Y]十（二三十=20~30 模糊区间，Y 在十前且与 X 相邻）｜十（=10）
CN_DIGIT = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
CN_NEXT = {1: 2, 2: 3, 3: 4, 4: 5, 5: 6, 6: 7, 7: 8, 8: 9}  # 二->三、三->四 ……
CN_NUM_PAT = re.compile(
    r"(?:([一二两三四五六七八九])([一二三四五六九])?)?\s*十\s*(?:([一二三四五六七八九]))?"
    r"\s*(?:元|块|rmb|人民币)(?:钱)?(?:以内|左右|上下|之内)?"
)
# 「十来块」≈ 10~15（必须带「来」，避免误吃「二三十元」）
ABOUT_TEN_PAT = re.compile(r"(?<![一二两三四五六七八九])十\s*来\s*(?:元|块|rmb|人民币)(?:钱)?")


def parse_cn_number(m: re.Match) -> Optional[int]:
    """解析「十 / X十 / X十Y」为整数（0-99）；无法解析返回 None。"""
    tens_raw, ones_raw = m.group(1), m.group(2)
    tens = CN_DIGIT.get(tens_raw, 1) if tens_raw else 1
    ones = CN_DIGIT.get(ones_raw, 0) if ones_raw else 0
    if tens_raw and tens > 1 and ones_raw:
        return tens * 10 + ones  # 三十五 = 35
    if tens_raw:
        return tens * 10  # 三十 = 30
    return 10 + ones  # 十X = 1X


# ---------------------------------------------------------------------------
# 否定口味（2026-10-14 新增）：「不吃辣」不等于「想吃辣」
# ---------------------------------------------------------------------------
# 直接否定：「不辣 / 别甜 / 免辣 / 忌辣」
NEG_TASTE_DIRECT = re.compile(r"(?:不|别|免|忌|戒|讨厌|嫌弃|受不了)(辣|甜|脆|酥|汉堡|堡|鸡|牛|鱼|素|汤)")
# 短语否定：「不吃（点/些）辣」「不想吃辣」「不要牛肉」
NEG_TASTE_PHRASE = re.compile(
    r"(?:不吃|不想吃|不要|别|免|去掉|讨厌|嫌弃|受不了|无)(?:点|些|任何|一?点)?\s*"
    r"(太)?(辣|甜|脆|酥|汉堡|堡|鸡|牛|鱼|素|汤)"
)
NEG_HINT_WORDS = ("不吃", "不想吃", "不要", "别", "免", "去掉", "讨厌", "嫌弃", "受不了", "忌", "戒", "无")

PEOPLE_PAT = re.compile(r"(\d+|[一二两三四五])\s*(?:个)?人(?:吃|份|的餐)?|(\d+)\s*份")
STORE_CODE_PAT = re.compile(r"(?:门店|店铺|餐厅)?\s*(?:编码|code|编号)\s*[:：]?\s*([A-Za-z0-9\-_]{3,})", re.I)
# 门店名称作地点：「麦当劳深圳同方信息港餐厅」「麦当劳（深圳南山科技园店）」
STORE_NAME_PAT = re.compile(
    r"麦当劳[（(]?\s*([^，。,.!！?？;；\s）)]{2,15}?)\s*(?:餐厅|甜品站|麦咖啡|店)[）)]?"
)
LOCATION_PATTERNS = [
    re.compile(r"(?:送到|送至|配送|外卖)(?:到|至)?\s*([^，。,.!！?？;；\s]{2,20})"),
    re.compile(r"(?:在|于)\s*([^，。,.!！?？;；\s]{2,20})\s*(?:的?麦当劳|吃|点|下单|取餐)"),
    # 句尾位置：「……，在深圳市南山区科技园」（2026-10-14 补充：尾部「在X」）
    re.compile(r"(?:在|于)\s*([^，。,.!！?？;；\s]{2,20})\s*$"),
    re.compile(r"附近\s*([^，。,.!！?？;；\s]{2,20})"),
    # 「X附近」：「科技园附近」「中关村附近」（2026-10-14 补充：位置在「附近」前）
    re.compile(r"([^，。,.!！?？;；\s]{2,20})\s*附近"),
]

# 常见语气词/停用字，位置名词里去掉
LOCATION_STOP = ("麦当劳", "麦门", "门店", "餐厅", "店铺", "附近", "这里", "那边", "店里", "甜品站", "麦咖啡")
# 疑问片段：不是位置（「附近有什么」「在哪」等）
LOCATION_QUESTION = ("什么", "哪些", "哪家", "哪里", "哪儿", "多少", "怎么")
# 位置里出现这些片段说明匹配到的不是地点（防止「控制在400千卡以内」被当成位置）
LOCATION_REJECT = ("千卡", "大卡", "kcal", "卡路里", "蛋白质", "蛋白", "克", "g", "G")


def _clean_location(loc: str) -> Optional[str]:
    """清洗并校验位置片段；不合格返回 None（数字开头/含营养单位/过短/疑问词）。"""
    loc = loc.strip().strip("的了呀吧呢")
    for stop in LOCATION_STOP:
        loc = loc.replace(stop, "")
    loc = loc.strip()
    if len(loc) < 2:
        return None
    if loc[0].isdigit():  # 地点不会以数字开头（「400千卡以内」）
        return None
    if any(q in loc for q in LOCATION_QUESTION):
        return None
    if any(u in loc for u in LOCATION_REJECT):
        return None
    return loc


def _match_any(text: str, keywords) -> Optional[str]:
    for kw in keywords:
        if kw in text:
            return kw
    return None


def _is_nutrition_context(text: str, start: int) -> bool:
    """匹配位置是否处于营养语境（前面是营养主语，如「热量控制在400」）。"""
    return bool(_NUTRI_SUBJECT_BEFORE.search(text[:start]))


# ---------------------------------------------------------------------------
# 预算
# ---------------------------------------------------------------------------


def _fuzzy_budget(text: str) -> tuple[Optional[int], Optional[int], Optional[str]]:
    """中文数字预算 -> (下限分, 上限分, 原文片段)。

    - 「二三十」= 20~30（十前双数字且相邻 -> 模糊区间，取上限）；
    - 「三十五」= 确切 35；「三十」= 30；「十块」= 10；
    - 「十来块」≈ 10~15。
    """
    m = ABOUT_TEN_PAT.search(text)
    if m:
        return 1000, 1500, m.group(0)
    m = CN_NUM_PAT.search(text)
    if m:
        tens_raw, before_raw, after_raw = m.group(1), m.group(2), m.group(3)
        tens = CN_DIGIT.get(tens_raw, 1) if tens_raw else 1
        if before_raw:
            b = CN_DIGIT[before_raw]
            if CN_NEXT.get(tens) == b:
                # 「二三十」= 20~30（相邻读数视为区间）
                return tens * 10 * 100, (tens + 1) * 10 * 100, m.group(0)
            n = tens * 10 + b  # 非常规读法，按字面处理
            return n * 100, n * 100, m.group(0)
        if after_raw:
            n = tens * 10 + CN_DIGIT[after_raw]  # 「三十五」= 35
            return n * 100, n * 100, m.group(0)
        return tens * 10 * 100, tens * 10 * 100, m.group(0)  # 「三十」= 30
    return None, None, None


def parse_budget(text: str) -> tuple[Optional[int], Optional[str], Optional[int], Optional[int], list[int]]:
    """返回 (预算分, 命中方式, 区间下限分, 区间上限分, 全部候选值分列)。

    命中方式：上限表述 / 金额表述 / 模糊区间 / None。
    多个不同预算表述时由调用方取最严（最小）值并提示。
    """
    candidates: list[int] = []

    # 1) 模糊区间（「二三十元」）——优先于普通数字模式
    low, high, frag = _fuzzy_budget(text)
    if low is not None and high is not None:
        if low != high:
            return high, "模糊区间", low, high, [low, high]
        return high, "金额表述", None, None, [high]

    # 2) 明确上限/金额表述（带营养语境守卫）
    m = BUDGET_NEG_PAT.search(text)
    if m and not _is_nutrition_context(text, m.start()):
        v = yuan_to_cents(m.group(1))
        if v is not None:
            candidates.append(v)
    for pat in BUDGET_PATTERNS:
        for m in pat.finditer(text):
            if _is_nutrition_context(text, m.start()):
                continue
            v = yuan_to_cents(m.group(1))
            if v is not None and v not in candidates:
                candidates.append(v)
    if not candidates:
        return None, None, None, None, []
    return candidates[0], "上限表述" if BUDGET_NEG_PAT.search(text) else "金额表述", None, None, candidates


# ---------------------------------------------------------------------------
# 人数
# ---------------------------------------------------------------------------


def parse_people(text: str) -> tuple[int, Optional[str]]:
    m = PEOPLE_PAT.search(text)
    if m:
        raw = m.group(1) or m.group(2)
        if raw is None:
            return 1, None
        if raw.isdigit():
            n = int(raw)
        else:
            n = CN_DIGIT.get(raw, 1)
        return max(1, min(n, 20)), f"{n} 人"
    # 中文数字人数：「十个人」「十二个人」
    m = re.search(r"([一二两三四五六七八九])?十([一二三四五六七八九])?\s*(?:个)?人", text)
    if m:
        n = parse_cn_number(m)
        if n:
            return max(1, min(n, 20)), f"{n} 人"
    return 1, None


# ---------------------------------------------------------------------------
# 口味（含否定）
# ---------------------------------------------------------------------------


def _taste_tag_for_word(word: str) -> Optional[str]:
    for tag, kws in TASTE_KEYWORDS.items():
        if word in kws:
            return tag
    return None


def parse_tastes(text: str) -> tuple[tuple, tuple]:
    """返回 (偏好口味, 避免口味)。

    规则：
    - 口味词前 0-4 字内出现否定词（不吃/不想吃/不要/别/免/讨厌/…）或
      「不/别/免 + 口味词」直接相连 -> 避免口味；
    - 同一口味既想要又避免 -> 避免优先（调用方记录冲突）；
    - 「不吃辣」绝不会污染成「想吃辣」，其他偏好（脆/甜）不受影响。
    """
    likes: list[str] = []
    avoids: list[str] = []
    for tag, kws in TASTE_KEYWORDS.items():
        for kw in kws:
            start = 0
            while True:
                i = text.find(kw, start)
                if i < 0:
                    break
                start = i + len(kw)
                window = text[max(0, i - 4) : i]
                if any(h in window for h in NEG_HINT_WORDS) or NEG_TASTE_DIRECT.search(window + kw):
                    if tag not in avoids:
                        avoids.append(tag)
                elif tag not in likes:
                    likes.append(tag)
    return tuple(likes), tuple(avoids)


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


def parse_location(text: str) -> tuple[Optional[str], Optional[str]]:
    """返回 (位置, 来源)。来源：门店名称 / 位置表述 / None。"""
    # 门店名称优先（更具体）：「麦当劳深圳同方信息港餐厅」
    m = STORE_NAME_PAT.search(text)
    if m:
        loc = _clean_location(m.group(1))
        if loc:
            return loc, "门店名称"
    for pat in LOCATION_PATTERNS:
        m = pat.search(text)
        if m:
            loc = _clean_location(m.group(1))
            if loc:
                return loc, "位置表述"
    return None, None


def parse_store_code(text: str) -> Optional[str]:
    m = STORE_CODE_PAT.search(text)
    return m.group(1) if m else None


def parse(text: str) -> Constraints:
    """主入口：一句话 -> Constraints。"""
    notes: list[str] = []
    sources: dict[str, str] = {}
    text = text.strip()
    if not text:
        notes.append("输入为空，使用默认约束")

    # -- 预算（含营养语境守卫、模糊区间、多值取严） -------------------------
    budget, budget_how, fuzzy_low, fuzzy_high, all_budgets = parse_budget(text)
    fuzzy = False
    if budget_how == "模糊区间" and fuzzy_low is not None and fuzzy_high is not None:
        fuzzy = True
        notes.append(
            f"识别到模糊预算（约 ¥{fuzzy_low // 100}–{fuzzy_high // 100}），按上限 "
            f"{format_yuan(budget)} 设置预算上限"
        )
        sources["budget_cents"] = "explicit"
    elif budget is not None:
        sources["budget_cents"] = "explicit"
        distinct = sorted(set(all_budgets))
        if len(distinct) > 1:
            strict = min(distinct)
            budget = strict
            notes.append(
                "检测到多个预算表述（"
                + "、".join(format_yuan(v) for v in distinct)
                + f"），按更严格的上限 {format_yuan(strict)} 处理"
            )

    # -- 人数 ---------------------------------------------------------------
    people, people_how = parse_people(text)
    sources["people"] = "explicit" if people_how else "default"

    # -- 用餐方式 -----------------------------------------------------------
    dining = parse_dining_mode(text)
    sources["dining_mode"] = "explicit" if dining_mode_matched(text) else "default"

    # -- 地点 / 门店编码 ----------------------------------------------------
    location, loc_how = parse_location(text)
    store_code = parse_store_code(text)
    if location:
        sources["location"] = "explicit"
        if loc_how == "门店名称":
            notes.append(f"识别到门店名称，按位置「{location}」查询附近门店")
    if store_code:
        sources["store_code"] = "explicit"
        notes.append(f"使用指定门店编码 {store_code}")
        if location:
            notes.append("同时提供了位置与门店编码：以门店编码为准，位置仅作参考")

    # -- 口味（偏好 + 避免 + 冲突） -----------------------------------------
    tastes, avoid_tastes = parse_tastes(text)
    conflict = tuple(t for t in tastes if t in avoid_tastes)
    if conflict:
        tastes = tuple(t for t in tastes if t not in avoid_tastes)
        names = "/".join(TASTE_CN.get(t, t) for t in conflict)
        notes.append(
            f"检测到冲突表述（同时想要又不想要：{names}），已按「不吃」处理（避免性约束优先）"
        )
    if tastes:
        sources["tastes"] = "explicit"
    if avoid_tastes:
        sources["avoid_tastes"] = "explicit"
        notes.append(
            "识别到避免口味："
            + "/".join(_taste_cn(t) for t in avoid_tastes)
            + "（候选组合将不含这些口味，不会被其他偏好覆盖）"
        )
    if "清淡" in text and "veggie" in tastes and not any("清淡" in n for n in notes):
        notes.append("「清淡」按清爽/素食类餐品筛选（启发式映射，非官方分类）")

    # -- 心情 ---------------------------------------------------------------
    mood = parse_mood(text)
    if mood:
        sources["mood"] = "explicit"

    # -- 营养偏好（明确区分「用户给的数」与「系统推断的阈值」） ---------------
    max_cal = None
    if CAL_LIMIT_PAT.search(text):
        max_cal = float(CAL_LIMIT_PAT.search(text).group(1))
        notes.append(f"按用户指定热量上限 {max_cal:.0f} 千卡筛选")
        sources["max_calories"] = "explicit"
    elif LOW_CAL_PAT.search(text):
        max_cal = 600.0  # 经验阈值：单人单餐低卡线（非官方标准，仅作筛选偏好）
        notes.append(
            "识别到低卡偏好，按单人单餐 ≤600 千卡筛选（饮食偏好，不构成医疗或减重建议）"
        )
        sources["max_calories"] = "inferred"

    min_protein = None
    m = PROTEIN_MIN_PAT.search(text)
    if m:
        min_protein = float(m.group(1))
        notes.append(f"按用户指定蛋白质下限 {min_protein:.0f}g 筛选")
        sources["min_protein_g"] = "explicit"
    elif HIGH_PROTEIN_PAT.search(text):
        min_protein = 25.0  # 经验阈值：高蛋白偏好线
        notes.append("识别到高蛋白偏好，按蛋白质 ≥25g 筛选（饮食偏好，不构成医疗或增肌建议）")
        sources["min_protein_g"] = "inferred"

    # 少糖：识别但不可执行（官方营养数据无糖分字段）——明示，不假装支持
    if LESS_SUGAR_PAT.search(text):
        notes.append(
            "识别到少糖/低糖偏好：官方营养数据没有糖分字段，无法按此筛选或验证——"
            "未假装支持（订单页可选择无糖饮品，但引擎不做承诺）"
        )

    # -- 丰盛偏好 -----------------------------------------------------------
    rich = bool(RICH_PAT.search(text))
    if rich:
        sources["rich"] = "explicit"
        notes.append("识别到丰盛偏好，将在预算内优先更丰富的组合")
    if rich and max_cal is not None:
        notes.append("检测到软冲突（丰盛偏好 + 低卡偏好）：两个条件都保留，由规划引擎按优先级权衡")

    # -- 饮料偏好：软上限（「不想点太多」）优先于硬避免（「不要饮料」） ------
    avoid_categories: tuple = ()
    max_drink_qty: Optional[int] = None
    if LESS_DRINK_PAT.search(text):
        max_drink_qty = max(1, people - 1)
        sources["max_drink_qty"] = "inferred"
        notes.append(
            f"识别到「少点饮料」偏好，饮料数量上限 {max_drink_qty} 份（系统推断的软上限，尽量满足，无法满足时会明示）"
        )
    elif AVOID_DRINK_PAT.search(text):
        avoid_categories = ("drink",)
        sources["avoid_categories"] = "explicit"
        notes.append("识别到「不要饮料」，候选组合将不含饮品")

    # -- 未提供条件的明示 ---------------------------------------------------
    if budget is None:
        notes.append("未识别到预算，将不设预算上限（仍会标注真实总价）")
    if sources.get("dining_mode") == "default":
        notes.append(
            "未识别用餐方式，默认按「到店自取」计算（不含配送费）；如需外送请说明「外送/外卖」"
        )
    if location is None and store_code is None and dining == "delivery":
        notes.append("外送场景但未提供地址/门店，需要用户补充或改用就近门店")

    return Constraints(
        budget_cents=budget,
        people=people,
        dining_mode=dining,
        location=location,
        store_code=store_code,
        tastes=tastes,
        avoid_tastes=avoid_tastes,
        mood=mood,
        max_calories=max_cal,
        min_protein_g=min_protein,
        rich=rich,
        avoid_categories=avoid_categories,
        max_drink_qty=max_drink_qty,
        fuzzy_budget=fuzzy,
        sources=sources,
        raw_text=text,
        notes=tuple(notes),
    )


def _taste_cn(tag: str) -> str:
    return TASTE_CN.get(tag, tag)
