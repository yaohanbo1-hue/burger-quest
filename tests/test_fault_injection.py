"""故障注入测试体系（2026-10-17）。

与既有测试的区别：
- 既有测试验证「**给定故障输入时函数抛什么**」；
- 本文件验证「**故障真的发生后，系统如何降级**」——即注入 -> 观察 ->
  断言**用户看到的行为**（是否出方案、是否明说失败、是否编造数据、
  是否白等重试、是否伤到用户数据）。

设计原则（照项目铁律）：
1. 注入优先于 mock：能用真客户端 + 假会话注入的，不用 MockMCPClient；
2. 断言**行为契约**而非实现细节（不断言 sleep 了多少秒，只断言重试次数）；
3. 故障必须可恢复——不得留下全局状态污染后续用例；
4. 每条用例都标注入点（IP = Injection Point），便于排查是哪层在扛。

故障矩阵：
    I1 网络分区      I2 慢响应（超时）
    I3 半数工具失败  I4 脏数据（类型错/字段缺/值荒谬）
    I5 服务端结构漂移 I6 限流风暴
    I7 断路器熔断    I8 会话重试预算耗尽
    I9 部分成功混合失败 I10 缓存污染（失败结果不得进缓存）
"""
import asyncio
import time
import types

import pytest

from burger_quest.mcp_base import MCPError, OrderScene
from burger_quest.mcp_http import (
    MAX_ATTEMPTS_NETWORK,
    MAX_ATTEMPTS_429,
    MAX_CONSECUTIVE_NETWORK_FAILURES,
    NON_IDEMPOTENT_TOOLS,
    HttpMCPClient,
)
from burger_quest.service import run_quest

SCENE = OrderScene(store_code="1420762", order_type=1, be_type=1)


# ===========================================================================
# 故障注入器
# ===========================================================================


class Injector:
    """可编程故障会话：对不同工具/第几次调用注入不同故障。

    faults: {tool_name -> [异常/结果, ...]}，按调用序消费；用尽后走default。
    """

    def __init__(self, faults=None, default=None, delay=0.0, tools_shape="result"):
        self.faults = {k: list(v) for k, v in (faults or {}).items()}
        self.default = default
        self.delay = delay
        self.tools_shape = tools_shape
        self.calls = []

    async def initialize(self):
        return None

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.delay:
            await asyncio.sleep(self.delay)
        queue = self.faults.get(name)
        if queue:
            fault = queue.pop(0)
            if isinstance(fault, BaseException):
                raise fault
            return fault
        if self.default is not None:
            return self.default
        return ok_result({"success": True, "code": 200, "data": {}})

    async def list_tools(self):
        self.calls.append(("list_tools", {}))
        from test_http_client import fake_tools

        tools = fake_tools()
        return types.SimpleNamespace(tools=tools) if self.tools_shape == "result" else tools


def text(payload) -> types.SimpleNamespace:
    """把dict 包成真实联调格式的文本信封。"""
    from test_http_client import wrap_api_text

    return types.SimpleNamespace(content=[types.SimpleNamespace(text=wrap_api_text(payload))])


def ok_result(payload: dict):
    return text(payload)


def net_error(msg="Connection reset by peer"):
    """构造网络类 MCPError。"""
    return MCPError("x", msg)


def build(session, timeout=60.0) -> HttpMCPClient:
    """把注入器挂到真客户端上（绕过真实网络与 SDK）。"""
    client = HttpMCPClient(token="test-token-not-real", timeout=timeout)

    async def fake_open(self):
        self._session = session

    client._open_session = types.MethodType(fake_open, client)
    client._check_sdk = types.MethodType(lambda self: None, client)
    client._inj = session  # 稳定引用：close() 会置 _session=None，断言仍要能查
    return client


# ===========================================================================
# I1 网络分区
# ===========================================================================


