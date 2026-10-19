"""CLI 子系统命令测试（detail / account / campaign / coupons，2026-10-19）。

背景：这四个子命令各约 130 行真实逻辑（参数校验、套餐重配、三类券分区、
JSON 落盘、错误路径），但此前零直接测试——cli.py 覆盖率因此只有 52%
（TECH_DEBT.md D2 在账债务）。本文件用 MockMCPClient 演示数据离线覆盖
主路径与错误路径，不访问网络、不需要 Token。

演示数据事实（burger_quest/data/demo_data.json）：
- D2001 = 麦辣鸡腿堡套餐（3 组 rounds，组 1 默认项 D1002）；
- D1001 = 巨无霸（单品，无 rounds）；D9999 不存在（meal_detail 抛 MCPError）；
- DEMO-001 有 2 张门店券；卡包有 1 张演示券；available-coupons 为「明确无券」。
"""
import json

from burger_quest import cli

COMBO = "D2001"  # 麦辣鸡腿堡套餐
SINGLE = "D1001"  # 巨无霸（单品）
STORE = "DEMO-001"


# ---------------------------------------------------------------------------
# detail：套餐装备（query-meal-detail 只读 + calculate-price 权威算价）
# ---------------------------------------------------------------------------


def test_detail_combo_shows_groups_and_authoritative_price(capsys):
    """套餐：展示官方 rounds 组成 + 套餐参考价（calculate-price 权威结果）。"""
    code = cli.main(["detail", COMBO, "--store-code", STORE, "--demo"])
    out = capsys.readouterr().out
    assert code == 0
    assert "套餐装备" in out and "麦辣鸡腿堡套餐" in out
    assert "【套餐组成】" in out and "【套餐参考价】" in out
    assert "calculate-price 权威结果" in out
    assert "演示数据" in out  # 明确标注非实时


def test_detail_single_item_reports_no_groups(capsys):
    """单品：官方未返回 rounds 时明说「未返回套餐组成」，不编造组成。"""
    code = cli.main(["detail", SINGLE, "--store-code", STORE, "--demo"])
    out = capsys.readouterr().out
    assert code == 0
    assert "未返回套餐组成" in out
    assert "巨无霸" in out


def test_detail_unknown_code_fails_honestly(capsys):
    """不存在的餐品编码：明确失败（退出码 2），不猜、不编造。"""
    code = cli.main(["detail", "D9999", "--store-code", STORE, "--demo"])
    out = capsys.readouterr().out
    assert code == 2
    assert "D9999" in out and "不存在" in out


def test_detail_reconfigure_bad_format(capsys):
    """--reconfigure 缺等号：参数错误（退出码 2），不进入查询。"""
    code = cli.main(["detail", COMBO, "--store-code", STORE, "--reconfigure", "oops", "--demo"])
    out = capsys.readouterr().out
    assert code == 2
    assert "格式错误" in out


def test_detail_reconfigure_bad_group_index(capsys):
    """--reconfigure 组序号非整数：参数错误（退出码 2）。"""
    code = cli.main(["detail", COMBO, "--store-code", STORE, "--reconfigure", "abc=D1002", "--demo"])
    out = capsys.readouterr().out
    assert code == 2
    assert "组序号不是整数" in out


def test_detail_reconfigure_valid_recalculates_as_singles(capsys):
    """合法重配：按单品组合提交官方算价，并明确标注「不是套餐改价」。"""
    code = cli.main(["detail", COMBO, "--store-code", STORE, "--reconfigure", "1=D1002", "--demo"])
    out = capsys.readouterr().out
    assert code == 0
    assert "【重配（按单品组合）】" in out
    assert "第 1 组" in out
    assert "官方算价" in out
    assert "不是套餐改价" in out  # 语义边界必须显式声明


def test_detail_reconfigure_unknown_choice_keeps_default(capsys):
    """重配指定组内不存在的选项：保留默认项并说明，不虚构餐品。"""
    code = cli.main(["detail", COMBO, "--store-code", STORE, "--reconfigure", "1=ZZZZ", "--demo"])
    out = capsys.readouterr().out
    assert code == 0
    assert "不存在选项" in out and "已保留默认项" in out


def test_detail_json_payload(tmp_path):
    """--json：落盘 payload 含名称/组成/来源，结构可再解析。"""
    target = tmp_path / "detail.json"
    code = cli.main(["detail", COMBO, "--store-code", STORE, "--json", str(target), "--demo"])
    assert code == 0
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["code"] == COMBO
    assert payload["name"] == "麦辣鸡腿堡套餐"
    assert payload["store_code"] == STORE
    assert payload["groups"] and payload["groups"][0]["choices"]
    assert payload["source"] == "演示数据（MockMCPClient，非实时）"


