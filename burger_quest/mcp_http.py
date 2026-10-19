"""真实 MCP 客户端：通过官方 mcp Python SDK 以 Streamable HTTP 连接麦当劳 MCP Server。

架构说明（为什么这样设计）：
- 引擎核心（service/planner/既有测试）全部是同步代码，MCPClient 接口也是同步的；
  本类在内部维护一个「后台事件循环线程」，把每次工具调用提交过去执行，
  对外仍呈现同步接口——service / Mock / 既有测试零改动。
- 不重复实现 MCP 协议：仅薄封装官方 mcp SDK 的 ClientSession。
- 连接生命周期：connect()（或 with 进入）时建立会话，close()（或 with 退出）时释放。
- 测试友好：_check_sdk 与 _open_session 可在实例上替换，从而在无网络/无 SDK 的
  环境下用假会话验证完整调用链。

效率与可靠性（2026-10-14 优化，基线实测后实施）：
- 分工具超时：默认 30s，calculate-price 45s（最重）；单次调用不会卡死任务；
- 有限退避重试：瞬时网络错误/5xx 最多 3 次（0.5s/1s/2s），429 最多 2 次（2s/4s，
  尊重限流并降速），401 不重试（直接提示检查 Token），业务错误（信封 success=false）
  与协议错误不重试（确定性响应）；
- 任务内去重 + 短 TTL 只读缓存：now-time-info 30s / 门店 2min / 菜单与详情 5min /
  营养 10min；优惠券与 calculate-price 绝不缓存（必须实时核验）；
- 调用审计：seq/duration_ms/purpose/attempts/mcp_call（见 mcp_base._record），
  绝不记录 Token。

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

超时与传输层（2026-10-17 全链路体检新增）：
- 连接超时与读取超时**分离**：httpx2.AsyncClient 显式配置
  Timeout(connect=10s, read=60s, write=30s, pool=10s)。read 必须大于最重工具
  的外层等待（calculate-price 45s），否则 SDK 默认 5s 读取超时会提前误杀
  慢但正常的权威算价；外层 _submit 仍是任务级总截止（30s/45s）。
- 错误映射区分 ConnectTimeout（网络/TLS 握手问题）与 ReadTimeout（服务端
  出数慢或被限速）——两者排查方向完全相反，之前都笼统报「连接失败」；
- 新 SDK 把 401/403/429 包装成 JSON-RPC 错误且不暴露状态码：403 与 401
  的处置不同（401=Token 失效，403=无权限/被 WAF 拦截，实测无 Authorization
  的 POST 正是 403），必须分别给出排查建议。

无信封响应（2026-10-17 体检新增）：
- 联调实测：除 query-my-coupons（无券时返回纯文本「暂无可用优惠券」）与
  delivery-query-addresses（无地址时可能返回纯文本说明）外，所有工具都返回
  「说明文字 + ## Original Response + JSON 信封」。这些工具若返回纯文本，
  说明是网关错误页/服务异常，**必须明确失败**（MCPError「响应解析失败」），
  不允许静默降级成空菜单/空门店——那会把协议故障误报成业务空结果。

安全约定：
- Token 只从环境变量 MCD_MCP_TOKEN 或调用方显式传入，绝不落库、绝不写日志；
- 审计记录（MCPClient.calls）会自动剔除 token/authorization 字段；
- 详细诊断信息（尝试次数/HTTP 状态/错误类型/耗时/trace）写**脱敏日志**
  （burger_quest/diagnostics.py，设置 BURGER_QUEST_LOG 才启用），面向用户的
  错误提示只给简洁原因与下一步建议。

重试策略（2026-10-17 稳定性增强，有上限、防风暴）：
- 重试对象：瞬时网络错误/5xx（最多 3 次尝试）、429（最多 2 次尝试）；
  401/403/业务错误（信封 success=false）/协议错误（isError）**不重试**；
- **Retry-After**：服务端在 429/503 响应头给出时按它等待（封顶 30s），
  否则用内置退避（0.5/1/2s 或 2/4s）× 随机抖动（50%-100%）；
- **会话重试预算**：单个客户端实例累计最多 15 次重试，杜绝「多个失败
  请求逐个重试」的风暴；
- **连续网络失败断路**：连续 3 次网络类失败后停止重试并快速失败
  （网络整体不可达时避免每个调用都耗 10s 连接超时）；
- **副作用工具禁重试**：写操作/账户变更类工具（NON_IDEMPOTENT_TOOLS）
  一律单次尝试——重试下单/支付/领券可能造成重复副作用；
- **追踪上下文**：每个客户端实例生成 trace_id，进入审计与诊断日志，
  用户可据此关联一次任务的全部调用（协议层无自定义 request id 出口，
  不自造协议字段）。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import inspect
import json
import os
import random
import re
import threading
import time
import uuid
from typing import Any, Optional

from .diagnostics import log_event
from .mcp_base import (
    CACHEABLE_TTL,
    MCPClient,
    MCPError,
    OrderScene,
    adapt_coupons,
    adapt_meal_detail,
    adapt_meals,
    adapt_nutrition,
    adapt_points,
    adapt_price,
    adapt_stores,
    adapt_time,
    build_calc_payload,
    check_envelope,
    parse_tool_text,
    _as_list,
    _first,
)
from .models import CouponQuery, PointsAccount

MCD_MCP_URL = "https://mcp.mcd.cn"
DEFAULT_TIMEOUT = 30.0  # 默认单次调用外层等待（基线实测：最慢单次 663ms，30s 充裕）
TOOL_TIMEOUTS = {"calculate-price": 45.0}  # 算价最重，放宽到 45s

# 传输层超时（2026-10-17 体检新增）。read 必须 >= 最重工具的外层等待（45s），
# 否则 SDK 默认 5s 读取超时会提前误杀慢但正常的 calculate-price。
CONNECT_TIMEOUT = 10.0   # TCP/TLS 握手：不通就是网络问题，10s 足够
READ_TIMEOUT = 60.0      # 等待服务端出数据：大于最重工具外层等待 45s
WRITE_TIMEOUT = 30.0     # 发送请求体（本引擎请求体很小）
POOL_TIMEOUT = 10.0      # 从连接池拿连接

# 允许返回「无 JSON 信封纯文本」的工具（联调实测的正常空态，2026-10-17 体检）：
# - query-my-coupons：无券时官方返回纯文本「暂无可用优惠券」；
# - delivery-query-addresses：无保存地址时可能返回纯文本说明。
# 其余工具无信封一律按「响应解析失败」明确报错，不静默降级为空结果。
PLAIN_TEXT_TOOLS = frozenset({"query-my-coupons", "delivery-query-addresses", "campaign-calendar"})

# 重试策略（有限次数，绝不无限重试）
MAX_ATTEMPTS_NETWORK = 3   # 瞬时网络错误 / 5xx
MAX_ATTEMPTS_429 = 2       # 限流：少试+长退避，尊重 600 次/分钟
BACKOFF_NETWORK = (0.5, 1.0, 2.0)   # 第 1/2/3 次尝试前的等待（秒）
BACKOFF_429 = (2.0, 4.0)
# 稳定性增强（2026-10-17）：
MAX_SESSION_RETRIES = 15              # 单客户端实例累计重试上限（防多请求重试风暴）
MAX_CONSECUTIVE_NETWORK_FAILURES = 3  # 连续网络类失败次数上限（断路：网络整体不可达时快速失败）
RETRY_AFTER_CAP = 30.0                # 尊重服务端 Retry-After，但单次等待封顶秒数
JITTER_RANGE = (0.5, 1.0)             # 抖动系数区间（只减不增，避免叠加限流）
# 有副作用的工具：重试可能造成重复下单/支付/领券/地址创建——一律单次尝试。
# 引擎 MVP 只读，不会调用这些工具；此清单是给未来调用方的硬护栏（契约见 MCP_INTEGRATION.md）。
NON_IDEMPOTENT_TOOLS = frozenset(
    {
        "create-order",
        "cancel-order",
        "auto-bind-coupons",
        "draw-lottery",
        "mall-create-order",
        "party-order-create",
        "delivery-create-address",
    }
)
_NETWORK_HINTS = (
    "connect", "ssl", "network", "timed out", "timeout", "unreachable", "resolve",
    "closed abruptly", "incomplete", "reset by peer", "broken pipe", "remote end closed",
    # 2026-10-17 体检新增的超时分类专用标记（对应 _map_sdk_error 的新消息）。
    # 不加大而化之的「连接」——旧路径的「MCP 连接失败」消息靠其内嵌 ASCII 提示词
    # 判重试，语义不变；测试级确定性错误不会被误判为可重试。
    "读取超时", "连接超时", "连接池超时", "写入超时", "对端", "重置",
)


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
    """把 SDK/网络异常翻译成用户可懂的 MCPError（含 401/403/429/5xx 官方错误码语义）。

    http_status：自建 http_client 的 event hook 记录的真实 HTTP 状态码（新版 SDK 会吞掉它）。

    超时区分（2026-10-17 体检）：ConnectTimeout=网络/TLS 握手失败，ReadTimeout=服务端
    出数慢——排查方向相反（换网络 vs 降频/稍后重试），之前笼统报「连接失败」。
    """
    if http_status == 401 or "401" in str(e):
        return MCPError(
            tool,
            "MCP Token 无效或已过期（401）：请到 https://open.mcd.cn/mcp 重新申请，"
            "并更新环境变量 MCD_MCP_TOKEN 或 .env 文件中的值",
            401,
        )
    if http_status == 403 or "403" in str(e):
        # 实测：不带 Authorization 的 POST 正是 403（网关拒绝）；与 401（Token 失效）分开
        return MCPError(
            tool,
            "MCP 服务拒绝访问（403）：可能是网络代理/WAF 拦截，或 Token 账户无该资源权限；"
            "请更换网络环境重试，或到 https://open.mcd.cn/mcp 确认账户权限",
            403,
        )
    if http_status == 429 or "429" in str(e):
        return MCPError(
            tool,
            "触发麦当劳 MCP 限流（429，每 Token 每分钟 600 次）：请稍后重试",
            429,
        )
    # 超时/传输层细分（在此之前：确定性错误不重试，重试逻辑见 _retry_backoff）
    err_text = type(e).__name__ + " " + str(e)
    if "ReadTimeout" in err_text or "读取超时" in err_text:
        return MCPError(
            tool,
            f"MCP 读取超时：已建立连接但服务端 {READ_TIMEOUT:.0f} 秒内未返回数据"
            "（网络正常但服务端出数慢或被限速；请稍后重试，持续出现请反馈）",
            408,
        )
    if "ConnectTimeout" in err_text or "连接超时" in err_text:
        return MCPError(
            tool,
            f"MCP 连接超时：{CONNECT_TIMEOUT:.0f} 秒内无法完成 TCP/TLS 握手"
            "（请检查网络/代理/VPN 能否访问 https://mcp.mcd.cn）",
            408,
        )
    if "ConnectError" in err_text or "连接失败" in err_text:
        return MCPError(tool, f"MCP 连接失败：{e}；请检查网络能否访问 https://mcp.mcd.cn")
    if "PoolTimeout" in err_text:
        return MCPError(tool, f"MCP 连接池超时：{POOL_TIMEOUT:.0f} 秒内未拿到可用连接（请稍后重试）", 408)
    if "WriteTimeout" in err_text:
        return MCPError(tool, "MCP 写入超时：请求发送未完成（网络上行被限速；请稍后重试）", 408)
    if "RemoteProtocolError" in err_text or "Invalid JSON" in err_text or "not valid JSON" in err_text:
        return MCPError(
            tool,
            f"MCP 响应协议错误：HTTP 层内容非法或被中间代理篡改（{e}）；"
            "请检查代理/VPN 设置后重试",
        )
    if http_status is None:
        # event hook 没拿到状态码时，从消息文本里找 5xx（httpx/SDK 的异常文本通常带码）
        m = re.search(r"\b(5\d\d)\b", str(e))
        if m:
            http_status = int(m.group(1))
    if http_status is not None and http_status >= 500:
        return MCPError(
            tool,
            f"麦当劳 MCP 服务端错误（HTTP {http_status}）：{e}；请稍后重试",
            http_status,
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
        # Token 解析优先级：显式参数 > 进程环境变量 > .env 文件（只读补缺，不覆盖环境变量）。
        # .env 支持让 Windows 用户免每次设环境变量；文件须被 .gitignore 排除（已配置），
        # 绝不要求把 Token 写进源码或提交到 Git。Token 值全程不打印、不进审计。
        from .dotenv import load_dotenv

        self.dotenv_applied = load_dotenv()
        self.token = token or os.environ.get("MCD_MCP_TOKEN")
        if not self.token:
            raise MCPError(
                "mcd-mcp",
                "未检测到 MCD_MCP_TOKEN。请先到 https://open.mcd.cn/mcp 申请 MCP Token，"
                "并以环境变量方式提供（不要把 Token 写进代码或提交到仓库）；"
                "Windows 用户也可放入仓库根目录的 .env 文件（该文件已被 .gitignore 排除）。"
                "如需离线体验请使用 demo 子命令——真实模式不会自动切换到演示数据。",
                code=401,
            )
        self.url = url
        self.timeout = timeout
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._session: Any = None  # SDK 的 ClientSession 懒加载，静态类型不可得；运行时不变量：connect() 成功后非 None
        self._stack = None
        self._http_client: Any = None
        self._http_statuses: list[int] = []  # event hook 记录的 HTTP 状态码（还原 401/429）
        self._http_retry_after: Optional[float] = None  # 最近一次响应头的 Retry-After（秒）
        self._read_cache: dict[str, tuple[float, object]] = {}  # 只读查询短 TTL 缓存
        self.cache_hits = 0
        self.cache_misses = 0
        # 稳定性增强（2026-10-17）：trace 上下文 + 重试预算 + 连续失败计数
        self.trace_id = uuid.uuid4().hex[:8]  # 实例级追踪 ID（审计/日志关联，不进用户提示）
        self._session_retries = 0
        self._consecutive_network_failures = 0

    # -- 连接管理 -----------------------------------------------------------

    def __enter__(self) -> "HttpMCPClient":
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def connect(self):
        """建立 MCP 会话（幂等）。失败抛 MCPError，消息可读，不泄露 Token。

        连接阶段的瞬时网络错误允许一次重试（0.5s 退避）；401 不重试。
        """
        if self._loop is not None:
            return
        self._check_sdk()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="mcd-mcp-loop")
        self._thread.start()
        try:
            self._submit(self._open_session(), tool="connect")
        except MCPError as e:
            # 仅对瞬时网络错误重试一次；认证/协议错误直接失败
            if e.code in (401, 429) or "超时" in str(e):
                self.close()
                raise
            try:
                time.sleep(0.5)
                self._submit(self._open_session(), tool="connect")
            except BaseException:
                self.close()
                raise
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
            # 尽力而为清理：单项失败不得中断其余资源释放，也不得向上抛
            # （close() 契约：幂等、安全、未连接时无操作）
            for closer in (session, stack):
                if closer is not None:
                    with contextlib.suppress(Exception):
                        await closer.__aexit__(None, None, None)
            if http_client is not None:
                with contextlib.suppress(Exception):
                    await http_client.aclose()

        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(_close(), loop).result(timeout=10)
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5)

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            with contextlib.suppress(Exception):
                self._loop.close()

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
            params: dict[str, Any] = dict(inspect.signature(stream_client).parameters)
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
                # 观测代码绝不能影响主流程：httpx event hook 内抛异常会直接搞坏客户端，
                # 因此整体抑制（状态码记录失败不该让请求失败）
                with contextlib.suppress(Exception):
                    self._http_statuses.append(int(response.status_code))
                    if len(self._http_statuses) > 20:
                        del self._http_statuses[:-20]
                    # Retry-After（秒形态；HTTP-date 形态忽略，用内置退避）
                    ra = response.headers.get("Retry-After") or response.headers.get("retry-after")
                    if ra:
                        try:
                            self._http_retry_after = max(0.0, float(str(ra).strip()))
                        except (TypeError, ValueError):
                            self._http_retry_after = None

            # 显式连接/读取超时（2026-10-17 体检）：SDK 默认 read=5s 会提前误杀
            # 慢但正常的 calculate-price（外层等待 45s）；httpx2 无 Timeout 时放弃显式配置
            client_kwargs: dict[str, Any] = {"headers": auth_headers, "event_hooks": {"response": [_record_status]}}
            timeout_cls = getattr(httpx2, "Timeout", None)
            if timeout_cls is not None:
                client_kwargs["timeout"] = timeout_cls(
                    connect=CONNECT_TIMEOUT, read=READ_TIMEOUT, write=WRITE_TIMEOUT, pool=POOL_TIMEOUT
                )
            self._http_client = httpx2.AsyncClient(**client_kwargs)
            return {"http_client": self._http_client}
        # 签名未知：按旧版习惯传 headers，让 SDK 自己报错
        return {"headers": auth_headers}

    # -- 统一调用 -----------------------------------------------------------

    def _map_error(self, tool: str, e: Exception) -> MCPError:
        status = self._http_statuses[-1] if self._http_statuses else None
        return _map_sdk_error(tool, e, http_status=status)

    def _timeout_for(self, tool: str) -> float:
        return TOOL_TIMEOUTS.get(tool, self.timeout)

    def _submit(self, coro, tool: str = "mcd-mcp", timeout: Optional[float] = None):
        """把协程提交到后台事件循环，同步等待结果；异常一律映射为 MCPError。"""
        if self._loop is None:
            coro.close()
            raise MCPError(tool, "MCP 会话未建立：请先 connect()，或使用 with HttpMCPClient(...) 上下文")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout=timeout if timeout is not None else self.timeout)
        except MCPError:
            raise
        except (TimeoutError, concurrent.futures.TimeoutError) as e:
            fut.cancel()
            # 2026-10-19 审计修复：timeout 为 None 时（如 list_tools 未传超时），
            # 旧代码直接 f"{timeout:.0f}" 会抛 TypeError 把超时错误变成崩溃。
            # 消息用「生效超时」（调用方传入值，缺省 self.timeout）。
            effective = timeout if timeout is not None else self.timeout
            raise MCPError(tool, f"MCP 调用超时（{effective:.0f} 秒无响应）；请检查网络后重试") from e
        except Exception as e:
            raise self._map_error(tool, e) from e

    def _is_network_error(self, e: MCPError) -> bool:
        """网络类错误（连接/超时/重置）：用于连续失败断路计数（429/业务错误不算）。"""
        return any(k in str(e).lower() for k in _NETWORK_HINTS)

    def _retry_after_seconds(self) -> Optional[float]:
        """服务端 Retry-After（秒形态）→ 实际等待（封顶 RETRY_AFTER_CAP）；无则 None。"""
        ra = self._http_retry_after
        if ra is None or ra <= 0:
            return None
        return min(ra, RETRY_AFTER_CAP)

    @staticmethod
    def _jittered(base: float) -> float:
        """退避抖动：50%-100% 区间（只减不增，避免叠加限流）。"""
        low, high = JITTER_RANGE
        # 抖动只需统计随机性，无需密码学强度（secrets 反而更慢）
        return base * (low + random.random() * (high - low))  # nosec B311

    def _retry_backoff(self, tool: str, e: MCPError, attempts: int) -> Optional[float]:
        """决定是否重试及退避秒数；None = 不重试（直接失败）。

        策略（2026-10-17 稳定性增强）：
        - 401/403 认证/权限错误：不重试（确定性，提示用户处理）；
        - 副作用工具（NON_IDEMPOTENT_TOOLS）：不重试（防重复下单/支付）；
        - 会话重试预算耗尽：不重试（防多失败请求的重试风暴）；
        - 连续网络失败达到上限：不重试（断路，网络整体不可达时快速失败）；
        - 429 限流：最多 2 次尝试；服务端给了 Retry-After 就按它等（封顶
          RETRY_AFTER_CAP），否则 2s/4s；
        - 5xx 与瞬时网络错误：最多 3 次尝试，退避 0.5s/1s/2s；
        - 业务错误（信封 success=false）与协议错误（isError）：不重试（确定性响应）；
        - 所有退避都乘随机抖动（50%-100%）。
        """
        if e.code in (401, 403):
            return None
        if tool in NON_IDEMPOTENT_TOOLS:
            log_event("mcp_retry_skipped_side_effect", trace=getattr(self, "trace_id", ""), tool=tool)
            return None
        if self._session_retries >= MAX_SESSION_RETRIES:
            log_event(
                "mcp_retry_budget_exhausted", trace=getattr(self, "trace_id", ""),
                tool=tool, session_retries=self._session_retries, cap=MAX_SESSION_RETRIES,
            )
            return None
        if self._consecutive_network_failures >= MAX_CONSECUTIVE_NETWORK_FAILURES:
            log_event(
                "mcp_circuit_open", trace=getattr(self, "trace_id", ""), tool=tool,
                consecutive_network_failures=self._consecutive_network_failures,
            )
            return None
        if e.code == 429:
            if attempts >= MAX_ATTEMPTS_429:
                return None
            base = self._retry_after_seconds()
            if base is None:
                base = BACKOFF_429[attempts - 1]
        else:
            server_error = e.code is not None and e.code >= 500
            network_error = self._is_network_error(e)
            if not (server_error or network_error):
                return None  # 业务/协议类确定性错误
            if attempts >= MAX_ATTEMPTS_NETWORK:
                return None
            base = BACKOFF_NETWORK[attempts - 1]
        return self._jittered(base)

    def _invoke_with_retry(self, tool: str, arguments: dict, timeout: float):
        """带有限退避重试的工具调用。返回 (result, attempts)。

        重试决策全部走 _retry_backoff（预算/断路/Retry-After/抖动）；
        每次尝试与每次放弃都写脱敏诊断日志（trace 关联）。
        """
        attempts = 0
        while True:
            attempts += 1
            t0 = time.perf_counter()
            session = self._session
            if session is None:
                raise MCPError(tool, "MCP 会话未建立：请先 connect()，或使用 with HttpMCPClient(...) 上下文")
            try:
                result = self._submit(
                    session.call_tool(tool, arguments),
                    tool=tool,
                    timeout=timeout,
                )
            except MCPError as e:
                duration_ms = (time.perf_counter() - t0) * 1000
                network_error = self._is_network_error(e)
                if network_error:
                    self._consecutive_network_failures += 1
                log_event(
                    "mcp_attempt_failed",
                    trace=self.trace_id, tool=tool, attempt=attempts,
                    duration_ms=round(duration_ms, 1), http_status=self._http_statuses[-1] if self._http_statuses else None,
                    error_code=e.code, network_error=network_error, error=str(e),
                )
                backoff = self._retry_backoff(tool, e, attempts)
                if backoff is None:
                    if attempts > 1:
                        e.args = (f"{e.args[0]}（已重试 {attempts - 1} 次均失败）",)
                    raise
                self._session_retries += 1
                log_event(
                    "mcp_retry_scheduled",
                    trace=self.trace_id, tool=tool, attempt=attempts,
                    backoff_ms=round(backoff * 1000), retry_after_s=self._http_retry_after,
                    session_retries=self._session_retries,
                )
                time.sleep(backoff)
            else:
                self._consecutive_network_failures = 0  # 成功即复位断路计数
                log_event(
                    "mcp_attempt_ok",
                    trace=self.trace_id, tool=tool, attempt=attempts,
                    duration_ms=round((time.perf_counter() - t0) * 1000, 1),
                )
                return result, attempts

    def _cache_key(self, tool: str, arguments: dict) -> str:
        return tool + "|" + json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)

    def _call(self, tool: str, arguments: dict, purpose: str = ""):
        """调用工具并做三级处理：TTL 去重 -> 有限重试 -> 信封/协议校验。

        三级都不允许静默放过：错误一律转成带原因的 MCPError，
        绝不允许「未知格式猜一个值」后继续往下走。
        """
        if self._session is None:
            raise MCPError(tool, "MCP 会话未建立：请先 connect()，或使用 with HttpMCPClient(...) 上下文")

        # 1) 只读查询去重/短 TTL 缓存（优惠券与算价不在可缓存集合，永远实时）
        ttl = CACHEABLE_TTL.get(tool)
        key = self._cache_key(tool, arguments)
        if ttl:
            hit = self._read_cache.get(key)
            if hit is not None and (time.monotonic() - hit[0]) < ttl:
                self.cache_hits += 1
                self._record(
                    tool, arguments, True,
                    note=f"去重命中（TTL {ttl:.0f}s 内未发起请求）",
                    purpose=purpose, mcp_call=False,
                )
                return hit[1]

        # 2) 实际调用（有限重试）
        t0 = time.perf_counter()
        try:
            result, attempts = self._invoke_with_retry(tool, arguments, self._timeout_for(tool))
        except MCPError:
            self._record(tool, arguments, False, note="调用失败", duration_ms=(time.perf_counter() - t0) * 1000,
                         purpose=purpose)
            raise
        duration_ms = (time.perf_counter() - t0) * 1000

        # 3) 协议级错误（isError）—— 不容忍静默流入适配器
        protocol_error = _protocol_error_text(result)
        if protocol_error is not None:
            self._record(tool, arguments, False, note=f"isError（attempts={attempts}）",
                         duration_ms=duration_ms, purpose=purpose, attempts=attempts)
            log_event("mcp_protocol_error", trace=self.trace_id, tool=tool, attempts=attempts,
                      detail=protocol_error[:200])
            raise MCPError(
                tool,
                f"MCP 协议错误（isError）：{protocol_error or '服务端拒绝了该调用'}",
            )
        parsed = parse_tool_text(result)
        # 4) 无信封响应（2026-10-17 体检）：除白名单工具的空态外，纯文本响应 =
        #    网关错误页/服务异常。明确失败，不静默降级成空菜单/空门店。
        #    注意：信封内 data 为字符串是合法的（list-nutrition-foods 的自定义文本），
        #    所以判的是「有没有信封」而不是「data 是不是字符串」。
        if isinstance(parsed, str) and tool not in PLAIN_TEXT_TOOLS:
            snippet = parsed.strip().replace("\n", " ")[:80]
            self._record(tool, arguments, False, note="无 JSON 信封", duration_ms=duration_ms,
                         purpose=purpose, attempts=attempts)
            log_event("mcp_no_envelope", trace=self.trace_id, tool=tool, attempts=attempts,
                      snippet=snippet)
            raise MCPError(
                tool,
                "MCP 响应解析失败：官方返回不是预期的 API 信封 JSON"
                "（可能是网关/代理错误页或服务异常；返回开头："
                + snippet
                + "）",
            )
        data = check_envelope(tool, parsed)
        # 只缓存成功结果——失败绝不进缓存（不会把失败当成功复用）
        if ttl:
            self._read_cache[key] = (time.monotonic(), data)
            self.cache_misses += 1
        self._record(tool, arguments, True, duration_ms=duration_ms, purpose=purpose, attempts=attempts)
        return data

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
        args: dict[str, Any] = {"beType": be_type, "searchType": 2}
        if city:
            args["city"] = city
        if keyword:
            args["keyword"] = keyword
        return adapt_stores(self._call("query-nearby-stores", args))

    def delivery_addresses(self) -> list[dict]:
        """delivery-query-addresses -> 已保存地址列表。

        2026-10-17 契约审计：官方未公布该工具的返回容器结构（Schema 无参数、
        真实账户无地址，仅验证过「空态」），因此用 _as_list 兼容多种容器
        （list / {"list"|"items"|"data"|"records"|"result"|"addresses": [...]} /
        dict 单体），并丢弃没有 addressId 的条目（没有 ID 的地址无法用于
        delivery-query-stores，留着只会把「无可用地址」误导成「查询失败」）——
        取不到即空列表，由上层明示「没有可用地址」，绝不猜测地址。
        """
        raw = self._call("delivery-query-addresses", {})
        addresses = _as_list(raw)
        return [
            a for a in addresses if isinstance(a, dict) and _first(a, "addressId", "address_id", "addressID", "id")
        ]

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

    def my_account(self) -> PointsAccount:
        """query-my-account（只读）-> 积分账户。

        安全：适配器丢弃 accountId（账户标识），只保留积分数值；
        数值字段缺失时为 None，由上层显示「未知」。
        """
        return adapt_points(self._call("query-my-account", {}, purpose="points_account"))

    # -- 第一阶段扩展（2026-10-18）：活动日历 + 可领券（均只读） --------------

    def campaign_calendar(self, specified_date: Optional[str] = None):
        """campaign-calendar（只读）-> CampaignParseResult。

        官方返回 **Markdown 文本**（非 JSON 信封），已在 PLAIN_TEXT_TOOLS 白名单；
        解析见 campaign.py。空列表不等于有效空结果——无法识别结构时返回
        parse_error，由 CLI/Web 显式提示，而不是显示「暂无活动」。
        """
        from .campaign import parse_campaign_result

        args: dict[str, Any] = {}
        if specified_date:
            args["specifiedDate"] = specified_date
        text = self._call("campaign-calendar", args, purpose="campaign_calendar")
        if not isinstance(text, str):
            text = json.dumps(text, ensure_ascii=False)
        return parse_campaign_result(text)

    def _call_raw(self, tool: str, arguments: dict, timeout: float = 30.0):
        """底层调用：绕过 SDK 的 structured content 校验，返回原始 CallToolResult。

        仅用于「官方 outputSchema 声明与真实返回不一致」的工具：
        available-coupons 无券时返回 data=null（声明为 array），SDK 会在
        call_tool 内部先抛 RuntimeError，导致拿不到原始结构。这里用
        send_request 直接取原始结果，由调用方按业务语义分类。
        """
        from mcp_types import CallToolRequest, CallToolRequestParams, CallToolResult

        async def _invoke():
            session = self._session
            if session is None:
                raise MCPError(tool, "MCP 会话未建立：请先 connect()，或使用 with HttpMCPClient(...) 上下文")
            return await session.send_request(
                CallToolRequest(params=CallToolRequestParams(name=tool, arguments=arguments)),
                CallToolResult,
            )

        result = self._submit(_invoke(), tool=tool, timeout=timeout)
        self._record(tool, arguments, True, note="raw call（绕过 structured 校验）")
        return result

    def available_coupons(self) -> CouponQuery:
        """available-coupons（只读）-> CouponQuery（ok / empty / unknown）。

        2026-10-18 裸 HTTP 实测（绕过 SDK）确认无券时的真实返回：
          {"success": false, "code": 499,
           "message": "No coupons available", "data": null}
        content 文本「暂无可领取的优惠券」。
        → code=499 是官方表达「无可领取券」的业务码，**不是错误**；
          与 query-my-coupons 的 success:true + coupons:[] 语义等价。
        分类：
          A. success=false + code=499 / "No coupons available" → empty（明确无券）
          B. success=true + data=list → ok
          C. success=true + data=null → unknown（有返回但语义无法确认）
          D. 其他 success=false → 抛 MCPError（真实业务错误）
        网络/401/429/5xx 由 _submit 的既有异常体系抛出（不在此吞掉）。
        """
        result = self._call_raw("available-coupons", {})
        payload = getattr(result, "structured_content", None)
        if isinstance(payload, dict):
            code = payload.get("code")
            msg = str(payload.get("message") or "")
            summary = {"success": payload.get("success"), "code": code, "message": msg[:80]}
            if payload.get("success") is False:
                # 精确匹配已实测的无券响应；code 或 message 任一不符 → 真实业务错误
                if code == 499 and msg.strip() == "No coupons available":
                    return CouponQuery(
                        state="empty",
                        note="官方明确返回：暂无可领取的优惠券（code=499 No coupons available）",
                        raw_summary=summary,
                    )
                raise MCPError(
                    "available-coupons",
                    f"官方返回业务错误（code={code}）：{msg or '未知错误'}",
                    code=code if isinstance(code, int) else None,
                )
            data = payload.get("data")
            if data is None:
                return CouponQuery(
                    state="unknown",
                    note="官方返回 success=true 但 data=null：无法确认是否有可领取券",
                    raw_summary=summary,
                )
            if not isinstance(data, list):
                raise MCPError(
                    "available-coupons",
                    f"响应契约错误：data 类型为 {type(data).__name__}，官方声明为 array",
                )
            coupons = adapt_coupons(data, source="available")
            if data and not coupons:
                raise MCPError(
                    "available-coupons",
                    "响应契约错误：券条目缺少 couponId/名称等必需字段，无法解析",
                )
            return CouponQuery(state="ok", coupons=coupons, raw_summary=summary)

        # 无 structuredContent：退回文本判定（官方 content 为 Markdown）
        text = ""
        for blk in getattr(result, "content", None) or []:
            chunk = getattr(blk, "text", None)
            if chunk:
                text += chunk
        summary = {"structured": False, "text_head": text[:60].replace("\n", " ")}
        # 只用实测的精确文案判定空态；任何「含暂无」的泛化匹配都可能把别的提示误判成无券
        if "暂无可领取的优惠券" in text:
            return CouponQuery(state="empty", note="官方文本明示：暂无可领取的优惠券", raw_summary=summary)
        return CouponQuery(
            state="unknown",
            note="官方未返回 structuredContent，文本无法判定券状态",
            raw_summary=summary,
        )

    def list_nutrition_foods(self):
        return adapt_nutrition(self._call("list-nutrition-foods", {}))

    def calculate_price(self, scene: OrderScene, items: list, coupons: list, purpose: str = ""):
        # 算价绝不走缓存：必须按当前方案实时核验（req: 最终总价的真实性）
        # coupons: Coupon 对象列表 / (id, code) 元组 / 纯 id 字符串（兼容）
        payload = build_calc_payload(scene, items, coupons)
        return adapt_price(self._call("calculate-price", payload, purpose=purpose or "price_check"))
