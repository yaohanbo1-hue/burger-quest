"""基线测量：单次任务的 MCP 调用次数、分工具拆解与耗时。

用法：
  python tools/measure_calls.py                 # demo 模式（离线）
  MCD_MCP_TOKEN=xxx python tools/measure_calls.py --real --store-code 1420762
                                                    --text "预算35，想吃辣，两个人"

只读测量：不修改任何项目代码行为，仅在客户端方法外层包计时/计数。
Token 只从环境变量读，绝不打印。
"""
from __future__ import annotations

import argparse
import collections
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from burger_quest.mcp_base import MCPClient  # noqa: E402
from burger_quest.service import run_quest  # noqa: E402

# 客户端方法 -> 官方工具名（用于分工具统计）
METHOD_TO_TOOL = {
    "now_time": "now-time-info",
    "nearby_stores": "query-nearby-stores",
    "delivery_addresses": "delivery-query-addresses",
    "delivery_stores": "delivery-query-stores",
    "query_meals": "query-meals",
    "meal_detail": "query-meal-detail",
    "store_coupons": "query-store-coupons",
    "my_coupons": "query-my-coupons",
    "list_nutrition_foods": "list-nutrition-foods",
    "calculate_price": "calculate-price",
    "list_tools": "list_tools",
}


def instrument(client: MCPClient):
    """给客户端所有工具方法包上计时/计数（原地包装，不影响行为）。"""
    stats = collections.OrderedDict()  # tool -> {calls, total_ms, max_ms, fails}
    seq = 0

    def wrap(name, tool, fn):
        def inner(*a, **kw):
            nonlocal seq
            seq += 1
            t0 = time.perf_counter()
            try:
                return fn(*a, **kw)
            except Exception:
                s = stats.setdefault(tool, {"calls": 0, "total_ms": 0.0, "max_ms": 0.0, "fails": 0})
                s["calls"] += 1
                s["fails"] += 1
                raise
            finally:
                ms = (time.perf_counter() - t0) * 1000
                s = stats.setdefault(tool, {"calls": 0, "total_ms": 0.0, "max_ms": 0.0, "fails": 0})
                s["calls"] += 1
                s["total_ms"] += ms
                s["max_ms"] = max(s["max_ms"], ms)

        return inner

    for method, tool in METHOD_TO_TOOL.items():
        if hasattr(client, method):
            setattr(client, method, wrap(method, tool, getattr(client, method)))
    return stats


def report(stats, wall_ms, label):
    total_calls = sum(s["calls"] for s in stats.values())
    print(f"\n===== {label} =====")
    print(f"总调用次数: {total_calls}    任务总耗时: {wall_ms:.0f} ms")
    print(f"{'工具':<28}{'次数':>4}{'失败':>4}{'总耗时ms':>10}{'最大ms':>9}")
    for tool, s in stats.items():
        print(f"{tool:<28}{s['calls']:>4}{s['fails']:>4}{s['total_ms']:>10.0f}{s['max_ms']:>9.0f}")
    return total_calls


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", action="store_true", help="真实 MCP 模式（需 MCD_MCP_TOKEN）")
    ap.add_argument("--text", default="预算 35 元，想吃辣，两个人，希望尽量高蛋白，不想点太多饮料")
    ap.add_argument("--store-code", default=None)
    args = ap.parse_args()

    if args.real:
        from burger_quest.mcp_http import HttpMCPClient

        client = HttpMCPClient()
        client.connect()
        label = "真实 MCP"
    else:
        from burger_quest.mcp_mock import MockMCPClient

        client = MockMCPClient()
        label = "演示模式（Mock）"

    stats = instrument(client)
    t0 = time.perf_counter()
    result = run_quest(args.text, client, store_code=args.store_code, generated_at="baseline")
    wall_ms = (time.perf_counter() - t0) * 1000
    client.close()

    n = report(stats, wall_ms, label)
    print(f"方案数: {len(result.options)}    选中: {result.chosen.item_names() if result.chosen else '无'}")
    if result.chosen:
        print(f"权威总价: ¥{result.chosen.total_cents / 100:.2f}")
    print(f"\n结论: 单任务 {n} 次 MCP 调用")


if __name__ == "__main__":
    main()
