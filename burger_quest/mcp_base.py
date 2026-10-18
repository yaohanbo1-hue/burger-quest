"""MCP 客户端抽象层。

- MCPClient：引擎唯一依赖的接口。真实实现走官方 mcp Python SDK（mcp_http.py），
  演示/测试走 fixtures（mcp_mock.py）。
- 适配器（adapt_*）：把 MCP 真实返回规范化为引擎模型。
- 数据来源审计：每次调用记录到 MCPClient.calls（工具名/耗时/成功状态/序号/用途），
  最终写入任务书的数据来源区块。

调用审计字段（2026-10-14 效率优化新增）：
- seq：调用序号（从 1 开始）；duration_ms：耗时；purpose：用途；
- ok：成功/失败；attempts：实际尝试次数（含重试）；mcp_call：是否真的发起了
  MCP 请求（False = 去重/缓存命中，未发起请求）；note：备注。
- 安全：args 经 _safe_args 脱敏，绝不记录 token/authorization/headers。

═══════════════════════════════════════════════════════════════
真实返回格式（2026-10-13 用官方 Token 联调实测，mcp 1.27.2 / 2.3.0 复核）
═══════════════════════════════════════════════════════════════
1. 工具返回的是**文本**：前面是大段字段说明，JSON 在 `## Original Response` 之后，
   且 JSON 后面可能还跟着「数据传参规则」等说明文字。
   信封结构：{"success":true,"code":200,"message":"...","datetime":"...","traceId":"...","data":...}
   success=false 时 message 是业务错误（如「缺少参数」「外送场景下必传 beCode」）。
2. data 结构（按工具）：
   - now-time-info: data={datetime,...}
   - query-nearby-stores: data=[{storeCode,storeName,address,distance,businessStatus,...,beCode?}]
   - query-meals: data={categories:[{name,meals:[{code,tags[]}]}]}  ← 只有编码和标签，无名称无价格
   - query-meal-detail: data={code,name,rounds[],image,modification,supportModify}
   - query-store-coupons: data=[优惠券]
   - query-my-coupons: data=[优惠券]；无券时返回纯文本「暂无可用优惠券」（无 JSON）
   - list-nutrition-foods: data=**字符串**，自定义格式：
       [160]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:
         猪柳麦满分,null,1288,308,16,16,24,781,213
   - calculate-price: data={price,originalPrice,discount,productOriginalPrice,productPrice,
       productList:[{productCode,productName,quantity,subtotal,originalSubtotal}],takeWayList[]}
       ← **金额单位是「分」**（如 2250 = ¥22.50）
3. 工具参数（list_tools 实测 Schema）：
   - query-nearby-stores: 必填 beType(1到店自提/5得来速)、searchType(1收藏/2按位置)；
     可选 city、keyword（两者至少一个）
   - query-meals / query-meal-detail / query-store-coupons / calculate-price：
     必填 storeCode、orderType(1到店/2外送)、beType(1到店取餐/2麦乐送/5得来速/6团餐)；
     外送(beType=2)还必填 beCode（来自配送地址）
   - query-meal-detail 餐品编码参数名是 code（不是 mealCode）
   - calculate-price 商品参数是 items[].productCode；优惠券是 items[].couponId（挂在商品上）
"""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional

from .campaign import CampaignParseResult
from .models import (
    Campaign,
    Coupon,
    CouponQuery,
    MenuItem,
    NutritionItem,
    PriceQuote,
    Store,
    yuan_to_cents,
)

# 引擎实际调用的麦当劳 MCP 工具。
# 计数口径（2026-10-18 统一，文档与代码必须一致）：
#   7 个常规主链路：now-time-info / query-nearby-stores / query-meals /
#                   query-store-coupons / query-my-coupons /
#                   list-nutrition-foods / calculate-price
#   1 个餐品详情回退：query-meal-detail（名称价格默认来自算价 productList）
#   2 个外送专用：delivery-query-addresses / delivery-query-stores
#   2 个第一阶段扩展（只读）：campaign-calendar / available-coupons
# 合计 12 个可用工具；TOOLS_RESERVED 中的写操作/抽奖工具一律不主动调用。
TOOLS_USED = [
    "now-time-info",
    "query-nearby-stores",
    "query-meals",
    "query-meal-detail",
    "query-store-coupons",
    "query-my-coupons",
    "list-nutrition-foods",
    "calculate-price",
    "campaign-calendar",
    "available-coupons",
]
# 预留但 MVP 不主动调用的工具（写进文档说明边界）
TOOLS_RESERVED = [
    "delivery-query-addresses",
    "delivery-query-stores",
    "delivery-create-address",
    "auto-bind-coupons",
    "create-order",
    "cancel-order",
    "query-order",
    "query-lottery-info",
    "draw-lottery",
    "query-my-prizes",
]

