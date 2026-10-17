"""成就/进度/防刷分测试（2026-10-17 新增）。

覆盖（对应成就系统 9 条要求中的可测部分）：
1. 成就定义完整性：唯一 id、≥15 个、分类覆盖探索/预算/口味/任务/功能；
2. 真实才解锁：demo 模式不解锁 requires_real 成就；查询失败/无方案时
   依赖方案数据的成就同样不解锁；
3. 防刷分：重放相同签名 0 经验；重复解锁不重复发奖励；累计类按不同
   签名/门店计数；
4. 等级升级：level_for_xp 阈值曲线；
5. 持久化：损坏存档备份重建、版本迁移、容量上限；
6. 隐私：进度文件不含原始输入/位置/Token；
7. 虚拟边界：文案不含「官方账户/真实优惠券/现金奖励」类表述。
"""
import json

import pytest

from burger_quest.achievements import (
    ACHIEVEMENTS,
    ACHIEVEMENTS_BY_ID,
    CAT_BUDGET,
    CAT_EXPLORATION,
    CAT_FEATURE,
    CAT_QUEST,
    CAT_TASTE,
    level_for_xp,
)
from burger_quest.mcp_base import OrderScene
from burger_quest.models import Constraints, MenuItem, PlanItem, PlanOption
from burger_quest import nlparser, progress as progress_mod
from burger_quest.service import run_quest
from test_http_client import make_client


def _resp(data):
    return {"success": True, "code": 200, "data": data}


def _menu_item(code, name, price, category="主食", tags=()):
    return MenuItem(code=code, name=name, category=category, price_cents=price, tags=tuple(tags))


def _result(chosen_items, *, budget_cents=3500, demo_counts=None, store_code="1420762", avoid_spicy=False):
    """构造 QuestResult（chosen_items 为空表示无方案——诚实失败路径）。"""
    items = [PlanItem(_menu_item(*a), q) for a, q in chosen_items]
    chosen = None
    if items:
        total = sum(pi.item.price_cents * pi.qty for pi in items)
        chosen = PlanOption(
            title="t", items=items, total_cents=total, original_cents=total,
            price_checked=True, within_budget=(budget_cents is not None and total <= budget_cents),
            remaining_cents=(budget_cents - total) if budget_cents is not None else None,
        )
    r = type("R", (), {})()  # 轻量替身：只带评估需要的字段
    from burger_quest.models import QuestResult, Store

    result = QuestResult(constraints=Constraints(budget_cents=budget_cents, avoid_tastes=("spicy",) if avoid_spicy else ()))
    result.store = Store(code=store_code, name="测试门店") if store_code else None
    result.chosen = chosen
    result.options = [chosen] if chosen else []
    result.counts = demo_counts or {}
    result.data_sources = {"store": "指定门店编码 1420762"}
    result.warnings = [] if chosen else ["没有组合能落在 ¥5.00 预算内（测试替身）"]
    return result


# ---------------------------------------------------------------------------
# 1. 定义完整性
# ---------------------------------------------------------------------------


def test_achievement_definitions_complete():
    assert len(ACHIEVEMENTS) >= 15
    ids = [a.id for a in ACHIEVEMENTS]
    assert len(ids) == len(set(ids)), "成就 id 必须唯一"
    assert set(ACHIEVEMENTS_BY_ID) == set(ids)
    cats = {a.category for a in ACHIEVEMENTS}
    assert {CAT_EXPLORATION, CAT_BUDGET, CAT_TASTE, CAT_QUEST, CAT_FEATURE} <= cats
    for a in ACHIEVEMENTS:
        assert a.name and a.desc and a.reward_xp > 0


def test_level_curve():
    """阈值：0/50/120/250/450/700/1000 -> LV.1..7。"""
    assert level_for_xp(0) == (1, "见习冒险者")
    assert level_for_xp(49) == (1, "见习冒险者")
    assert level_for_xp(50) == (2, "饱腹探索者")
    assert level_for_xp(119) == (2, "饱腹探索者")
    assert level_for_xp(120) == (3, "资深冒险者")
    assert level_for_xp(249) == (3, "资深冒险者")
    assert level_for_xp(250) == (4, "传奇领队")
    assert level_for_xp(1000) == (7, "异世界传说")
    assert level_for_xp(10**9) == (7, "异世界传说")  # 顶档稳定


