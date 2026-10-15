"""分享卡渲染测试：安全（防注入）、必备声明与 2026-10-14 视觉升级结构。

安全原则：所有用户输入与 MCP 返回文本一律视为不可信内容，必须转义；
结构原则：LIVE/DEMO 标签、装备溯源、价格四段、营养缺失显未知、无外部资源依赖。
"""
import html as html_mod
import re

import pytest

from burger_quest import cards, nlparser, rpg
from burger_quest.mcp_mock import MockMCPClient
from burger_quest.models import Coupon, MenuItem, PlanItem, PlanOption
from burger_quest.service import run_quest


def _result(text="想吃辣，预算35，外送到南山科技园"):
    client = MockMCPClient()
    return run_quest(text, client, generated_at="2026-10-13 18:51:16")


def _malicious_option():
    """构造携带恶意文本的方案（模拟被污染的 MCP 返回/用户输入）。"""
    return PlanOption(
        title="t",
        items=[
            PlanItem(
                MenuItem(
                    code='"><script>alert(1)</script>',
                    name="<b>麦辣鸡腿堡</b>",
                    category="<i>汉堡</i>",
                    price_cents=2100,
                    tags=("<img src=x onerror=alert(2)>",),
                ),
                2,
            ),
            PlanItem(MenuItem(code="D2", name='堡"堡\'堡&堡', price_cents=1250), 1),
        ],
        coupons=[
            Coupon(id='c"1', name="<script>alert(3)</script>券", value_cents=500, threshold_cents=2000),
        ],
        total_cents=2850,
        original_cents=3350,
        discount_cents=500,
        delivery_fee_cents=900,
        within_budget=True,
        remaining_cents=650,
        reasons=["口味命中：<b>麦辣</b>"],
        unmet=["超预算 <script>bad</script>"],
        price_checked=True,
    )


# ---------------------------------------------------------------------------
# 基础必备元素
# ---------------------------------------------------------------------------


def test_html_card_contains_required_elements():
    r = _result()
    assert r.card_html
    assert "Burger Quest · 麦门异世界" in r.card_html
    assert "NON-OFFICIAL" in r.card_html
    assert "非麦当劳官方项目" in r.card_html
    assert r.quest_name in r.card_html
    assert "演示数据" in r.card_html


def test_html_card_shows_demo_mode_badge():
    r = _result()
    assert "DEMO · 离线演示数据" in r.card_html
    assert "LIVE · 麦当劳 MCP 实时数据" not in r.card_html


def test_html_card_live_mode_badge():
    """LIVE 模式标签（真实 MCP 数据）。"""
    r = _result()
    card = cards.render_html(
        r.constraints, r.chosen, r.store, r.time_info, "麦当劳 MCP 实时数据", "2026-10-14", demo=False
    )
    assert "LIVE · 麦当劳 MCP 实时数据" in card
    assert "DEMO · 离线演示数据" not in card


# ---------------------------------------------------------------------------
# 安全：转义与防注入
# ---------------------------------------------------------------------------


def test_html_card_escapes_user_text():
    """用户输入中的 HTML 必须被转义（位置信息会进入任务背景）。"""
    r = _result("外送到<b>测试</b>科技园，预算35")
    assert "<b>测试</b>" not in r.card_html
    assert "&lt;b&gt;测试&lt;/b&gt;" in r.card_html
    r2 = _result("想吃<script>alert(1)</script>辣，预算35")
    assert "<script>" not in r2.card_html


def test_html_card_escapes_malicious_mcp_fields():
    """MCP 返回字段（编码/名称/分类/标签/券名/理由）中的恶意文本全部转义。"""
    opt = _malicious_option()
    c = nlparser.parse("预算50，想吃辣")
    card = cards.render_html(c, opt, None, "", "来源", "2026-10-14", demo=False)
    #  script/img/onerror 等标签与事件处理器不得原样出现
    for payload in (
        "<script>alert(1)</script>",
        "<script>alert(3)</script>",
        "<b>麦辣鸡腿堡</b>",
        "<i>汉堡</i>",
        "<img src=x onerror=alert(2)>",
        "<script>bad</script>",
    ):
        assert payload not in card, f"未转义: {payload}"
    # 转义后的实体存在
    assert "&lt;script&gt;" in card
    assert "&lt;b&gt;麦辣鸡腿堡&lt;/b&gt;" in card
    # 属性上下文安全：编码里的引号被转义，不会 break out 属性
    assert '&quot;&gt;&lt;script&gt;' in card


