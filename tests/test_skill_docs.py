"""Skill 文档一致性测试（2026-10-14；2026-10-19 支线任务扩展）。

守卫 SKILL.md 与源代码不漂移：
- 文档中提到的 MCP 工具名必须真实存在（TOOLS_USED + TOOLS_SKILL_READONLY +
  TOOLS_RESERVED），防止虚构工具名/参数；
- 必备章节与红线条款齐全（角色定位/调用链/LIVE-DEMO/不自动下单/不索要 Token）；
- 文档声称的解析规则与 nlparser 实际行为一致（抽查关键用例）；
- 文档中的三个数值示例与引擎实际输出一致（示例可执行、不编数字）。
"""
import re
from pathlib import Path

import pytest

from burger_quest import nlparser
from burger_quest.mcp_base import TOOLS_RESERVED, TOOLS_SKILL_READONLY, TOOLS_USED
from burger_quest.mcp_mock import MockMCPClient
from burger_quest.service import run_quest

SKILL_PATH = Path(__file__).resolve().parent.parent / "SKILL.md"
SKILL_TEXT = SKILL_PATH.read_text(encoding="utf-8")

# 文档中允许出现的非 MCP kebab-case 词（项目名/连接器名/命令等）
KNOWN_NON_TOOLS = {"mcd-mcp", "burger-quest", "open-mcd-cn", "mcd-developer-innovation-challenge"}


def _kebab_tokens(text: str) -> set:
    """提取反引号里的 kebab-case 词（形如 query-meals）。"""
    return set(re.findall(r"`([a-z][a-z0-9]*(?:-[a-z0-9]+)+)`", text))


def test_all_mcp_tool_names_in_skill_are_real():
    """SKILL.md 提到的每个 MCP 工具名都必须在代码的真实工具清单里。"""
    known = set(TOOLS_USED) | set(TOOLS_SKILL_READONLY) | set(TOOLS_RESERVED) | KNOWN_NON_TOOLS
    mentioned = _kebab_tokens(SKILL_TEXT)
    unknown = sorted(t for t in mentioned if t not in known)
    assert not unknown, f"SKILL.md 提到不存在的工具/标识：{unknown}（不得虚构 MCP 工具名）"


def test_skill_mentions_all_tools_the_engine_uses():
    """引擎实际调用的 11 个工具 + 外送 1 个都应在文档中出现。"""
    for tool in list(TOOLS_USED) + ["delivery-query-addresses"]:
        assert tool in SKILL_TEXT, f"SKILL.md 缺少工具说明：{tool}"


def test_skill_mentions_all_sidequest_tools():
    """支线任务调用链覆盖的 15 个只读工具都应在文档中出现（2026-10-19 实测）。"""
    for tool in TOOLS_SKILL_READONLY:
        assert tool in SKILL_TEXT, f"SKILL.md 支线任务缺少工具说明：{tool}"


def test_skill_sidequest_sections_present():
    """支线任务四区块 + 1024 模式章节齐全。"""
    for section in (
        "## 10. 支线任务",
        "### 10.1 麦享会宝库",
        "### 10.2 派对据点侦查",
        "### 10.3 团餐公会",
        "### 10.4 冒险日志",
        "## 11. 1024 程序员节模式",
    ):
        assert section in SKILL_TEXT, f"SKILL.md 缺少章节：{section}"


def test_skill_required_sections():
    """必备章节齐全（可执行性：AI 按章节顺序即可走完流程）。"""
    for section in (
        "# Burger Quest",
        "## 0. 角色定位",
        "## 1. 前置检查",
        "## 2. 解析冒险者需求",
        "## 3. MCP 调用链",
        "## 4. 套餐规划规则",
        "## 5. RPG 输出格式",
        "## 6. 分享成就卡",
        "## 7. 错误处理",
        "## 8. 边界（红线）",
        "## 9. 对话示例",
    ):
        assert section in SKILL_TEXT, f"SKILL.md 缺少章节：{section}"


def test_skill_red_lines_present():
    """红线条款：不自动下单、不索要 Token、不编造、LIVE/DEMO 分明。"""
    assert "create-order" in SKILL_TEXT and "cancel-order" in SKILL_TEXT
    assert "绝不" in SKILL_TEXT and "Token" in SKILL_TEXT  # 不索要/不泄露 Token
    assert "LIVE" in SKILL_TEXT and "DEMO" in SKILL_TEXT
    assert "不得把演示数据说成刚刚查询到的真实数据" in SKILL_TEXT or "不把离线演示数据冒充实时查询结果" in SKILL_TEXT
    assert "不编造" in SKILL_TEXT


def test_skill_covers_delivery_chain():
    """外送全链路（2026-10-19 补全）：地址 → 可配送门店 → beCode 链路，且诚实标注未实测。"""
    assert "delivery-query-stores" in SKILL_TEXT and "delivery-query-addresses" in SKILL_TEXT
    assert "beCode" in SKILL_TEXT
    # 诚实边界：成功路径未实测必须显式声明
    assert "成功返回未经真实账户实测" in SKILL_TEXT


def test_skill_covers_drive_through():
    """得来速场景（beType=5）在识别规则与调用链中都有覆盖。"""
    assert "得来速" in SKILL_TEXT
    assert "beType` 一律传 **5**" in SKILL_TEXT


