"""Burger Quest · 麦门异世界

基于麦当劳中国官方 MCP Server 的游戏化点餐规划 Skill / 引擎。
"""
from __future__ import annotations

__version__ = "0.1.0"
PROJECT_NAME = "Burger Quest · 麦门异世界"

from .models import (  # noqa: F401
    Constraints,
    Coupon,
    MenuItem,
    NutritionItem,
    PlanItem,
    PlanOption,
    PriceQuote,
    QuestResult,
    Store,
)
