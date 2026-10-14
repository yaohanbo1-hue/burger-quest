"""演示 / 测试用 Mock 客户端：完全离线，数据来自 fixtures/demo_data.json。

⚠️ 重要：fixtures 中的餐品、价格、优惠券均为「演示数据」，**结构与麦当劳 MCP
真实返回保持兼容**（2026-10-13 联调实测后对齐），但绝不是实时菜单。

与真实服务的行为对齐点（联调实测）：
- query-meals 只返回「编码 + 分类 + 标签」，不含名称与价格；
- 名称来自 query-meal-detail；价格只来自 calculate-price（金额单位：分）；
- list-nutrition-foods 返回自定义分隔文本；
- calculate-price 需要 orderType/beType，外送另需 beCode；
- query-my-coupons 无券时返回纯文本。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .mcp_base import (
    MCPClient,
    MCPError,
    OrderScene,
    TOOLS_USED,
    adapt_coupons,
    adapt_meal_detail,
    adapt_meals,
    adapt_nutrition,
    adapt_price,
    adapt_stores,
    adapt_time,
    build_calc_payload,
)
from .models import MenuItem, PlanItem, PriceQuote

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

    def _detail(self, code: str) -> dict:
        return self.data.get("meal_details", {}).get(str(code), {})

    def _unit_cents(self, code: str) -> Optional[int]:
        d = self._detail(code)
        if "price_cents" in d:
            return int(d["price_cents"])
        if "price_yuan" in d:
            return int(round(float(d["price_yuan"]) * 100))
        return None

    def _all_coupons(self, store_code: str) -> dict:
        out = {c["couponId"]: c for c in self._store(store_code).get("coupons", [])}
        for c in self.data.get("my_coupons", []):
            out.setdefault(c["couponId"], c)
        return out

    # -- 工具实现（与真实 MCP 返回结构对齐） ---------------------------------

    def now_time(self) -> dict:
        self._record("now-time-info", {}, True, purpose="time_context")
        return adapt_time(self.data.get("now_time_info", {}))

    def list_tools(self) -> list[dict]:
        """演示版工具发现：只返回引擎实际调用的 8 个工具（无官方 Schema）。

        冒烟测试 --demo 模式用它做离线重复执行；真实 Schema 以官方服务为准。
        """
        self._record("list_tools", {}, True)
        return [{"name": t, "description": "（演示数据，非官方 Schema）", "input_schema": None} for t in TOOLS_USED]

    def nearby_stores(self, keyword: str, city: str = "", be_type: int = 1):
        """演示用附近门店查询（searchType=2 按位置）。

        相关性 = 关键词 2 字片段与门店 地址/城市/名称 的重合数 + 城市命中；
        并列最高分返回多家（用于候选选择流程）；无关联返回空。
        不传关键词和城市时返回默认演示门店（模拟无地址时的服务端行为）。
        """
        self._record("query-nearby-stores", {"keyword": keyword, "city": city, "beType": be_type}, True)
        stores = self.data.get("stores", [])
        if not keyword and not city:
            default = str(self.data.get("default_store_code", ""))
            return adapt_stores([s for s in stores if str(s.get("storeCode")) == default])

        def relevance(s) -> int:
            text = (s.get("address", "") or "") + (s.get("city", "") or "") + (s.get("storeName", "") or "")
            score = sum(1 for i in range(len(keyword) - 1) if keyword[i : i + 2] in text) if keyword else 0
            if city and city in text:
                score += 3
            return score

        scored = [(relevance(s), s) for s in stores]
        best = max((sc for sc, _ in scored), default=0)
        if best <= 0:
            return []
        stores = [s for sc, s in scored if sc == best]
        return adapt_stores(stores)

    def delivery_addresses(self) -> list[dict]:
        self._record("delivery-query-addresses", {}, True)
        return [a for a in self.data.get("delivery_addresses", []) if isinstance(a, dict)]

    def delivery_stores(self, address_id: str, be_type: int = 2):
        self._record("delivery-query-stores", {"addressId": address_id, "beType": be_type}, True)
        mapping = self.data.get("delivery_stores", {})
        return adapt_stores(mapping.get(str(address_id), []))

    def query_meals(self, scene: OrderScene):
        """只返回「编码 + 分类 + 标签」骨架（与真实 query-meals 一致，无名称无价格）。"""
        self._record("query-meals", {"storeCode": scene.store_code, "orderType": scene.order_type, "beType": scene.be_type}, True)
        store = self._store(scene.store_code)
        if scene.store_code not in self._menu_cache:
            self._menu_cache[scene.store_code] = store.get("categories", [])
        return adapt_meals({"categories": self._menu_cache[scene.store_code]})

    def meal_detail(self, scene: OrderScene, code: str) -> dict:
        self._record("query-meal-detail", {"storeCode": scene.store_code, "code": code}, True)
        detail = self._detail(code)
        if not detail:
            raise MCPError("query-meal-detail", f"演示餐品 {code} 不存在", code=404)
        payload = {"code": str(code), "name": detail.get("name", ""), "rounds": detail.get("rounds", [])}
        return adapt_meal_detail({"data": payload})

    def enriched_menu(self, scene: OrderScene) -> list[MenuItem]:
        """测试辅助：直接返回带名称与价格的菜单（等价于服务端的「骨架+详情+价格发现」结果）。"""
        out = []
        for m in self.query_meals(scene):
            cents = self._unit_cents(m.code)
            if cents is None:
                continue
            out.append(
                MenuItem(
                    code=m.code,
                    name=self._detail(m.code).get("name", m.code),
                    category=m.category,
                    price_cents=cents,
                    tags=m.tags,
                )
            )
        return out

    def store_coupons(self, scene: OrderScene):
        self._record("query-store-coupons", {"storeCode": scene.store_code}, True)
        return adapt_coupons(self._store(scene.store_code).get("coupons", []), source="store")

    def my_coupons(self):
        self._record("query-my-coupons", {}, True)
        # 与真实服务一致：无券时返回纯文本（无 JSON），适配器得到空列表
        if not self.data.get("my_coupons"):
            return []
        return adapt_coupons(self.data.get("my_coupons", []), source="my")

    def list_nutrition_foods(self):
        self._record("list-nutrition-foods", {}, True)
        rows = self.data.get("nutrition", [])
        if not rows:
            return []
        # 按真实服务的自定义文本格式构造，再走适配器解析（往返保证解析器被覆盖）
        headers = [
            "productName", "nutritionDescription", "energyKj", "energyKcal",
            "protein", "fat", "carbohydrate", "sodium", "calcium",
        ]
        lines = [f"[{len(rows)}]{{{','.join(headers)}}}:"]
        for r in rows:
            kcal = r.get("energyKcal", 0)
            kj = r.get("energyKj", round(float(kcal) * 4.184))
            lines.append(
                f"  {r.get('productName', '')},null,{kj},{kcal},"
                f"{r.get('protein', '')},{r.get('fat', '')},{r.get('carbohydrate', '')},"
                f"{r.get('sodium', '')},{r.get('calcium', '')}"
            )
        return adapt_nutrition("\n".join(lines))

    def calculate_price(self, scene: OrderScene, items: list, coupon_ids: list, purpose: str = ""):
        """演示算价：逻辑与真实工具对齐（金额单位：分；券挂在第一件商品上）。"""
        payload = build_calc_payload(scene, items, coupon_ids)
        self._record(
            "calculate-price",
            {**payload, "storeCode": scene.store_code, "orderType": scene.order_type, "beType": scene.be_type},
            True,
            purpose=purpose or "price_check",
        )
        original = 0
        per_item: dict[str, int] = {}
        product_list = []
        for i, entry in enumerate(payload["items"]):
            unit = self._unit_cents(entry["productCode"])
            if unit is None:
                return adapt_price({"success": False, "code": 400, "message": f"餐品 {entry['productCode']} 价格缺失，无法计算"})
            line = unit * entry["quantity"]
            original += line
            per_item[entry["productCode"]] = per_item.get(entry["productCode"], 0) + line
            product_list.append(
                {
                    "productCode": entry["productCode"],
                    "productName": self._detail(entry["productCode"]).get("name", entry["productCode"]),
                    "quantity": entry["quantity"],
                    "subtotal": line,
                    "originalSubtotal": line,
                }
            )
        discount = 0
        all_coupons = self._all_coupons(scene.store_code)
        for cid in coupon_ids:
            c = all_coupons.get(cid)
            if not c:
                return adapt_price({"success": False, "code": 400, "message": f"优惠券 {cid} 不存在"})
            threshold = int(round(float(c.get("threshold", 0)) * 100))
            if threshold and original < threshold:
                return adapt_price({"success": False, "code": 400, "message": f"优惠券「{c.get('couponName')}」未达使用门槛"})
            pct = c.get("discount")
            if pct:
                applicable = c.get("applicableMeals") or list(per_item.keys())
                base = sum(v for k, v in per_item.items() if k in applicable)
                discount += int(round(base * float(pct)))
            else:
                discount += int(round(float(c.get("value", 0)) * 100))
        delivery_fee = 900 if scene.order_type == 2 else 0  # 演示：外送 ¥9
        total = original - discount + delivery_fee
        return adapt_price(
            {
                "success": True,
                "code": 200,
                "data": {
                    "price": total,
                    "originalPrice": original,
                    "discount": discount,
                    "deliveryFee": delivery_fee,
                    "productList": product_list,
                },
            }
        )