def test_html_card_escapes_quotes_and_special_chars():
    """引号、&、尖括号等特殊字符一律转义。"""
    opt = PlanOption(
        title="t",
        items=[PlanItem(MenuItem(code="D2", name='堡"堡\'堡&堡<堡>', price_cents=1250), 1)],
        total_cents=1250,
        original_cents=1250,
        price_checked=True,
    )
    c = nlparser.parse("预算50")
    card = cards.render_html(c, opt, None, "", "来源", "2026-10-14", demo=True)
    assert "&quot;" in card
    assert "&#x27;" in card
    assert "&amp;" in card
    assert "&lt;堡&gt;" in card
    # 原文不得出现
    assert '堡"堡' not in card


def test_markdown_card_escapes_malicious_fields():
    """Markdown 卡同样转义（防止在 Markdown 渲染器里被解释为 HTML）。"""
    opt = _malicious_option()
    c = nlparser.parse("预算50，想吃辣")
    md = cards.render_markdown(c, opt, None, "", "来源", "2026-10-14", demo=False)
    for payload in ("<script>alert(1)</script>", "<b>麦辣鸡腿堡</b>", "<script>bad</script>"):
        assert payload not in md
    assert "&lt;script&gt;" in md


def test_no_token_in_cards():
    r = _result()
    for field in (r.card_html, r.card_markdown, r.story, r.quest_name):
        assert "Bearer" not in field
        assert "MCD_MCP_TOKEN" not in field


# ---------------------------------------------------------------------------
# 结构：价格四段 / 营养诚实 / 溯源 / 装饰不碰数据
# ---------------------------------------------------------------------------


def test_price_sections_distinguished():
    """原价 / 已核实优惠 / 应付总价（权威）/ 剩余预算 四段清晰区分。"""
    r = _result()
    card = r.card_html
    assert "原价合计" in card
    assert "应付总价" in card
    assert "剩余预算" in card
    # 权威标注
    assert "calculate-price 权威结果" in r.card_markdown


def test_unverified_total_shows_unknown_not_decoration():
    """未通过权威核验的总价显示未知，绝不用菜单价加总冒充。"""
    opt = PlanOption(
        title="t",
        items=[PlanItem(MenuItem(code="D1", name="幽灵汉堡", price_cents=9999), 1)],
        total_cents=None,
        original_cents=9999,
        price_checked=False,
    )
    c = nlparser.parse("预算50")
    card = cards.render_html(c, opt, None, "", "来源", "2026-10-14", demo=True)
    assert "未知（未通过 calculate-price 权威核验）" in card
    assert "¥99.99" not in card.split("应付总价")[1][:80]  # 总价行不是菜单价


def test_missing_nutrition_shows_unknown():
    """营养数据缺失：明确显示未知，不用装饰性数字填空。"""
    opt = PlanOption(
        title="t",
        items=[PlanItem(MenuItem(code="D9", name="神秘新品", price_cents=1000), 1)],
        total_cents=1000,
        original_cents=1000,
        nutrition={},  # 零覆盖
        nutrition_complete=False,
        nutrition_missing=["神秘新品"],
        price_checked=True,
    )
    c = nlparser.parse("预算50")
    card = cards.render_html(c, opt, None, "", "来源", "2026-10-14", demo=True)
    assert "营养数据未知" in card
    assert "不猜测数值" in card
    assert "神秘新品" in card