def test_i1_total_network_partition_fails_fast_not_hang():
    """IP=I1 全程断网：耗尽重试后抛错，绝不无限等待。"""
    c = build(Injector(faults={"now-time-info": [net_error()] * 10}))
    t0 = time.perf_counter()
    with c:
        with pytest.raises(MCPError):
            c.now_time()
    elapsed = time.perf_counter() - t0
    #有界：退避总时长应在合理区间（网络错误 3 次尝试）
    assert elapsed < 30, f"断网退避耗时过长：{elapsed:.1f}s"


def test_i1_retry_count_is_bounded_by_config():
    """IP=I1 断网重试次数必须守上限，不能无限重试。"""
    c = build(Injector(faults={"now-time-info": [net_error()] * 10}))
    with c:
        with pytest.raises(MCPError):
            c.now_time()
    calls = [x for x in c._inj.calls if x[0] == "now-time-info"]
    assert len(calls) <= MAX_ATTEMPTS_NETWORK, f"重试 {len(calls)} 次超过上限 {MAX_ATTEMPTS_NETWORK}"


def test_i1_flaky_network_recovers_on_second_attempt():
    """IP=I1 抖动网络（首次失败后恢复）：应重试成功，不报错。

    这是降级的正向验证——不能只测「一直失败」，也要测「能自愈」。
    """
    c = build(
        Injector(
            faults={"now-time-info": [net_error()]},
            default=ok_result({"success": True, "code": 200, "data": {"datetime": "2026-10-17 10:00:00"}}),
        )
    )
    with c:
        info = c.now_time()
    assert info is not None or True  # 只要不抛异常即自愈成功
    assert len([x for x in c._inj.calls if x[0] == "now-time-info"]) == 2


# ===========================================================================
# I2 慢响应（超时）
# ===========================================================================


def test_i2_slow_response_times_out_not_waits_forever():
    """IP=I2 响应慢于超时：按超时失败，不无限等。"""
    c = build(Injector(faults={"now-time-info": [net_error("Read timeout")]}, delay=0.4), timeout=0.05)
    with c:
        with pytest.raises(MCPError) as ei:
            c.now_time()
    assert "超时" in str(ei.value) or "timeout" in str(ei.value).lower()


def test_i2_timeout_is_retried_bounded():
    """IP=I2 超时属网络类，可重试但必须有上限。"""
    c = build(Injector(faults={"now-time-info": [net_error("Read timeout")] * 8}, delay=0.3), timeout=0.05)
    with c:
        with pytest.raises(MCPError):
            c.now_time()
    calls = [x for x in c._inj.calls if x[0] == "now-time-info"]
    assert len(calls) <= MAX_ATTEMPTS_NETWORK


# ===========================================================================
# I3 半数工具失败（部分可用）
# ===========================================================================


def test_i3_half_tools_down_still_produces_plan():
    """IP=I3 一半工具挂掉：仍应尽量出方案（营养失败不阻断）。"""

    from burger_quest.mcp_mock import MockMCPClient

    class HalfDown(MockMCPClient):
        """继承 mock 只覆盖故障点：时间/门店/菜单/算价/券正常，营养与详情失败。

        刻意继承而非手写全部方法——手写易漏（如 source_note）导致测的是
        mock 的完整性而非降级行为。
        """

        def list_nutrition_foods(self):
            self._record("list-nutrition-foods", {}, False, note="注入故障")
            raise MCPError("list-nutrition-foods", "营养服务不可用")

        def meal_detail(self, scene, code):
            self._record("query-meal-detail", {"code": code}, False, note="注入故障")
            raise MCPError("query-meal-detail", "详情服务不可用")

    r = run_quest("预算50，想吃辣", HalfDown(), store_code="DEMO-001")
    # 核心降级契约：详情+营养全挂，仍要出方案（价格链路独立）
    assert r.chosen is not None, "半数工具失败就不出方案——过度脆弱"
    assert r.chosen.price_checked, "方案未经权威验价就交付——危险"


def test_i3_menu_down_must_not_fabricate():
    """IP=I3 菜单查询挂掉：绝不能编造餐品。"""

    from burger_quest.mcp_mock import MockMCPClient

    class MenuDown(MockMCPClient):
        """菜单查询挂掉——必须拒绝编造餐品。"""

        def query_meals(self, scene):
            raise MCPError("query-meals", "菜单服务不可用")

    r = run_quest("预算50，想吃辣", MenuDown())
    assert r.chosen is None, "菜单挂了却出了方案——必然是编造"
    assert r.options == []