# 工具 -> 调用用途（审计与调用次数分析用；calculate-price 的用途由调用方细化）
TOOL_PURPOSE = {
    "now-time-info": "time_context",
    "query-nearby-stores": "store_resolution",
    "delivery-query-addresses": "store_resolution",
    "delivery-query-stores": "store_resolution",
    "query-meals": "menu_skeleton",
    "query-meal-detail": "meal_name",
    "query-store-coupons": "coupon_pool",
    "query-my-coupons": "coupon_pool",
    "list-nutrition-foods": "nutrition",
    "calculate-price": "price_check",
    "campaign-calendar": "campaign_calendar",
    "available-coupons": "coupon_pool",
    "list_tools": "tool_discovery",
}

# 可短 TTL 缓存的只读工具 -> TTL 秒数（2026-10-14 效率优化）。
# 硬性排除：优惠券（券池/有效性实时变）、calculate-price（必须按当前方案实时核验）、
# 库存类信息——绝不把旧结果当实时结果。
CACHEABLE_TTL = {
    "now-time-info": 30.0,
    "query-nearby-stores": 120.0,
    "delivery-query-addresses": 60.0,
    "delivery-query-stores": 120.0,
    "query-meals": 300.0,
    "query-meal-detail": 300.0,
    "list-nutrition-foods": 600.0,
}


def summarize_calls(calls: list) -> dict:
    """调用审计摘要：真实请求数、去重命中数、失败数、总耗时、分工具计数。"""
    real = [c for c in calls if isinstance(c, dict) and c.get("mcp_call", True)]
    dedup = [c for c in calls if isinstance(c, dict) and not c.get("mcp_call", True)]
    by_tool: dict[str, int] = {}
    for c in real:
        by_tool[c.get("tool", "?")] = by_tool.get(c.get("tool", "?"), 0) + 1
    durations = [c.get("duration_ms") or 0.0 for c in real]
    return {
        "total_calls": len(real),
        "dedup_hits": len(dedup),
        "failed": sum(1 for c in real if c.get("ok") is False),
        "total_duration_ms": round(sum(durations), 1),
        "by_tool": by_tool,
    }


class MCPError(Exception):
    """MCP 调用失败（网络/鉴权/限流/服务端/业务错误）。消息面向用户，可解释。"""

    def __init__(self, tool: str, message: str, code: Optional[int] = None):
        self.tool = tool
        self.code = code
        super().__init__(f"[{tool}] {message}")


# ---------------------------------------------------------------------------
# 订单场景（官方工具需要 orderType + beType 组合，联调实测）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderScene:
    """一次点餐场景的门店 + 订单类型组合（官方算价/菜单/详情/券工具的统一入参）。"""

    store_code: str
    order_type: int  # 1=到店（含自取+得来速），2=外送
    be_type: int     # 1=到店取餐，2=麦乐送，5=得来速，6=企业团餐
    be_code: str = ""  # 外送/得来速必填（来自配送地址/门店查询）


def scene_for_mode(dining_mode: str, store_code: str = "", be_code: str = "") -> OrderScene:
    """用餐方式 -> 官方 orderType/beType 组合（联调实测语义）。"""
    if dining_mode == "delivery":
        return OrderScene(store_code, order_type=2, be_type=2, be_code=be_code)
    # 到店自取 / 堂食：官方 orderType=1 & beType=1（堂食与自取的区别在下单时的取餐方式）
    return OrderScene(store_code, order_type=1, be_type=1)


# ---------------------------------------------------------------------------
# 真实返回解析：从「说明文字 + JSON + 说明文字」中提取信封
# ---------------------------------------------------------------------------


