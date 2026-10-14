"""展示层：分享成就卡渲染（HTML / Markdown）——2026-10-14 视觉升级。

设计方向：复古 RPG 游戏界面 + 麦当劳红黄配色 + 汉堡冒险主题。
硬约束：
- 明确独立项目声明（NON-OFFICIAL FAN PROJECT + 免责声明），不得让用户误认为官方产品；
- 所有用户输入 / MCP 返回文本一律 html.escape，防注入；
- 装饰（像素边框、等级、进度条、图标）只做包装，绝不生成或修改业务数字；
- 营养信息只在真实数据存在时展示，缺失明确显示「未知」，不用装饰性数字填空；
- 总价只认 calculate-price 权威结果；未核验显示「未知」；
- 纯本地 HTML/CSS，无外部 CDN / 字体 / 图片依赖；适配手机与打印。
"""
from __future__ import annotations

import html
from typing import Optional

from .models import Constraints, PlanOption, Store, format_yuan
from . import rpg
from .planner import classify

PROJECT_NAME = "Burger Quest · 麦门异世界"
DISCLAIMER = (
    "非麦当劳官方项目，参赛作品，仅供娱乐参考；餐品信息、价格及供应状态以麦当劳官方渠道实时结果为准。"
)
NON_OFFICIAL_TAG = "NON-OFFICIAL FAN PROJECT"

# 装备分类 -> 图标（本地 emoji 渲染，无外部资源）
GEAR_ICON = {"main": "🍔", "side": "🍟", "drink": "🥤", "dessert": "🍦", "other": "✨"}

# 预算档位 -> 任务等级（纯装饰，与 rpg.BUDGET_TIERS 对齐；不参与任何业务判断）
LEVEL_TIERS = [(2000, 1), (3500, 2), (6000, 3), (10**9, 4)]


def _level_and_tier(c: Constraints) -> tuple[int, str]:
    """(任务等级, 角色称号)。无预算时按 LV.1 / 见习冒险者 展示（装饰性）。"""
    if c.budget_cents is None:
        return 1, "见习冒险者"
    for limit, name in rpg.BUDGET_TIERS:
        if c.budget_cents <= limit:
            tier = name
            break
    else:
        tier = rpg.BUDGET_TIERS[-1][1]
    for limit, lv in LEVEL_TIERS:
        if c.budget_cents <= limit:
            return lv, tier
    return 4, tier


def _mode_badge(demo: bool) -> str:
    if demo:
        return '<span class="mode demo">DEMO · 离线演示数据</span>'
    return '<span class="mode live">LIVE · 麦当劳 MCP 实时数据</span>'


def _gear_rows(option: PlanOption, demo: bool) -> str:
    """装备列表：图标 + 绰号 + 真实名称 + 数量 + 单价 + 可溯源编码。"""
    e = html.escape
    source_text = "演示数据 fixtures（非实时菜单）" if demo else "官方 MCP 返回（query-meals / calculate-price）"
    rows = []
    for pi in option.items:
        cat = classify(pi.item)
        icon = GEAR_ICON.get(cat, "✨")
        epithet = rpg.EPITHET_MAP.get(pi.item.name, rpg.EPITHET_FALLBACK.get(cat, "补给"))
        qty = f'<span class="qty">×{pi.qty}</span>' if pi.qty > 1 else ""
        price = pi.item.price_text()
        code = e(str(pi.item.code))
        rows.append(
            f'<li class="gear-item">'
            f'<span class="g-icon">{icon}</span>'
            f'<span class="g-body">'
            f'<span class="g-name">{e(pi.item.name)}{qty}</span>'
            f'<span class="g-epithet">{e(epithet)}</span>'
            f'<span class="g-code">商品编码 {code} · 来源 {e(source_text)}</span>'
            f"</span>"
            f'<span class="g-price">{e(price)}</span>'
            f"</li>"
        )
    for c in option.coupons:
        coupon_val = (
            f"-{format_yuan(c.value_cents)}" if c.value_cents else ("折扣券" if c.percent else "")
        )
        rows.append(
            f'<li class="gear-item coupon">'
            f'<span class="g-icon">🎟️</span>'
            f'<span class="g-body">'
            f'<span class="g-name">{e(c.name)}</span>'
            f'<span class="g-epithet">券灵加持（已提交官方核验）</span>'
            f'<span class="g-code">券编码 {e(str(c.id))} · 来源 {e("演示数据 fixtures" if demo else "query-store-coupons / query-my-coupons（真实券池）")}</span>'
            f"</span>"
            f'<span class="g-price">{e(coupon_val)}</span>'
            f"</li>"
        )
    return "".join(rows)


