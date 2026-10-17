"""世界观/剧情/文案快照测试（2026-10-17 新增）。

锁定四条硬边界（对应文案任务 8 条要求中的可测部分）：
1. **文案快照**：任务名矩阵、故事关键元素、状态文案模板——结构稳定可回归；
2. **虚构/真实分离**：剧情章节文本与成就絮语**绝不包含**真实餐品名/价格/
   门店编码/金额单位——真实数据只从 MCP 来；
3. **品牌风险**：用户可表面文本无「官方推荐/出品/授权」等冒称；
   独立作品声明锚点在位；
4. **错误文案不掩盖故障**：失败/数据不足模板必须包含真实原因占位符，
   且不得出现「一切正常」式安慰措辞掩盖失败。
"""
import re

import pytest

from burger_quest import lore
from burger_quest.achievements import ACHIEVEMENTS, ACHIEVEMENTS_BY_ID
from burger_quest.cards import DISCLAIMER, NON_OFFICIAL_TAG, render_html, render_markdown
from burger_quest.models import Constraints, MenuItem, PlanItem, PlanOption, Store
from burger_quest import nlparser, rpg


# ---------------------------------------------------------------------------
# 1. 文案快照（结构回归）
# ---------------------------------------------------------------------------


def test_quest_name_matrix_snapshot():
    """任务名矩阵：心情场景 × 预算身份 × 用餐方式——结构确定可复现。"""
    c = nlparser.parse("我想吃辣，预算35")
    opt = PlanOption(title="t", items=[PlanItem(MenuItem(code="A", name="麦辣鸡腿堡", price_cents=2400), 1)])
    name = rpg.quest_name(c, opt)
    assert name.startswith("无名冒险者的第一道门槛")
    assert "精打细算的游侠" in name and "辣味偏好" in name and "到店取餐密令" in name
    # 心情/预算/方式三轴各自的映射片段
    assert "午夜黄金拱门试炼" in rpg.quest_name(nlparser.parse("深夜想吃辣"), opt)
    assert "麦乐送远征" in rpg.quest_name(nlparser.parse("外送，预算35"), opt)
    assert "节俭骑士" in rpg.quest_name(nlparser.parse("预算12，想吃辣"), opt)
    # 确定性
    assert rpg.quest_name(c, opt) == rpg.quest_name(c, opt)


def test_story_snapshot_elements():
    c = nlparser.parse("我想吃辣，预算35，外送到南山科技园")
    story = rpg.story(c, "演示门店", demo=True)
    for frag in ("¥35.00", "演示菜单", "『演示门店』", "南山科技园", "祝你好运，冒险者"):
        assert frag in story, f"故事缺少稳定元素：{frag}"
    # 无门店名时用通用据点表述
    no_store = rpg.story(c, "", demo=True)
    assert "黄金拱门据点" in no_store
    # 确定性
    assert rpg.story(c, "演示门店", demo=True) == story


def test_story_appends_unlocked_chapters():
    c = nlparser.parse("我想吃辣，预算35")
    base = rpg.story(c, "门店", demo=False)
    chapter = lore.CHAPTERS[0]
    with_chapter = rpg.story(c, "门店", demo=False, chapters=[chapter])
    assert "【异世界见闻】" in with_chapter and chapter.name in with_chapter
    assert base != with_chapter and base in with_chapter  # 只追加不修改事实部分


def test_state_copy_templates_complete():
    """状态文案模板齐全且占位符明确（不掩盖故障）。"""
    for key in ("loading", "no_data", "network_down", "auth_fail", "rate_limited"):
        text = lore.state_copy(key, what="X", reason="Y")
        assert "{" not in text, f"{key} 模板占位符未填充"
        assert text != key, f"{key} 模板缺失"
    # 未知 key 透明失败（不静默返回空）
    assert lore.state_copy("nonexistent_key") == "nonexistent_key"


# ---------------------------------------------------------------------------
# 2. 虚构/真实分离（核心合规边界）
# ---------------------------------------------------------------------------

_PRICE_PAT = re.compile(r"¥\s?\d|\d+\s?元|\d{3,}\s?分")
_MEAL_PAT = re.compile(r"麦辣|巨无霸|板烧|薯条|麦乐鸡|可乐|甜筒|鸡翅|麦香鱼|吉士")
_STORE_PAT = re.compile(r"\b\d{6,}\b")  # 门店编码形态