def extract_api_json(text: str) -> Optional[dict]:
    """从 MCP 工具返回的文本中提取 API 信封 JSON。

    实测格式：字段说明 + `## Original Response` + {"success":...} + 传参规则说明。
    策略：从头扫描每个 `{`，尝试 raw_decode，取第一个含 success/data/code 键的对象。
    （字段说明里的 `{productName,...}` 不是合法 JSON，会被自然跳过。）
    """
    if not isinstance(text, str):
        return None
    decoder = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text, m.start())
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and ("success" in obj or "data" in obj or "code" in obj):
            return obj
    return None


def check_envelope(tool: str, payload: Any) -> Any:
    """校验 API 信封：success=false 时抛出带业务原因的 MCPError；成功则返回 data。"""
    if not isinstance(payload, dict):
        return payload
    if payload.get("success") is False:
        code = payload.get("code")
        msg = payload.get("message") or "未知业务错误"
        raise MCPError(
            tool,
            f"麦当劳 MCP 业务错误（code={code}）：{msg}",
            code=code if isinstance(code, int) else None,
        )
    return payload.get("data", payload)


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
        # 官方未定义容器键名的工具（如 delivery-query-addresses）按常见容器键兼容；
        # 含 "addresses"（配送地址推测容器名，2026-10-17 契约审计补充）。
        for k in ("list", "items", "data", "records", "result", "addresses"):
            if isinstance(v.get(k), list):
                return v[k]
        return [v]
    return [v]


def _to_cents_official(v) -> Optional[int]:
    """官方算价金额 -> 分。

    联调实测：官方返回**整数分**（如 2250 = ¥22.50）。
    防御：带小数的数值/字符串按「元」解释（防止单位变更导致 100 倍误差）。
    """
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    looks_yuan = (isinstance(v, str) and "." in v) or (isinstance(v, float) and not f.is_integer() and abs(f) < 1000)
    return int(round(f * 100)) if looks_yuan else int(round(f))


# ---------------------------------------------------------------------------
# 抽象接口
# ---------------------------------------------------------------------------


class MCPClient(ABC):
    """引擎唯一依赖的 MCP 客户端接口。

    每个方法：成功返回规范化数据；失败抛 MCPError（消息可读、带错误码）。
    """

    def __init__(self):
        self.calls: list[dict] = []  # 审计记录（见模块 docstring 的字段说明）
        self._seq = 0

    def _record(
        self,
        tool: str,
        args: dict,
        ok: bool,
        note: str = "",
        duration_ms: Optional[float] = None,
        purpose: str = "",
        attempts: int = 1,
        mcp_call: bool = True,
    ):
        """记录一次调用（或一次去重命中）。绝不记录 token/authorization/headers。"""
        self._seq += 1
        self.calls.append(
            {
                "seq": self._seq,
                "tool": tool,
                "args": _safe_args(args),
                "ok": ok,
                "note": note,
                "duration_ms": round(duration_ms, 1) if duration_ms is not None else None,
                "purpose": purpose or TOOL_PURPOSE.get(tool, ""),
                "attempts": attempts,
                "mcp_call": mcp_call,
            }
        )

    # -- 基础信息 -----------------------------------------------------------

    @abstractmethod
    def now_time(self) -> dict:
        """now-time-info -> {'datetime': ..., 'time_period': ...}"""

    @abstractmethod
    def list_tools(self) -> list[dict]:
        """工具发现（只读）-> [{'name', 'description', 'input_schema'}]。

        冒烟测试用它核对官方工具清单；Mock 返回演示版清单（无官方 Schema）。
        """

    @abstractmethod
    def nearby_stores(self, keyword: str, city: str = "", be_type: int = 1) -> list[Store]:
        """query-nearby-stores（searchType=2 按位置）-> 附近门店列表"""

    @abstractmethod
    def campaign_calendar(self, specified_date: Optional[str] = None) -> CampaignParseResult:
        """campaign-calendar（只读）-> CampaignParseResult（ok / empty / parse_error）。

        官方返回 Markdown 文本（非 JSON 信封）；解析见 campaign.py。
        specified_date：yyyy-MM-dd，可选；传入时官方返回该日前后各一个有活动的日子。
        仅展示官方活动，不派生奖品/优惠金额/资格。**空列表不等于有效空结果**：
        无法识别结构时必须返回 parse_error。
        """

    @abstractmethod
    def available_coupons(self) -> CouponQuery:
        """available-coupons（只读）-> CouponQuery（区分 ok / empty / unknown）。

        实测：无券时官方返回 code=499 + data=null（业务语义是「无券」），
        与 query-my-coupons 的 success:true + coupons:[] 等价；必须区分，
        不能把「未知」当成「0 张券」。
        第一阶段只查询展示，**绝不调用 auto-bind-coupons、绝不领取**。
        """

    @abstractmethod
    def delivery_addresses(self) -> list[dict]:
        """delivery-query-addresses -> 用户已保存的配送地址（含 addressId）"""

    @abstractmethod
    def delivery_stores(self, address_id: str, be_type: int = 2) -> list[Store]:
        """delivery-query-stores -> 某配送地址可配送的门店（含 beCode）"""

    @abstractmethod
    def query_meals(self, scene: OrderScene) -> list[MenuItem]:
        """query-meals -> 门店可售餐品「骨架」（编码/分类/标签；名称价格需详情与算价补充）"""

    @abstractmethod
    def meal_detail(self, scene: OrderScene, code: str) -> dict:
        """query-meal-detail -> {code, name, rounds, is_combo}"""

    @abstractmethod
    def store_coupons(self, scene: OrderScene) -> list[Coupon]:
        """query-store-coupons -> 当前门店可用优惠券"""

    @abstractmethod
    def my_coupons(self) -> list[Coupon]:
        """query-my-coupons -> 我的卡包优惠券"""

    @abstractmethod
    def list_nutrition_foods(self) -> list[NutritionItem]:
        """list-nutrition-foods -> 常见餐品营养数据"""

    @abstractmethod
    def calculate_price(self, scene: OrderScene, items: list, coupon_ids: list, purpose: str = "") -> PriceQuote:
        """calculate-price -> 权威价格计算（金额单位为分）。

        purpose: price_discovery=短名单价格发现 | plan_verification=组合验价。
        本工具绝不缓存——每次都必须按当前方案实时核验。
        """

    # -- 数据来源描述 -------------------------------------------------------

    def connect(self):
        """建立会话（Mock/离线客户端为无操作；真实客户端建立 MCP 会话）。"""

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
    dt = _first(d, "datetime", "currentTime", "time", "now", "formatted", default="")
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
                be_code=str(_first(r, "beCode", "be_code", default="") or ""),
                raw=r,
            )
        )
    return out


