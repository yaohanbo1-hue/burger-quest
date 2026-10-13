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