def test_chapter_texts_contain_no_real_data():
    """剧情文本不得包含真实价格/餐品/门店编码——剧情只讲故事。"""
    for chapter in lore.CHAPTERS:
        for text in (chapter.unlock_text, chapter.summary, chapter.name):
            assert not _PRICE_PAT.search(text), f"{chapter.id} 含价格形态：{text[:40]}"
            assert not _MEAL_PAT.search(text), f"{chapter.id} 含真实餐品名：{text[:40]}"
            assert not _STORE_PAT.search(text), f"{chapter.id} 含门店编码"


def test_achievement_lore_contains_no_real_data():
    for ach in ACHIEVEMENTS:
        assert not _PRICE_PAT.search(ach.lore_text), f"{ach.id} 絮语含价格"
        assert not _MEAL_PAT.search(ach.lore_text), f"{ach.id} 絮语含真实餐品名"


def test_npc_and_premise_are_original_fiction():
    """NPC/世界观为原创虚构，不引用官方角色。"""
    all_text = lore.WORLD_PREMISE + json_names(lore.NPCS)
    for official in ("麦当劳叔叔", "汉堡神偷", "奶昔大哥", "大鸟", "McDonald", "Ronald"):
        assert official not in all_text, f"世界观引用官方角色：{official}"


def json_names(npcs: dict) -> str:
    return "".join(f"{v['name']}{v['role']}{v['voice']}" for v in npcs.values())


# ---------------------------------------------------------------------------
# 3. 品牌风险扫描
# ---------------------------------------------------------------------------


def test_forbidden_claims_absent_from_user_facing_text():
    """用户可表面（成就/剧情/卡片/状态文案）不得出现官方背书式表述。"""
    surfaces = (
        [a.name + a.desc + a.lore_text for a in ACHIEVEMENTS]
        + [c.name + c.unlock_text + c.summary for c in lore.CHAPTERS]
        + list(lore.STATE_COPY.values())
        + [lore.WORLD_PREMISE, lore.HOST_PERSONA, lore.HOST_NAME]
    )
    for text in surfaces:
        for claim in lore.FORBIDDEN_CLAIMS:
            assert claim not in text, f"用户可表面出现禁止表述「{claim}」：{text[:40]}"


def test_required_declarations_present():
    """独立作品声明锚点：卡片常量 + 成就虚拟声明。"""
    from burger_quest.achievements import VIRTUAL_NOTICE

    assert NON_OFFICIAL_TAG == "NON-OFFICIAL FAN PROJECT"
    assert "非麦当劳官方项目" in DISCLAIMER
    assert "虚拟进度" in VIRTUAL_NOTICE
    card = render_html(
        Constraints(budget_cents=3500),
        PlanOption(title="t", items=[PlanItem(MenuItem(code="A", name="麦辣鸡腿堡", price_cents=2400), 1)], total_cents=2400, price_checked=True),
        Store(code="1420762", name="x"), "", "麦当劳 MCP 实时数据", "2026-10-17 18:00",
    )
    assert NON_OFFICIAL_TAG in card and "非麦当劳官方项目" in card


def test_cards_markdown_also_declares_independent_project():
    md = render_markdown(
        Constraints(budget_cents=3500),
        PlanOption(title="t", items=[PlanItem(MenuItem(code="A", name="麦辣鸡腿堡", price_cents=2400), 1)], total_cents=2400, price_checked=True),
        Store(code="1420762", name="x"), "", "麦当劳 MCP 实时数据", "2026-10-17 18:00",
    )
    assert "非麦当劳官方项目" in md


# ---------------------------------------------------------------------------
# 4. 章节-成就绑定一致性（防止文案漂移出未定义条件）
# ---------------------------------------------------------------------------


def test_chapter_requirements_reference_real_achievements():
    ids = set(ACHIEVEMENTS_BY_ID)
    for chapter in lore.CHAPTERS:
        assert chapter.requires, f"{chapter.id} 未绑定解锁条件"
        for ach_id in chapter.requires:
            assert ach_id in ids, f"{chapter.id} 绑定了不存在的成就：{ach_id}"


def test_chapters_ordered_and_unique():
    assert [c.index for c in lore.CHAPTERS] == list(range(1, len(lore.CHAPTERS) + 1))
    assert len({c.id for c in lore.CHAPTERS}) == len(lore.CHAPTERS)


def test_chapter_unlock_logic_deterministic():
    chapter = lore.CHAPTERS[0]
    assert chapter.is_unlocked(set()) is False
    assert chapter.is_unlocked({"first_plan"}) is True
    before, after = set(), {"first_plan"}
    assert lore.newly_unlocked_chapters(before, after) == [chapter]
    assert lore.newly_unlocked_chapters(after, after) == []  # 不重复播报
