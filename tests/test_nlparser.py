"""自然语言解析测试。"""
from burger_quest import nlparser


def test_budget_variants():
    assert nlparser.parse("预算35").budget_cents == 3500
    assert nlparser.parse("身上只有 30 块").budget_cents == 3000
    assert nlparser.parse("¥25.5").budget_cents == 2550
    assert nlparser.parse("不超过40元").budget_cents == 4000
    assert nlparser.parse("随便吃点什么").budget_cents is None


def test_people():
    assert nlparser.parse("两个人吃").people == 2
    assert nlparser.parse("3人份").people == 3
    assert nlparser.parse("两个人").people == 2
    assert nlparser.parse("一个人").people == 1


def test_tastes():
    c = nlparser.parse("想吃辣的和甜的")
    assert "spicy" in c.tastes and "sweet" in c.tastes
    c = nlparser.parse("要牛肉堡")
    assert "beef" in c.tastes and "burger" in c.tastes


def test_mood():
    assert nlparser.parse("加班到凌晨好累").mood == "night"
    assert nlparser.parse("今天发工资了庆祝一下").mood == "celebrate"
    assert nlparser.parse("好累啊").mood == "tired"


def test_dining_mode():
    assert nlparser.parse("外送到南山科技园").dining_mode == "delivery"
    assert nlparser.parse("我到店自取").dining_mode == "takeaway"
    assert nlparser.parse("在店里吃").dining_mode == "dine_in"
    assert nlparser.parse("来点吃的").dining_mode == "takeaway"  # 默认最保守


def test_location_and_store_code():
    assert nlparser.parse("外送到南山科技园").location == "南山科技园"
    assert nlparser.parse("门店编码 DEMO-001").store_code == "DEMO-001"


def test_nutrition_preferences():
    c = nlparser.parse("减肥中想吃低卡的")
    assert c.max_calories == 600.0
    c = nlparser.parse("热量控制在400千卡以内")
    assert c.max_calories == 400.0
    c = nlparser.parse("增肌要高蛋白")
    assert c.min_protein_g == 25.0


def test_rich_preference():
    assert nlparser.parse("两个人想吃点好的").rich is True
    assert nlparser.parse("随便吃点").rich is False


def test_notes_for_missing_budget():
    c = nlparser.parse("来点吃的")
    assert any("未识别到预算" in n for n in c.notes)


# --- 小食/配餐偏好（2026-10-18 用户反馈：说了「要一份小食」但方案没加） -----


def test_prefer_side_parsed_from_explicit_request():
    for text in ("预算100，要一份小食", "来点薯条", "加个鸡翅", "配一份小食套餐"):
        assert nlparser.parse(text).prefer_side is True, text


def test_prefer_side_not_set_when_avoiding_side():
    assert nlparser.parse("不吃鸡翅").prefer_side is False
    assert nlparser.parse("不要小食").prefer_side is False


def test_prefer_side_not_set_by_default():
    assert nlparser.parse("预算35，想吃辣").prefer_side is False


# --- A 档：口语金额 / 不要小食 / 场景词不当地址（2026-10-18 用户实测短板） ---


def test_chinese_hundred_budget():
    assert nlparser.parse("两个人，一百块以内").budget_cents == 10000
    assert nlparser.parse("一百五左右").budget_cents == 15000  # 口语「一百五」= 150
    assert nlparser.parse("一百二十块").budget_cents == 12000
    assert nlparser.parse("两百块").budget_cents == 20000


def test_avoid_side_categories():
    c = nlparser.parse("预算35，不要饮料，不要小食")
    assert "drink" in c.avoid_categories and "side" in c.avoid_categories
    assert c.prefer_side is False


def test_scene_words_are_not_locations():
    # 「在公司加班」「加班到九点」等场景/时间词不是地址，应清洗为空（由服务层提示补充地址）
    assert nlparser.parse("在公司加班，晚上十点，想吃点热的").location is None
    assert nlparser.parse("加班到九点，想吃点热的").location is None


# --- C 档：主动澄清提示（只提示不猜） ---


def test_quantity_hint_is_surfaced():
    c = nlparser.parse("三个汉堡，两杯可乐")
    assert any("数量表述" in n for n in c.notes)


def test_three_people_is_not_quantity_hint():
    c = nlparser.parse("三个人，预算100")
    assert not any("数量表述" in n for n in c.notes)
