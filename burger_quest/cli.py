"""命令行入口。

用法：
  # 演示模式（离线，使用 fixtures 演示数据，无需 MCP Token）
  python -m burger_quest demo "我想吃辣，预算35，外送到南山科技园，一个人"

  # 真实模式（需要 MCD_MCP_TOKEN 环境变量；Token 只从环境变量读取）
  export MCD_MCP_TOKEN=xxxxx
  python -m burger_quest quest "想吃辣，预算35" --store-code DEMO-001

  # 查看引擎使用的 MCP 工具清单
  python -m burger_quest tools
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import sys
from pathlib import Path

from . import __version__
from .mcp_base import MCPError, TOOLS_RESERVED, TOOLS_USED
from .service import run_quest


def _print_result(result, card_html: Path = None, card_md: Path = None, json_out: Path = None) -> int:
    c = result.constraints
    print("=" * 62)
    print(f"🍔 {result.quest_name or '任务失败'}")
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
    if result.warnings:
        print("\n【提示】")
        for w in result.warnings:
            print("  ⚠️ " + w)
    if result.chosen is None:
        print("\n❌ 未生成方案。原因见上方提示——引擎不会编造餐品、价格或优惠。")
    else:
        print("\n【数据来源】")
        for k, v in result.data_sources.items():
            if isinstance(v, dict):
                v = json.dumps(v, ensure_ascii=False)
            print(f"  - {k}: {v}")
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
                    "nutrition": o.nutrition,
                }
                for o in result.options
            ],
            "warnings": result.warnings,
            "data_sources": result.data_sources,
        }
        json_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"🧾 结构化结果(JSON) 已保存：{json_out}")
    return 0 if result.chosen is not None else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="burger-quest", description=__doc__)
    parser.add_argument("--version", action="version", version=f"burger-quest {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_common(p, with_text=True):
        if with_text:
            p.add_argument("text", help="用户自然语言需求，如：想吃辣，预算35，外送到南山科技园")
        p.add_argument("--store-code", default=None, help="指定门店编码（跳过附近门店查询）")
        p.add_argument("--fixture", default=None, help="演示数据 JSON 路径（默认 fixtures/demo_data.json）")
        p.add_argument("--card-html", type=Path, default=None, help="分享卡 HTML 输出路径")
        p.add_argument("--card-md", type=Path, default=None, help="分享卡 Markdown 输出路径")
        p.add_argument("--json", type=Path, default=None, help="结构化结果 JSON 输出路径")

    p_demo = sub.add_parser("demo", help="演示模式：离线 Mock 数据")
    add_common(p_demo)
    p_quest = sub.add_parser("quest", help="真实模式：调用麦当劳 MCP（需 MCD_MCP_TOKEN）")
    add_common(p_quest)
    sub.add_parser("tools", help="列出引擎使用的 MCP 工具")

    args = parser.parse_args(argv)

    if args.cmd == "tools":
        print("引擎实际调用的麦当劳 MCP 工具：")
        for t in TOOLS_USED:
            print(f"  - {t}")
        print("\n预留但 MVP 不主动调用（涉及真实资金/库存操作）：")
        for t in TOOLS_RESERVED:
            print(f"  - {t}")
        return 0

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if args.cmd == "demo":
        from .mcp_mock import MockMCPClient

        client = MockMCPClient(fixture_path=args.fixture)
        result = run_quest(args.text, client, store_code=args.store_code, generated_at=now)
        return _print_result(result, args.card_html, args.card_md, args.json)

    if args.cmd == "quest":
        from .mcp_http import HttpMCPClient

        async def _run():
            try:
                async with HttpMCPClient() as client:
                    return run_quest(args.text, client, store_code=args.store_code, generated_at=now)
            except MCPError as e:
                print(f"❌ MCP 连接失败：{e}")
                return None

        result = asyncio.run(_run())
        if result is None:
            return 2
        return _print_result(result, args.card_html, args.card_md, args.json)

    return 1


if __name__ == "__main__":
    sys.exit(main())