def adapt_meals(raw: Any) -> list[MenuItem]:
    """query-meals 返回 {categories:[{name, meals:[{code,tags}]}]}。

    注意：官方菜单**不含名称与价格**——名称由 query-meal-detail 补充，
    价格由 calculate-price 补充。这里只带回编码/分类/标签。
    """
    cats: list = []
    if isinstance(raw, dict):
        cats = raw.get("categories") or raw.get("list") or []
    elif isinstance(raw, list):
        cats = raw
    out: list[MenuItem] = []
    seen = set()
    for cat in cats:
        if not isinstance(cat, dict):
            continue
        cat_name = str(cat.get("name", "") or "")
        for meal in _as_list(cat.get("meals") or cat.get("items")):
            if not isinstance(meal, dict):
                continue
            code = _first(meal, "code", "mealCode", "productCode")
            if not code:
                continue
            code = str(code)
            if code in seen:
                continue
            seen.add(code)
            out.append(
                MenuItem(
                    code=code,
                    name="",  # 名称待 query-meal-detail 补充
                    category=cat_name,
                    price_cents=None,  # 价格待 calculate-price 补充
                    tags=_extract_tags(meal),
                    raw=meal,
                )
            )
    return out


def adapt_meal_detail(raw: Any) -> dict:
    """query-meal-detail -> {code, name, rounds, is_combo}。"""
    d = raw if isinstance(raw, dict) else {}
    data = d.get("data")
    if not isinstance(data, dict):
        data = d  # 无 data 包裹时信封即数据本体
    code = str(_first(data, "code", "mealCode", default="") or "")
    name = str(_first(data, "name", "mealName", default="") or "")
    rounds = data.get("rounds") or []
    return {
        "code": code,
        "name": name,
        "rounds": rounds if isinstance(rounds, list) else [],
        "is_combo": bool(rounds),
        "raw": data,
    }


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
            # 优惠券金额单位按「元」解析（官方未在文档中明确；联调时账号无券可验证，
            # 已在 MCP_INTEGRATION.md 待联调项登记）
            value_cents = yuan_to_cents(val)
        if thr not in (None, ""):
            threshold_cents = yuan_to_cents(thr)
        # couponCode：官方算价要求与 couponId 同时提交（联调实测 600010）。
        # 券列表响应中的字段名未经真实有券账户验证（联调账户无券），按官方算价
        # Schema 的命名惯例取候选；为空时只传 couponId（官方会拒绝，引擎诚实回退无券）。
        code = _first(r, "couponCode", "coupon_code", default="")
        applicable = _first(r, "applicableMeals", "mealCodes", "applicableCodes", "productCodes", default=[])
        out.append(
            Coupon(
                id=str(cid),
                name=str(name),
                code=str(code or ""),
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


def parse_nutrition_text(text: str) -> list[dict]:
    """解析 list-nutrition-foods 的自定义文本格式（联调实测）。

    格式：[N]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:
           名称,null,能量kJ,能量kcal,蛋白质,脂肪,碳水,钠,钙
    返回 dict 列表（键为表头）。
    """
    if not isinstance(text, str):
        return []
    m = re.search(r"\[\d+\]\{([^}]*)\}", text)
    if not m:
        return []
    headers = [h.strip() for h in m.group(1).split(",")]
    rows = []
    for line in text[m.end() :].splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ">", "-", "[")):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != len(headers):
            continue
        rows.append(dict(zip(headers, parts)))
    return rows


