"""MCP 客户端抽象层。

- MCPClient：引擎唯一依赖的接口。真实实现走官方 mcp Python SDK（mcp_http.py），
  演示/测试走 fixtures（mcp_mock.py）。
- 适配器（_adapt_*）：麦当劳 MCP 官方文档只发布工具清单与描述，未公布每个工具的
  详细 JSON Schema，因此这里对所有字段做「防御式提取」——按多个常见键名尝试、
  取不到就留空并在 warnings 里说明，绝不编造。
- 数据来源审计：每次调用记录到 MCPClient.calls，最终写入任务书的数据来源区块。
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any, Optional

from .models import (
    Coupon,
    MenuItem,
    NutritionItem,
    PriceQuote,
    Store,
    yuan_to_cents,
)

# 引擎实际调用的麦当劳 MCP 工具（MVP 最小集；query-meal-detail 预留未接入，见 TOOLS_RESERVED）
TOOLS_USED = [
    "now-time-info",
    "query-nearby-stores",
    "query-meals",
    "query-store-coupons",
    "query-my-coupons",
    "list-nutrition-foods",
    "calculate-price",
]
# 预留但 MVP 不主动调用的工具：
# - query-meal-detail：套餐组成查询，联调验证参数后可接入（用于展示套餐内容/替换选项）；
# - 其余为涉及真实资金/库存/账户状态的写操作，按比赛要求不接入。
TOOLS_RESERVED = [
    "query-meal-detail",
    "available-coupons",
    "auto-bind-coupons",
    "create-order",
    "cancel-order",
    "query-order",
]


class MCPError(Exception):
    """MCP 调用失败（网络/鉴权/限流/服务端错误）。消息面向用户，可解释。"""

    def __init__(self, tool: str, message: str, code: Optional[int] = None):
        self.tool = tool
        self.code = code
        super().__init__(f"[{tool}] {message}")


# ---------------------------------------------------------------------------
# 防御式字段提取
# ---------------------------------------------------------------------------


def _first(d: dict, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _as_list(v) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        # 兼容 {"list": [...]} / {"items": [...]} 等包裹
        for k in ("list", "items", "data", "records", "result"):
            if isinstance(v.get(k), list):
                return v[k]
        return [v]
    return [v]


def _extract_price_cents(raw: dict) -> Optional[int]:
    """从餐品原始数据提取价格（分）。单位判定规则：
    - 键名含 cent/fen/分 -> 视为「分」；
    - 否则视为「元」；
    - 金额大于 500 元的整数且是数值型 -> 推断为「分」（避免把 2550 分误判成 2550 元）。
    """
    for key in ("priceCents", "priceInCents", "priceFen", "price", "currentPrice", "salePrice", "priceYuan", "originalPrice"):
        if key in raw and raw[key] not in (None, ""):
            v = raw[key]
            kl = key.lower()
            if "cent" in kl or "fen" in kl or "分" in key:
                try:
                    return int(float(v))
                except (TypeError, ValueError):
                    return None
            cents = yuan_to_cents(v)
            if cents is not None and cents > 50000 and isinstance(v, (int, float)) and float(v).is_integer():
                # 启发式：>500 元的整数价格极可能是「分」
                return int(v)
            return cents
    return None


def _extract_tags(raw: dict) -> tuple:
    tags = _first(raw, "tags", "labels", "tagList", default=[])
    out = []
    for t in _as_list(tags):
        if isinstance(t, str):
            out.append(t)
        elif isinstance(t, dict):
            name = _first(t, "name", "tagName", "label")
            if name:
                out.append(str(name))
    return tuple(out)


# ---------------------------------------------------------------------------
# 抽象接口
# ---------------------------------------------------------------------------


class MCPClient(ABC):
    """引擎唯一依赖的 MCP 客户端接口。

    每个方法：成功返回规范化数据；失败抛 MCPError（消息可读、带错误码）。
    """

    def __init__(self):
        self.calls: list[dict] = []  # 审计记录

    def _record(self, tool: str, args: dict, ok: bool, note: str = ""):
        self.calls.append({"tool": tool, "args": _safe_args(args), "ok": ok, "note": note})

    # -- 基础信息 -----------------------------------------------------------

    @abstractmethod
    def now_time(self) -> dict:
        """now-time-info -> {'datetime': ..., 'time_period': 'breakfast/lunch/...'}"""

    @abstractmethod
    def nearby_stores(self, address: str) -> list[Store]:
        """query-nearby-stores -> 附近门店列表"""

    @abstractmethod
    def query_meals(self, store_code: str) -> list[MenuItem]:
        """query-meals -> 当前门店可售餐品（含价格则带回）"""

    @abstractmethod
    def meal_detail(self, meal_code: str) -> dict:
        """query-meal-detail -> 套餐组成/可替换选项（原始结构透传）"""

    @abstractmethod
    def store_coupons(self, store_code: str) -> list[Coupon]:
        """query-store-coupons -> 当前门店可用优惠券"""

    @abstractmethod
    def my_coupons(self) -> list[Coupon]:
        """query-my-coupons -> 我的卡包优惠券"""

    @abstractmethod
    def list_nutrition_foods(self) -> list[NutritionItem]:
        """list-nutrition-foods -> 常见餐品营养数据"""

    @abstractmethod
    def calculate_price(
        self,
        store_code: str,
        items: list[dict],
        coupon_ids: list[str],
        dining_mode: str,
    ) -> PriceQuote:
        """calculate-price -> 权威价格计算（商品金额、配送费、优惠、应付总价）"""

    # -- 数据来源描述 -------------------------------------------------------

    def close(self):
        """释放客户端资源（Mock 为无操作；真实客户端关闭 MCP 会话与后台线程）。"""

    def source_note(self) -> dict:
        return {
            "server": "麦当劳中国 MCP Server (https://mcp.mcd.cn)",
            "client": type(self).__name__,
            "tools": TOOLS_USED,
        }


def _safe_args(args: dict) -> dict:
    """脱敏：绝不把 token 等敏感字段写进审计记录。"""
    return {k: v for k, v in args.items() if k.lower() not in ("token", "authorization", "headers")}


# ---------------------------------------------------------------------------
# 通用适配器（mock 与 http 客户端共用）
# ---------------------------------------------------------------------------


def adapt_time(raw: Any) -> dict:
    d = raw if isinstance(raw, dict) else {}
    dt = _first(d, "datetime", "currentTime", "time", "now", default="")
    period = _first(d, "timePeriod", "period", "mealPeriod", default="")
    return {"datetime": str(dt), "time_period": str(period)}


def adapt_stores(raw: Any) -> list[Store]:
    out = []
    for r in _as_list(raw):
        if not isinstance(r, dict):
            continue
        code = _first(r, "storeCode", "code", "storeId", "id")
        if not code:
            continue
        out.append(
            Store(
                code=str(code),
                name=str(_first(r, "storeName", "name", "title", default="")),
                address=str(_first(r, "address", "storeAddress", default="")),
                city=str(_first(r, "city", "cityName", default="")),
                raw=r,
            )
        )
    return out


def adapt_meals(raw: Any) -> list[MenuItem]:
    out = []
    for r in _as_list(raw):
        if not isinstance(r, dict):
            continue
        code = _first(r, "mealCode", "code", "productCode", "id", "sku")
        name = _first(r, "mealName", "name", "title", "productName")
        if not code or not name:
            continue
        out.append(
            MenuItem(
                code=str(code),
                name=str(name),
                category=str(_first(r, "category", "categoryName", "type", default="")),
                price_cents=_extract_price_cents(r),
                tags=_extract_tags(r),
                raw=r,
            )
        )
    return out


def adapt_coupons(raw: Any, source: str) -> list[Coupon]:
    out = []
    for r in _as_list(raw):
        if not isinstance(r, dict):
            continue
        cid = _first(r, "couponId", "id", "code")
        name = _first(r, "couponName", "name", "title")
        if not cid or not name:
            continue
        kind = "threshold"
        value_cents = None
        threshold_cents = None
        percent = None
        # 满减：value/threshold 常见键名
        val = _first(r, "value", "amount", "discountAmount", "subtract")
        thr = _first(r, "threshold", "limitAmount", "useThreshold", "conditionAmount")
        pct = _first(r, "discount", "percent", "rate")
        if pct not in (None, ""):
            try:
                p = float(pct)
                percent = p / 100.0 if p > 1 else p
                kind = "percent"
            except (TypeError, ValueError):
                percent = None
        if val not in (None, ""):
            value_cents = yuan_to_cents(val)
        if thr not in (None, ""):
            threshold_cents = yuan_to_cents(thr)
        applicable = _first(r, "applicableMeals", "mealCodes", "applicableCodes", default=[])
        out.append(
            Coupon(
                id=str(cid),
                name=str(name),
                source=source,
                kind=kind if percent is not None else "threshold",
                value_cents=value_cents,
                threshold_cents=threshold_cents,
                percent=percent,
                applicable_codes=tuple(str(x) for x in _as_list(applicable) if x),
                raw=r,
            )
        )
    return out


def _to_int(v) -> Optional[int]:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def adapt_nutrition(raw: Any) -> list[NutritionItem]:
    out = []
    for r in _as_list(raw):
        if not isinstance(r, dict):
            continue
        name = _first(r, "foodName", "name", "mealName", "title")
        if not name:
            continue

        def num(*keys):
            v = _first(r, *keys)
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        out.append(
            NutritionItem(
                name=str(name),
                calories_kcal=num("energy", "energyKcal", "calories", "kcal", "热量", "能量"),
                protein_g=num("protein", "proteinG", "蛋白质"),
                fat_g=num("fat", "fatG", "脂肪"),
                carb_g=num("carbohydrate", "carb", "carbohydrateG", "碳水", "碳水化合物"),
                sodium_mg=num("sodium", "sodiumMg", "钠"),
                calcium_mg=num("calcium", "calciumMg", "钙"),
                raw=r,
            )
        )
    return out


def adapt_price(raw: Any) -> PriceQuote:
    d = raw if isinstance(raw, dict) else {}
    total = _first(d, "totalPrice", "payPrice", "total", "totalFee", "actualPrice")
    original = _first(d, "originalPrice", "totalOriginalPrice", "goodsPrice")
    discount = _first(d, "discountPrice", "discount", "couponDiscount")
    fee = _first(d, "deliveryFee", "deliveryPrice", "shippingFee", "packageFee")
    ok = _first(d, "success", "ok", default=True)
    msg = str(_first(d, "message", "msg", "errorMsg", default=""))
    if total in (None, ""):
        return PriceQuote(ok=False, message=msg or "calculate-price 未返回应付总价", raw=d)
    try:
        ok_flag = bool(ok) if isinstance(ok, bool) else str(ok).lower() in ("1", "true", "success")
    except Exception:
        ok_flag = True
    return PriceQuote(
        total_cents=yuan_to_cents(total),
        original_cents=yuan_to_cents(original),
        discount_cents=yuan_to_cents(discount),
        delivery_fee_cents=yuan_to_cents(fee),
        ok=ok_flag,
        message=msg,
        raw=d,
    )


def build_calc_payload(items: list, coupon_ids: list[str], dining_mode: str) -> dict:
    """构造 calculate-price 入参（引擎侧统一结构，适配层只负责发送）。"""
    return {
        "items": [{"mealCode": pi.item.code, "quantity": pi.qty} for pi in items],
        "couponIds": list(coupon_ids),
        "diningMode": dining_mode,
    }


def parse_tool_text(result: Any) -> Any:
    """把 MCP call_tool 返回的 content 解析成 Python 对象（通常是 JSON 字符串）。"""
    content = getattr(result, "content", None)
    if content is None and isinstance(result, dict):
        content = result.get("content")
    if isinstance(content, str):
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return content
    if isinstance(content, list) and content:
        first = content[0]
        text = getattr(first, "text", None)
        if text is None and isinstance(first, dict):
            text = first.get("text")
        if isinstance(text, str):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
    return content
