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

import sys


def _ensure_utf8_output() -> None:
    """把标准输出/错误切到 UTF-8（errors=replace），防止窄代码页控制台崩溃。

    背景（2026-10-16 Windows CI 实测）：GitHub Actions 的 Windows runner 默认
    cp1252 代码页，print 含 emoji（🩺/🍔/⚔️）或中文时直接
    UnicodeEncodeError 崩溃（Linux/macOS 与本地 GBK 管道都不暴露此问题）。
    reconfigure 后最坏情况是控制台显示替代表情，但绝不崩溃、退出码语义不变。
    """
    for stream in (sys.stdout, sys.stderr):
        # reconfigure 只存在于 TextIOWrapper；被替换/管道捕获的场景可能没有，
        # 用 getattr 探测而非 isinstance（子类形态太多），调用失败静默跳过
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            with suppress(Exception):
                reconfigure(encoding="utf-8", errors="replace")  # Python 3.7+


import argparse
import datetime
import json
import os
from contextlib import suppress
from pathlib import Path

from . import __version__
from .mcp_base import MCPClient, MCPError, TOOLS_RESERVED, TOOLS_USED, summarize_calls
from .models import format_yuan
from .service import run_quest


def _print_result(
    result,
    card_html: Path | None = None,
    card_md: Path | None = None,
    json_out: Path | None = None,
    mode: str = "quest",
) -> int:
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
        for line in rpg.price_scope_lines(result.store, c):
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
            client: MCPClient = MockMCPClient(fixture_path=args.fixture)
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


