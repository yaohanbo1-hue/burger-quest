#!/usr/bin/env python3
"""Burger Quest 离线演示统一入口（examples/run_demos.py）。

在干净环境中重复运行全部演示场景：

    pip install -e ".[dev]"     # 或 pip install .
    python examples/run_demos.py            # 运行全部场景
    python examples/run_demos.py --quick    # 只跑 4 个核心场景
    python examples/run_demos.py --cards    # 同时重新生成示例成就卡

约定：
- 全部场景使用包内演示数据（DEMO，非实时菜单），不需要 MCP Token；
- 每个场景打印：用户输入 -> 推荐装备 -> 价格结算 -> 验证重点 -> 退出码；
- 场景 4（预算不足）预期失败（退出码 1）——诚实失败也是演示内容；
- 本脚本只读演示数据，不调用任何真实 MCP、不产生任何写操作。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CARDS_DIR = REPO_ROOT / "examples" / "cards"

# (场景名, 用户输入, 验证重点, 预期退出码, 卡片文件名前缀或 None)
SCENARIOS = [
    (
        "场景 1 · 预算受限的辣味偏好（外送）",
        "我想吃辣，预算35，外送到南山科技园，一个人",
        [
            "预算 ¥35 / 1 人 / 外送 / 辣味 全部正确解析",
            "麦辣鸡腿堡 + 麦辣鸡翅，原价 ¥33，双券 -¥8，配送费 ¥9，应付 ¥34（权威核验）",
            "剩余预算 ¥1.00；总价标注 calculate-price 权威结果",
        ],
        0,
        "demo_spicy",
    ),
    (
        "场景 2 · 两人用餐的预算规划（丰盛）",
        "两个人想吃点好的，预算100，堂食",
        [
            "2 人 + 丰盛偏好 -> 双主食（套餐）+ 小食，主食 2 份够分食",
            "原价 ¥91，满30减5 -¥5，应付 ¥86，剩余 ¥14",
            "整餐热量约 1540 千卡（真实演示营养数据汇总）",
        ],
        0,
        "demo_share",
    ),
    (
        "场景 3 · 营养偏好组合搜索（低卡）",
        "减肥中，想吃低卡的，预算40，到店自取",
        [
            "低卡偏好（≤600 千卡/人）-> 整餐约 540 千卡，满足偏好",
            "双层吉士堡 + 玉米杯 + 零度可乐，应付 ¥25，剩余 ¥15",
            "提示「饮食偏好，不构成医疗或减重建议」",
        ],
        0,
        "demo_light",
    ),
    (
        "场景 4 · 预算不足（诚实失败）",
        "预算5块，想吃汉堡",
        [
            "没有组合能落在 ¥5 预算内——明确失败并给出调整建议",
            "不生成任何方案、不编造任何价格（退出码 1 属预期）",
        ],
        1,
        None,
    ),
    # ---- 以下为附加场景（--quick 跳过）----
    (
        "附加 A · 多条件综合（蛋白 + 少饮料 + 模糊预算）",
        "预算 35 元，想吃辣，两个人，希望尽量高蛋白，不想点太多饮料",
        [
            "模糊预算「35 元」+ 蛋白偏好 + 饮料软上限同时生效",
            "最多 3 个可比较方案，逐条标注符合/不完全符合",
        ],
        0,
        None,
    ),
    (
        "附加 B · 超预算参考（最接近真实方案）",
        "预算12块，想吃辣",
        [
            "给出最接近的真实方案（双层吉士堡 ¥13）并明确标注超预算 ¥1",
            "价格未被篡改成预算内——不伪造成功",
        ],
        0,
        None,
    ),
    (
        "附加 C · 品类硬避免（不要饮料）",
        "不要饮料，预算20",
        [
            "候选组合完全不含饮品；预算内 ¥13，剩余 ¥7",
        ],
        0,
        None,
    ),
    (
        "附加 D · 多家候选门店（不默默选择）",
        "在演示门店吃，到店自取，预算35",
        [
            "同时匹配两家演示门店 -> 列出候选并暂停（退出码 1）",
            "不查询菜单、不生成任何价格",
        ],
        1,
        None,
    ),
]


def run_scenario(name: str, text: str, points: list, expect_rc: int, card_prefix, gen_cards: bool) -> bool:
    print("=" * 66)
    print(f"🎬 {name}")
    print(f"   用户输入：{text}")
    print("-" * 66)
    cmd = [sys.executable, "-m", "burger_quest", "demo", text]
    if gen_cards and card_prefix:
        CARDS_DIR.mkdir(parents=True, exist_ok=True)
        cmd += [
            "--card-html", str(CARDS_DIR / f"{card_prefix}.html"),
            "--card-md", str(CARDS_DIR / f"{card_prefix}.md"),
        ]
    proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    out = proc.stdout
    # 只打印关键片段，避免刷屏
    keep = []
    for line in out.splitlines():
        if any(k in line for k in ("⚔️", "🎟️", "原价合计", "优惠减免", "配送费", "应付总价", "剩余预算", "超出预算", "属性面板", "MCP 调用审计", "⚠️", "❌", "⏸", "候选门店", "编码")):
            keep.append(line)
    for line in keep[:18]:
        print("   " + line)
    print("-" * 66)
    ok = proc.returncode == expect_rc
    print(f"   退出码：{proc.returncode}（预期 {expect_rc}）{'✅' if ok else '❌ 不符预期'}")
    for p in points:
        print(f"   · {p}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="Burger Quest 离线演示统一入口")
    ap.add_argument("--quick", action="store_true", help="只跑 4 个核心场景")
    ap.add_argument("--cards", action="store_true", help="为核心场景重新生成示例成就卡")
    args = ap.parse_args()

    scenarios = SCENARIOS[:4] if args.quick else SCENARIOS
    results = []
    for name, text, points, rc, prefix in scenarios:
        results.append((name, run_scenario(name, text, points, rc, prefix, args.cards)))
    print("=" * 66)
    passed = sum(1 for _n, ok in results if ok)
    print(f"🎞 演示完成：{passed}/{len(results)} 个场景行为符合预期")
    if args.cards:
        print(f"📇 示例成就卡目录：{CARDS_DIR}")
    print("📌 全部场景为 DEMO 离线演示数据（非实时菜单）；真实 MCP 联调记录见 MCP_INTEGRATION.md §3.5")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