def adapt_nutrition(raw: Any) -> list[NutritionItem]:
    """data 是自定义文本（联调实测）；兼容 data 为 list/dict 的情况。"""
    rows: list[dict] = []
    if isinstance(raw, str):
        rows = parse_nutrition_text(raw)
    else:
        for r in _as_list(raw):
            if isinstance(r, dict):
                rows.append(r)
    out = []
    for r in rows:
        name = _first(r, "productName", "foodName", "name", "mealName")
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
                calories_kcal=num("energyKcal", "energy", "calories", "kcal", "热量", "能量"),
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
    """calculate-price -> PriceQuote。金额单位：分（联调实测）。"""
    d = raw if isinstance(raw, dict) else {}
    if isinstance(d.get("data"), dict):
        d = d["data"]
    total = _first(d, "price", "totalPrice", "payPrice", "total", "totalFee", "actualPrice")
    original = _first(d, "originalPrice", "totalOriginalPrice", "goodsPrice", "productOriginalPrice")
    discount = _first(d, "discount", "discountPrice", "couponDiscount")
    fee = _first(d, "deliveryFee", "deliveryPrice", "shippingFee", "packageFee")
    msg = str(_first(d, "message", "msg", "errorMsg", default=""))
    if total in (None, ""):
        return PriceQuote(ok=False, message=msg or "calculate-price 未返回应付总价", raw=d)
    return PriceQuote(
        total_cents=_to_cents_official(total),
        original_cents=_to_cents_official(original),
        discount_cents=_to_cents_official(discount),
        delivery_fee_cents=_to_cents_official(fee),
        ok=True,
        message=msg,
        raw=d,
    )


def extract_product_prices(quote: PriceQuote) -> dict:
    """从 calculate-price 的 productList 提取「单品实付（分）」。

    用于价格发现：一次调用传入多件商品（各 1 件），从返回的 productList
    得到每件商品的 subtotal，即该商品的真实单价（分）——不是估算，是权威返回。

    2026-10-16 审计加固：
    - quantity≥2 时按「行小计 ÷ 数量」归一化为单价（防止把 2 件小计当成单价高估）；
    - quantity≤0 的异常行跳过（除零保护）；
    - 非正金额（0 或负）**不在此处过滤**——由调用方（service）结合语境排除并明示，
      保持本函数「忠实提取」的单一职责。
    """
    out: dict[str, int] = {}
    if not quote.ok:
        return out
    for entry in _as_list(quote.raw.get("productList")):
        if not isinstance(entry, dict):
            continue
        code = _first(entry, "productCode", "code")
        subtotal = _first(entry, "subtotal", "price")
        if code and subtotal not in (None, ""):
            cents = _to_cents_official(subtotal)
            if cents is None:
                continue
            qty_raw = _first(entry, "quantity", "qty", "count")
            try:
                qty = int(qty_raw) if qty_raw not in (None, "") else 1
            except (TypeError, ValueError):
                qty = 1
            if qty >= 2:
                cents = cents // qty  # 行小计归一为单价
            out[str(code)] = cents
    return out


