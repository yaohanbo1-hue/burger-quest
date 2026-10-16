"""命令行入口。

用法：
  # 演示模式（完全离线，使用内置演示数据，无需 MCP Token；数据非实时，仅用于体验与测试）
  python -m burger_quest demo "我想吃辣，预算35，外送到南山科技园，一个人"

  # 真实模式（调用麦当劳中国官方 MCP；需要 MCD_MCP_TOKEN，未配置时明确报错，不会回退演示数据）
  export MCD_MCP_TOKEN="你的Token"
  python -m burger_quest quest "我想吃辣，预算35，外送到南山科技园"
  # 找到多家附近门店时，按提示选择一家后用 --store-code 重新运行：
  python -m burger_quest quest "我想吃辣，预算35" --store-code <编码>

  # 先查询附近门店（获取真实门店编码）
  python -m burger_quest stores "南山科技园"

  # 只读 MCP 冒烟测试（连接/工具发现/关键查询；不调用任何写操作）
  python -m burger_quest smoke --demo        # 离线可重复执行
  export MCD_MCP_TOKEN="你的Token"
  python -m burger_quest smoke               # 真实服务七步只读检查

  # 查看引擎实际调用的 MCP 工具清单
  python -m burger_quest tools

退出码：0=成功；1=未生成方案（含等待选择门店）；2=配置或连接错误（如缺少 Token）。
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

from . import __version__
from .mcp_base import MCPError, TOOLS_RESERVED, TOOLS_USED, summarize_calls
from .models import format_yuan
from .service import run_quest


def _print_result(result, card_html: Path = None, card_md: Path = None, json_out: Path = None, mode: str = "quest") -> int:
    c = result.constraints
    print("=" * 62)
    print(f"🍔 {result.quest_name or '任务未完成'}")
    print("=" * 62)
    if result.story:
        print("\n【任务背景】\n" + result.story)
    if result.gear_lines:
        print("\n【推荐装备】")
        for line in result.gear_lines:
            print("  " + line)
    if result.chosen is not None:
        from . import rpg

        print("\n【价格结算】")
        for line in rpg.price_lines(result.chosen, c):
            print("  " + line)
    if len(result.options) > 1:
        print("\n【方案对比】按推荐顺序排列（最多 3 个可比较方案；总价均为 calculate-price 权威结果）：")
        for i, o in enumerate(result.options, 1):
            mark = "👉" if o is result.chosen else "  "
            budget_tag = ""
            if o.within_budget is True:
                budget_tag = "预算内"
            elif o.within_budget is False:
                budget_tag = "超预算"
            total = format_yuan(o.total_cents) if o.total_cents is not None else "价格未校验"
            print(f"  {mark} [{i}] {o.title} —— {o.item_names()}（{total}{('，' + budget_tag) if budget_tag else ''}）")
            for r in o.reasons:
                print(f"        ✅ {r}")
            for u in o.unmet:
                print(f"        ⚠️ 不完全符合：{u}")
            for w in o.warnings:
                print(f"        ⚠️ {w}")
    elif result.chosen is not None:
        for u in result.chosen.unmet:
            print(f"\n⚠️ 不完全符合：{u}")
        for w in result.chosen.warnings:
            print(f"⚠️ {w}")
    if result.store_candidates:
        print("\n【候选门店】找到多家附近门店，请选择一家后重新运行（未查询菜单、未生成任何价格）：")
        for i, s in enumerate(result.store_candidates, 1):
            addr = f"  {s.address}" if s.address else ""
            print(f"  [{i}] {s.name or '（未命名门店）'}  编码：{s.code}{addr}")
        print("\n选择后重新运行：")
        print(f'  python -m burger_quest {mode} "{c.raw_text}" --store-code <编码>')
    if result.warnings:
        print("\n【提示】")
        for w in result.warnings:
            print("  ⚠️ " + w)
    if result.chosen is None:
        if result.store_candidates:
            print("\n⏸ 已暂停：等待你选择门店（不会替你默认选择，也不会生成任何价格）。")
        else:
            print("\n❌ 未生成方案。原因见上方提示——引擎不会编造餐品、价格或优惠。")
    else:
        print("\n【数据来源】")
        for k, v in result.data_sources.items():
            if isinstance(v, dict):
                v = json.dumps(v, ensure_ascii=False)
            print(f"  - {k}: {v}")
    if result.mcp_calls:
        summary = summarize_calls(result.mcp_calls)
        by_tool = "、".join(f"{t}×{n}" for t, n in summary["by_tool"].items())
        print(
            f"\n【MCP 调用审计】真实请求 {summary['total_calls']} 次"
            f"（去重命中 {summary['dedup_hits']} 次，失败 {summary['failed']} 次，"
            f"总耗时 {summary['total_duration_ms']:.0f} ms）"
        )
        if by_tool:
            print(f"  分工具：{by_tool}")
    if card_html and result.card_html:
        card_html.parent.mkdir(parents=True, exist_ok=True)
        card_html.write_text(result.card_html, encoding="utf-8")
        print(f"\n📇 分享卡(HTML) 已保存：{card_html}")
    if card_md and result.card_markdown:
        card_md.parent.mkdir(parents=True, exist_ok=True)
        card_md.write_text(result.card_markdown, encoding="utf-8")
        print(f"📇 分享卡(Markdown) 已保存：{card_md}")
    if json_out:
        payload = {
            "quest_name": result.quest_name,
            "story": result.story,
            "gear": result.gear_lines,
            "constraints": c.describe(),
            "constraints_debug": c.to_debug_dict(),  # 结构化解析结果（默认不含原始输入）
            "options": [
                {
                    "title": o.title,
                    "items": [{"code": pi.item.code, "name": pi.item.name, "qty": pi.qty} for pi in o.items],
                    "coupons": [c2.id for c2 in o.coupons],
                    "total_cents": o.total_cents,
                    "original_cents": o.original_cents,
                    "discount_cents": o.discount_cents,
                    "delivery_fee_cents": o.delivery_fee_cents,
                    "within_budget": o.within_budget,
                    "remaining_cents": o.remaining_cents,
                    "reasons": o.reasons,
                    "unmet": o.unmet,
                    "nutrition": o.nutrition,
                    "nutrition_complete": o.nutrition_complete,
                    "nutrition_missing": o.nutrition_missing,
                    "price_checked": o.price_checked,
                }
                for o in result.options
            ],
            "store_candidates": [
                {"code": s.code, "name": s.name, "address": s.address} for s in result.store_candidates
            ],
            "warnings": result.warnings,
            "data_sources": result.data_sources,
            "mcp_calls_summary": summarize_calls(result.mcp_calls) if result.mcp_calls else None,
        }
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"🧾 结构化结果(JSON) 已保存：{json_out}")
    return 0 if result.chosen is not None else 1


def _cmd_stores(args) -> int:
    """stores 子命令：只查附近门店，帮助用户获取真实门店编码。"""
    if args.demo:
        from .mcp_mock import MockMCPClient

        try:
            client = MockMCPClient(fixture_path=args.fixture)
        except MCPError as e:
            print(f"❌ {e}")
            return 2
        mode = "demo"
    else:
        from .mcp_http import HttpMCPClient

        try:
            client = HttpMCPClient()
            client.connect()
        except MCPError as e:
            print(f"❌ {e}")
            print("提示：演示模式可加 --demo 查看内置演示门店（DEMO-001 / DEMO-002）")
            return 2
        mode = "quest"
    try:
        stores = client.nearby_stores(args.address)
    except MCPError as e:
        print(f"❌ 门店查询失败：{e}")
        return 2
    finally:
        client.close()
    if not stores:
        print(f"未在「{args.address}」找到附近门店。")
        print("请补充更具体的地址（建议包含城市/商圈）；麦当劳 MCP 服务范围不含港澳台。")
        return 1
    print(f"在「{args.address}」找到 {len(stores)} 家门店：")
    for i, s in enumerate(stores, 1):
        addr = f"  {s.address}" if s.address else ""
        print(f"  [{i}] {s.name or '（未命名门店）'}  编码：{s.code}{addr}")
    print("\n选择一家后运行：")
    print(f'  python -m burger_quest {mode} "你的需求" --store-code <编码>')
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="burger-quest",
        description="Burger Quest · 麦门异世界 —— 基于麦当劳中国官方 MCP 的游戏化点餐规划",
        epilog=(
            "示例：\n"
            '  python -m burger_quest demo "我想吃辣，预算35，外送到南山科技园"\n'
            '  python -m burger_quest quest "我想吃辣，预算35，外送到南山科技园"\n'
            '  python -m burger_quest stores "南山科技园"\n'
            "  python -m burger_quest doctor              # 真实连接诊断（需 Token）\n"
            "  python -m burger_quest smoke --demo\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"burger-quest {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_plan_args(p):
        p.add_argument("text", help="自然语言需求，如：想吃辣，预算35，外送到南山科技园")
        p.add_argument(
            "--store-code",
            default=None,
            help="指定门店编码（先用 stores 子命令查询真实编码；DEMO-001/DEMO-002 仅存在于演示数据）",
        )
        p.add_argument("--card-html", type=Path, default=None, help="分享卡 HTML 输出路径")
        p.add_argument("--card-md", type=Path, default=None, help="分享卡 Markdown 输出路径")
        p.add_argument("--json", type=Path, default=None, help="结构化结果 JSON 输出路径")
        p.add_argument(
            "--card-snapshot",
            action="store_true",
            help="成就卡标注为「LIVE SNAPSHOT 历史快照」：真实 MCP 调用的历史结果，"
            "明确声明金额不代表当前价格/优惠/供应状态（用于存档分享真实查询结果）",
        )

    p_demo = sub.add_parser(
        "demo",
        help="演示模式：完全离线，使用内置演示数据（无需 Token，数据非实时）",
        description=(
            "演示模式：完全离线运行，使用 fixtures/demo_data.json 内置演示数据，"
            "无需 MCP Token。餐品与价格均为演示数值，不是实时菜单；"
            "用于快速体验与自动化测试。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_plan_args(p_demo)
    p_demo.add_argument("--fixture", default=None, help="演示数据 JSON 路径（默认 fixtures/demo_data.json）")

    p_quest = sub.add_parser(
        "quest",
        help="真实模式：调用麦当劳中国官方 MCP（需 MCD_MCP_TOKEN；未配置时明确报错，不会回退演示数据）",
        description=(
            "真实模式：调用麦当劳中国官方 MCP Server（https://mcp.mcd.cn）。\n"
            "需要环境变量 MCD_MCP_TOKEN（到 https://open.mcd.cn/mcp 申请）；\n"
            "未配置 Token 时明确报错（退出码 2），不会自动切换到演示数据。\n"
            "本模式不接受 --fixture 参数；门店通过位置自动查询附近门店，\n"
            "找到多家时列出候选，用 --store-code 指定其中一家。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_plan_args(p_quest)

    p_stores = sub.add_parser(
        "stores",
        help="查询附近门店，获取门店编码（真实模式需 Token；--demo 用演示数据）",
        description=(
            "查询附近门店并输出门店编码列表，用于获取 --store-code 需要的真实编码。\n"
            "真实模式需要 MCD_MCP_TOKEN；加 --demo 使用内置演示数据（离线）。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_stores.add_argument("address", help="地址/位置，如：南山科技园")
    p_stores.add_argument("--demo", action="store_true", help="使用演示数据（离线）")
    p_stores.add_argument("--fixture", default=None, help="演示数据 JSON 路径（仅 --demo 有效）")

    p_smoke = sub.add_parser(
        "smoke",
        help="只读 MCP 冒烟测试：连接/工具发现/关键查询（--demo 离线可重复；真实模式需 Token）",
        description=(
            "只读 MCP 冒烟测试：连接 -> 工具发现 -> now-time-info -> query-nearby-stores\n"
            "-> query-meals -> query-meal-detail -> calculate-price，逐步报告 PASS/FAIL。\n"
            "Token 只从环境变量 MCD_MCP_TOKEN 读取，绝不打印；\n"
            "只调用只读工具，不调用下单/取消/资金等任何写操作。\n"
            "加 --demo 使用内置演示数据离线执行（可重复、无需 Token）。\n"
            "退出码：0=全部通过；1=有步骤失败；2=配置错误。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_smoke.add_argument("--location", default="深圳市南山区科技园", help="探测位置（默认「深圳市南山区科技园」）")
    p_smoke.add_argument("--store-code", default=None, help="直接指定门店编码（跳过门店查询）")
    p_smoke.add_argument("--demo", action="store_true", help="离线演示模式：使用内置演示数据（无需 Token，可重复执行）")
    p_smoke.add_argument("--json", default=None, help="冒烟结果 JSON 输出路径")

    p_doctor = sub.add_parser(
        "doctor",
        help="真实 MCP 连接诊断：Python/依赖/Token/网络/初始化/工具发现/只读调用（缺 Token 直接失败，不回退 Demo）",
        description=(
            "真实 MCP 连接诊断（只读）：依次检查 Python 版本 -> 官方 mcp SDK -> MCD_MCP_TOKEN 存在性\n"
            "-> 网络可达 -> MCP 初始化 -> 工具发现 -> 一次只读工具调用。每步 PASS/FAIL +\n"
            "原因 + 下一步建议；失败分类：TOKEN_MISSING / 401 / 403 / 429 / NETWORK / SDK /\n"
            "TOOL_MISSING / BUSINESS。Token 只查存在性，绝不打印值。\n"
            "退出码：0=全部通过；1=有失败；2=配置错误（缺 Token/缺 SDK/Python 版本不符）。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_doctor.add_argument("--json", default=None, help="诊断结果 JSON 输出路径（不含任何敏感值）")

    sub.add_parser("tools", help="列出引擎实际调用的 MCP 工具")

    args = parser.parse_args(argv)

    if args.cmd == "tools":
        print("引擎实际调用的麦当劳 MCP 工具：")
        for t in TOOLS_USED:
            print(f"  - {t}")
        print("\n预留但 MVP 不主动调用（涉及真实资金/库存/账户状态，或需联调后接入）：")
        for t in TOOLS_RESERVED:
            print(f"  - {t}")
        return 0

    if args.cmd == "stores":
        return _cmd_stores(args)

    if args.cmd == "smoke":
        from . import smoke

        argv = ["--location", args.location]
        if args.store_code:
            argv += ["--store-code", args.store_code]
        if args.demo:
            argv.append("--demo")
        if args.json:
            argv += ["--json", str(args.json)]
        return smoke.main(argv)

    if args.cmd == "doctor":
        from . import doctor

        argv = []
        if args.json:
            argv += ["--json", str(args.json)]
        return doctor.main(argv)

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if args.cmd == "demo":
        from .mcp_mock import MockMCPClient

        if args.card_snapshot:
            # 演示数据不得伪装成真实 MCP 历史快照——明确提示并忽略该标记
            print("⚠️ --card-snapshot 仅适用于真实 MCP 模式（quest）；演示数据不会标注 LIVE SNAPSHOT")
        try:
            client = MockMCPClient(fixture_path=args.fixture)
        except MCPError as e:
            print(f"❌ {e}")
            return 2
        result = run_quest(
            args.text, client, store_code=args.store_code, generated_at=now,
            card_snapshot=args.card_snapshot,
        )
        return _print_result(result, args.card_html, args.card_md, args.json, mode="demo")

    if args.cmd == "quest":
        from .mcp_http import HttpMCPClient

        try:
            with HttpMCPClient() as client:
                result = run_quest(
                    args.text, client, store_code=args.store_code, generated_at=now,
                    card_snapshot=args.card_snapshot,
                )
        except MCPError as e:
            print(f"❌ {e}")
            return 2
        return _print_result(result, args.card_html, args.card_md, args.json, mode="quest")

    return 1


if __name__ == "__main__":
    sys.exit(main())
