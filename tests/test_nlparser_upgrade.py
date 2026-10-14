"""中文需求解析升级回归测试（2026-10-14）。

覆盖实测发现的问题与用户要求的表达全集：
- 预算：35元以内 / 不超过35块 / 大概二三十元（模糊区间）/ 三十五（确切）/
  十来块 / 多预算取最严 / 营养数字不干扰预算；
- 人数：我一个人 / 两人份 / 四个人一起吃 / 十个人；
- 口味：想吃辣 / 不吃辣（否定）/ 不吃辣+想吃脆的（互不覆盖）/ 冲突表述 / 偏清淡；
- 营养：低卡 / 高蛋白 / 少糖（识别但明示不可执行）/ 明确数值 / 非医疗声明；
- 用餐方式、地点（街道/商场/门店名/括号门店名）、心情与风格；
- 多条件互不干扰；sources 溯源（explicit/inferred/default）；
- 调试表示默认不回显原始输入。
"""
from burger_quest import nlparser
from burger_quest.mcp_base import scene_for_mode
from burger_quest.mcp_mock import MockMCPClient
from burger_quest.planner import build_plan

# ---------------------------------------------------------------------------
# 预算
# ---------------------------------------------------------------------------


def test_budget_explicit_forms():
    assert nlparser.parse("35 元以内想吃辣").budget_cents == 3500
    assert nlparser.parse("不超过 35 块").budget_cents == 3500
    assert nlparser.parse("预算35").budget_cents == 3500
    assert nlparser.parse("身上只有 30 块").budget_cents == 3000
    assert nlparser.parse("¥25.5").budget_cents == 2550
    assert nlparser.parse("随便吃点什么").budget_cents is None


def test_budget_fuzzy_range_takes_upper_bound_with_note():
    """模糊预算「二三十元」= 20~30：按上限 30 设置，并注明区间与处理方式。"""
    c = nlparser.parse("大概二三十元")
    assert c.budget_cents == 3000
    assert c.fuzzy_budget is True
    assert any("模糊预算" in n and "上限" in n for n in c.notes)

    assert nlparser.parse("三四十块搞定").budget_cents == 4000
    assert nlparser.parse("二三十块").budget_cents == 3000
    # 「十来块」≈ 10~15
    c2 = nlparser.parse("十来块")
    assert c2.budget_cents == 1500 and c2.fuzzy_budget is True


def test_budget_chinese_exact_numbers():
    """确切中文数字不是区间。"""
    assert nlparser.parse("三十五块").budget_cents == 3500
    assert nlparser.parse("三十五块").fuzzy_budget is False
    assert nlparser.parse("三十块").budget_cents == 3000
    assert nlparser.parse("十二块").budget_cents == 1200
    assert nlparser.parse("十块").budget_cents == 1000


def test_budget_multiple_expressions_take_strictest():
    """多个预算表述：取更严格的上限并明示。"""
    c = nlparser.parse("预算35，不超过50")
    assert c.budget_cents == 3500
    assert any("多个预算表述" in n and "更严格" in n for n in c.notes)


def test_nutrition_numbers_do_not_pollute_budget_or_location():
    """营养数字不干扰预算与地点（2026-10-14 修复的 P0）。"""
    c = nlparser.parse("热量控制在400千卡以内")
    assert c.max_calories == 400.0
    assert c.budget_cents is None  # 400 是千卡，不是 400 元
    assert c.location is None      # 「控制在400千卡以内」不是地点

    c2 = nlparser.parse("热量不超过800大卡，想吃辣")
    assert c2.max_calories == 800.0
    assert c2.budget_cents is None
    assert c2.location is None

    c3 = nlparser.parse("蛋白质至少30g")
    assert c3.min_protein_g == 30.0
    assert c3.budget_cents is None


# ---------------------------------------------------------------------------
# 人数
# ---------------------------------------------------------------------------


def test_people_forms():
    assert nlparser.parse("我一个人").people == 1
    assert nlparser.parse("两人份").people == 2
    assert nlparser.parse("四个人一起吃").people == 4
    assert nlparser.parse("两个人吃").people == 2
    assert nlparser.parse("3人份").people == 3
    assert nlparser.parse("十个人一起").people == 10


# ---------------------------------------------------------------------------
# 口味（含否定与冲突）
# ---------------------------------------------------------------------------