def _cmd_detail(args) -> int:
    """套餐装备：展示官方返回的套餐组成/可替换项/特制选项；可按单品组合重配算价。

    边界：官方 calculate-price 未公开「套餐替换」参数，本命令**不做套餐改价**；
    --reconfigure 是把套餐内默认项当单品组合提交官方算价，与套餐价可能不同。
    """
    from . import mealconfig
    from .mcp_base import scene_for_mode
    from .models import MenuItem, PlanItem

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    client: MCPClient
    if args.demo:
        from .mcp_mock import MockMCPClient

        client = MockMCPClient()
        source = "演示数据（MockMCPClient，非实时）"
    else:
        from .mcp_http import HttpMCPClient

        client = HttpMCPClient()
        source = "麦当劳官方 MCP（实时）"

    replacements: dict[int, str] = {}
    if args.reconfigure:
        for pair in args.reconfigure.split(","):
            if not pair.strip():
                continue
            if "=" not in pair:
                print(f"❌ --reconfigure 格式错误：{pair!r}（应为 组序号=选项编码）")
                return 2
            gid, code = pair.split("=", 1)
            try:
                replacements[int(gid.strip())] = code.strip()
            except ValueError:
                print(f"❌ --reconfigure 组序号不是整数：{gid!r}")
                return 2

    try:
        with client:
            scene = scene_for_mode("takeaway", args.store_code)
            detail = client.meal_detail(scene, args.code)
            groups = mealconfig.parse_detail(detail)
            if not groups:
                print(f"ℹ️ 餐品 {args.code} 未返回套餐组成（可能是单品，或官方结构变化）。")
                print(f"   名称：{detail.get('name') or '（官方未返回）'}  来源：{source}")
                return 0

            print(f"🍔 套餐装备 · {detail.get('name') or '（官方未返回名称）'}（{args.code}）")
            print(f"   门店 {args.store_code} · 生成于 {now} · 来源：{source}")
            print("   说明：以下组成/可替换项/特制选项均来自官方 query-meal-detail 返回\n")

            print("【套餐组成】")
            for g in groups:
                print(f" {g.index}. {g.name} ×{g.quantity}")
                for c in g.choices:
                    mark = "（默认）" if c.is_default else ""
                    diff = f"，{c.diff_text}" if c.diff_text else ""
                    mod = "，可替换" if c.modifiable else ""
                    print(f"    · {c.name}（{c.code}）{mark}{diff}{mod}")
                    for item in c.modifications:
                        names = " / ".join(
                            f"{v['name']}{'（已选）' if v.get('selected_quantity') else ''}"
                            for v in item.get("values") or []
                        )
                        if names:
                            print(f"        特制选项（仅展示，不参与算价）：{names}")
            print()

            # 套餐参考价（官方算价）
            combo_quote = client.calculate_price(
                scene, [PlanItem(MenuItem(code=args.code, name=detail.get("name") or "", category=""), 1)], [],
                purpose="meal_detail_combo_price",
            )
            if combo_quote.ok and combo_quote.total_cents is not None:
                print(f"【套餐参考价】应付 {format_yuan(combo_quote.total_cents)}（calculate-price 权威结果）")
            else:
                print("【套餐参考价】官方算价未返回可用总价 —— 明确失败，不估算。")

            if replacements:
                items, notes = mealconfig.apply_replacements(groups, replacements)
                print("\n【重配（按单品组合）】")
                for n in notes:
                    print(f"  · {n}")
                if not items:
                    print("  ⚠️ 重配后没有可用单品，未发起算价。")
                    return 1
                plan_items = [
                    PlanItem(MenuItem(code=code, name=name, category=""), qty) for code, name, qty in items
                ]
                quote = client.calculate_price(scene, plan_items, [], purpose="meal_reconfigure_price")
                combo_desc = " + ".join(f"{name}×{qty}" for _code, name, qty in items)
                print(f"  单品组合：{combo_desc}")
                if quote.ok and quote.total_cents is not None:
                    print(f"  官方算价：应付 {format_yuan(quote.total_cents)}（calculate-price 权威结果）")
                    print(
                        "  ⚠️ 说明：官方算价接口未公开「套餐替换」参数，本价格是**单品组合**的官方报价，"
                        "不是套餐改价；两者可能不同，请以官方点餐页最终价为准。"
                    )
                else:
                    print(f"  ❌ 官方算价未返回可用总价：{quote.message or '未知原因'}（不估算、不用旧价冒充）")
                    return 1
    except MCPError as e:
        print(f"❌ {e}")
        return 2

    if args.json:
        payload = {
            "generated_at": now,
            "source": source,
            "code": args.code,
            "name": detail.get("name"),
            "store_code": args.store_code,
            "groups": [
                {
                    "index": g.index,
                    "name": g.name,
                    "quantity": g.quantity,
                    "choices": [
                        {
                            "code": c.code,
                            "name": c.name,
                            "is_default": c.is_default,
                            "diff_text": c.diff_text,
                            "modifiable": c.modifiable,
                            "modifications": c.modifications,
                        }
                        for c in g.choices
                    ],
                }
                for g in groups
            ],
            "combo_price_ok": combo_quote.ok,
            "combo_price_cents": combo_quote.total_cents,
            "reconfigure": args.reconfigure,
        }
        Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nJSON 已写入：{args.json}")
    return 0


def _cmd_campaign(args) -> int:
    """麦门限时委托：只读查询官方活动日历（不报名、不预约、不领奖）。"""
    from . import campaign as campaign_mod

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    client: MCPClient
    if args.demo:
        from .mcp_mock import MockMCPClient

        client = MockMCPClient()
        source_note = "演示数据（MockMCPClient，非实时活动）"
    else:
        from .mcp_http import HttpMCPClient

        client = HttpMCPClient()
        source_note = "麦当劳官方 campaign-calendar（实时）"

    try:
        with client:
            try:
                items = client.campaign_calendar(args.date)
            except MCPError as e:
                print(f"❌ 活动日历不可用：{e}")
                print("   降级说明：无法获取官方活动，本次不展示任何活动（不编造）。")
                return 2
    except MCPError as e:
        print(f"❌ {e}")
        return 2

    if not items:
        print(f"ℹ️ 官方活动日历未返回活动（来源：{source_note}）。")
        print("   可能是当月暂无活动或接口字段变化；可用 --date yyyy-MM-dd 指定基准日重试。")
        return 0

    counts = campaign_mod.summarize(items)
    print(f"🍟 麦门限时委托 · 来源：{source_note}")
    print(f"   生成时间 {now} · 进行中 {counts['live']} · 未来 {counts['upcoming']} · 往期 {counts['past']}")
    print("   说明：查看活动 ≠ 参加活动；第一阶段不报名、不预约、不创建订单、不领取奖励。\n")

    for label, key in (("进行中", "live"), ("未来", "upcoming"), ("往期", "past")):
        subset = [c for c in items if c.status == key]
        if not subset:
            continue
        print(f"【{label}】")
        for c in subset:
            print(f"  · {c.date}  {c.title}")
            if c.content:
                first = c.content.splitlines()[0].strip()
                if first:
                    print(f"    {first[:80]}")
        print()

    if args.json:
        payload = {
            "generated_at": now,
            "source": "campaign-calendar",
            "source_note": source_note,
            "readonly": True,
            "counts": counts,
            "campaigns": [
                {
                    "date": c.date,
                    "title": c.title,
                    "content": c.content,
                    "image_url": c.image_url,
                    "status": c.status,
                }
                for c in items
            ],
        }
        Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"JSON 已写入：{args.json}")
    return 0