def test_skill_error_code_table_present():
    """官方业务码速查表覆盖全部实测码（空态码 vs 失败码必须分清）。"""
    for code in ("499", "600010", "600058", "610303", "610501"):
        assert code in SKILL_TEXT, f"SKILL.md 缺少业务码 {code} 的处置说明"


def test_skill_card_skeleton_present():
    """成就卡骨架内联（含转义要求与隐私红线），不依赖仓库代码。"""
    assert "NON-OFFICIAL FAN PROJECT" in SKILL_TEXT
    assert "#da291c" in SKILL_TEXT and "#ffc72c" in SKILL_TEXT
    assert "HTML 转义" in SKILL_TEXT
    assert "绝不出现" in SKILL_TEXT  # 卡片隐私红线


def test_skill_has_five_examples():
    """至少五个对话示例（预算受限/两人/营养/地点模糊/MCP 不可用）。"""
    for i in range(1, 6):
        assert f"### 示例 {i}" in SKILL_TEXT, f"缺少示例 {i}"
    for kw in ("预算受限", "两人用餐", "营养偏好", "地点模糊", "MCP 暂时不可用"):
        assert kw in SKILL_TEXT, f"示例缺少场景：{kw}"


def test_skill_call_chain_matches_code():
    """调用链描述与 service.py 实际顺序一致（算价探测前置、详情回退）。"""
    # 文档必须先说算价发现、后说详情回退
    i_price = SKILL_TEXT.find("价格发现（一次调用同时拿名称 + 单价）")
    i_detail = SKILL_TEXT.find("query-meal-detail")
    assert 0 < i_price < i_detail, "文档应先描述算价发现、再描述详情回退（与代码一致）"
    # 券试探顺序：单券 -> 双券 -> 回退无券
    assert "单券" in SKILL_TEXT and "双券" in SKILL_TEXT
    assert "不默认可叠加" in SKILL_TEXT


# ---------------------------------------------------------------------------
# 文档声称的解析规则 vs nlparser 实际行为
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,check",
    [
        ("不吃辣", lambda c: c.avoid_tastes == ("spicy",) and c.tastes == ()),
        ("不吃辣，想吃脆的", lambda c: c.avoid_tastes == ("spicy",) and "crispy" in c.tastes),
        ("大概二三十元", lambda c: c.budget_cents == 3000 and c.fuzzy_budget is True),
        ("少糖", lambda c: any("少糖" in n for n in c.notes)),
        ("两个人", lambda c: c.people == 2),
        ("低卡", lambda c: c.max_calories == 600.0),
        ("高蛋白", lambda c: c.min_protein_g == 25.0),
        ("外送到南山科技园", lambda c: c.dining_mode == "delivery" and c.location == "南山科技园"),
        ("麦当劳深圳同方信息港餐厅", lambda c: c.location == "深圳同方信息港"),
        ("来点吃的", lambda c: c.dining_mode == "takeaway"),
    ],
)
def test_skill_parsing_rules_match_engine(text, check):
    """SKILL.md 解析表里的每条规则都与引擎实际行为一致。"""
    assert check(nlparser.parse(text)), f"文档规则与引擎不一致：{text}"


# ---------------------------------------------------------------------------
# 文档数值示例 vs 引擎实际输出（示例必须可执行、不编数字）
# ---------------------------------------------------------------------------


def test_skill_example_1_matches_engine():
    """示例 1（¥12 预算受限）：最接近真实方案 ¥13、超预算 ¥1。"""
    r = run_quest("我就 12 块钱，想吃点麦当劳，帮我在南山科技园附近配一套", MockMCPClient())
    assert r.chosen is not None
    assert r.chosen.total_cents == 1300
    assert r.chosen.within_budget is False
    assert r.chosen.remaining_cents == -100
    assert "双层吉士堡" in r.chosen.item_names()


def test_skill_example_2_matches_engine():
    """示例 2（¥100 两人丰盛）：原价 ¥107、优惠 ¥8、应付 ¥99、剩 ¥1。"""
    r = run_quest("我们两个人想吃点好的，预算 100，堂食", MockMCPClient())
    o = r.chosen
    assert o is not None
    assert o.original_cents == 10700
    assert o.discount_cents == 800
    assert o.total_cents == 9900
    assert o.remaining_cents == 100
    assert o.within_budget is True


def test_skill_example_3_matches_engine():
    """示例 3（低卡 ¥40）：应付 ¥25、热量 550 千卡、蛋白质 17g。"""
    r = run_quest("减肥中，想吃低卡的，预算 40，一个人，到店自取", MockMCPClient())
    o = r.chosen
    assert o is not None
    assert o.total_cents == 2500
    assert o.remaining_cents == 1500
    assert o.nutrition.get("calories_kcal") == 550.0
    assert o.nutrition.get("protein_g") == 17.0


def test_skill_frontmatter_valid():
    """frontmatter 含 name/description（WorkBuddy Skill 可被发现）。"""
    assert SKILL_TEXT.startswith("---")
    head = SKILL_TEXT.split("---", 2)[1]
    assert "name: burger-quest" in head
    assert "description:" in head and len(head.split("description:")[1].strip()) > 50
