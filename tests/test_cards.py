"""分享卡渲染测试：安全与必备声明。"""
import html as html_mod

from burger_quest import cards, nlparser, rpg
from burger_quest.mcp_mock import MockMCPClient
from burger_quest.planner import build_plan
from burger_quest.service import run_quest


def _result(text="想吃辣，预算35，外送到南山科技园"):
    client = MockMCPClient()
    return run_quest(text, client, generated_at="2026-10-13 18:51:16")


def test_html_card_contains_required_elements():
    r = _result()
    assert r.card_html
    assert "Burger Quest · 麦门异世界" in r.card_html
    assert "NON-OFFICIAL" in r.card_html
    assert "非麦当劳官方项目" in r.card_html
    assert r.quest_name in r.card_html
    assert "演示数据" in r.card_html


def test_html_card_escapes_user_text():
    """用户输入中的 HTML 必须被转义（位置信息会进入任务背景）。"""
    r = _result("外送到<b>测试</b>科技园，预算35")
    assert "<b>测试</b>" not in r.card_html
    assert "&lt;b&gt;测试&lt;/b&gt;" in r.card_html
    r2 = _result("想吃<script>alert(1)</script>辣，预算35")
    assert "<script>" not in r2.card_html


def test_markdown_card():
    r = _result()
    assert r.card_markdown
    assert r.card_markdown.startswith("# 🍔 Burger Quest")
    assert "非麦当劳官方项目" in r.card_markdown
    assert "应付总价" in r.card_markdown


def test_no_token_in_cards():
    r = _result()
    for field in (r.card_html, r.card_markdown, r.story, r.quest_name):
        assert "Bearer" not in field
        assert "MCD_MCP_TOKEN" not in field
