"""真实 MCP 客户端：通过官方 mcp Python SDK 以 Streamable HTTP 连接麦当劳 MCP Server。

为什么不手写协议：比赛要求「没有必要时不要自行重复实现 MCP Client」。
本客户端只是官方 SDK 的一层薄封装 + 防御式适配，不重复实现 MCP 协议本身。

安全约定：
- Token 只从环境变量 MCD_MCP_TOKEN 或调用方显式传入，绝不落库、绝不写日志；
- 审计记录（MCPClient.calls）会自动剔除 token/authorization 字段。
"""
from __future__ import annotations

import os
from typing import Optional

from . import mcp_base
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
    parse_tool_text,
)

MCD_MCP_URL = "https://mcp.mcd.cn"


class HttpMCPClient(MCPClient):
    """生产客户端。token 缺失时构造即失败（避免误以为连上了真实服务）。"""

    def __init__(self, token: Optional[str] = None, url: str = MCD_MCP_URL):
        super().__init__()
        self.token = token or os.environ.get("MCD_MCP_TOKEN")
        if not self.token:
            raise MCPError(
                "mcd-mcp",
                "未检测到 MCD_MCP_TOKEN。请先到 https://open.mcd.cn/mcp 申请 MCP Token，"
                "并以环境变量方式提供（不要把 Token 写进代码或提交到仓库）。",
                code=401,
            )
        self.url = url
        self._session = None
        self._stack = None

    # -- 连接管理 -----------------------------------------------------------

    async def __aenter__(self):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        self._stack = streamablehttp_client(self.url, headers={"Authorization": f"Bearer {self.token}"})
        read, write, _ = await self._stack.__aenter__()
        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        await self._session.initialize()
        return self

    async def __aexit__(self, *exc):
        for closer in (self._session, self._stack):
            if closer is not None:
                try:
                    await closer.__aexit__(*exc)
                except Exception:
                    pass
        self._session = None
        self._stack = None

    # -- 统一调用 -----------------------------------------------------------

    async def _call(self, tool: str, arguments: dict) -> object:
        if self._session is None:
            raise MCPError(tool, "MCP 会话未建立，请使用 async with HttpMCPClient(...) 进入上下文")
        try:
            result = await self._session.call_tool(tool, arguments)
        except Exception as e:  # SDK 异常统一转成可读错误
            msg = str(e)
            code = None
            for candidate in (401, 429):
                if str(candidate) in msg:
                    code = candidate
            if code == 401:
                raise MCPError(tool, "MCP Token 无效或已过期（401），请检查 Authorization 配置", 401) from e
            if code == 429:
                raise MCPError(tool, "触发限流（429，600 次/分钟），请稍后重试", 429) from e
            raise MCPError(tool, f"MCP 调用失败：{msg}") from e
        self._record(tool, arguments, True)
        return parse_tool_text(result)

    # -- 工具封装 -----------------------------------------------------------

    async def now_time(self) -> dict:
        return adapt_time(await self._call("now-time-info", {}))

    async def nearby_stores(self, address: str):
        raw = await self._call("query-nearby-stores", {"address": address})
        return adapt_stores(raw)

    async def query_meals(self, store_code: str):
        raw = await self._call("query-meals", {"storeCode": store_code})
        return adapt_meals(raw)

    async def meal_detail(self, meal_code: str) -> dict:
        raw = await self._call("query-meal-detail", {"mealCode": meal_code})
        return raw if isinstance(raw, dict) else {"raw": raw}

    async def store_coupons(self, store_code: str):
        raw = await self._call("query-store-coupons", {"storeCode": store_code})
        return adapt_coupons(raw, source="store")

    async def my_coupons(self):
        raw = await self._call("query-my-coupons", {})
        return adapt_coupons(raw, source="my")

    async def list_nutrition_foods(self):
        raw = await self._call("list-nutrition-foods", {})
        return adapt_nutrition(raw)

    async def calculate_price(self, store_code: str, items: list, coupon_ids: list, dining_mode: str):
        payload = build_calc_payload(items, coupon_ids, dining_mode)
        payload["storeCode"] = store_code
        raw = await self._call("calculate-price", payload)
        return adapt_price(raw)