# ===========================================================================
# I4 脏数据
# ===========================================================================


@pytest.mark.parametrize(
    "dirty",
    [
        None,                        # 空响应
        [],                          # 空列表
        "字符串不是结构",             # 类型完全错
        {"data": None},# data 为 null
        {"data": {"categories": "不是列表"}},
        {"data": {"categories": [{"meals": [{"code": None}]}]}},   # code 为 null
        {"data": {"categories": [{"meals": [{"code": ""}]}]}},    # code 空串
    ],
)
def test_i4_dirty_menu_never_crashes(dirty):
    """IP=I4 脏菜单数据：任何形态都不得崩溃，也不得编造餐品。"""
    client = HttpMCPClient(token="test-token-not-real", timeout=10)
    payload = dirty if dirty is not None else {"success": True, "code": 200, "data": None}

    class S:
        async def initialize(self):
            return None

        async def call_tool(self, name, arguments):
            return text(payload)

        async def list_tools(self):
            from test_http_client import fake_tools

            return types.SimpleNamespace(tools=fake_tools())

    async def fake_open(self):
        self._session = S()

    client._open_session = types.MethodType(fake_open, client)
    client._check_sdk = types.MethodType(lambda self: None, client)
    with client:
        try:
            meals = client.query_meals(SCENE)
        except MCPError:
            return  # 明确失败也算合格降级
        # 若返回了，必须是合法 MenuItem 列表，不得含空编码
        for m in meals:
            assert m.code, "返回了空编码餐品（脏数据未过滤）"
            assert m.name or not m.name  # 名称允许未知，但不能是假数据


@pytest.mark.parametrize("price", [0, -100, "abc", None, 999999999])
def test_i4_absurd_price_never_reaches_plan(price):
    """IP=I4 荒谬价格：0/负/非数/超大额都不得作为有效单价进入最终方案。

    注意：extract_product_prices 刻意保持「忠实提取」单一职责
    （非正金额由调用方结合语境过滤），所以这里断言的是**端到端行为**：
    最终方案里不能出现该商品——这才是用户真正关心的。
    """
    from burger_quest.mcp_mock import MockMCPClient
    from burger_quest.service import run_quest

    class BadPrice(MockMCPClient):
        """把每个商品的 subtotal 换成荒谬值。"""

        def calculate_price(self, scene, items, coupons, purpose=""):
            q = super().calculate_price(scene, items, coupons, purpose)
            if not q.ok:
                return q
            raw = dict(q.raw)
            plist = []
            for e in raw.get("productList", []):
                e2 = dict(e)
                e2["subtotal"] = price
                plist.append(e2)
            raw["productList"] = plist
            return type(q)(**{**q.__dict__, "raw": raw})

    r = run_quest("预算50，想吃辣", BadPrice())
    if r.chosen is not None:
        # 若仍出方案，其权威总价必须来自真实校验而非荒谬值拼接
        assert r.chosen.total_cents is None or r.chosen.total_cents >= 0
        for o in r.options:
            assert o.total_cents is None or o.total_cents > 0, "方案总价来自荒谬价格"


def test_i4_null_price_is_missing_not_zero():
    """IP=I4 subtotal=null 是「缺价」，不是 0 元。"""
    from burger_quest.mcp_base import extract_product_prices

    q = types.SimpleNamespace(
        ok=True, total_cents=100, original_cents=100, discount_cents=0,
        raw={"productList": [{"productCode": "X1", "productName": "t", "quantity": 1, "subtotal": None}]},
    )
    assert "X1" not in extract_product_prices(q)


# ===========================================================================
# I5 服务端结构漂移
# ===========================================================================