def _cmd_coupons(args) -> int:
    """优惠券猎人：三类券分开查询展示（全部只读，不领取）。"""
    from .mcp_base import scene_for_mode

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    client: MCPClient
    if args.demo:
        from .mcp_mock import MockMCPClient

        client = MockMCPClient()
        source_note = "演示数据（MockMCPClient，非实时）"
    else:
        from .mcp_http import HttpMCPClient

        client = HttpMCPClient()
        source_note = "麦当劳官方 MCP（实时）"

    sections: list[dict] = []
    try:
        with client:
            # A. 可领取（available-coupons）——只查不领
            try:
                available = client.available_coupons()
                sections.append({"name": "可领取（麦麦省）", "state": "ok", "items": available, "note": ""})
            except MCPError as e:
                sections.append({"name": "可领取（麦麦省）", "state": "unknown", "items": [], "note": str(e)})

            # B. 我的卡包
            try:
                mine = client.my_coupons()
                sections.append({"name": "我的卡包", "state": "ok", "items": mine, "note": ""})
            except MCPError as e:
                sections.append({"name": "我的卡包", "state": "unknown", "items": [], "note": str(e)})

            # C. 门店适用（可选，需门店编码）
            if args.store_code:
                try:
                    store_items = client.store_coupons(scene_for_mode("takeaway", args.store_code))
                    sections.append({"name": f"门店 {args.store_code} 适用", "state": "ok", "items": store_items, "note": ""})
                except MCPError as e:
                    sections.append({"name": f"门店 {args.store_code} 适用", "state": "unknown", "items": [], "note": str(e)})
            else:
                sections.append({
                    "name": "门店适用",
                    "state": "skipped",
                    "items": [],
                    "note": "未提供 --store-code，未查询（避免误把「卡包券」当成「该门店可用券」）",
                })
    except MCPError as e:
        print(f"❌ {e}")
        return 2

    print(f"🎟️ 优惠券猎人 · 来源：{source_note}")
    print(f"   生成时间 {now} · 全部只读：不领取、不绑定、不修改账户\n")

    for s in sections:
        print(f"【{s['name']}】")
        if s["state"] == "unknown":
            print(f"  ⚠️ 状态未知：{s['note'][:160]}")
        elif s["state"] == "skipped":
            print(f"  ℹ️ {s['note']}")
        elif not s["items"]:
            print("  （官方返回为空——不排除有券，只是本次查询未返回）")
        else:
            for c in s["items"]:
                bits = [c.name or c.id]
                if c.threshold_cents:
                    bits.append(f"门槛 ¥{c.threshold_cents / 100:.2f}")
                # 有效期字段官方未在所有券种返回：缺失时明确标「未知」，不猜
                expires = c.raw.get("expiresAt") or c.raw.get("expireTime") or c.raw.get("validEndTime")
                bits.append(f"有效期至 {expires}" if expires else "有效期未知")
                print("  · " + " · ".join(bits))
        print()

    if args.json:
        payload = {
            "generated_at": now,
            "source": "available-coupons / query-my-coupons / query-store-coupons",
            "source_note": source_note,
            "readonly": True,
            "store_code": args.store_code,
            "sections": [
                {
                    "name": s["name"],
                    "state": s["state"],
                    "note": s["note"],
                    "coupons": [
                        {
                            "id": c.id,
                            "name": c.name,
                            "threshold_cents": c.threshold_cents,
                            "value_cents": c.value_cents,
                            "percent": c.percent,
                            "expires_at": c.raw.get("expiresAt") or c.raw.get("expireTime") or c.raw.get("validEndTime"),
                            "source": c.source,
                        }
                        for c in s["items"]
                    ],
                }
                for s in sections
            ],
        }
        Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"JSON 已写入：{args.json}")
    return 0


