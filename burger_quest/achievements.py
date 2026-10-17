"""成就与经验系统（2026-10-17 新增）。

设计原则：
1. **统一定义**：每个成就有唯一 id、触发条件描述、奖励 XP、展示文本、分类，
   以及「是否需要真实 MCP 数据」标记（requires_real）；
2. **真实才解锁**：演示模式（MockMCPClient）或真实查询失败时，requires_real
   的成就一律不解锁——虚拟进度不冒充实测成果；
3. **确定性**：评估只依赖 QuestResult 事实字段与传入的历史集合，无随机、
   无时间依赖——相同输入相同输出，可测试、可复现；
4. **防刷分**：
   - 每次任务计算 run_signature（约束+门店+餐品+权威总价的哈希）；
     历史签名集合内重放 -> 不计经验；
   - 成就一次性：已解锁的成就重复满足不重复发奖励；
   - 累计类成就按「不同签名/不同门店」计数，重复相同输入刷不出进度；
5. **边界清晰**：经验与等级只是**应用内虚拟进度**，与麦当劳官方无关，
   不代表任何优惠、权益或现金奖励（CLI/文档均有明示）。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional

from .models import QuestResult, format_yuan
from .planner import classify, taste_hit
from . import lore

# ---------------------------------------------------------------------------
# 分类常量
# ---------------------------------------------------------------------------
CAT_EXPLORATION = "exploration"  # 探索（门店/菜单/营养/外送链路）
CAT_BUDGET = "budget"            # 预算掌控
CAT_TASTE = "taste"              # 口味与搭配
CAT_QUEST = "quest"              # 任务完成（含诚实失败）
CAT_FEATURE = "feature"          # 功能使用（卡片/导出/演示模式）

RUN_XP_SUCCESS = 5  # 首次成功产出权威方案的基础经验（重放不计）

# 等级曲线（累计 XP 阈值 -> 称号）。level_for_xp() 按阈值推导，确定性。
LEVEL_TIERS = [
    (0, "见习冒险者"),
    (50, "饱腹探索者"),
    (120, "资深冒险者"),
    (250, "传奇领队"),
    (450, "麦门行者"),
    (700, "拱门大师"),
    (1000, "异世界传说"),
]


def level_for_xp(xp: int) -> tuple[int, str]:
    """累计经验 -> (等级, 称号)。确定性，纯函数。"""
    level, title = 1, LEVEL_TIERS[0][1]
    for idx, (threshold, name) in enumerate(LEVEL_TIERS, start=1):
        if xp >= threshold:
            level, title = idx, name
        else:
            break
    return level, title


@dataclass(frozen=True)
class Achievement:
    """成就定义（单一事实来源；新增成就必须在此登记）。"""

    id: str            # 唯一标识（存进进度文件；改名等于新成就，禁止复用旧 id）
    name: str          # 展示名
    desc: str          # 触发条件（用户可读，必须事实准确）
    reward_xp: int     # 首次解锁奖励
    requires_real: bool  # True = 需要真实 MCP 数据（demo/查询失败不可解锁）
    category: str      # CAT_* 之一

    def brief(self) -> str:
        tag = "需真实数据" if self.requires_real else "任意模式"
        return f"{self.name}（+{self.reward_xp} XP · {tag}）——{self.desc}"

    @property
    def lore_text(self) -> str:
        """解锁絮语（纯虚构短句，单一事实来源在 lore.py；无登记则空）。"""
        return lore.lore_for(self.id)


ACHIEVEMENTS: tuple[Achievement, ...] = (
    # -- 探索 -----------------------------------------------------------------
    Achievement("first_store", "初次据点", "通过真实查询确定一家门店", 10, True, CAT_EXPLORATION),
    Achievement("menu_reader", "菜单学者", "真实菜单返回不少于 100 个餐品编码", 15, True, CAT_EXPLORATION),
    Achievement("nutrition_reader", "营养档案", "真实营养数据不少于 100 条", 15, True, CAT_EXPLORATION),
    Achievement("delivery_scout", "麦乐送侦察", "真实外送链路查询到可配送门店", 20, True, CAT_EXPLORATION),
    Achievement("store_collector", "据点收藏家", "累计在 3 个不同门店完成规划", 25, True, CAT_EXPLORATION),
    # -- 预算 -----------------------------------------------------------------
    Achievement("budget_keeper", "预算守门人", "在设定预算内产出权威核验方案", 15, True, CAT_BUDGET),
    Achievement("exact_hit", "分毫不差", "应付总价恰好等于预算（权威核验）", 25, True, CAT_BUDGET),
    Achievement("value_hunter", "价值猎手", "符合预算且预算使用率不低于 90%", 20, True, CAT_BUDGET),
    Achievement("coupon_scholar", "券林探子", "真实查询到至少 1 张可用优惠券", 10, True, CAT_BUDGET),
    Achievement("coupon_master", "契约达成", "方案优惠经官方核验真实生效", 30, True, CAT_BUDGET),
    # -- 口味 -----------------------------------------------------------------
    Achievement("spicy_first", "辣味初体验", "方案含官方标签为「辣」的餐品", 10, True, CAT_TASTE),
    Achievement("no_spicy_will", "免辣誓言", "要求不吃辣且方案中没有任何辣味餐品", 15, True, CAT_TASTE),
    Achievement("combo_fan", "套餐爱好者", "方案含官方标签为「套餐」的餐品", 10, True, CAT_TASTE),
    Achievement("variety_seeker", "百味追求", "单个方案覆盖不少于 3 个品类", 15, True, CAT_TASTE),
    # -- 任务完成 --------------------------------------------------------------
    Achievement("first_plan", "初战告捷", "首次产出经权威核验的点餐方案", 30, True, CAT_QUEST),
    Achievement("honest_failure", "诚实的失败", "真实查询后预算内无解，如实说明而未编造", 20, True, CAT_QUEST),
    Achievement("multi_option", "三选一", "一次产出 3 个可比较方案", 15, True, CAT_QUEST),
    # -- 功能使用 --------------------------------------------------------------
    Achievement("card_master", "卡片收藏家", "累计生成 3 张不同任务的分享卡", 15, False, CAT_FEATURE),
    Achievement("json_scholar", "结构化学者", "使用 --json 导出过结构化结果", 5, False, CAT_FEATURE),
    Achievement("demo_explorer", "演示先行者", "在演示模式完成一次规划", 5, False, CAT_FEATURE),
)

ACHIEVEMENTS_BY_ID = {a.id: a for a in ACHIEVEMENTS}
CATEGORY_CN = {
    CAT_EXPLORATION: "探索",
    CAT_BUDGET: "预算",
    CAT_TASTE: "口味",
    CAT_QUEST: "任务",
    CAT_FEATURE: "功能",
}
VIRTUAL_NOTICE = "游戏进度为应用内虚拟进度，与麦当劳官方无关，不代表任何优惠、权益或现金奖励"


def run_signature(result: QuestResult) -> str:
    """本次任务的防重放签名（确定性）：约束原文 + 门店 + 餐品 + 权威总价。

    只含会话内容哈希，不含位置原文之外的隐私；用于「相同输入相同数据只计一次」。
    """
    c = result.constraints
    chosen = result.chosen
    parts = [
        c.raw_text or "",
        result.store.code if result.store else "",
        ",".join(f"{pi.item.code}x{pi.qty}" for pi in chosen.items) if chosen else "",
        str(chosen.total_cents) if chosen and chosen.total_cents is not None else "",
        str(c.people),
    ]
    # usedforsecurity=False：仅作去重键（相同输入相同数据只计一次），非安全用途
    return hashlib.sha1("|".join(parts).encode("utf-8"), usedforsecurity=False).hexdigest()[:16]


def _counts(result: QuestResult) -> dict:
    """从 data_sources 提取真实计数（service 负责填充 counts 字段）。"""
    counts = result.counts or {}
    return {
        "menu": int(counts.get("menu") or 0),
        "nutrition": int(counts.get("nutrition") or 0),
        "coupons": int(counts.get("coupons") or 0),
    }


def _condition_met(ach_id: str, result: QuestResult, demo: bool, stores_seen: set, card_signatures: set, used_json: bool) -> bool:
    """成就触发条件求值（纯函数，只读 result 事实字段）。"""
    chosen = result.chosen
    c = result.constraints
    counts = _counts(result)
    store_source = str(result.data_sources.get("store") or "")
    warnings = " ".join(result.warnings)

    if ach_id == "first_store":
        return result.store is not None
    if ach_id == "menu_reader":
        return counts["menu"] >= 100
    if ach_id == "nutrition_reader":
        return counts["nutrition"] >= 100
    if ach_id == "delivery_scout":
        return "delivery-query-stores" in store_source
    if ach_id == "store_collector":
        return len(stores_seen) >= 3
    if ach_id == "budget_keeper":
        return c.budget_cents is not None and chosen is not None and chosen.within_budget is True
    if ach_id == "exact_hit":
        return (
            c.budget_cents is not None
            and chosen is not None
            and chosen.total_cents is not None
            and chosen.total_cents == c.budget_cents
        )
    if ach_id == "value_hunter":
        if c.budget_cents is None or chosen is None or chosen.within_budget is not True or not c.budget_cents:
            return False
        return (chosen.total_cents or 0) / c.budget_cents >= 0.9
    if ach_id == "coupon_scholar":
        return counts["coupons"] >= 1
    if ach_id == "coupon_master":
        return chosen is not None and bool(chosen.discount_cents)
    if ach_id == "spicy_first":
        return chosen is not None and any(taste_hit(pi.item, ("spicy",)) for pi in chosen.items)
    if ach_id == "no_spicy_will":
        return (
            chosen is not None
            and "spicy" in (c.avoid_tastes or ())
            and not any(taste_hit(pi.item, ("spicy",)) for pi in chosen.items)
        )
    if ach_id == "combo_fan":
        return chosen is not None and any("套餐" in (pi.item.tags or ()) for pi in chosen.items)
    if ach_id == "variety_seeker":
        return chosen is not None and len({classify(pi.item) for pi in chosen.items}) >= 3
    if ach_id == "first_plan":
        return chosen is not None
    if ach_id == "honest_failure":
        return chosen is None and (
            "没有组合能落在" in warnings or "价格暂不可验证" in warnings or "未能获取任何餐品名称" in warnings
        )
    if ach_id == "multi_option":
        return len(result.options) >= 3
    if ach_id == "card_master":
        return len(card_signatures) >= 3
    if ach_id == "json_scholar":
        return used_json
    if ach_id == "demo_explorer":
        return demo and chosen is not None
    return False


@dataclass(frozen=True)
class AchievementUnlock:
    """一次评估的解锁记录（already=True 表示历史已解锁，本次不重复发奖励）。"""

    achievement: Achievement
    already: bool

    @property
    def xp_awarded(self) -> int:
        return 0 if self.already else self.achievement.reward_xp

    def line(self) -> str:
        if self.already:
            return f"🏅 {self.achievement.name}（已解锁，不重复计经验）"
        return f"🆕 解锁成就：{self.achievement.name}（+{self.achievement.reward_xp} XP）——{self.achievement.desc}"

    def lore_line(self) -> str:
        """解锁絮语（虚构叙事，仅新解锁时播报；与事实行分离）。"""
        if self.already or not self.achievement.lore_text:
            return ""
        return f"📖 {self.achievement.lore_text}"


@dataclass(frozen=True)
class RunProgress:
    """一次任务的经验结算（确定性；持久化由 progress.py 负责）。"""

    signature: str
    repeated: bool          # 签名在历史集合内（重放）
    run_xp: int             # 本次基础经验（重放/失败为 0）
    unlock_xp: int          # 本次新解锁成就奖励合计
    unlocks: tuple          # tuple[AchievementUnlock]
    blocked: tuple          # tuple[Achievement] 因 demo/数据不足未评估的真实成就（展示用）
    chapters: tuple = ()    # tuple[Chapter] 本次新解锁的剧情章节

    @property
    def total_xp(self) -> int:
        return self.run_xp + self.unlock_xp

    def summary_lines(self) -> list[str]:
        lines = [VIRTUAL_NOTICE]
        if self.repeated:
            lines.append("⏱️ 重复挑战：与历史记录相同，本次不计经验（防刷分）")
        for u in self.unlocks:
            lines.append(u.line())
            lore_line = u.lore_line()
            if lore_line:
                lines.append(lore_line)
        for chapter in self.chapters:
            lines.append(f"📜 剧情推进 · {chapter.name}：{chapter.unlock_text}")
        if self.run_xp:
            lines.append(f"📈 任务经验 +{self.run_xp}")
        real_blocked = [a for a in self.blocked]
        if real_blocked:
            lines.append(
                "🔒 需真实数据的成就未解锁（演示模式或查询失败不冒充实测）："
                + "、".join(a.name for a in real_blocked[:6])
                + ("…" if len(real_blocked) > 6 else "")
            )
        if self.total_xp:
            lines.append(f"💰 本次共获经验 +{self.total_xp}")
        else:
            lines.append("💰 本次共获经验 +0")
        return lines


def evaluate_run(
    result: QuestResult,
    demo: bool,
    unlocked: set,
    seen_signatures: set,
    stores_seen: set,
    card_signatures: set,
    used_json: bool = False,
    card_generated: bool = False,
) -> RunProgress:
    """评估一次任务：返回解锁记录与经验结算。

    参数全部来自调用方（CLI 负责从进度文件读取后传入），本函数不做任何 IO。
    unlocked/seen_signatures/stores_seen/card_signatures 为历史集合的**副本**
    由调用方维护；函数内会就地扩展用于本次条件判断（如 store_collector）。
    card_generated：本次是否实际生成了分享卡（card_master 按「不同任务的卡」
    计数，重复相同任务刷不出进度）。
    """
    signature = run_signature(result)
    repeated = signature in seen_signatures

    # 历史集合就地更新（供累计类成就条件判断；调用方决定是否持久化）
    if result.store is not None:
        stores_seen.add(result.store.code)
    if card_generated and result.chosen is not None:
        card_signatures.add(signature)

    unlocks: list[AchievementUnlock] = []
    blocked: list[Achievement] = []
    for ach in ACHIEVEMENTS:
        if ach.requires_real and demo:
            blocked.append(ach)  # 演示模式：真实成就不评估、不解锁
            continue
        if ach.requires_real and result.chosen is None and ach.id not in ("first_store", "honest_failure", "delivery_scout", "menu_reader", "nutrition_reader", "coupon_scholar"):
            # 无权威方案时，依赖方案数据的成就不可能真实满足
            continue
        if _condition_met(ach.id, result, demo, stores_seen, card_signatures, used_json):
            unlocks.append(AchievementUnlock(ach, already=ach.id in unlocked))
    # demo_explorer 等任意模式成就照常评估

    run_xp = 0
    if not repeated and result.chosen is not None and not demo:
        run_xp = RUN_XP_SUCCESS  # 真实任务的首次成功基础经验（重放不计）
    unlock_xp = sum(u.xp_awarded for u in unlocks)
    # 剧情章节：本次新解锁的成就可能推进剧情线（纯虚构叙事，与真实数据分离）
    after = set(unlocked) | {u.achievement.id for u in unlocks if not u.already}
    chapters = tuple(lore.newly_unlocked_chapters(set(unlocked), after))
    return RunProgress(
        signature=signature,
        repeated=repeated,
        run_xp=run_xp,
        unlock_xp=unlock_xp,
        unlocks=tuple(unlocks),
        blocked=tuple(blocked),
        chapters=chapters,
    )


def format_achievement_list(unlocked: set) -> list[str]:
    """成就图鉴文本（进度查看用）：按分类分组，标注解锁状态。"""
    lines = [f"🏆 成就图鉴（{len(unlocked)}/{len(ACHIEVEMENTS)}）", VIRTUAL_NOTICE]
    for cat, cat_cn in CATEGORY_CN.items():
        items = [a for a in ACHIEVEMENTS if a.category == cat]
        if not items:
            continue
        lines.append(f"【{cat_cn}】")
        for a in items:
            mark = "✅" if a.id in unlocked else "⬜"
            lines.append(f"  {mark} {a.brief()}")
    return lines