def _price_rows(option: PlanOption, c: Constraints) -> str:
    """价格结算四段：原价 / 已核实优惠 / 应付总价（权威）/ 剩余预算。未知不填空。"""
    e = html.escape
    rows = []

    def row(label: str, value: str, cls: str = "") -> str:
        return f'<div class="p-row {cls}"><span>{e(label)}</span><span>{e(value)}</span></div>'

    if option.original_cents is not None:
        rows.append(row("原价合计", format_yuan(option.original_cents)))
    else:
        rows.append(row("原价合计", "未知", "unknown"))
    if option.discount_cents:
        rows.append(row("已核实优惠", f"-{format_yuan(option.discount_cents)}", "discount"))
    elif option.coupons:
        rows.append(row("已核实优惠", "官方核验未产生优惠", "unknown"))
    if option.delivery_fee_cents:
        rows.append(row("配送费", format_yuan(option.delivery_fee_cents)))
    if option.price_checked and option.total_cents is not None:
        rows.append(row("应付总价", format_yuan(option.total_cents), "total"))
    else:
        # 未通过权威核验：明确未知，绝不用菜单价加总冒充
        rows.append(row("应付总价", "未知（未通过 calculate-price 权威核验）", "total unknown"))
    if c.budget_cents is not None and option.remaining_cents is not None:
        if option.remaining_cents >= 0:
            rows.append(row("剩余预算", format_yuan(option.remaining_cents), "remain"))
        else:
            rows.append(row("超出预算", format_yuan(-option.remaining_cents), "over"))
    return "".join(rows)


def _budget_bar(option: PlanOption, c: Constraints) -> str:
    """预算使用率进度条（装饰；宽度由真实总价/预算计算，无预算不显示）。"""
    if c.budget_cents is None or option.total_cents is None or c.budget_cents <= 0:
        return ""
    used = option.total_cents / c.budget_cents
    pct = min(100.0, used * 100)
    over = "over" if used > 1 else ""
    label = f"{used * 100:.0f}%" + (" · 已超支" if used > 1 else "")
    return (
        f'<div class="bar-wrap {over}">'
        f'<div class="bar-label"><span>⚡ 预算使用率</span><span>{html.escape(label)}</span></div>'
        f'<div class="bar"><div class="bar-fill" style="width:{pct:.1f}%"></div></div>'
        f"</div>"
    )