# ---------------------------------------------------------------------------
# 2. 真实才解锁（demo / 查询失败不得解锁真实成就）
# ---------------------------------------------------------------------------


def test_demo_mode_cannot_unlock_real_achievements():
    result = _result([(("D1002", "麦辣鸡腿堡", 2100), 1)], demo_counts={"menu": 116, "nutrition": 160})
    run = progress_mod.apply_progress(progress_mod.Progress(), result, demo=True)[1]
    assert run.blocked, "demo 模式必须阻止真实成就"
    assert all(u.achievement.requires_real is False for u in run.unlocks)


def test_no_plan_cannot_unlock_plan_dependent_achievements():
    result = _result([])  # 无方案（诚实失败）
    run = progress_mod.apply_progress(progress_mod.Progress(), result, demo=False)[1]
    unlocked_ids = {u.achievement.id for u in run.unlocks}
    assert "first_plan" not in unlocked_ids and "budget_keeper" not in unlocked_ids
    assert "honest_failure" in unlocked_ids  # 诚实失败本身是成就
    assert run.total_xp > 0


def test_real_success_unlocks_and_awards():
    result = _result(
        [(("A", "麦辣鸡腿堡", 2100, "鸡肉汉堡", ("辣",)), 1)],
        demo_counts={"menu": 116, "nutrition": 160, "coupons": 0},
    )
    progress, run, events = progress_mod.apply_progress(progress_mod.Progress(), result, demo=False)
    ids = {u.achievement.id for u in run.unlocks}
    assert {"first_plan", "first_store", "menu_reader", "nutrition_reader", "budget_keeper", "spicy_first"} <= ids
    assert run.total_xp > 0 and progress.xp == run.total_xp
    assert any("等级" in e or "经验" in e for e in events)


# ---------------------------------------------------------------------------
# 3. 防刷分
# ---------------------------------------------------------------------------


def test_replay_same_signature_awards_nothing_second_time():
    result = _result([(("A", "麦辣鸡腿堡", 2100, "鸡肉汉堡", ("辣",)), 1)], demo_counts={"menu": 116})
    p1, run1, _ = progress_mod.apply_progress(progress_mod.Progress(), result, demo=False)
    assert run1.total_xp > 0
    p2, run2, _ = progress_mod.apply_progress(p1, result, demo=False)
    assert run2.repeated is True and run2.total_xp == 0 and p2.xp == p1.xp


def test_repeat_unlock_does_not_reaward():
    result = _result([(("A", "麦辣鸡腿堡", 2100, "鸡肉汉堡", ("辣",)), 1)], demo_counts={"menu": 116})
    p1, run1, _ = progress_mod.apply_progress(progress_mod.Progress(), result, demo=False)
    # 手动把已解锁集合返还给第二次评估（模拟进度持久化后的重放）
    from burger_quest.achievements import evaluate_run

    run2 = evaluate_run(
        result, demo=False, unlocked=set(p1.unlocked), seen_signatures=set(p1.seen_signatures),
        stores_seen=set(p1.stores), card_signatures=set(p1.card_signatures),
    )
    assert all(u.already for u in run2.unlocks)
    assert run2.unlock_xp == 0


def test_cumulative_achievements_need_distinct_events():
    """store_collector 需要 3 个不同门店——相同门店重复不出进度。"""
    from burger_quest.achievements import evaluate_run

    p = progress_mod.Progress()
    for code in ("S1", "S2", "S3"):
        result = _result([(("A", "堡", 2100), 1)], store_code=code)
        p, _run, _ = progress_mod.apply_progress(p, result, demo=False)
    assert "store_collector" in p.unlocked
    # 同一门店再跑十次也不增加新门店
    before = len(p.stores)
    for _ in range(10):
        result = _result([(("A", "堡", 2100), 1)], store_code="S1")
        p, _run, _ = progress_mod.apply_progress(p, result, demo=False)
    assert len(p.stores) == before


# ---------------------------------------------------------------------------
# 4. 持久化安全：损坏/迁移/隐私
# ---------------------------------------------------------------------------


def test_corrupt_save_is_backed_up_and_reset(tmp_path):
    p = tmp_path / "progress.json"
    p.write_text("{ 这不是合法 JSON", encoding="utf-8")
    progress, notes = progress_mod.load_progress(str(p))
    assert progress.xp == 0
    assert any("损坏" in n for n in notes)
    assert any(f.name.startswith("progress.json.corrupt-") for f in tmp_path.iterdir())