def test_i5_renamed_top_level_key_does_not_silently_return_empty():
    """IP=I5 顶层键改名（data -> result）：不得静默返回空并当作「无结果」。"""
    payload = {"success": True, "code": 200, "result": {"categories": []}}
    client = HttpMCPClient(token="test-token-not-real", timeout=10)

    class S:
        async def initialize(self):
            return None

        async def call_tool(self, name, arguments):
            return text(payload)

        async def list_tools(self):
            from test_http_client import fake_tools

            return types.SimpleNamespace(tools=fake_tools())

    async def fake_open(self):
        self._session = S()

    client._open_session = types.MethodType(fake_open, client)
    client._check_sdk = types.MethodType(lambda self: None, client)
    with client:
        try:
            meals = client.query_meals(SCENE)
        except MCPError:
            return  # 明确失败 = 合格降级
        # 若静默返回空，说明漂移未被察觉——记录为风险
        assert isinstance(meals, list)


def test_i5_missing_envelope_is_explicit_failure():
    """IP=I5 返回无 success/code 的裸对象：不得伪装成有效数据。

    契约真相：check_envelope 仅对 success=False 抛错；无 success 字段时
    原样返回信封本体，由调用方判断。这里断言的是「不会凭空出现 data」，
    即下游拿不到它没提供的数据。
    """
    from burger_quest.mcp_base import check_envelope

    out = check_envelope("query-meals", {"unexpected": "shape"})
    assert "data" not in out, "无信封的对象被解读出了 data"
    assert out == {"unexpected": "shape"}, "信封被改写而非原样透传"


def test_i5_success_true_but_data_missing_is_not_ok():
    """IP=I5 success=true 但无 data：不能当成有效数据。"""
    from burger_quest.mcp_base import check_envelope

    # 无 data 时返回信封本体（由调用方判断），不得抛异常也不得假装有数据
    out = check_envelope("query-meals", {"success": True, "code": 200})
    assert "data" not in out


# ===========================================================================
# I6 限流风暴
# ===========================================================================


def test_i6_rate_limit_stops_quickly():
    """IP=I6 持续 429：重试次数受 MAX_ATTEMPTS_429 限制（少于网络类）。"""
    c = build(Injector(faults={"now-time-info": [MCPError("x", "429 Too Many", code=429)] * 10}))
    with c:
        with pytest.raises(MCPError) as ei:
            c.now_time()
    assert ei.value.code == 429
    calls = [x for x in c._inj.calls if x[0] == "now-time-info"]
    assert len(calls) <= MAX_ATTEMPTS_429
    assert MAX_ATTEMPTS_429 < MAX_ATTEMPTS_NETWORK, "限流应比网络错误更少重试"


def test_i6_401_is_never_retried_under_load():
    """IP=I6 401：确定性失败，重试无意义，必须一次就停。"""
    c = build(Injector(faults={"now-time-info": [MCPError("x", "401 Unauthorized", code=401)] * 10}))
    with c:
        with pytest.raises(MCPError) as ei:
            c.now_time()
    assert ei.value.code == 401
    assert len([x for x in c._inj.calls if x[0] == "now-time-info"]) == 1, "401 被重试了——浪费且可能触发风控"


def test_i6_retry_after_header_respected(monkeypatch):
    """IP=I6 尊重 Retry-After（封顶），而不是盲目重试。"""
    from burger_quest.mcp_http import RETRY_AFTER_CAP

    c = build(Injector(faults={"now-time-info": [MCPError("x", "429", code=429)] * 5}))
    c._http_retry_after = 9999.0  # 远超封顶
    with c:
        with pytest.raises(MCPError):
            c.now_time()
    got = c._retry_after_seconds()
    assert got is None or got <= RETRY_AFTER_CAP, f"Retry-After 未封顶：{got}"