def extract_product_names(quote: PriceQuote) -> dict:
    """从 calculate-price 的 productList 提取商品名称（联调实测自带）。"""
    out: dict[str, str] = {}
    if not quote.ok:
        return out
    for entry in _as_list(quote.raw.get("productList")):
        if not isinstance(entry, dict):
            continue
        code = _first(entry, "productCode", "code")
        name = _first(entry, "productName", "name")
        if code and name:
            out[str(code)] = str(name)
    return out


def resolve_coupon_pair(coupon) -> tuple[str, str]:
    """把任意券形态解析为 (couponId, couponCode)。

    兼容：Coupon 对象 / (id, code) 元组 / 纯 id 字符串。
    官方 calculate-price 要求两者同时提交（真实联调实测 600010）。
    """
    if coupon is None:
        return "", ""
    if isinstance(coupon, str):
        return coupon, ""
    if isinstance(coupon, (tuple, list)) and len(coupon) >= 1:
        cid = str(coupon[0] or "")
        code = str(coupon[1] or "") if len(coupon) > 1 else ""
        return cid, code
    return str(getattr(coupon, "id", "") or ""), str(getattr(coupon, "code", "") or "")


def build_calc_payload(scene: OrderScene, items: list, coupons: list) -> dict:
    """构造 calculate-price 入参（联调实测 Schema，2026-10-16 补 couponCode）。

    items: list[PlanItem]；coupons 与商品**一一配对**挂载。
    官方 Schema（list_tools 实测，2026-10-17 契约审计快照）：
    items[] 的每项都是独立对象，`couponId` 与 `couponCode` 定义在**商品级**
    （items.items.properties），即每件商品可各带一张券——不是「整单一张券」。

    因此：
    - 第 i 张券挂到第 i 件商品（i < len(items)；coupons 按调用方给定的优先级排序）；
    - 券数 > 商品数时，超出部分**不静默丢弃**——由调用方（planner.coupon_sets）
      保证试探规模不超过商品数；本函数只挂得下的部分，避免伪造提交。

    coupons 兼容三种形态（按优先级）：
    - Coupon 对象：取 .id 与 .code（code 为空则只传 couponId）；
    - (id, code) 元组；
    - 纯字符串（仅 id，旧调用路径/mock）。
    """
    pairs = [resolve_coupon_pair(c) for c in (coupons or [])]
    payload_items = []
    for i, pi in enumerate(items):
        entry = {"productCode": pi.item.code, "quantity": pi.qty}
        if i < len(pairs):
            coupon_id, coupon_code = pairs[i]
            if coupon_id:
                entry["couponId"] = coupon_id
                if coupon_code:
                    entry["couponCode"] = coupon_code
        payload_items.append(entry)
    payload = {
        "storeCode": scene.store_code,
        "orderType": scene.order_type,
        "beType": scene.be_type,
        "items": payload_items,
    }
    if scene.be_code:
        payload["beCode"] = scene.be_code
    return payload


def parse_tool_text(result: Any) -> Any:
    """把 MCP call_tool 返回解析为 Python 对象。

    联调实测：content 文本 = 字段说明 + JSON 信封 + 传参规则说明。
    优先提取信封 JSON；提取不到则原样返回文本（由适配器/调用方处理）。

    协议合规说明（2026-10-17 稳定性增强）：Streamable HTTP 的 SSE 流由官方
    SDK 在传输层完成聚合，到这里 content 只是 ContentBlock 列表——本函数只做
    「把所有文本块按序拼接 → 从文本里找信封」，绝不把 SSE 事件、普通 JSON、
    JSON-RPC 帧混为一谈（不自研协议解析）。多个文本块按序拼接是防御性的：
    个别服务端/代理形态可能把说明与信封拆成两个块。
    """
    content = getattr(result, "content", None)
    if content is None and isinstance(result, dict):
        content = result.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list) and content:
        texts = []
        for block in content:
            block_text = getattr(block, "text", None)
            if block_text is None and isinstance(block, dict):
                block_text = block.get("text")
            if isinstance(block_text, str):
                texts.append(block_text)
        joined = "\n".join(texts) if texts else None
        if joined is None:
            return content
        text = joined
    else:
        return content
    envelope = extract_api_json(text)
    return envelope if envelope is not None else text