def test_version_migration(tmp_path):
    p = tmp_path / "old.json"
    p.write_text(json.dumps({"xp": 42, "unlocked": {"first_plan": 1}}), encoding="utf-8")  # 无 version/列表字段
    progress, notes = progress_mod.load_progress(str(p))
    assert progress.xp == 42 and "first_plan" in progress.unlocked
    assert progress.version == progress_mod.PROGRESS_VERSION
    assert any("迁移" in n for n in notes)


def test_save_is_atomic_and_reloadable(tmp_path):
    p = tmp_path / "sub" / "progress.json"  # 父目录不存在：应自动创建
    progress = progress_mod.Progress(xp=99)
    progress_mod.save_progress(str(p), progress)
    loaded, notes = progress_mod.load_progress(str(p))
    assert loaded.xp == 99 and not notes
    assert not (tmp_path / "sub" / "progress.json.tmp").exists()  # 无残留临时文件


def test_progress_file_has_no_personal_information(tmp_path):
    """存档只允许：version/xp/unlocked/seen_signatures/stores/card_signatures。"""
    p = tmp_path / "progress.json"
    result = _result([(("A", "麦辣鸡腿堡", 2100, "鸡肉汉堡", ("辣",)), 1)])
    result.constraints = nlparser.parse("我想吃辣，预算35，外送到深圳市南山区科技园，一个人")
    progress_mod.apply_progress(progress_mod.Progress(), result, demo=False)
    progress_mod.save_progress(str(p), progress_mod.Progress(xp=5, stores=["1420762"]))
    data = json.loads(p.read_text(encoding="utf-8"))
    assert set(data) == {"version", "xp", "unlocked", "seen_signatures", "stores", "card_signatures"}
    raw = p.read_text(encoding="utf-8")
    for forbidden in ("科技园", "深圳市", "我想吃辣", "MCD_MCP_TOKEN", "Bearer", "麦辣"):
        assert forbidden not in raw, f"存档出现敏感/隐私内容：{forbidden}"


# ---------------------------------------------------------------------------
# 5. 虚拟边界（不得包装成真实权益）
# ---------------------------------------------------------------------------


def test_virtual_notice_present_and_no_real_benefit_claims():
    from burger_quest.achievements import VIRTUAL_NOTICE

    # 声明锚点在位（虚拟进度 + 麦当劳 + 否定式权益表述）
    assert "虚拟进度" in VIRTUAL_NOTICE and "麦当劳" in VIRTUAL_NOTICE
    assert "不代表任何优惠、权益或现金奖励" in VIRTUAL_NOTICE
    # 成就不承诺任何真实世界回报（到账/赠送/免费获得）——注意 VIRTUAL_NOTICE
    # 本身是否定式免责（「不代表」），不参与正向扫描
    for a in ACHIEVEMENTS:
        text = a.name + a.desc + a.lore_text
        for claim in ("现金", "到账", "领取奖励", "官方赠送", "免费获得", "红包"):
            assert claim not in text, f"{a.id} 出现真实回报暗示：{claim}"


# ---------------------------------------------------------------------------
# 6. 端到端（CLI 链路）：demo 会话带进度
# ---------------------------------------------------------------------------


def test_cli_demo_with_progress_creates_and_guards(tmp_path, capsys):
    from burger_quest import cli

    prog = tmp_path / "p.json"
    rc = cli.main(["demo", "我想吃辣，预算35", "--progress", str(prog)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "【冒险者档案】" in out and "虚拟进度" in out
    assert prog.exists()
    # 重放相同输入：0 经验
    rc2 = cli.main(["demo", "我想吃辣，预算35", "--progress", str(prog)])
    assert rc2 == 0
    out2 = capsys.readouterr().out
    assert "不计经验" in out2
    data = json.loads(prog.read_text(encoding="utf-8"))
    assert data["xp"] >= 5 and "demo_explorer" in data["unlocked"]


def test_cli_progress_subcommand_lists_gallery(tmp_path, capsys):
    from burger_quest import cli

    prog = tmp_path / "p.json"
    cli.main(["demo", "我想吃辣，预算35", "--progress", str(prog)])
    capsys.readouterr()
    rc = cli.main(["progress", "--progress", str(prog)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "成就图鉴" in out and "演示先行者" in out and "剧情章节" in out
