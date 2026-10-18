"""只读 MCP 冒烟测试：验证「连接 -> 工具发现 -> 关键查询」整条链路可用。

安全约定（硬约束）：
- Token 只从环境变量 MCD_MCP_TOKEN 读取；本模块绝不打印 Token，也不接受
  Token 作为命令行参数（避免落入 shell history / 进程列表）；
- 只调用只读工具：list_tools / now-time-info / query-nearby-stores /
  query-meals / query-meal-detail / calculate-price。
  calculate-price 是价格计算（不产生订单），属只读；
- 绝不调用 create-order / cancel-order / draw-lottery / auto-bind-coupons 等
  任何写操作或资金操作。

用法：
  export MCD_MCP_TOKEN="你的Token"
  python -m burger_quest smoke                          # 默认用深圳市南山区科技园探测
  python -m burger_quest smoke --location "北京市朝阳区"
  python -m burger_quest smoke --store-code 1420762     # 跳过门店查询，直接指定
  python -m burger_quest smoke --demo                   # 离线演示模式（可重复，无需 Token）
  python -m burger_quest smoke --json result.json       # 机器可读结果

退出码：0=全部通过；1=有步骤失败；2=配置错误（如缺少 Token / 演示数据缺失）。
"""
from __future__ import annotations

import argparse
import json
import sys

from .mcp_base import MCPClient, MCPError, TOOLS_USED, scene_for_mode
from .models import MenuItem, PlanItem

# 冒烟测试允许调用的工具（全部只读）。写操作工具出现在审计记录里即视为安全回归。
READONLY_TOOLS = frozenset(
    {
        "list_tools",
        "now-time-info",
        "query-nearby-stores",
        "query-meals",
        "query-meal-detail",
        "query-store-coupons",
        "query-my-coupons",
        "list-nutrition-foods",
        "calculate-price",
        "delivery-query-addresses",
    }
)

DEFAULT_LOCATION = "深圳市南山区科技园"


class SmokeReport:
    """收集每一步的通过/失败，最后统一输出（绝不中途静默）。"""

    def __init__(self) -> None:
        self.steps: list[dict] = []

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.steps.append({"step": name, "ok": bool(ok), "detail": detail})
        mark = "✅ PASS" if ok else "❌ FAIL"
        print(f"  {mark}  {name}" + (f"  —— {detail}" if detail else ""))

    @property
    def passed(self) -> bool:
        return all(s["ok"] for s in self.steps)

    @property
    def failed_count(self) -> int:
        return sum(1 for s in self.steps if not s["ok"])

    def to_dict(self) -> dict:
        return {"passed": self.passed, "failed": self.failed_count, "steps": self.steps}


def _split_city(location: str) -> tuple[str, str]:
    """「深圳市南山区科技园」-> ("深圳市", "南山区科技园")（与 service._split_city 同规则）。"""
    loc = (location or "").strip()
    if "市" in loc:
        i = loc.find("市")
        return loc[: i + 1], (loc[i + 1 :].strip() or loc[: i + 1])
    return "", loc


