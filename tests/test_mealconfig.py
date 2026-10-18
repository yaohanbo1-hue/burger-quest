"""套餐装备解析测试（query-meal-detail 结构 -> 组成/可替换项/特制选项）。"""
from burger_quest.mealconfig import apply_replacements, default_items, parse_detail

DETAIL = {
    "code": "9900011121",
    "name": "测试套餐",
    "rounds": [
        {
            "id": 1,
            "name": "麦香鱼",
            "quantity": 1,
            "choices": [
                {
                    "code": "1600",
                    "name": "麦香鱼",
                    "isDefault": 1,
                    "diffPrice": "+ ¥0",
                    "supportModify": True,
                    "modification": {
                        "items": [
                            {
                                "minValues": 0,
                                "maxValues": 2,
                                "values": [
                                    {"code": "100126", "name": "麦香鱼酱", "price": 0, "selectedQuantity": 1},
                                    {"code": "100141", "name": "吉士", "price": 0, "selectedQuantity": 0},
                                ],
                            }
                        ]
                    },
                }
            ],
        },
        {
            "id": 4,
            "name": "选择饮料",
            "quantity": 1,
            "choices": [
                {"code": "3505", "name": "锡兰红茶", "isDefault": 1, "diffPrice": "+ ¥0"},
                {"code": "3071", "name": "无糖可口可乐中杯", "isDefault": 0, "diffPrice": "- ¥1.5", "supportModify": True},
                {"code": "3111", "name": "无糖可口可乐大杯", "isDefault": 0, "diffPrice": "+ ¥0.5"},
            ],
        },
    ],
}


def test_parse_detail_groups_and_choices():
    groups = parse_detail(DETAIL)
    assert [g.index for g in groups] == [1, 4]
    assert groups[0].name == "麦香鱼" and groups[0].quantity == 1
    drink = groups[1]
    assert len(drink.choices) == 3
    assert drink.choices[0].is_default is True
    assert drink.choices[1].diff_cents == -150
    assert drink.choices[2].diff_cents == 50
    # 特制选项只保留官方返回的字段
    mods = groups[0].choices[0].modifications
    assert mods and mods[0]["values"][0]["name"] == "麦香鱼酱"


def test_default_items_uses_default_choice():
    groups = parse_detail(DETAIL)
    assert default_items(groups) == [("1600", "麦香鱼", 1), ("3505", "锡兰红茶", 1)]


def test_apply_replacements_valid_and_invalid():
    groups = parse_detail(DETAIL)
    items, notes = apply_replacements(groups, {4: "3071"})
    assert items[-1] == ("3071", "无糖可口可乐中杯", 1)
    assert any("无糖可口可乐中杯" in n for n in notes)

    # 不存在的选项编码：保留默认并如实说明（不虚构餐品）
    items2, notes2 = apply_replacements(groups, {4: "999999"})
    assert items2[-1] == ("3505", "锡兰红茶", 1)
    assert any("不存在选项" in n for n in notes2)


def test_parse_empty_detail_is_safe():
    assert parse_detail({}) == []
    assert parse_detail({"rounds": []}) == []
    assert parse_detail({"rounds": [{"id": 1, "name": "x", "choices": []}]}) == []