def test_positive_tastes():
    assert "spicy" in nlparser.parse("想吃辣").tastes
    assert "crispy" in nlparser.parse("想吃脆的").tastes
    c = nlparser.parse("想吃辣的和甜的")
    assert "spicy" in c.tastes and "sweet" in c.tastes


def test_negative_taste_not_parsed_as_positive():
    """「不吃辣」是避免口味，绝不变良成「想吃辣」（2026-10-14 修复的 P0）。"""
    c = nlparser.parse("不吃辣")
    assert c.tastes == ()
    assert c.avoid_tastes == ("spicy",)
    assert any("避免口味" in n for n in c.notes)


def test_negative_taste_not_overridden_by_other_preferences():
    """「不吃辣」+「想吃脆的」：避免约束优先，其他偏好正常生效。"""
    c = nlparser.parse("不吃辣，想吃脆的")
    assert c.avoid_tastes == ("spicy",)
    assert "crispy" in c.tastes
    assert "spicy" not in c.tastes


def test_taste_conflict_resolved_to_avoid_with_note():
    """同一口味既想要又避免：避免优先并明示冲突。"""
    c = nlparser.parse("想吃辣但不吃辣")
    assert c.avoid_tastes == ("spicy",)
    assert c.tastes == ()
    assert any("冲突" in n and "避免性约束优先" in n for n in c.notes)


def test_light_taste_maps_to_veggie_with_note():
    """「偏清淡」映射到清爽/素食类，并注明是启发式映射。"""
    c = nlparser.parse("偏清淡")
    assert "veggie" in c.tastes
    assert any("清淡" in n and "启发式" in n for n in c.notes)


# ---------------------------------------------------------------------------
# 营养偏好
# ---------------------------------------------------------------------------


def test_nutrition_preferences():
    c = nlparser.parse("减肥中想吃低卡的")
    assert c.max_calories == 600.0
    c = nlparser.parse("热量控制在400千卡以内")
    assert c.max_calories == 400.0
    c = nlparser.parse("增肌要高蛋白")
    assert c.min_protein_g == 25.0


def test_nutrition_not_medical_advice():
    """低卡/高蛋白不得表述为医疗或减重承诺。"""
    notes = " ".join(nlparser.parse("低卡").notes)
    assert "不构成医疗" in notes and "减重" in notes
    notes2 = " ".join(nlparser.parse("高蛋白").notes)
    assert "不构成医疗" in notes2


def test_less_sugar_recognized_but_honestly_not_actionable():
    """「少糖」：识别到，但明示无糖数据、无法按此筛选——不假装支持。"""
    c = nlparser.parse("少糖")
    assert any("少糖" in n and "无法按此筛选" in n and "未假装支持" in n for n in c.notes)
    # 不产生任何伪约束
    assert c.max_calories is None and c.min_protein_g is None


def test_low_cal_plus_rich_conflict_noted():
    """丰盛 + 低卡软冲突：两个条件都保留，明示由引擎权衡。"""
    c = nlparser.parse("低卡又丰盛")
    assert c.max_calories == 600.0 and c.rich is True
    assert any("软冲突" in n for n in c.notes)


# ---------------------------------------------------------------------------
# 用餐方式 / 地点 / 心情风格
# ---------------------------------------------------------------------------


def test_dining_modes():
    assert nlparser.parse("外送到南山科技园").dining_mode == "delivery"
    assert nlparser.parse("我到店自取").dining_mode == "takeaway"
    assert nlparser.parse("在店里吃").dining_mode == "dine_in"
    assert nlparser.parse("来点吃的").dining_mode == "takeaway"


def test_locations():
    assert nlparser.parse("外送到南山科技园").location == "南山科技园"
    assert nlparser.parse("在购物公园").location == "购物公园"
    assert nlparser.parse("科技园附近").location == "科技园"


def test_store_name_as_location():
    """门店名称作地点（2026-10-14 新增）。"""
    c = nlparser.parse("想去麦当劳深圳同方信息港餐厅")
    assert c.location == "深圳同方信息港"
    assert any("门店名称" in n for n in c.notes)
    c2 = nlparser.parse("麦当劳（深圳南山科技园店）")
    assert c2.location == "深圳南山科技园"


def test_location_plus_store_code_priority():
    """位置与门店编码同时给出：编码优先，位置仅作参考。"""
    c = nlparser.parse("外送到南山科技园，门店编码 1420762")
    assert c.store_code == "1420762"
    assert c.location == "南山科技园"
    assert any("以门店编码为准" in n for n in c.notes)