def _resolve_progress_path(args) -> str:
    """解析 --progress：裸旗标读环境变量；返回空 = 不启用（默认不写文件）。"""
    value = getattr(args, "progress", None)
    if not value:
        return ""
    if value == "__ENV__":
        return os.environ.get("BURGER_QUEST_PROGRESS", "")
    return str(value)


def _print_progress_section(args, result, demo: bool) -> None:
    """【冒险者档案】：本地虚拟进度结算（成就/经验/剧情解锁）。

    安全边界：仅在显式提供进度路径时写文件；文件只含经验、成就 id、
    签名哈希与门店编码（无个人信息、无 Token）。进度为应用内虚拟进度，
    与麦当劳官方无关。
    """
    from . import lore
    from . import progress as progress_mod

    path = _resolve_progress_path(args)
    print("\n【冒险者档案】（本地虚拟进度 · 与麦当劳官方无关）")
    if not path:
        print("  " + lore.state_copy("progress_off"))
        return
    prog, notes = progress_mod.load_progress(path)
    for note in notes:
        print(f"  ⚠️ {note}")
    prog, run, events = progress_mod.apply_progress(
        prog,
        result,
        demo=demo,
        used_json=bool(getattr(args, "json", None)),
        card_generated=bool(result.card_html and (getattr(args, "card_html", None) or getattr(args, "card_md", None))),
    )
    progress_mod.save_progress(path, prog)
    level, title = prog.level()
    for line in events:
        print("  " + line)
    from .achievements import ACHIEVEMENTS
    from . import lore as _lore

    unlocked = prog.unlocked_ids()
    print(f"  📊 等级 LV.{level} {title} · 累计经验 {prog.xp} · 成就 {len(unlocked)}/{len(ACHIEVEMENTS)} · 据点 {len(prog.stores)} 个")
    chapters = _lore.unlocked_chapters(unlocked)
    if chapters:
        print("  📚 剧情进度：" + "、".join(c.name for c in chapters))
    else:
        print("  📚 剧情进度：尚未开启（完成探索任务以解锁章节）")
    print(f"  {lore.state_copy('progress_hint')}（存档：{path}；仅含虚拟进度，无个人信息）")


def _print_achievement_gallery(args) -> int:
    """progress 子命令：进度查看 + 成就图鉴 + 剧情章节一览。"""
    from . import lore
    from . import progress as progress_mod
    from .achievements import ACHIEVEMENTS

    path = _resolve_progress_path(args) or getattr(args, "progress", None) or ""
    print("🏆 Burger Quest 冒险者档案（本地虚拟进度 · 与麦当劳官方无关）")
    if not path:
        print(lore.state_copy("progress_off"))
        for line in lore_format_gallery(set()):
            print(line)
        return 0
    prog, notes = progress_mod.load_progress(path)
    for note in notes:
        print(f"⚠️ {note}")
    level, title = prog.level()
    unlocked = prog.unlocked_ids()
    print(f"📊 LV.{level} {title} · 累计经验 {prog.xp} · 成就 {len(unlocked)}/{len(ACHIEVEMENTS)} · 据点 {len(prog.stores)} 个")
    for line in lore_format_gallery(unlocked):
        print(line)
    print("\n📚 剧情章节（随真实探索逐步解锁）：")
    for chapter in lore.CHAPTERS:
        mark = "✅" if chapter.is_unlocked(unlocked) else "🔒"
        print(f"  {mark} {chapter.name}——{chapter.summary}")
    print(f"\n存档：{path}（仅含虚拟进度：经验、成就 id、签名哈希、门店编码；无个人信息）")
    return 0


def lore_format_gallery(unlocked: set) -> list:
    from .achievements import format_achievement_list

    return format_achievement_list(unlocked)


