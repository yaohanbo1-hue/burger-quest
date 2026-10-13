"""RPG 文案层测试。"""
from burger_quest import nlparser, rpg
from burger_quest.mcp_base import scene_for_mode
from burger_quest.planner import build_plan


def _best_option(client, text, menu, coupons, nutrition):
    c = nlparser.parse(text)
    scene = scene_for_mode("takeaway", "DEMO-001")
    options, _ = build_plan(
        c, menu, coupons, nutrition, lambda items, cids: client.calculate_price(scene, items, cids)
    )
    assert options
    return c, options[0]


def test_quest_name_deterministic(client, menu, coupons, nutrition):
    c, opt = _best_option(client, "想吃辣，预算35", menu, coupons, nutrition)
    name1 = rpg.quest_name(c, opt)
    name2 = rpg.quest_name(c, opt)
    assert name1 == name2
    # 无用餐方式关键词 -> 默认到店自取
    assert "到店取餐密令" in name1
    assert "辣味" in name1
    c2 = nlparser.parse("外送，预算35")
    assert "麦乐送远征" in rpg.quest_name(c2, opt)


def test_story_mentions_budget_and_demo(client, menu, coupons, nutrition):
    c, opt = _best_option(client, "想吃辣，预算35，外送到南山科技园", menu, coupons, nutrition)
    story = rpg.story(c, "演示门店", demo=True)
    assert "¥35.00" in story
    assert "演示菜单" in story
    story_live = rpg.story(c, "演示门店", demo=False)
    assert "真实在售" in story_live


def test_gear_lines_contain_real_names_and_prices(client, menu, coupons, nutrition):
    c, opt = _best_option(client, "想吃辣，预算35", menu, coupons, nutrition)
    lines = rpg.gear_lines(opt)
    assert lines
    for pi in opt.items:
        assert any(pi.item.name in line and pi.item.price_text() in line for line in lines)


def test_price_lines_use_authoritative_total(client, menu, coupons, nutrition):
    c, opt = _best_option(client, "想吃辣，预算35", menu, coupons, nutrition)
    lines = rpg.price_lines(opt, c)
    assert any("calculate-price 权威结果" in line for line in lines)
    assert any("剩余预算" in line for line in lines)
