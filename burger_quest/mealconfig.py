"""套餐装备解析（query-meal-detail -> 可展示的组成 / 可替换项 / 特制选项）。

2026-10-18 真实联调实测返回结构（节选）：

    rounds: [
      {id, name, quantity, minQuantity, maxQuantity,
       choices: [{code, name, quantity, isDefault, diffPrice: "+ ¥0" / "- ¥1.5",
                  supportModify, modification: {items: [{minValues, maxValues,
                     values: [{code, price, name, maxQuantity, minQuantity,
                               selectedQuantity, selectedKey, unselectedKey}]}]}}]}
    ]

边界（不得越过）：
- 展示字段全部来自官方返回，不派生任何未返回的选项；
- **不提供「套餐替换改价」**：官方 list_tools 未声明 calculate-price 的替换参数
  （inputSchema 为空，官方仓库亦无参数文档），伪造传参等于自造价格。
  需要重配时走 `--reconfigure`：把套餐内各默认项当作**单品组合**提交
  calculate-price，得到的是官方对单品组合的真实报价，与套餐价可能不同，
  输出中明确标注这一点。
"""
from __future__ import annotations

import re
from typing import Any, Optional
from dataclasses import dataclass, field

_DIFF_RE = re.compile(r"([+-])\s*¥?\s*(\d+(?:\.\d+)?)")


@dataclass
class Choice:
    """套餐某一组里的一个可选项（对应 rounds[].choices[]）。"""

    code: str
    name: str
    is_default: bool = False
    diff_text: str = ""
    diff_cents: Optional[int] = None  # + 加价 / - 减价；解析失败为 None
    modifiable: bool = False
    modifications: list[dict] = field(default_factory=list)


@dataclass
class RoundGroup:
    """套餐的一组（如「选择饮料」），含组内可选项。"""

    index: int
    name: str
    quantity: int = 1
    choices: list[Choice] = field(default_factory=list)

    def default_choice(self) -> Optional[Choice]:
        for c in self.choices:
            if c.is_default:
                return c
        return self.choices[0] if self.choices else None


def _diff_cents(text: str) -> Optional[int]:
    m = _DIFF_RE.search(text or "")
    if not m:
        return None
    sign = 1 if m.group(1) == "+" else -1
    try:
        return sign * int(round(float(m.group(2)) * 100))
    except ValueError:
        return None


def parse_detail(detail: dict) -> list[RoundGroup]:
    """meal_detail 适配结果 -> RoundGroup 列表（结构不完整时尽量保留已返回部分）。"""
    groups: list[RoundGroup] = []
    rounds = detail.get("rounds") if isinstance(detail, dict) else None
    for i, r in enumerate(rounds or []):
        if not isinstance(r, dict):
            continue
        choices: list[Choice] = []
        for ch in r.get("choices") or []:
            if not isinstance(ch, dict):
                continue
            mods: list[dict] = []
            modification = ch.get("modification")
            if isinstance(modification, dict):
                for item in modification.get("items") or []:
                    if not isinstance(item, dict):
                        continue
                    values = [
                        {
                            "code": v.get("code"),
                            "name": v.get("name"),
                            "price_cents": v.get("price"),
                            "selected_quantity": v.get("selectedQuantity"),
                            "max_quantity": v.get("maxQuantity"),
                        }
                        for v in item.get("values") or []
                        if isinstance(v, dict)
                    ]
                    if values:
                        mods.append(
                            {
                                "min_values": item.get("minValues"),
                                "max_values": item.get("maxValues"),
                                "values": values,
                            }
                        )
            diff_text = str(ch.get("diffPrice") or "")
            choices.append(
                Choice(
                    code=str(ch.get("code") or ""),
                    name=str(ch.get("name") or ""),
                    is_default=bool(ch.get("isDefault")),
                    diff_text=diff_text,
                    diff_cents=_diff_cents(diff_text),
                    modifiable=bool(ch.get("supportModify")),
                    modifications=mods,
                )
            )
        if not choices:
            continue
        groups.append(
            RoundGroup(
                index=int(r.get("id") or (i + 1)),
                name=str(r.get("name") or f"第 {i + 1} 组"),
                quantity=int(r.get("quantity") or 1),
                choices=choices,
            )
        )
    return groups


def default_items(groups: list[RoundGroup]) -> list[tuple[str, str, int]]:
    """套餐默认组成 -> [(code, name, qty)]，用于单品组合算价。"""
    out: list[tuple[str, str, int]] = []
    for g in groups:
        c = g.default_choice()
        if c is not None and c.code:
            out.append((c.code, c.name or g.name, max(1, g.quantity)))
    return out


def apply_replacements(
    groups: list[RoundGroup], replacements: dict[int, str]
) -> tuple[list[tuple[str, str, int]], list[str]]:
    """按 {组序号: 新选项 code} 重配，返回 (items, 变更说明)。

    只允许选择**该组 choices 里真实存在的 code**；未知 code 记入说明并忽略，
    绝不凭用户输入虚构餐品。
    """
    items: list[tuple[str, str, int]] = []
    notes: list[str] = []
    for g in groups:
        chosen: Optional[Choice] = g.default_choice()
        new_code = replacements.get(g.index)
        if new_code:
            match = next((c for c in g.choices if c.code == new_code), None)
            if match is None:
                notes.append(f"第 {g.index} 组（{g.name}）不存在选项 {new_code}，已保留默认项")
            else:
                chosen = match
                notes.append(f"第 {g.index} 组（{g.name}）：{chosen.name}（{chosen.diff_text or '原价'}）")
        if chosen is not None and chosen.code:
            items.append((chosen.code, chosen.name or g.name, max(1, g.quantity)))
    return items, notes
