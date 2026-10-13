"""展示层：分享成就卡渲染（HTML / Markdown）。

卡片必须包含（比赛要求）：
- 项目名称「Burger Quest · 麦门异世界」；
- 非官方项目声明；
- 任务名、故事、装备、价格、剩余预算；
- 数据来源声明（实时 MCP 数据 or 演示数据）；
- 所有用户/菜单文本一律 html.escape，防止注入。
"""
from __future__ import annotations

import html
from typing import Optional

from .models import Constraints, PlanOption, Store, format_yuan
from . import rpg

PROJECT_NAME = "Burger Quest · 麦门异世界"
DISCLAIMER = "非麦当劳官方项目，参赛作品，仅供娱乐参考；餐品信息、价格及供应状态以麦当劳官方渠道实时结果为准。"
NON_OFFICIAL_TAG = "NON-OFFICIAL FAN PROJECT"

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ margin:0; padding:24px; background:#f6f1e7; font-family:"PingFang SC","Microsoft YaHei",system-ui,sans-serif; }}
  .card {{ max-width:680px; margin:0 auto; background:#fffdf8; border:2px solid #ffc72c; border-radius:20px; overflow:hidden; box-shadow:0 8px 24px rgba(0,0,0,.08); }}
  .head {{ background:#da291c; color:#fff; padding:22px 26px; }}
  .head .brand {{ display:flex; align-items:center; gap:12px; font-weight:700; font-size:15px; letter-spacing:.5px; }}
  .arch {{ width:38px; height:38px; background:#ffc72c; color:#da291c; border-radius:10px; display:flex; align-items:center; justify-content:center; font-weight:900; font-size:24px; }}
  .quest {{ margin-top:14px; font-size:22px; font-weight:800; line-height:1.35; }}
  .tag {{ display:inline-block; margin-top:10px; font-size:11px; background:rgba(255,255,255,.18); border:1px solid rgba(255,255,255,.5); padding:2px 10px; border-radius:999px; letter-spacing:1px; }}
  .body {{ padding:22px 26px; color:#3a2c1c; }}
  h3 {{ margin:18px 0 8px; font-size:14px; color:#a0630f; letter-spacing:2px; }}
  h3:first-child {{ margin-top:0; }}
  .story {{ font-size:14px; line-height:1.8; background:#fff8e6; border-left:4px solid #ffc72c; padding:12px 14px; border-radius:0 10px 10px 0; }}
  .gear li {{ margin:6px 0; font-size:14px; }}
  .gear {{ padding-left:20px; }}
  .price {{ background:#fdf3f2; border:1px dashed #da291c; border-radius:12px; padding:12px 16px; }}
  .price .row {{ display:flex; justify-content:space-between; font-size:14px; padding:3px 0; }}
  .price .total {{ font-size:18px; font-weight:800; color:#da291c; }}
  .reasons li {{ font-size:13px; color:#6b5a3e; margin:4px 0; }}
  .meta {{ margin-top:18px; border-top:1px solid #eee2c8; padding-top:12px; font-size:12px; color:#8a7a5c; line-height:1.7; }}
  .meta b {{ color:#a0630f; }}
  .disclaimer {{ margin-top:10px; font-size:11px; color:#b04a3a; background:#fdf3f2; border-radius:8px; padding:8px 12px; }}
</style>
</head>
<body>
<div class="card">
  <div class="head">
    <div class="brand"><span class="arch">M</span><span>{project}</span></div>
    <div class="quest">{quest_name}</div>
    <span class="tag">{non_official}</span>
  </div>
  <div class="body">
    <h3>任 务 背 景</h3>
    <div class="story">{story}</div>
    <h3>推 荐 装 备</h3>
    <ul class="gear">{gear}</ul>
    <h3>价 格 结 算</h3>
    <div class="price">{price_rows}</div>
    <h3>智 囊 团 备 注</h3>
    <ul class="reasons">{reasons}</ul>
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
    gear = "".join(f"<li>{e(line)}</li>" for line in rpg.gear_lines(option))
    rows = ""
    price_rows = rpg.price_lines(option, constraints)
    for i, line in enumerate(price_rows):
        cls = "row total" if line.startswith("应付总价") else "row"
        if "：" in line:
            k, v = line.split("：", 1)
            rows += f'<div class="{cls}"><span>{e(k)}</span><span>{e(v)}</span></div>'
    reasons = "".join(f"<li>{e(r)}</li>" for r in option.reasons)
    if not option.reasons:
        reasons = "<li>（无）</li>"
    return _HTML_TEMPLATE.format(
        title=f"{PROJECT_NAME} - 任务成就卡",
        project=e(PROJECT_NAME),
        quest_name=e(rpg.quest_name(constraints, option)),
        non_official=NON_OFFICIAL_TAG,
        story=e(rpg.story(constraints, store.name if store else "", demo=demo)),
        gear=gear,
        price_rows=rows,
        reasons=reasons,
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
    lines = []
    lines.append(f"# 🍔 {PROJECT_NAME} · 任务成就卡")
    lines.append("")
    lines.append(f"**{rpg.quest_name(constraints, option)}**")
    lines.append("")
    lines.append(f"> {NON_OFFICIAL_TAG}")
    lines.append("")
    lines.append("## 任务背景")
    lines.append(rpg.story(constraints, store.name if store else "", demo=demo))
    lines.append("")
    lines.append("## 推荐装备")
    for line in rpg.gear_lines(option):
        lines.append(f"- {line}")
    lines.append("")
    lines.append("## 价格结算")
    for line in rpg.price_lines(option, constraints):
        lines.append(f"- {line}")
    lines.append("")
    lines.append("## 智囊团备注")
    for r in option.reasons:
        lines.append(f"- {r}")
    lines.append("")
    lines.append("---")
    lines.append(f"- 冒险者需求：{constraints.describe()}")
    lines.append(f"- 数据来源：{source_note}")
    lines.append(f"- 生成时间：{generated_at}")
    lines.append(f"- ⚠️ {DISCLAIMER}")
    return "\n".join(lines) + "\n"
