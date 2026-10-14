"""规划引擎升级的离线演示固化测试（对应 examples/planner_demos.md）。

四个场景全部离线运行（MockMCPClient 演示数据），无需 Token，可重复执行。
每个测试断言该演示要验证的核心约束——演示文档与测试保持一致。
"""
from burger_quest import cli


def _run(text, capsys):
    rc = cli.main(["demo", text])
    out = capsys.readouterr().out
    return rc, out


def test_demo_a_multi_condition_user_sentence(capsys):
    """演示 A：预算/辣味/两人/高蛋白/少饮料 —— 用户原话。"""
    rc, out = _run("预算 35 元，想吃辣，两个人，希望尽量高蛋白，不想点太多饮料", capsys)
    assert rc == 0
    # 三个可比较方案，全部预算内、价格已核验
    assert "最多 3 个可比较方案" in out
    assert out.count("预算内）") >= 2
    # 权威核验与叠券
    assert "优惠券已省 ¥8.00（权威核验）" in out
    assert "calculate-price 权威结果" in out
    # 营养偏好（真实数据）与少饮料偏好
    assert "蛋白质 41g 达标（偏好 ≥25g）" in out
    assert "饮料 0 份，符合少饮偏好（≤1）" in out
    # 不完全符合的部分被如实标注（不隐瞒）
    assert "不完全符合：主食 1 份，少于 2 人" in out
    # 演示数据明示
    assert "演示数据" in out


def test_demo_b_over_budget_reference(capsys):
    """演示 B：超预算一分到一块 —— 给最接近真实参考并明确标注。"""
    rc, out = _run("预算12块，想吃辣", capsys)
    assert rc == 0
    assert "超出预算：¥1.00" in out
    assert "不完全符合：超出预算 ¥1.00" in out
    assert "calculate-price 权威结果" in out
    # 未命中口味也如实说明
    assert "口味偏好（辣味）未被任何餐品命中" in out
    # 没有把价格篡改为预算内的假成功
    assert "应付总价：¥13.00" in out


def test_demo_c_avoid_drink_hard(capsys):
    """演示 C：不要饮料（硬避免）—— 组合不含饮品且预算内。"""
    rc, out = _run("不要饮料，预算20", capsys)
    assert rc == 0
    assert "预算内，剩余 ¥7.00" in out
    # 装备行与方案行不出现饮品
    for line in out.splitlines():
        if "⚔️" in line or "阵型 ——" in line:
            assert "可乐" not in line, f"方案行不应含饮品：{line}"
    # 方案对比区块存在（多于一个候选或单个都会打印）
    assert "省钱精算阵型" in out


def test_demo_d_no_feasible_solution(capsys):
    """演示 D：预算远低于任何组合 —— 可解释失败 + 建议，不编造成功。"""
    rc, out = _run("预算5块，想吃汉堡", capsys)
    assert rc == 1
    assert "未生成方案" in out
    assert "没有组合能落在 ¥5.00 预算内" in out
    assert "未编造任何价格" in out
    assert "可尝试" in out  # 调整建议