def _nutrition_block(option: PlanOption) -> str:
    """属性面板：只展示 option.nutrition 里已汇总的真实数据；缺失明确显示未知。

    option.nutrition 由 planner 用 list-nutrition-foods 的真实返回汇总而成
    （零覆盖时为 {}）——卡片层不做任何匹配猜测。
    """
    e = html.escape
    n = option.nutrition or {}
    if not n:
        missing = "、".join(option.nutrition_missing) if option.nutrition_missing else "全部餐品"
        return (
            '<div class="stats unknown-stats">'
            '<span class="s-label">🧪 属性面板</span>'
            f'<span class="s-unknown">营养数据未知（{e(missing)} 无官方营养记录——不猜测数值）</span>'
            "</div>"
        )
    cells = []
    for key, label, unit in (
        ("calories_kcal", "热量", "千卡"),
        ("protein_g", "蛋白质", "g"),
        ("fat_g", "脂肪", "g"),
    ):
        v = n.get(key)
        cells.append(
            f'<div class="stat"><span class="s-k">{label}</span>'
            f'<span class="s-v">{e(f"{v:.0f}")}<i>{unit}</i></span></div>'
            if v is not None
            else f'<div class="stat"><span class="s-k">{label}</span><span class="s-v unknown">未知</span></div>'
        )
    note = ""
    if not option.nutrition_complete:
        note = (
            '<div class="stats-note">⚠️ 部分餐品无营养数据（'
            + e("、".join(option.nutrition_missing))
            + "），数值可能偏低——未猜测</div>"
        )
    return '<div class="stats">' + "".join(cells) + note + "</div>"


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ --mc-red:#da291c; --mc-yellow:#ffc72c; --ink:#3a2c1c; --paper:#fffdf8; --line:#e8d9b8; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; padding:20px 12px; background:#2b2118; font-family:"PingFang SC","Microsoft YaHei",system-ui,sans-serif; }}
  .card {{ max-width:680px; margin:0 auto; background:var(--paper); border:3px solid var(--ink);
    box-shadow:8px 8px 0 rgba(0,0,0,.45); overflow:hidden; }}
  /* ---------- 头部：品牌 + 任务标题 ---------- */
  .head {{ background:var(--mc-red); color:#fff; padding:18px 20px 16px; position:relative; }}
  .head::after {{ content:""; position:absolute; left:0; right:0; bottom:0; height:6px;
    background:repeating-linear-gradient(90deg,var(--mc-yellow) 0 14px,transparent 14px 28px); }}
  .brand-row {{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; }}
  .arch {{ width:40px; height:40px; background:var(--mc-yellow); color:var(--mc-red); border:3px solid var(--ink);
    display:flex; align-items:center; justify-content:center; font-weight:900; font-size:24px;
    font-family:ui-monospace,Consolas,monospace; box-shadow:3px 3px 0 rgba(0,0,0,.4); }}
  .brand {{ font-weight:800; font-size:15px; letter-spacing:.5px; }}
  .badges {{ margin-left:auto; display:flex; gap:6px; flex-wrap:wrap; }}
  .tag {{ font-size:10px; background:rgba(255,255,255,.16); border:1px solid rgba(255,255,255,.55);
    padding:2px 8px; letter-spacing:1px; font-family:ui-monospace,Consolas,monospace; }}
  .mode {{ font-size:10px; padding:2px 8px; border:1px solid var(--ink); font-weight:800;
    font-family:ui-monospace,Consolas,monospace; letter-spacing:1px; }}
  .mode.live {{ background:#1f9d55; color:#fff; }}
  .mode.demo {{ background:var(--mc-yellow); color:var(--ink); }}
  .quest {{ margin-top:12px; font-size:20px; font-weight:900; line-height:1.4;
    font-family:ui-monospace,Consolas,monospace; text-shadow:2px 2px 0 rgba(0,0,0,.35); }}
  .title-row {{ display:flex; align-items:center; gap:8px; margin-top:8px; flex-wrap:wrap; }}
  .lv {{ background:var(--mc-yellow); color:var(--ink); border:2px solid var(--ink); font-weight:900;
    font-size:12px; padding:2px 8px; font-family:ui-monospace,Consolas,monospace; box-shadow:2px 2px 0 rgba(0,0,0,.35); }}
  .tier {{ font-size:12px; background:rgba(0,0,0,.25); padding:2px 8px; }}
  /* ---------- 主体 ---------- */
  .body {{ padding:18px 20px 20px; color:var(--ink); }}
  h3 {{ margin:16px 0 8px; font-size:13px; color:#a0630f; letter-spacing:3px;
    font-family:ui-monospace,Consolas,monospace; }}
  h3::before {{ content:"▸ "; color:var(--mc-red); }}
  .story {{ font-size:13px; line-height:1.9; background:#fff8e6; border:2px solid var(--line);
    border-left:6px solid var(--mc-yellow); padding:10px 12px; }}
  /* ---------- 装备 ---------- */
  .gear {{ list-style:none; margin:0; padding:0; }}
  .gear-item {{ display:flex; align-items:center; gap:10px; padding:8px 10px; margin:6px 0;
    background:#fff; border:2px solid var(--line); }}
  .gear-item.coupon {{ background:#fffbe8; border-style:dashed; }}
  .g-icon {{ font-size:22px; width:34px; text-align:center; flex:none; }}
  .g-body {{ flex:1; min-width:0; }}
  .g-name {{ display:block; font-size:14px; font-weight:700; }}
  .qty {{ color:var(--mc-red); font-weight:900; margin-left:2px; }}
  .g-epithet {{ display:block; font-size:11px; color:#a0630f; }}
  .g-code {{ display:block; font-size:10px; color:#9b8b6e; font-family:ui-monospace,Consolas,monospace;
    word-break:break-all; }}
  .g-price {{ font-size:14px; font-weight:800; color:var(--mc-red); white-space:nowrap; font-family:ui-monospace,Consolas,monospace; }}
  /* ---------- 价格结算 ---------- */
  .price {{ background:#fdf3f2; border:2px dashed var(--mc-red); padding:10px 12px; }}
  .p-row {{ display:flex; justify-content:space-between; gap:10px; font-size:13px; padding:3px 0;
    font-family:ui-monospace,Consolas,monospace; }}
  .p-row.total {{ font-size:17px; font-weight:900; color:var(--mc-red); border-top:2px solid var(--ink);
    margin-top:4px; padding-top:6px; }}
  .p-row.discount {{ color:#1f9d55; font-weight:700; }}
  .p-row.remain {{ font-weight:700; }}
  .p-row.over {{ color:#b00; font-weight:900; }}
  .p-row .unknown {{ color:#8a7a5c; font-size:12px; }}
  .bar-wrap {{ margin-top:10px; }}
  .bar-label {{ display:flex; justify-content:space-between; font-size:11px; color:#8a6b3a;
    font-family:ui-monospace,Consolas,monospace; margin-bottom:3px; }}
  .bar {{ height:12px; background:#f3e7c8; border:2px solid var(--ink); }}
  .bar-fill {{ height:100%; background:repeating-linear-gradient(45deg,var(--mc-yellow) 0 8px,#e6a700 8px 16px); }}
  .bar-wrap.over .bar-fill {{ background:repeating-linear-gradient(45deg,#c00 0 8px,#800 8px 16px); }}
  /* ---------- 属性面板 ---------- */
  .stats {{ display:flex; gap:8px; flex-wrap:wrap; }}
  .stat {{ flex:1; min-width:88px; background:#fff; border:2px solid var(--line); padding:8px;
    text-align:center; }}
  .s-k {{ display:block; font-size:11px; color:#8a7a5c; }}
  .s-v {{ display:block; font-size:18px; font-weight:900; color:var(--ink);
    font-family:ui-monospace,Consolas,monospace; }}
  .s-v i {{ font-size:10px; font-style:normal; color:#8a7a5c; margin-left:2px; }}
  .s-v.unknown, .s-unknown {{ color:#8a7a5c; font-size:12px; font-weight:400; }}
  .unknown-stats {{ display:block; background:#fff; border:2px dashed var(--line); padding:10px;
    font-size:12px; color:#8a7a5c; }}
  .stats-note {{ width:100%; font-size:11px; color:#a0630f; margin-top:4px; }}
  /* ---------- 备注 ---------- */
  .reasons, .unmet {{ margin:0; padding-left:18px; }}
  .reasons li {{ font-size:12px; color:#6b5a3e; margin:4px 0; }}
  .unmet li {{ font-size:12px; color:#a0630f; margin:4px 0; }}
  /* ---------- 页脚 ---------- */
  .meta {{ margin-top:16px; border-top:2px solid var(--line); padding-top:10px; font-size:11px;
    color:#8a7a5c; line-height:1.8; word-break:break-all; }}
  .meta b {{ color:#a0630f; }}
  .disclaimer {{ margin-top:10px; font-size:11px; color:#b04a3a; background:#fdf3f2;
    border:2px solid #f0c9c4; padding:8px 10px; line-height:1.7; }}
  /* ---------- 移动端 ---------- */
  @media (max-width:480px) {{
    body {{ padding:10px 6px; }}
    .body {{ padding:14px 12px 16px; }}
    .quest {{ font-size:16px; }}
    .g-price {{ font-size:12px; }}
    .p-row {{ font-size:12px; }}
    .p-row.total {{ font-size:15px; }}
  }}
  /* ---------- 打印 ---------- */
  @media print {{
    body {{ background:#fff; padding:0; }}
    .card {{ box-shadow:none; border-width:2px; max-width:100%; }}
    .gear-item, .price, .stats, .story {{ break-inside:avoid; }}
  }}
</style>
</head>
<body>
<div class="card">
  <div class="head">
    <div class="brand-row">
      <span class="arch">M</span>
      <span class="brand">{project}</span>
      <span class="badges"><span class="tag">{non_official}</span>{mode_badge}</span>
    </div>
    <div class="quest">{quest_name}</div>
    <div class="title-row"><span class="lv">LV.{level}</span><span class="tier">称号 · {tier}</span></div>
  </div>
  <div class="body">
    <h3>任 务 背 景</h3>
    <div class="story">{story}</div>
    <h3>推 荐 装 备</h3>
    <ul class="gear">{gear}</ul>
    <h3>价 格 结 算</h3>
    <div class="price">{price_rows}</div>
    {budget_bar}
    <h3>属 性 面 板</h3>
    {nutrition_block}
    <h3>智 囊 团 备 注</h3>
    <ul class="reasons">{reasons}</ul>
    {unmet_block}
    <div class="meta">
      <div><b>冒险者需求：</b>{constraints}</div>
      <div><b>数据来源：</b>{source}</div>
      <div><b>生成时间：</b>{generated_at}</div>
    </div>
    <div class="disclaimer">⚠️ {disclaimer}</div>
  </div>
</div>
</body>
</html>
"""


def render_html(
    constraints: Constraints,
    option: PlanOption,
    store: Optional[Store],
    time_info: str,
    source_note: str,
    generated_at: str,
    demo: bool = False,
) -> str:
    e = html.escape
    if option.unmet:
        unmet_block = (
            '<h3>不 完 全 符 合</h3><ul class="unmet">'
            + "".join(f"<li>⚠️ {e(u)}</li>" for u in option.unmet)
            + "</ul>"
        )
    else:
        unmet_block = ""
    level, tier = _level_and_tier(constraints)
    return _HTML_TEMPLATE.format(
        title=f"{PROJECT_NAME} - 任务成就卡",
        project=e(PROJECT_NAME),
        non_official=NON_OFFICIAL_TAG,
        mode_badge=_mode_badge(demo),
        quest_name=e(rpg.quest_name(constraints, option)),
        level=level,
        tier=e(tier),
        story=e(rpg.story(constraints, store.name if store else "", demo=demo)),
        gear=_gear_rows(option, demo),
        price_rows=_price_rows(option, constraints),
        budget_bar=_budget_bar(option, constraints),
        nutrition_block=_nutrition_block(option),
        reasons="".join(f"<li>{e(r)}</li>" for r in option.reasons) or "<li>（无）</li>",
        unmet_block=unmet_block,
        constraints=e(constraints.describe()),
        source=e(source_note),
        generated_at=e(generated_at),
        disclaimer=e(DISCLAIMER),
    )


def render_markdown(
    constraints: Constraints,
    option: PlanOption,
    store: Optional[Store],
    time_info: str,
    source_note: str,
    generated_at: str,
    demo: bool = False,
) -> str:
    """Markdown 成就卡：无 HTML 环境下完整可读（表格 + 分隔线 + 徽章文本）。"""
    e = html.escape  # Markdown 同样转义，防止在渲染器里被解释为 HTML
    level, tier = _level_and_tier(constraints)
    mode = "DEMO · 离线演示数据" if demo else "LIVE · 麦当劳 MCP 实时数据"
    lines = []
    lines.append(f"# 🍔 {PROJECT_NAME} · 任务成就卡")
    lines.append("")
    lines.append(f"**{rpg.quest_name(constraints, option)}**")
    lines.append("")
    lines.append(f"`{NON_OFFICIAL_TAG}` · `{mode}` · `LV.{level} {tier}`")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 📜 任务背景")
    lines.append("")
    lines.append(rpg.story(constraints, store.name if store else "", demo=demo))
    lines.append("")
    lines.append("## ⚔️ 推荐装备")
    lines.append("")
    lines.append("| 装备 | 说明 | 数量 | 单价 | 溯源编码 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for pi in option.items:
        cat = classify(pi.item)
        icon = GEAR_ICON.get(cat, "✨")
        epithet = rpg.EPITHET_MAP.get(pi.item.name, rpg.EPITHET_FALLBACK.get(cat, "补给"))
        name = e(pi.item.name)
        lines.append(
            f"| {icon} {name} | {e(epithet)} | {pi.qty} | {e(pi.item.price_text())} | `{e(str(pi.item.code))}` |"
        )
    for c in option.coupons:
        val = f"-{format_yuan(c.value_cents)}" if c.value_cents else ("折扣券" if c.percent else "—")
        lines.append(f"| 🎟️ {e(c.name)} | 券灵加持（已提交官方核验） | — | {val} | `{e(str(c.id))}` |")
    lines.append("")
    lines.append("## 💰 价格结算")
    lines.append("")
    lines.append("| 项目 | 金额 |")
    lines.append("| --- | --- |")
    lines.append(
        f"| 原价合计 | {format_yuan(option.original_cents) if option.original_cents is not None else '未知'} |"
    )
    if option.discount_cents:
        lines.append(f"| 已核实优惠 | -{format_yuan(option.discount_cents)} |")
    elif option.coupons:
        lines.append("| 已核实优惠 | 官方核验未产生优惠 |")
    if option.delivery_fee_cents:
        lines.append(f"| 配送费 | {format_yuan(option.delivery_fee_cents)} |")
    if option.price_checked and option.total_cents is not None:
        lines.append(f"| **应付总价（calculate-price 权威结果）** | **{format_yuan(option.total_cents)}** |")
    else:
        lines.append("| **应付总价** | **未知（未通过 calculate-price 权威核验）** |")
    if constraints.budget_cents is not None and option.remaining_cents is not None:
        if option.remaining_cents >= 0:
            lines.append(f"| 剩余预算 | {format_yuan(option.remaining_cents)} |")
        else:
            lines.append(f"| 超出预算 | {format_yuan(-option.remaining_cents)} |")
    if constraints.budget_cents and option.total_cents is not None:
        used = option.total_cents / constraints.budget_cents * 100
        lines.append("")
        lines.append(f"⚡ 预算使用率：`{'█' * max(1, min(20, int(used / 5)))}{'░' * max(0, 20 - int(used / 5))}` {used:.0f}%")
    lines.append("")
    lines.append("## 🧪 属性面板")
    lines.append("")
    n = option.nutrition or {}
    if not n:
        missing = "、".join(option.nutrition_missing) if option.nutrition_missing else "全部餐品"
        lines.append(f"营养数据未知（{missing} 无官方营养记录——不猜测数值）")
    else:
        for key, label, unit in (("calories_kcal", "热量", "千卡"), ("protein_g", "蛋白质", "g"), ("fat_g", "脂肪", "g")):
            v = n.get(key)
            lines.append(f"- {label}：{f'{v:.0f} {unit}' if v is not None else '未知'}")
        if not option.nutrition_complete:
            lines.append(f"- ⚠️ 部分餐品无营养数据（{'、'.join(option.nutrition_missing)}），数值可能偏低——未猜测")
    lines.append("")
    lines.append("## 🧠 智囊团备注")
    lines.append("")
    for r in option.reasons:
        lines.append(f"- {e(r)}")
    if option.unmet:
        lines.append("")
        lines.append("## ⚠️ 不完全符合")
        lines.append("")
        for u in option.unmet:
            lines.append(f"- ⚠️ {e(u)}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"- **冒险者需求**：{constraints.describe()}")
    lines.append(f"- **数据来源**：{e(source_note)}")
    lines.append(f"- **生成时间**：{e(generated_at)}")
    lines.append(f"- ⚠️ **{DISCLAIMER}**")
    return "\n".join(lines) + "\n"