def test_mood_and_style():
    assert nlparser.parse("加班回血").mood == "tired"
    assert nlparser.parse("周五庆祝").mood == "celebrate"
    assert nlparser.parse("加班到凌晨好累").mood == "night"
    assert nlparser.parse("丰盛一点").rich is True
    assert nlparser.parse("随便吃点").rich is False


# ---------------------------------------------------------------------------
# 多条件互不干扰
# ---------------------------------------------------------------------------


def test_multi_condition_no_interference():
    """预算/人数/否定口味同句，互不干扰。"""
    c = nlparser.parse("两个人不吃辣预算35")
    assert c.people == 2
    assert c.budget_cents == 3500
    assert c.avoid_tastes == ("spicy",)
    assert c.tastes == ()

    c2 = nlparser.parse("我想吃辣，预算35，外送到南山科技园，一个人")
    assert c2.tastes == ("spicy",)
    assert c2.budget_cents == 3500
    assert c2.people == 1
    assert c2.dining_mode == "delivery"
    assert c2.location == "南山科技园"


# ---------------------------------------------------------------------------
# 溯源与调试表示
# ---------------------------------------------------------------------------


def test_sources_distinguish_explicit_inferred_default():
    """明确给出的限制 vs 系统推断的软偏好 vs 系统默认。"""
    c = nlparser.parse("想吃辣，预算35，两个人")
    assert c.sources["budget_cents"] == "explicit"
    assert c.sources["people"] == "explicit"
    assert c.sources["tastes"] == "explicit"
    assert c.sources["dining_mode"] == "default"  # 未提用餐方式 -> 系统默认

    c2 = nlparser.parse("低卡")
    assert c2.sources["max_calories"] == "inferred"  # 从软偏好推断的阈值
    c3 = nlparser.parse("热量控制在400千卡以内")
    assert c3.sources["max_calories"] == "explicit"  # 用户明确给的数

    c4 = nlparser.parse("少点饮料")
    assert c4.sources["max_drink_qty"] == "inferred"
    c5 = nlparser.parse("不要饮料")
    assert c5.sources["avoid_categories"] == "explicit"


def test_debug_dict_hides_raw_text_by_default():
    """调试表示默认不回显原始输入（避免不必要泄露）。"""
    c = nlparser.parse("预算35，想吃辣，外送到南山科技园")
    d = c.to_debug_dict()
    assert "raw_text" not in d
    assert d["raw_text_length"] == len("预算35，想吃辣，外送到南山科技园")
    assert d["budget_cents"] == 3500
    assert d["tastes"] == ["spicy"]
    assert d["sources"]["budget_cents"] == "explicit"
    # 显式要求时才带原文
    assert c.to_debug_dict(include_raw=True)["raw_text"].startswith("预算35")


# ---------------------------------------------------------------------------
# 规划引擎：避免口味是硬约束
# ---------------------------------------------------------------------------


def _demo_env():
    client = MockMCPClient()
    scene = scene_for_mode("takeaway", "DEMO-001")
    menu = client.enriched_menu(scene)
    coupons = client.store_coupons(scene) + client.my_coupons()
    nutrition = client.list_nutrition_foods()
    return client, scene, menu, coupons, nutrition


def test_planner_avoids_spicy_when_user_says_no():
    """「不吃辣」：任何方案都不含辣味餐品，且理由中明示。"""
    client, scene, menu, coupons, nutrition = _demo_env()
    c = nlparser.parse("不吃辣，想吃脆的，预算50")
    options, _ = build_plan(c, menu, coupons, nutrition, lambda i, ids: client.calculate_price(scene, i, ids))
    assert options
    for o in options:
        assert all("辣" not in pi.item.name for pi in o.items)
        assert any("避免口味" in r for r in o.reasons)


def test_planner_explain_when_avoid_leaves_nothing():
    """避免口味排掉所有餐品：可解释失败，不静默给空。"""
    client, scene, menu, coupons, nutrition = _demo_env()
    # 构造只剩辣味餐品的菜单
    from burger_quest.models import MenuItem

    spicy_only = [m for m in menu if "辣" in m.name]
    c = nlparser.parse("不吃辣，预算50")
    options, warnings = build_plan(c, spicy_only, coupons, nutrition, lambda i, ids: client.calculate_price(scene, i, ids))
    assert options == []
    assert any("不吃" in w and "没有可用餐品" in w for w in warnings)
