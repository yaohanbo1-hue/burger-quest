"""真实 MCP 客户端：通过官方 mcp Python SDK 以 Streamable HTTP 连接麦当劳 MCP Server。

架构说明（为什么这样设计）：
- 引擎核心（service/planner/既有测试）全部是同步代码，MCPClient 接口也是同步的；
  本类在内部维护一个「后台事件循环线程」，把每次工具调用提交过去执行，
  对外仍呈现同步接口——service / Mock / 既有测试零改动。
- 不重复实现 MCP 协议：仅薄封装官方 mcp SDK 的 ClientSession。
- 连接生命周期：connect()（或 with 进入）时建立会话，close()（或 with 退出）时释放。
- 测试友好：_check_sdk 与 _open_session 可在实例上替换，从而在无网络/无 SDK 的
  环境下用假会话验证完整调用链。

SDK 版本兼容（实测 mcp 1.27.2 / 2.3.0，2026-10-13 联调发现并处理）：
1. 导出名：旧版 streamablehttp_client，新版 streamable_http_client（_import_stream_client 双兼容）；
2. 请求头：旧版工厂直接收 headers=，新版只收 http_client=（需自建 httpx2.AsyncClient）；
3. transport yield：旧版 (read, write, get_session_id)，新版 (read, write)；
4. list_tools 返回：1.x 是裸 list，2.x 是 ListToolsResult（.tools 属性）——list_tools 双兼容；
5. HTTP 状态码：新版 SDK 把 401/429 包装成 JSON-RPC MCPError 且不暴露状态码，
   因此自建 http_client 时挂 event hook 记录最近状态码，用于还原 401/429 语义。

错误通道（两条都不能漏，2026-10-13 阶段 5 实测确认）：
- 业务错误：服务端把错误放在文本信封 success=false 里 -> check_envelope 转 MCPError；
- 协议错误：SDK/协议层 isError=True（如未知工具名的部分形态）/ JSON-RPC 异常
  -> _raise_if_protocol_error / _submit 异常映射转 MCPError。
两条通道都不允许「未知格式猜一个值」后静默放过。

安全约定：
- Token 只从环境变量 MCD_MCP_TOKEN 或调用方显式传入，绝不落库、绝不写日志；
- 审计记录（MCPClient.calls）会自动剔除 token/authorization 字段。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import os
import threading
from typing import Optional

from .mcp_base import (
    MCPClient,
    MCPError,
    OrderScene,
    adapt_coupons,
    adapt_meal_detail,
    adapt_meals,
    adapt_nutrition,
    adapt_price,
    adapt_stores,
    adapt_time,
    build_calc_payload,
    check_envelope,
    parse_tool_text,
)

MCD_MCP_URL = "https://mcp.mcd.cn"
DEFAULT_TIMEOUT = 60.0


def _import_stream_client():
    """导入官方 SDK 的 Streamable HTTP 客户端工厂（双版本兼容）。"""
    try:
        from mcp import ClientSession  # noqa: F401
    except ImportError as e:
        raise MCPError(
            "mcd-mcp",
            "缺少官方 mcp SDK，真实模式需要它：请先执行 pip install mcp"
            "（演示模式 demo 无需安装，可离线运行）",
        ) from e
    try:
        from mcp.client.streamable_http import streamablehttp_client  # 旧版 SDK

        return streamablehttp_client
    except ImportError:
        pass
    try:
        from mcp.client.streamable_http import streamable_http_client  # 新版 SDK

        return streamable_http_client
    except ImportError as e:
        raise MCPError(
            "mcd-mcp",
            f"已安装的 mcp SDK 版本不兼容（找不到 Streamable HTTP 客户端导出）：{e}；"
            "请升级：pip install -U mcp",
        ) from e


def _map_sdk_error(tool: str, e: Exception, http_status: Optional[int] = None) -> MCPError:
    """把 SDK/网络异常翻译成用户可懂的 MCPError（含 401/429 官方错误码语义）。

    http_status：自建 http_client 的 event hook 记录的真实 HTTP 状态码（新版 SDK 会吞掉它）。
    """
    if http_status == 401 or "401" in str(e):
        return MCPError(
            tool,
            "MCP Token 无效或已过期（401）：请到 https://open.mcd.cn/mcp 重新申请，"
            "并更新环境变量 MCD_MCP_TOKEN",
            401,
        )
    if http_status == 429 or "429" in str(e):
        return MCPError(
            tool,
            "触发麦当劳 MCP 限流（429，每 Token 每分钟 600 次）：请稍后重试",
            429,
        )
    msg = str(e)
    low = msg.lower()
    if any(k in low for k in ("connect", "ssl", "network", "timed out", "unreachable", "resolve")):
        return MCPError(tool, f"MCP 连接失败：{msg}；请检查网络能否访问 https://mcp.mcd.cn")
    return MCPError(tool, f"MCP 调用失败：{msg}")


def _protocol_error_text(result) -> Optional[str]:
    """提取 MCP 协议级错误文本（isError/is_error=True 时）；非错误返回 None。

    属性名兼容：1.x 用 isError，2.x 用 is_error（pydantic 模型字段）。
    """
    flag = getattr(result, "is_error", None)
    if flag is None:
        flag = getattr(result, "isError", None)
    if not flag:
        return None
    content = getattr(result, "content", None)
    if isinstance(content, list) and content:
        first = content[0]
        text = getattr(first, "text", None)
        if text is None and isinstance(first, dict):
            text = first.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    if isinstance(content, str) and content.strip():
        return content.strip()
    return ""


class HttpMCPClient(MCPClient):
    """生产客户端（同步门面）。token 缺失时构造即失败（避免误以为连上了真实服务）。"""

    def __init__(self, token: Optional[str] = None, url: str = MCD_MCP_URL, timeout: float = DEFAULT_TIMEOUT):
        super().__init__()
        self.token = token or os.environ.get("MCD_MCP_TOKEN")
        if not self.token:
            raise MCPError(
                "mcd-mcp",
                "未检测到 MCD_MCP_TOKEN。请先到 https://open.mcd.cn/mcp 申请 MCP Token，"
                "并以环境变量方式提供（不要把 Token 写进代码或提交到仓库）。"
                "如需离线体验请使用 demo 子命令——真实模式不会自动切换到演示数据。",
                code=401,
            )
        self.url = url
        self.timeout = timeout
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._session = None
        self._stack = None
        self._http_client = None
        self._http_statuses: list[int] = []  # event hook 记录的 HTTP 状态码（还原 401/429）

    # -- 连接管理 -----------------------------------------------------------

    def __enter__(self) -> "HttpMCPClient":
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def connect(self):
        """建立 MCP 会话（幂等）。失败抛 MCPError，消息可读，不泄露 Token。"""
        if self._loop is not None:
            return
        self._check_sdk()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="mcd-mcp-loop")
        self._thread.start()
        try:
            self._submit(self._open_session())
        except BaseException:
            self.close()
            raise

    def close(self):
        """释放会话与后台线程（幂等、安全，未连接时为无操作）。"""
        loop = self._loop
        thread = self._thread
        if loop is None:
            return
        session, stack, http_client = self._session, self._stack, self._http_client
        # 先摘掉状态，保证重复 close / 异常路径安全
        self._loop = None
        self._thread = None
        self._session = None
        self._stack = None
        self._http_client = None

        async def _close():
            for closer in (session, stack):
                if closer is not None:
                    try:
                        await closer.__aexit__(None, None, None)
                    except Exception:
                        pass
            if http_client is not None:
                try:
                    await http_client.aclose()
                except Exception:
                    pass

        try:
            asyncio.run_coroutine_threadsafe(_close(), loop).result(timeout=10)
        except Exception:
            pass
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5)

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    @staticmethod
    def _check_sdk():
        """真实模式依赖官方 mcp SDK；缺失/不兼容时给出可操作的提示（演示模式不需要）。"""
        _import_stream_client()

    async def _open_session(self):
        stream_client = _import_stream_client()
        from mcp import ClientSession

        auth_headers = {"Authorization": f"Bearer {self.token}"}
        self._stack = stream_client(self.url, **self._transport_kwargs(stream_client, auth_headers))
        entered = await self._stack.__aenter__()
        # 旧版 SDK yield (read, write, get_session_id)；新版 yield (read, write)
        read, write = entered[0], entered[1]
        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        await self._session.initialize()

    def _transport_kwargs(self, stream_client, auth_headers: dict) -> dict:
        """按 SDK 版本选择传 Authorization 的方式，并记录 HTTP 状态码。

        - 旧版 SDK：streamablehttp_client(url, headers=...) 直接收 headers；
        - 新版 SDK（实测 1.27.2）：只收 http_client，需自建 httpx2.AsyncClient 带 headers；
          同时挂 event hook 记录状态码（新版 SDK 会把 401/429 包装掉，不暴露状态码）。
        """
        try:
            params = inspect.signature(stream_client).parameters
        except (TypeError, ValueError):
            params = {}
        if "headers" in params:
            return {"headers": auth_headers}
        if "http_client" in params:
            try:
                import httpx2
            except ImportError as e:
                raise MCPError(
                    "mcd-mcp",
                    "当前 mcp SDK 需要 httpx2 传递请求头，但 httpx2 不可用；请升级：pip install -U mcp",
                ) from e

            async def _record_status(response):
                try:
                    self._http_statuses.append(int(response.status_code))
                    if len(self._http_statuses) > 20:
                        del self._http_statuses[:-20]
                except Exception:
                    pass

            self._http_client = httpx2.AsyncClient(headers=auth_headers, event_hooks={"response": [_record_status]})
            return {"http_client": self._http_client}
        # 签名未知：按旧版习惯传 headers，让 SDK 自己报错
        return {"headers": auth_headers}

    # -- 统一调用 -----------------------------------------------------------

    def _map_error(self, tool: str, e: Exception) -> MCPError:
        status = self._http_statuses[-1] if self._http_statuses else None
        return _map_sdk_error(tool, e, http_status=status)

    def _submit(self, coro, tool: str = "mcd-mcp"):
        """把协程提交到后台事件循环，同步等待结果；异常一律映射为 MCPError。"""
        if self._loop is None:
            coro.close()
            raise MCPError(tool, "MCP 会话未建立：请先 connect()，或使用 with HttpMCPClient(...) 上下文")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout=self.timeout)
        except MCPError:
            raise
        except (TimeoutError, concurrent.futures.TimeoutError) as e:
            fut.cancel()
            raise MCPError(tool, f"MCP 调用超时（{self.timeout:.0f} 秒无响应）；请检查网络后重试") from e
        except Exception as e:
            raise self._map_error(tool, e) from e

    def _call(self, tool: str, arguments: dict):
        """调用工具并做两级校验：协议级 isError -> 信封 success=false。

        两级都不允许静默放过：错误一律转成带原因的 MCPError，
        绝不允许「未知格式猜一个值」后继续往下走。
        """
        if self._session is None:
            raise MCPError(tool, "MCP 会话未建立：请先 connect()，或使用 with HttpMCPClient(...) 上下文")

        async def _invoke():
            return await self._session.call_tool(tool, arguments)

        result = self._submit(_invoke(), tool=tool)
        protocol_error = _protocol_error_text(result)
        if protocol_error is not None:
            self._record(tool, arguments, False, note="isError")
            raise MCPError(
                tool,
                f"MCP 协议错误（isError）：{protocol_error or '服务端拒绝了该调用'}",
            )
        self._record(tool, arguments, True)
        return check_envelope(tool, parse_tool_text(result))

    def list_tools(self) -> list[dict]:
        """工具发现（只读）：返回 [{name, description, input_schema}]。

        SDK 1.x 的 list_tools() 返回裸 list；2.x 返回 ListToolsResult（.tools）。
        冒烟测试用它核对「项目依赖的工具仍在官方工具清单里」。
        """
        if self._session is None:
            raise MCPError("list_tools", "MCP 会话未建立：请先 connect()，或使用 with HttpMCPClient(...) 上下文")

        async def _invoke():
            return await self._session.list_tools()

        result = self._submit(_invoke(), tool="list_tools")
        self._record("list_tools", {}, True)
        tools = getattr(result, "tools", None)
        if tools is None and isinstance(result, (list, tuple)):
            tools = list(result)
        out = []
        for t in tools or []:
            schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None)
            out.append(
                {
                    "name": str(getattr(t, "name", "") or ""),
                    "description": str(getattr(t, "description", "") or ""),
                    "input_schema": schema,
                }
            )
        return out

    # -- 工具封装（参数名与 orderType/beType 组合均为 2026-10-13 联调实测） --

    def now_time(self) -> dict:
        return adapt_time(self._call("now-time-info", {}))

    def nearby_stores(self, keyword: str, city: str = "", be_type: int = 1):
        # searchType=2 按位置搜索；city 与 keyword 至少一个（官方校验）
        args = {"beType": be_type, "searchType": 2}
        if city:
            args["city"] = city
        if keyword:
            args["keyword"] = keyword
        return adapt_stores(self._call("query-nearby-stores", args))

    def delivery_addresses(self) -> list[dict]:
        raw = self._call("delivery-query-addresses", {})
        if isinstance(raw, dict):
            addresses = raw.get("addresses") or raw.get("list") or []
        else:
            addresses = raw if isinstance(raw, list) else []
        return [a for a in addresses if isinstance(a, dict)]

    def delivery_stores(self, address_id: str, be_type: int = 2):
        return adapt_stores(
            self._call("delivery-query-stores", {"beType": be_type, "addressId": address_id})
        )

    def query_meals(self, scene: OrderScene):
        return adapt_meals(
            self._call(
                "query-meals",
                {
                    "storeCode": scene.store_code,
                    "orderType": scene.order_type,
                    "beType": scene.be_type,
                    **({"beCode": scene.be_code} if scene.be_code else {}),
                },
            )
        )

    def meal_detail(self, scene: OrderScene, code: str) -> dict:
        return adapt_meal_detail(
            self._call(
                "query-meal-detail",
                {
                    "storeCode": scene.store_code,
                    "orderType": scene.order_type,
                    "beType": scene.be_type,
                    "code": code,
                    **({"beCode": scene.be_code} if scene.be_code else {}),
                },
            )
        )

    def store_coupons(self, scene: OrderScene):
        return adapt_coupons(
            self._call(
                "query-store-coupons",
                {
                    "storeCode": scene.store_code,
                    "orderType": scene.order_type,
                    "beType": scene.be_type,
                    **({"beCode": scene.be_code} if scene.be_code else {}),
                },
            ),
            source="store",
        )

    def my_coupons(self):
        return adapt_coupons(self._call("query-my-coupons", {}), source="my")

    def list_nutrition_foods(self):
        return adapt_nutrition(self._call("list-nutrition-foods", {}))

    def calculate_price(self, scene: OrderScene, items: list, coupon_ids: list):
        payload = build_calc_payload(scene, items, coupon_ids)
        return adapt_price(self._call("calculate-price", payload))