def test_partial_nutrition_noted():
    """部分餐品无营养数据：标注可能偏低，不假装完整。"""
    opt = PlanOption(
        title="t",
        items=[PlanItem(MenuItem(code="D1", name="巨无霸", price_cents=2500), 1)],
        total_cents=2500,
        original_cents=2500,
        nutrition={"calories_kcal": 520.0, "protein_g": 27.0, "fat_g": 28.0},
        nutrition_complete=False,
        nutrition_missing=["幽灵小食"],
        price_checked=True,
    )
    c = nlparser.parse("预算50")
    card = cards.render_html(c, opt, None, "", "来源", "2026-10-14", demo=True)
    assert "520" in card  # 真实数据展示
    assert "部分餐品无营养数据" in card and "未猜测" in card


def test_gear_rows_have_traceable_codes():
    """每件装备带商品编码与来源（可追溯）。"""
    r = _result()
    card = r.card_html
    assert "商品编码" in card
    assert "D1002" in card or "D2001" in card  # 演示数据编码
    assert "来源" in card


def test_level_and_tier_are_decoration_only():
    """等级/称号是装饰：不改写任何业务字段。"""
    r = _result()
    before = (r.chosen.total_cents, r.chosen.original_cents, len(r.chosen.items), r.chosen.remaining_cents)
    card = cards.render_html(
        r.constraints, r.chosen, r.store, r.time_info, r.data_sources["mode"], "2026-10-14", demo=True
    )
    after = (r.chosen.total_cents, r.chosen.original_cents, len(r.chosen.items), r.chosen.remaining_cents)
    assert before == after  # 渲染不碰方案对象
    assert "LV." in card and "称号" in card


def test_budget_bar_uses_real_numbers():
    """进度条宽度由真实总价/预算计算。"""
    r = _result()
    card = r.card_html
    assert "预算使用率" in card
    used = r.chosen.total_cents / r.constraints.budget_cents * 100
    assert f"width:{used:.1f}%" in card


def test_no_external_resources():
    """纯本地实现：无外部 CDN / 字体 / 图片 / @import / url() 引用。"""
    r = _result()
    card = r.card_html
    assert "<link" not in card
    assert "@import" not in card
    assert "url(" not in card
    assert "http://" not in card and "https://" not in card.replace("https://mcp.mcd.cn", "")
    # 无外链脚本/图片
    assert "<script" not in card
    assert "<img" not in card


def test_mobile_and_print_adaptation():
    """响应式与打印样式存在。"""
    r = _result()
    assert "viewport" in r.card_html
    assert "@media (max-width:480px)" in r.card_html
    assert "@media print" in r.card_html
    assert "break-inside:avoid" in r.card_html


# ---------------------------------------------------------------------------
# Markdown 结构
# ---------------------------------------------------------------------------


def test_markdown_card():
    r = _result()
    assert r.card_markdown
    assert r.card_markdown.startswith("# 🍔 Burger Quest")
    assert "非麦当劳官方项目" in r.card_markdown
    assert "应付总价" in r.card_markdown


def test_markdown_card_structure():
    """Markdown 卡：徽章行、装备表格、价格表格、模式标签齐全。"""
    r = _result()
    md = r.card_markdown
    assert "DEMO · 离线演示数据" in md
    assert "LV." in md
    assert "| 装备 | 说明 | 数量 | 单价 | 溯源编码 |" in md
    assert "| 项目 | 金额 |" in md
    assert "商品编码" not in md or "`D" in md  # 编码在表格里
    assert "📜 任务背景" in md and "⚔️ 推荐装备" in md and "💰 价格结算" in md and "🧪 属性面板" in md


def test_markdown_unknown_total():
    """Markdown 同样对未核验总价显示未知。"""
    opt = PlanOption(
        title="t",
        items=[PlanItem(MenuItem(code="D1", name="幽灵汉堡", price_cents=9999), 1)],
        total_cents=None,
        price_checked=False,
    )
    c = nlparser.parse("预算50")
    md = cards.render_markdown(c, opt, None, "", "来源", "2026-10-14", demo=True)
    assert "未知（未通过 calculate-price 权威核验）" in md


def test_html_card_has_no_trailing_whitespace():
    """渲染产物无行尾空白（模板占位符为空时不得留下纯空格行；CI 空白检查把关）。"""
    r = _result()
    bad = [(i + 1, line) for i, line in enumerate(r.card_html.split("\n")) if line != line.rstrip()]
    assert not bad, f"行尾空白: {bad[:3]}"