def run_smoke(client: MCPClient, location: str = DEFAULT_LOCATION, store_code: str = "") -> SmokeReport:
    """对未连接的客户端执行八步只读冒烟。任何一步失败都记录并继续（汇总报告，不中途静默）。"""
    report = SmokeReport()

    # 1. 连接（会话建立本身就是第一个检查点）
    try:
        client.connect()
        report.add("连接 MCP Server", True, f"{type(client).__name__} 会话已建立")
    except MCPError as e:
        report.add("连接 MCP Server", False, str(e))
        return report

    # 2. 工具发现：项目依赖的 8 个工具必须都在官方清单里
    try:
        tools = client.list_tools()
        names = {t.get("name") for t in tools}
        missing = [t for t in TOOLS_USED if t not in names]
        if missing:
            report.add("工具发现 list_tools", False, f"官方清单缺少项目依赖工具：{missing}")
        else:
            report.add("工具发现 list_tools", True, f"发现 {len(names)} 个工具，项目使用的 {len(TOOLS_USED)} 个全部在位")
    except MCPError as e:
        report.add("工具发现 list_tools", False, str(e))

    # 3. 服务器时间
    try:
        t = client.now_time()
        dt = t.get("datetime", "")
        if dt:
            report.add("now-time-info", True, f"服务器时间 {dt}")
        else:
            report.add("now-time-info", False, "返回为空（未解析到 datetime）")
    except MCPError as e:
        report.add("now-time-info", False, str(e))

    # 4. 门店：--store-code 直达；否则按位置查询（多候选时冒烟取第一家并明示）
    scene = None
    store_candidates: list = []  # 候选门店（菜单失败时有界回退，如打烊）
    try:
        if store_code:
            scene = scene_for_mode("takeaway", store_code)
            report.add("query-nearby-stores", True, f"已跳过查询，使用指定门店编码 {store_code}")
        else:
            city, keyword = _split_city(location)
            stores = client.nearby_stores(keyword, city=city, be_type=1)
            if not stores:
                report.add("query-nearby-stores", False, f"「{location}」未返回门店（换 --location 或 --store-code 重试）")
            else:
                store_candidates = list(stores)
                scene = scene_for_mode("takeaway", stores[0].code)
                if len(stores) > 1:
                    report.add(
                        "query-nearby-stores",
                        True,
                        f"「{location}」返回 {len(stores)} 家候选门店（冒烟取第一家 {stores[0].code}；"
                        "正式使用请用 --store-code 指定）",
                    )
                else:
                    report.add("query-nearby-stores", True, f"「{location}」-> {stores[0].name}（编码 {stores[0].code}）")
    except MCPError as e:
        report.add("query-nearby-stores", False, str(e))

    # 5. 菜单骨架（候选门店有界回退：首家可能打烊/停止营业，最多再试 2 家）
    menu = []
    if scene is not None:
        attempts = [scene] + [
            scene_for_mode("takeaway", s.code) for s in store_candidates[1:3]
        ] if store_candidates else [scene]
        tried: list[str] = []
        last_error = ""
        for attempt_scene in attempts:
            if attempt_scene.store_code in tried:
                continue
            tried.append(attempt_scene.store_code)
            try:
                menu = client.query_meals(attempt_scene)
                if menu:
                    scene = attempt_scene
                    note = f"门店 {scene.store_code} 返回 {len(menu)} 个餐品编码"
                    if len(tried) > 1:
                        note += f"（首家门店不可用，已回退到第 {len(tried)} 家候选）"
                    report.add("query-meals", True, note)
                    break
                last_error = f"门店 {attempt_scene.store_code} 未返回任何餐品"
            except MCPError as e:
                last_error = str(e)
                continue
        else:
            report.add("query-meals", False, last_error or "候选门店均未返回菜单")
    else:
        report.add("query-meals", False, "上一步未确定门店场景，跳过（不编造菜单）")

    # 6. 餐品详情（拿第一个编码的名称）
    # scene 与 menu 同生共死（menu 非空即来自某次 attempt_scene 的成功查询），
    # 显式双条件：既满足类型收敛，也在不变量被破坏时明确跳过而非崩溃
    if menu and scene is not None:
        try:
            code = menu[0].code
            detail = client.meal_detail(scene, code)
            name = detail.get("name", "")
            if name:
                report.add("query-meal-detail", True, f"{code} -> {name}")
            else:
                report.add("query-meal-detail", False, f"{code} 未返回名称")
        except MCPError as e:
            report.add("query-meal-detail", False, str(e))
    else:
        report.add("query-meal-detail", False, "无菜单编码，跳过")

    # 7. 算价（只读：价格计算，不产生订单）
    if menu and scene is not None:
        try:
            item = MenuItem(code=menu[0].code, name="冒烟测试", category=menu[0].category)
            quote = client.calculate_price(scene, [PlanItem(item, 1)], [])
            if quote.ok and quote.total_cents is not None:
                report.add(
                    "calculate-price",
                    True,
                    f"{menu[0].code} 单价 ¥{quote.total_cents / 100:.2f}（金额单位：分）",
                )
            else:
                report.add("calculate-price", False, quote.message or "未返回应付总价")
        except MCPError as e:
            report.add("calculate-price", False, str(e))
    else:
        report.add("calculate-price", False, "无菜单编码，跳过")

    return report


def _print_summary(report: SmokeReport, mode: str) -> None:
    total = len(report.steps)
    if report.passed:
        print(f"\n🎉 冒烟测试全部通过（{total}/{total}，{mode}）。")
    else:
        print(f"\n💥 冒烟测试未通过：{report.failed_count}/{total} 步失败（{mode}）。")
        print("失败步骤已如实列出；请逐项排查，未通过的步骤不影响其它步骤的独立结论。")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="burger-quest smoke",
        description=(
            "只读 MCP 冒烟测试：连接 -> 工具发现 -> 时间/门店/菜单/详情/算价。\n"
            "Token 只从环境变量 MCD_MCP_TOKEN 读取（绝不打印）；\n"
            "只调用只读工具，不调用任何下单/取消/资金操作。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--location", default=DEFAULT_LOCATION, help=f"探测位置（默认「{DEFAULT_LOCATION}」）")
    parser.add_argument("--store-code", default=None, help="直接指定门店编码（跳过门店查询）")
    parser.add_argument("--demo", action="store_true", help="离线演示模式：使用内置演示数据（无需 Token，可重复执行）")
    parser.add_argument("--json", default=None, help="冒烟结果 JSON 输出路径")
    args = parser.parse_args(argv)

    # 构造客户端（不连接）：Token 缺失/演示数据缺失 -> 退出码 2
    if args.demo:
        from .mcp_mock import MockMCPClient

        mode = "demo（离线演示数据）"
        try:
            client: MCPClient = MockMCPClient()
        except MCPError as e:
            print(f"❌ {e}")
            return 2
    else:
        from .mcp_http import HttpMCPClient

        mode = "quest（真实 MCP）"
        try:
            client = HttpMCPClient()
        except MCPError as e:
            print(f"❌ {e}")
            return 2

    print(f"🔍 Burger Quest 只读 MCP 冒烟测试（{mode}）")
    print(f"   目标服务：https://mcp.mcd.cn   探测位置：{args.location}")
    try:
        report = run_smoke(client, location=args.location, store_code=args.store_code or "")
    finally:
        client.close()

    # 安全自检：整个冒烟过程不允许出现写操作工具
    # t is not None：审计记录缺 tool 字段时None 混入会让 sorted 直接 TypeError
    called = {c.get("tool") for c in getattr(client, "calls", []) if isinstance(c, dict)}
    forbidden = sorted(t for t in called if t is not None and t not in READONLY_TOOLS)
    if forbidden:
        report.add("安全自检（仅只读工具）", False, f"出现非只读调用：{forbidden}")
    else:
        report.add("安全自检（仅只读工具）", True, f"共调用 {len(called)} 个工具，全部只读")

    _print_summary(report, mode)

    if args.json:
        payload = report.to_dict()
        payload["mode"] = mode
        payload["location"] = args.location
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"🧾 冒烟结果(JSON) 已保存：{args.json}")

    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