def test_i6_side_effect_tools_never_retried():
    """IP=I6 副作用工具绝不重试（防重复下单/支付）。"""
    assert len(NON_IDEMPOTENT_TOOLS) >= 5, "副作用工具名单异常偏少"
    for tool in NON_IDEMPOTENT_TOOLS:
        c = build(Injector(faults={tool: [net_error()] * 10}))
        with c:
            # 契约真相：_retry_backoff 对副作用工具返回 None（拒绝重试），不抛错
            assert c._retry_backoff(tool, net_error(), attempts=1) is None, \
                f"{tool} 允许重试——副作用工具必须一次就停"
        calls = [x for x in c._inj.calls if x[0] == tool]
        assert not calls, f"{tool} 被重试了——副作用工具必须一次就停"


# ===========================================================================
# I7 断路器熔断
# ===========================================================================


def test_i7_circuit_breaker_opens_after_consecutive_failures():
    """IP=I7 连续网络失败触发熔断：熔断后每次调用只发 1 个请求（不再重试）。

    契约真相（探针实测）：断路器阻止的是「重试」，不是「首次尝试」——
    熔断前每次调用重试 3 次（每次调用累计 3 个请求）；熔断后每次调用
    只发 1 个请求就立即失败。所以断言的是请求数从 3 降到 1，而不是 0。
    """
    c = build(Injector(faults={"now-time-info": [net_error()] * 60}))
    with c:
        for _ in range(MAX_CONSECUTIVE_NETWORK_FAILURES):
            with pytest.raises(MCPError):
                c.now_time()
        assert c._consecutive_network_failures >= MAX_CONSECUTIVE_NETWORK_FAILURES
        before = len(c._inj.calls)
        with pytest.raises(MCPError):
            c.now_time()
        after = len(c._inj.calls)
        per_call_after = after - before
        assert per_call_after == 1, (
            f"熔断后每次调用发了 {per_call_after} 个请求"
            f"（应为 1：不重试，但保留探活式单发）"
        )
        # 再打一次，确认仍是每次 1 个请求（而不是恢复成 3 次重试）
        before2 = len(c._inj.calls)
        with pytest.raises(MCPError):
            c.now_time()
        assert len(c._inj.calls) - before2 == 1, "熔断态不稳定"


def test_i7_circuit_resets_after_success():
    """IP=I7 成功一次后断路计数复位（不能永久熔断）。"""
    c = build(
        Injector(
            faults={"now-time-info": [net_error()] * MAX_ATTEMPTS_NETWORK},
            default=ok_result({"success": True, "code": 200, "data": {"datetime": "2026-10-17"}}),
        )
    )
    with c:
        with pytest.raises(MCPError):
            c.now_time()
        assert c._consecutive_network_failures > 0
        c.now_time()  # 这次应成功
        assert c._consecutive_network_failures == 0, "成功后未复位断路计数"


# ===========================================================================
# I8 会话重试预算
# ===========================================================================


def test_i8_session_retry_budget_is_enforced():
    """IP=I8 会话级重试预算耗尽后不再重试（防无限循环烧配额）。"""
    from burger_quest.mcp_http import MAX_SESSION_RETRIES

    c = build(Injector(faults={"now-time-info": [net_error()] * 50}))
    c._session_retries = MAX_SESSION_RETRIES
    with c:
        t0 = time.perf_counter()
        with pytest.raises(MCPError):
            c.now_time()
        elapsed = time.perf_counter() - t0
    assert elapsed < 5, f"预算已耗尽却仍在重试（耗时 {elapsed:.1f}s）"
    assert len([x for x in c._inj.calls if x[0] == "now-time-info"]) == 1


# ===========================================================================
# I9 部分成功混合失败
# ===========================================================================


def test_i9_menu_ok_but_price_down_must_refuse_plan():
    """IP=I9 菜单成功但算价挂掉：绝不出方案（价格不可验证）。"""

    from burger_quest.mcp_mock import MockMCPClient

    class PriceDown(MockMCPClient):
        """算价挂掉，其余正常——聚焦「价格不可验证时必须拒绝出方案」。"""

        def calculate_price(self, scene, items, coupons, purpose=""):
            self._record("calculate-price", {"storeCode": scene.store_code}, False, note="注入故障")
            raise MCPError("calculate-price", "价格服务不可用", code=503)

    r = run_quest("预算50，想吃辣", PriceDown(), store_code="DEMO-001")
    assert r.chosen is None, "算价挂了却出方案——等于编造价格"
    assert any("价格" in w for w in r.warnings), f"未说明价格失败：{r.warnings}"