def test_markdown_card_has_no_trailing_whitespace():
    r = _result()
    bad = [line for line in r.card_markdown.split("\n") if line != line.rstrip()]
    assert not bad, f"行尾空白: {bad[:3]}"


# ---------------------------------------------------------------------------
# LIVE 历史快照语义（2026-10-15 发布审计新增）
# ---------------------------------------------------------------------------


def _snapshot_cards():
    """直接渲染快照卡（demo=False：快照标记只属于真实 MCP 历史结果）。"""
    r = _result()
    html = cards.render_html(
        r.constraints, r.chosen, r.store, r.time_info, r.data_sources["mode"],
        "2026-10-14 20:27:01", demo=False, snapshot=True,
    )
    md = cards.render_markdown(
        r.constraints, r.chosen, r.store, r.time_info, r.data_sources["mode"],
        "2026-10-14 20:27:01", demo=False, snapshot=True,
    )
    return html, md


def test_snapshot_badge_and_disclaimer():
    """快照卡：LIVE SNAPSHOT 徽章 + 历史快照说明（含生成时间与时效声明）。"""
    html, _md = _snapshot_cards()
    assert "LIVE SNAPSHOT · 真实 MCP 历史实测" in html
    assert "历史快照" in html
    assert "2026-10-14 20:27:01" in html
    assert "仅代表该次查询时的返回结果" in html
    assert "不代表麦当劳当前价格、优惠或库存" in html
    # 不再使用实时 LIVE 徽章
    assert "LIVE · 麦当劳 MCP 实时数据" not in html


def test_snapshot_markdown_note():
    _html, md = _snapshot_cards()
    assert "LIVE SNAPSHOT · 真实 MCP 历史实测" in md
    assert "历史快照说明" in md
    assert "2026-10-14 20:27:01" in md
    assert "仅代表该次查询时的返回结果" in md


def test_live_card_without_snapshot_has_no_snapshot_note():
    """普通实时卡不得带快照说明（避免语义混淆）。"""
    html, _md = _snapshot_cards()
    assert "历史快照" in html  # 上面已验快照卡有
    r = _result()
    plain = cards.render_html(
        r.constraints, r.chosen, r.store, r.time_info, r.data_sources["mode"],
        "2026-10-15 21:00:00", demo=False, snapshot=False,
    )
    assert "LIVE SNAPSHOT" not in plain
    assert "历史快照" not in plain


def test_snapshot_card_keeps_real_data_and_provenance():
    """快照卡保留真实数据与溯源（不因快照语义丢失商品编码/来源）。"""
    html, md = _snapshot_cards()
    assert "商品编码" in html and "来源" in html
    assert "calculate-price 权威结果" in md
    assert rpg_price_total(_result()) in md  # 真实总价仍在卡上


def rpg_price_total(r):
    return rpg.price_lines(r.chosen, r.constraints)[-1].split("：", 1)[1].split("（")[0]


def test_snapshot_card_still_escapes_malicious_input():
    """快照语义不影响转义（纵深防御）。"""
    r = _result("外送到<b>测试</b>科技园，预算35")
    html = cards.render_html(
        r.constraints, r.chosen, r.store, r.time_info, r.data_sources["mode"],
        "2026-10-14 20:27:01", demo=False, snapshot=True,
    )
    assert "<b>测试</b>" not in html
    assert "&lt;b&gt;测试&lt;/b&gt;" in html
    assert "LIVE SNAPSHOT" in html


def test_demo_data_never_marked_as_live_snapshot():
    """红线：演示数据即使传了 snapshot 也不得标 LIVE SNAPSHOT（mock 即 demo）。"""
    client = MockMCPClient()
    r = run_quest("我想吃辣，预算35，外送到南山科技园，一个人", client, generated_at="2026-10-14 20:27:01", card_snapshot=True)
    assert "LIVE SNAPSHOT" not in r.card_html
    assert "DEMO · 离线演示数据" in r.card_html  # 演示数据只能标 DEMO