def main(argv=None) -> int:
    _ensure_utf8_output()
    parser = argparse.ArgumentParser(
        prog="burger-quest",
        description="Burger Quest · 麦门异世界 —— 基于麦当劳中国官方 MCP 的游戏化点餐规划",
        epilog=(
            "示例：\n"
            '  python -m burger_quest demo "我想吃辣，预算35，外送到南山科技园"\n'
            '  python -m burger_quest quest "我想吃辣，预算35，外送到南山科技园"\n'
            '  python -m burger_quest stores "南山科技园"\n'
            "  python -m burger_quest web              # 本地 Web 界面（默认仅本机 127.0.0.1:8787）\n"
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
            "--progress",
            nargs="?",
            const="__ENV__",
            default=None,
            help=(
                "启用本地虚拟进度（成就/经验/剧情）：--progress <文件路径>；"
                "裸 --progress 则读环境变量 BURGER_QUEST_PROGRESS。默认不写任何文件。"
            ),
        )
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

    p_prog = sub.add_parser(
        "progress",
        help="查看本地虚拟进度与成就图鉴（--progress 指定存档；未指定只显示图鉴）",
        description=(
            "查看本地虚拟进度（经验/等级/成就/剧情章节）。\n"
            "进度只在显式提供 --progress <路径>（或环境变量 BURGER_QUEST_PROGRESS）时读写；\n"
            "存档仅含虚拟进度数据（经验、成就 id、签名哈希、门店编码），无个人信息。\n"
            "本地进度与麦当劳官方账户无关，不做任何同步。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_prog.add_argument("--progress", default=None, help="进度文件路径（也可用环境变量 BURGER_QUEST_PROGRESS）")

    p_web = sub.add_parser(
        "web",
        help="本地 Web 界面（默认仅监听 127.0.0.1；Token 只在服务端，不下发浏览器）",
        description=(
            "启动本地 Web 界面（不熟悉命令行的用户首选）。\n"
            "- 默认只监听 http://127.0.0.1:8787 ——仅本机可访问；\n"
            "- Token 只从环境变量 MCD_MCP_TOKEN 或 .env 读取（服务端），绝不下发浏览器；\n"
            "- 真实查询失败会明确报错，不会自动切换演示数据；\n"
            "- --allow-lan 才监听其它网卡（局域网设备可访问，请评估风险）；\n"
            "- 停止：Ctrl+C。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_web.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1，仅本机）")
    p_web.add_argument("--port", type=int, default=8787, help="监听端口（默认 8787）")
    p_web.add_argument("--progress", default=None, help="本地虚拟进度存档路径（可选；不传则不记录）")
    p_web.add_argument("--allow-lan", action="store_true", help="允许监听非 localhost 地址（局域网暴露风险自担）")

    p_doctor = sub.add_parser(
        "doctor",
        help="真实 MCP 连接诊断：L1 服务可访问（无需 Token）/认证/工具发现/只读调用（缺 Token 直接失败，不回退 Demo）",        description=(
            "真实 MCP 连接诊断（只读）：Python 版本 -> 官方 mcp SDK -> L1 服务可访问（HTTP 探测，无需 Token）\n"
            "-> MCD_MCP_TOKEN 存在性 -> L2 认证（MCP 初始化）-> L3 工具发现 -> L4 一次只读工具调用。\n"
            "每步 PASS/FAIL + 原因 + 下一步建议；末尾输出五层验证状态汇总（L5 由 smoke/quest 覆盖）。\n"
            "失败分类：TOKEN_MISSING / 401 / 403 / 429 / NETWORK / TIMEOUT_CONNECT / TIMEOUT_READ / "
            "RESPONSE / SDK / TOOL_MISSING / BUSINESS。Token 只查存在性，绝不打印值。\n"
            "退出码：0=全部通过；1=有失败；2=配置错误（缺 Token/缺 SDK/Python 版本不符）。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_doctor.add_argument("--json", default=None, help="诊断结果 JSON 输出路径（不含任何敏感值）")

    p_detail = sub.add_parser(
        "detail",
        help="套餐装备：查看官方返回的套餐组成/可替换项（只读）；--reconfigure 按单品组合重算价",
        description=(
            "查看套餐真实组成、可替换项与特制选项（query-meal-detail，只读）。\n"
            "- 组成/选项/差价全部来自官方返回，不派生未返回的选项；\n"
            "- 特制选项（去冰/加酱等）仅展示：官方算价接口未公开替换参数，不伪造改价；\n"
            "- --reconfigure 把套餐默认项当「单品组合」提交 calculate-price，得到官方单品报价\n"
            "  （与套餐价可能不同，输出会明确标注）。\n"
            "退出码：0=成功；1=算价失败；2=配置/参数错误。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_detail.add_argument("code", help="餐品编码（套餐或单品）")
    p_detail.add_argument("--store-code", required=True, help="门店编码（详情查询需要门店上下文）")
    p_detail.add_argument("--reconfigure", default=None, help='替换项，如 "4=3071"（组序号=选项编码，逗号分隔）')
    p_detail.add_argument("--json", default=None, help="结果 JSON 输出路径")
    p_detail.add_argument("--demo", action="store_true", help="离线演示（明确标注演示数据）")

    p_campaign = sub.add_parser(
        "campaign",
        help="麦门限时委托：查询官方活动日历（只读；不报名、不领奖）",
        description=(
            "查询麦当劳中国当月营销活动日历（campaign-calendar，只读）。\n"
            "- 展示官方返回的活动名称、日期、状态（进行中/未来/往期）与来源；\n"
            "- 只读：不报名、不预约、不创建订单、不领取任何奖励；\n"
            "- 时间或活动接口不可用时明确降级，不编造活动。\n"
            "退出码：0=成功；2=配置/连接错误（如缺 Token）。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_campaign.add_argument("--date", default=None, help="基准日期 yyyy-MM-dd（官方返回该日前后各一个有活动的日子）")
    p_campaign.add_argument("--json", default=None, help="结果 JSON 输出路径")
    p_campaign.add_argument("--demo", action="store_true", help="离线演示（结果明确标注为演示数据）")

    p_coupons = sub.add_parser(
        "coupons",
        help="优惠券猎人：三类券分开查询展示（可领取 / 我的卡包 / 门店适用；只读，不领取）",
        description=(
            "优惠券猎人（全部只读）：\n"
            "- available-coupons：当前可领取的麦麦省券（**不领取**，不调用 auto-bind-coupons）；\n"
            "- query-my-coupons：我的卡包券；\n"
            "- query-store-coupons：指定门店适用券（需 --store-code）；\n"
            "- 三类状态分开标注，绝不混为一谈；字段缺失时明确「未知」。\n"
            "退出码：0=成功；2=配置/连接错误（如缺 Token）。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_coupons.add_argument("--store-code", default=None, help="门店编码（查询该门店适用券时需要）")
    p_coupons.add_argument("--json", default=None, help="结果 JSON 输出路径")
    p_coupons.add_argument("--demo", action="store_true", help="离线演示（结果明确标注为演示数据）")

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

    if args.cmd == "progress":
        return _print_achievement_gallery(args)

    if args.cmd == "web":
        from . import webui

        return webui.serve(
            host=args.host, port=args.port, progress_path=args.progress, allow_lan=args.allow_lan
        )

    if args.cmd == "stores":
        return _cmd_stores(args)

    if args.cmd == "campaign":
        return _cmd_campaign(args)

    if args.cmd == "detail":
        return _cmd_detail(args)

    if args.cmd == "coupons":
        return _cmd_coupons(args)

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
            client: MCPClient = MockMCPClient(fixture_path=args.fixture)
        except MCPError as e:
            print(f"❌ {e}")
            return 2
        result = run_quest(
            args.text, client, store_code=args.store_code, generated_at=now,
            card_snapshot=args.card_snapshot,
        )
        rc = _print_result(result, args.card_html, args.card_md, args.json, mode="demo")
        _print_progress_section(args, result, demo=True)
        return rc

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
        rc = _print_result(result, args.card_html, args.card_md, args.json, mode="quest")
        _print_progress_section(args, result, demo=False)
        return rc

    return 1


if __name__ == "__main__":
    sys.exit(main())