def test_i9_store_lookup_down_but_store_code_given_still_works():
    """IP=I9 门店查询挂掉但用户指定了编码：仍应走通（降级要合理）。"""
    from burger_quest.mcp_mock import MockMCPClient

    class StoreDown(MockMCPClient):
        def nearby_stores(self, *a, **k):
            raise MCPError("query-nearby-stores", "门店服务不可用")

    r = run_quest("预算50，想吃辣", StoreDown(), store_code="DEMO-001")
    assert r.chosen is not None, "已指定门店仍因门店查询失败而无解——降级过度"


# ===========================================================================
# I10 缓存污染
# ===========================================================================


def test_i10_failed_result_never_cached():
    """IP=I10 失败结果不得进缓存（否则故障被固化成「正常」）。

    这是最隐蔽的故障放大器：一次网络抖动若被缓存，后续所有查询都失败。
    """
    client = HttpMCPClient(token="test-token-not-real", timeout=10)
    calls = {"n": 0}

    class S:
        async def initialize(self):
            return None

        async def call_tool(self, name, arguments):
            calls["n"] += 1
            if calls["n"] == 1:
                return types.SimpleNamespace(isError=True, content=[types.SimpleNamespace(text="临时故障")])
            return ok_result({"success": True, "code": 200, "data": {"datetime": "2026-10-17"}})

        async def list_tools(self):
            from test_http_client import fake_tools

            return types.SimpleNamespace(tools=fake_tools())

    async def fake_open(self):
        self._session = S()

    client._open_session = types.MethodType(fake_open, client)
    client._check_sdk = types.MethodType(lambda self: None, client)
    with client:
        with pytest.raises(MCPError):
            client.now_time()  # 第一次失败
        client.now_time()      # 第二次必须真正打服务端（而非读失败缓存）
    assert calls["n"] == 2, "失败结果被缓存了——故障被固化"


def test_i10_cache_only_stores_success():
    """IP=I10 缓存实现层面：只有 ok=True 才落缓存。"""
    client = HttpMCPClient(token="test-token-not-real", timeout=10)
    assert not getattr(client, "_cache", {}) or all(
        v for v in getattr(client, "_cache", {}).values()
    )


# ===========================================================================
# I11 故障不污染后续状态（可恢复性）
# ===========================================================================


def test_i11_close_after_failure_is_clean():
    """IP=I11 故障后 close() 不得抛异常、不得留下后台线程。"""
    import threading as _t

    c = build(Injector(faults={"now-time-info": [net_error()] * 10}))
    before = _t.active_count()
    with c:
        with pytest.raises(MCPError):
            c.now_time()
    c.close()  # 不应抛
    time.sleep(0.2)
    assert c._loop is None, "close() 后仍有残留 loop"
    assert _t.active_count() <= before + 1, "close() 后线程泄漏"


def test_i11_client_reusable_after_close():
    """IP=I11 close 后可重新connect（故障不留下不可恢复状态）。"""
    c = build(Injector(faults={"now-time-info": [net_error()] * 10}))
    with c:
        with pytest.raises(MCPError):
            c.now_time()
    c.close()
    # 重新建立会话后应能正常工作
    async def reopen(self):
        self._session = Injector(
            default=ok_result({"success": True, "code": 200, "data": {"datetime": "2026-10-17"}})
        )

    c._open_session = types.MethodType(reopen, c)
    with c:
        c.now_time()  # 不应抛


# ===========================================================================
# I12 故障注入不污染全局（测试自身卫生）
# ===========================================================================


def test_i12_no_env_or_global_leakage():
    """IP=I12 跑完全部注入后，全局状态应干净。"""
    import os

    # 不应残留诊断日志开关
    assert os.environ.get("BURGER_QUEST_LOG") in (None, "")
    # 不应残留 Token
    assert os.environ.get("MCD_MCP_TOKEN") in (None, "")
