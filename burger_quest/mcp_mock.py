"""演示 / 测试用 Mock 客户端：完全离线，数据来自 fixtures/demo_data.json。

⚠️ 重要：fixtures 中的餐品、价格、优惠券均为「演示数据」，结构与麦当劳 MCP
返回保持兼容，但绝不是实时菜单。演示输出中会显著标注数据来源。
Mock 的 calculate-price 用 fixtures 价格模拟满减逻辑，行为与真实工具一致
（返回权威总价），但数值仅供演示。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .mcp_base import (
    MCPClient,
    MCPError,
    adapt_coupons,
    adapt_meals,
    adapt_nutrition,
    adapt_price,
    adapt_stores,
    adapt_time,
    build_calc_payload,
)

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "demo_data.json"


class MockMCPClient(MCPClient):
    def __init__(self, fixture_path: Optional[Path] = None):
        super().__init__()
        path = Path(fixture_path) if fixture_path else FIXTURE_PATH
        if not path.exists():
            raise MCPError("mock", f"演示数据不存在：{path}")
        self.data = json.loads(path.read_text(encoding="utf-8"))
        self._menu_cache: dict[str, list] = {}

    # -- 内部工具 -----------------------------------------------------------

    def _store(self, store_code: str) -> dict:
        for s in self.data.get("stores", []):
            if str(s.get("storeCode")) == str(store_code):
                return s
        raise MCPError("query-meals", f"演示门店 {store_code} 不存在", code=404)

    def _menu_raw(self, store_code: str) -> list:
        if store_code not in self._menu_cache:
            store = self._store(store_code)
            self._menu_cache[store_code] = store.get("meals", [])
        return self._menu_cache[store_code]

    def _find_meal(self, store_code: str, meal_code: str) -> Optional[dict]:
        for m in self._menu_raw(store_code):
            if str(m.get("mealCode")) == str(meal_code):
                return m
        return None

    # -- 工具实现 -----------------------------------------------------------

    def now_time(self) -> dict:
        return adapt_time(self.data.get("now_time_info", {}))

    def nearby_stores(self, address: str):
        """演示用附近门店查询。

        与真实工具的差异（仅演示，docstring 即声明）：
        - 传入地址时，按「地址与门店地址/城市/名称共享的 2 字片段数」做相关性过滤，
          模拟「按位置返回附近门店」；多个门店并列最高分时返回多家（用于候选选择流程）；
          完全无关联时返回空列表（模拟查无门店）；
        - 不传地址时返回默认演示门店（真实服务器会要求地址，service 层对真实模式已拦截）。
        """
        self._record("query-nearby-stores", {"address": address}, True)
        stores = self.data.get("stores", [])
        if address:
            def relevance(s) -> int:
                text = (s.get("address", "") or "") + (s.get("city", "") or "") + (s.get("storeName", "") or "")
                return sum(1 for i in range(len(address) - 1) if address[i : i + 2] in text)

            scored = [(relevance(s), s) for s in stores]
            best = max((sc for sc, _ in scored), default=0)
            if best <= 0:
                return []
            stores = [s for sc, s in scored if sc == best]
        else:
            default = str(self.data.get("default_store_code", ""))
            stores = [s for s in stores if str(s.get("storeCode")) == default]
        return adapt_stores(stores)

    def query_meals(self, store_code: str):
        self._record("query-meals", {"storeCode": store_code}, True)
        return adapt_meals(self._menu_raw(store_code))

    def meal_detail(self, meal_code: str) -> dict:
        self._record("query-meal-detail", {"mealCode": meal_code}, True)
        store_code = self.data.get("default_store_code", "")
        meal = self._find_meal(store_code, meal_code)
        if meal is None:
            raise MCPError("query-meal-detail", f"演示餐品 {meal_code} 不存在", code=404)
        return meal

    def store_coupons(self, store_code: str):
        self._record("query-store-coupons", {"storeCode": store_code}, True)
        return adapt_coupons(self._store(store_code).get("coupons", []), source="store")

    def my_coupons(self):
        self._record("query-my-coupons", {}, True)
        return adapt_coupons(self.data.get("my_coupons", []), source="my")

    def list_nutrition_foods(self):
        self._record("list-nutrition-foods", {}, True)
        return adapt_nutrition(self.data.get("nutrition", []))

    def calculate_price(self, store_code: str, items: list, coupon_ids: list, dining_mode: str):
        payload = build_calc_payload(items, coupon_ids, dining_mode)
        self._record("calculate-price", {**payload, "storeCode": store_code}, True)
        menu = {m.code: m for m in self.query_meals(store_code)}
        original = 0
        per_item: dict[str, int] = {}
        for entry in payload["items"]:
            meal = menu.get(entry["mealCode"])
            if meal is None or meal.price_cents is None:
                return adapt_price({"message": f"餐品 {entry['mealCode']} 价格缺失，无法计算"})
            line = meal.price_cents * entry["quantity"]
            original += line
            per_item[entry["mealCode"]] = per_item.get(entry["mealCode"], 0) + line
        discount = 0
        store = self._store(store_code)
        all_coupons = {c["couponId"]: c for c in store.get("coupons", [])}
        for c in self.data.get("my_coupons", []):
            all_coupons.setdefault(c["couponId"], c)
        for cid in coupon_ids:
            c = all_coupons.get(cid)
            if not c:
                return adapt_price({"message": f"优惠券 {cid} 不存在"})
            threshold = int(round(float(c.get("threshold", 0)) * 100))
            if threshold and original < threshold:
                return adapt_price({"message": f"优惠券「{c.get('couponName')}」未达使用门槛"})
            pct = c.get("discount")
            if pct:
                applicable = c.get("applicableMeals") or list(per_item.keys())
                base = sum(v for k, v in per_item.items() if k in applicable)
                discount += int(round(base * float(pct)))
            else:
                discount += int(round(float(c.get("value", 0)) * 100))
        delivery_fee = 900 if dining_mode == "delivery" else 0  # 演示：外送 ¥9
        total = original - discount + delivery_fee
        return adapt_price(
            {
                "totalPrice": total / 100,
                "originalPrice": original / 100,
                "discountPrice": discount / 100,
                "deliveryFee": delivery_fee / 100,
            }
        )