# ---------------------------------------------------------------------------
# account：麦享会积分（query-my-account 只读）
# ---------------------------------------------------------------------------


def test_account_demo_shows_points_without_account_id(capsys):
    """积分账户：展示数值 + 演示标注；accountId 绝不出现。"""
    code = cli.main(["account", "--demo"])
    out = capsys.readouterr().out
    assert code == 0
    assert "麦享会积分" in out and "可用积分" in out
    assert "不读取、不输出 accountId" in out
    assert "演示数据" in out
    assert "accountId" not in out.replace("不读取、不输出 accountId", "")  # 除声明句外无标识输出


def test_account_json_payload(tmp_path):
    """--json：payload 标记 readonly，只含数值字段。"""
    target = tmp_path / "account.json"
    code = cli.main(["account", "--json", str(target), "--demo"])
    assert code == 0
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["readonly"] is True
    assert payload["source"] == "query-my-account"
    assert set(payload) >= {"available", "accumulative", "used", "expiring_next_month"}
    assert "accountId" not in payload and "account_id" not in payload


# ---------------------------------------------------------------------------
# campaign：麦门限时委托（campaign-calendar 只读）
# ---------------------------------------------------------------------------


def test_campaign_demo_lists_activities(capsys):
    """活动日历：演示数据 1 条进行中活动；声明「查看 ≠ 参加」。"""
    code = cli.main(["campaign", "--demo"])
    out = capsys.readouterr().out
    assert code == 0
    assert "麦门限时委托" in out
    assert "【进行中】" in out
    assert "9.9 元早餐两件套" in out
    assert "查看活动 ≠ 参加活动" in out


def test_campaign_json_payload(tmp_path):
    """--json：counts 与 campaigns 列表结构完整。"""
    target = tmp_path / "campaign.json"
    code = cli.main(["campaign", "--json", str(target), "--demo"])
    assert code == 0
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["source"] == "campaign-calendar"
    assert payload["readonly"] is True
    assert payload["counts"]["live"] == 1
    assert payload["campaigns"][0]["status"] == "live"


def test_campaign_accepts_date_argument(capsys):
    """--date 基准日期参数被接受（演示模式忽略具体日期也能出结果）。"""
    code = cli.main(["campaign", "--date", "2026-10-19", "--demo"])
    assert code == 0
    assert "麦门限时委托" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# coupons：优惠券猎人（三类券分区只读）
# ---------------------------------------------------------------------------


def test_coupons_without_store_code_skips_store_section(capsys):
    """无 --store-code：三类分区齐全，门店适用明确 skipped（不把卡包券当门店券）。"""
    code = cli.main(["coupons", "--demo"])
    out = capsys.readouterr().out
    assert code == 0
    assert "【可领取（麦麦省）】" in out
    assert "【我的卡包】" in out
    assert "【门店适用】" in out
    assert "官方明确：0 张可领取券" in out  # empty 态精确表达
    assert "未提供 --store-code" in out  # skipped 态有据可查
    assert "薯条半价尝鲜券" in out  # 卡包 1 张演示券照实展示


def test_coupons_with_store_code_queries_store_pool(capsys):
    """有 --store-code：门店分区改为实查（DEMO-001 有 2 张演示门店券）。"""
    code = cli.main(["coupons", "--store-code", STORE, "--demo"])
    out = capsys.readouterr().out
    assert code == 0
    assert f"【门店 {STORE} 适用】" in out
    assert "满30减5" in out
    assert "未提供 --store-code" not in out


def test_coupons_json_payload(tmp_path):
    """--json：三个分区状态分明（empty / ok / ok），字段含阈值与来源。"""
    target = tmp_path / "coupons.json"
    code = cli.main(["coupons", "--store-code", STORE, "--json", str(target), "--demo"])
    assert code == 0
    payload = json.loads(target.read_text(encoding="utf-8"))
    states = {s["name"]: s["state"] for s in payload["sections"]}
    assert states["可领取（麦麦省）"] == "empty"
    assert states["我的卡包"] == "ok"
    assert states[f"门店 {STORE} 适用"] == "ok"
    assert payload["readonly"] is True
    my = payload["sections"][1]["coupons"][0]
    assert my["id"] == "C-DEMO-FRIES-HALF" and my["source"] == "my"
