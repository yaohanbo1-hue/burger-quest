"""MCP 调用效率与可靠性测试（2026-10-14 优化）。

覆盖：
- 调用数量：单任务真实 MCP 请求数有上限；详情调用被算价探测取代；
- 超时：慢会话 -> 可读超时错误；分工具超时值；
- 重试：瞬时网络错误后成功 / 持续失败有限次数 / 401 不重试 / 429 降速 /
  业务错误不重试；
- 去重与 TTL 缓存：同输入只读查询命中缓存；TTL 过期重取；失败不进缓存；
- 绝不缓存：calculate-price 与优惠券相同入参也重新请求；
- 审计字段：seq/duration_ms/purpose/attempts/mcp_call，且不含敏感信息；
- 失败不静默：价格失败不出方案（无虚假零价）。
"""
import time
import types

import pytest

from burger_quest import cli
from burger_quest.mcp_base import MCPError, scene_for_mode, summarize_calls
from burger_quest.mcp_http import (
    BACKOFF_429,
    BACKOFF_NETWORK,
    JITTER_RANGE,
    MAX_ATTEMPTS_429,
    MAX_ATTEMPTS_NETWORK,
    HttpMCPClient,
)
from burger_quest.mcp_mock import MockMCPClient
from burger_quest.service import run_quest
from test_http_client import wrap_api_text


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _ok_result(payload):
    return types.SimpleNamespace(content=[types.SimpleNamespace(text=wrap_api_text(payload))])


class ScriptedSession:
    """按尝试次数脚本化返回/抛错的假会话（用于重试与缓存测试）。

    - script: list[Exception | SimpleNamespace]，最后一个元素重复；
    - fail_first_n: 前 N 次尝试一律抛瞬时网络错误（跨调用累计）。
    """

    def __init__(self, script=None, fail_first_n=0):
        self.script = script or []
        self.fail_first_n = fail_first_n
        self.attempts = 0
        self.seen_args = []

    async def initialize(self):
        return None

    async def call_tool(self, name, arguments):
        self.attempts += 1
        self.seen_args.append((name, arguments))
        if self.attempts <= self.fail_first_n:
            raise Exception("Connection reset by peer")
        if self.script:
            item = self.script[min(self.attempts - 1, len(self.script) - 1)]
            if isinstance(item, Exception):
                raise item
            return item
        return _ok_result({"success": True, "code": 200, "data": {"datetime": "T"}})


def make_scripted_client(script=None, fail_first_n=0, tool="now-time-info", timeout=5.0):
    client = HttpMCPClient(token="test-token-not-real", timeout=timeout)
    session = ScriptedSession(script, fail_first_n)

    async def fake_open(self):
        self._session = session

    client._open_session = types.MethodType(fake_open, client)
    client._check_sdk = types.MethodType(lambda self: None, client)
    return client, session


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """退避 sleep 置零，测试不真的等待。"""
    import burger_quest.mcp_http as mh

    monkeypatch.setattr(mh.time, "sleep", lambda s: None)


# ---------------------------------------------------------------------------
# 调用数量（基线对比）
# ---------------------------------------------------------------------------


def test_single_task_call_count_within_budget():
    """单任务真实 MCP 请求数 ≤13（优化前基线 24）；详情调用被算价探测取代。"""
    client = MockMCPClient()
    r = run_quest("预算 35 元，想吃辣，两个人，希望尽量高蛋白，不想点太多饮料", client)
    real = [c for c in client.calls if c.get("mcp_call", True)]
    by_tool = summarize_calls(client.calls)["by_tool"]
    assert len(real) <= 13, f"调用数超标：{len(real)}"
    assert by_tool.get("query-meal-detail", 0) == 0, "名称应来自算价 productList，不应逐个查详情"
    # 方案质量不降级：3 个可比较方案 + 权威总价
    assert len(r.options) == 3
    assert r.chosen.price_checked and r.chosen.total_cents == 2500


def test_price_discovery_purpose_and_probe_once():
    """一次 price_discovery 探测；其余算价都是 plan_verification。"""
    client = MockMCPClient()
    run_quest("预算 35 元，想吃辣，两个人，希望尽量高蛋白，不想点太多饮料", client)
    purposes = [c.get("purpose") for c in client.calls if c["tool"] == "calculate-price"]
    assert purposes.count("price_discovery") == 1
    assert purposes.count("plan_verification") >= 1
    assert set(purposes) <= {"price_discovery", "plan_verification", "price_check"}


def test_coupon_sets_capped_per_combo():
    """每个组合最多 3 次算价试探；有券时先探最优单券（无券基线被叠券方案支配）。"""
    from burger_quest.models import Coupon, MenuItem, PlanItem
    from burger_quest.planner import coupon_sets

    menu_item = MenuItem(code="B1", name="麦辣鸡腿堡", price_cents=2100, tags=("辣",))
    combo = [PlanItem(menu_item, 1)]
    coupons = [
        Coupon(id=f"C{i}", name=f"券{i}", value_cents=100 * i, threshold_cents=1000) for i in range(1, 6)
    ]
    sets = coupon_sets(combo, coupons, 2100)
    assert len(sets) <= 3
    # 最优单券是减免最多的 C5；不再无条件先探无券
    assert sets[0] == [coupons[4]]
    # 无可用券时只有无券基线
    assert coupon_sets(combo, [], 2100) == [[]]


# ---------------------------------------------------------------------------
# 超时
# ---------------------------------------------------------------------------


def test_slow_call_times_out_with_readable_error():
    """慢会话触发超时：可读错误，不会永久卡死。"""
    import asyncio

    class SlowSession(ScriptedSession):
        async def call_tool(self, name, arguments):
            self.attempts += 1
            await asyncio.sleep(5)
            return _ok_result({"success": True, "code": 200, "data": {}})

    client = HttpMCPClient(token="x", timeout=0.05)
    session = SlowSession([])

    async def fake_open(self):
        self._session = session

    client._open_session = types.MethodType(fake_open, client)
    client._check_sdk = types.MethodType(lambda self: None, client)
    with client:
        with pytest.raises(MCPError) as ei:
            client.now_time()
    assert "超时" in str(ei.value)
    assert "0 秒" in str(ei.value) or "0秒" in str(ei.value)


def test_per_tool_timeouts():
    """分工具超时：默认 30s，算价 45s。"""
    client = HttpMCPClient(token="x")
    assert client._timeout_for("query-meals") == 30.0
    assert client._timeout_for("now-time-info") == 30.0
    assert client._timeout_for("calculate-price") == 45.0


# ---------------------------------------------------------------------------
# 重试
# ---------------------------------------------------------------------------


def test_transient_network_error_retried_then_succeeds():
    """瞬时网络错误重试后成功；审计记录真实尝试次数。"""
    script = [
        Exception("Connection reset by peer"),
        Exception("Connection reset by peer"),
        _ok_result({"success": True, "code": 200, "data": {"datetime": "T"}}),
    ]
    client, session = make_scripted_client(script)
    with client:
        t = client.now_time()
    assert t["datetime"] == "T"
    assert session.attempts == 3
    rec = [c for c in client.calls if c["tool"] == "now-time-info" and c["mcp_call"]][0]
    assert rec["ok"] is True and rec["attempts"] == 3


def test_persistent_network_error_fails_after_max_attempts():
    """持续网络错误：最多 3 次尝试后失败，消息说明重试次数。"""
    client, session = make_scripted_client([Exception("Connection reset by peer")])
    with client:
        with pytest.raises(MCPError) as ei:
            client.now_time()
    assert session.attempts == MAX_ATTEMPTS_NETWORK == 3
    assert "已重试 2 次均失败" in str(ei.value)


def test_401_not_retried():
    """401 认证错误不重试：直接提示检查 Token。"""
    client, session = make_scripted_client([Exception("HTTP 401 Unauthorized")])
    with client:
        with pytest.raises(MCPError) as ei:
            client.now_time()
    assert session.attempts == 1
    assert ei.value.code == 401 and "Token" in str(ei.value)


def test_429_rate_limited_with_fewer_attempts():
    """429 限流：最多 2 次尝试（尊重限流，不无限重试）。"""
    client, session = make_scripted_client([Exception("429 Too Many Requests")])
    with client:
        with pytest.raises(MCPError) as ei:
            client.now_time()
    assert session.attempts == MAX_ATTEMPTS_429 == 2
    assert ei.value.code == 429 and "限流" in str(ei.value)


def test_business_error_not_retried():
    """业务错误（信封 success=false）是确定性响应，不重试。"""
    client, session = make_scripted_client(
        [_ok_result({"success": False, "code": 400, "message": "缺少参数"})]
    )
    with client:
        with pytest.raises(MCPError) as ei:
            client.now_time()
    assert session.attempts == 1
    assert "缺少参数" in str(ei.value)


def test_server_error_5xx_retried():
    """5xx 服务端错误按瞬时故障重试。"""
    script = [
        Exception("Server returned an error response (500)"),
        _ok_result({"success": True, "code": 200, "data": {"datetime": "T"}}),
    ]
    client, session = make_scripted_client(script)
    with client:
        assert client.now_time()["datetime"] == "T"
    assert session.attempts == 2


def test_retry_decision_table():
    """重试决策表：401/业务错误不重试；429/5xx/网络有限重试。

    2026-10-17 稳定性增强：退避值带随机抖动（50%-100%），精确等式改为区间断言。
    """
    client = HttpMCPClient(token="x")
    assert client._retry_backoff("t", MCPError("t", "x", 401), 1) is None
    # 直接验证抖动区间（单次采样落在 50%-100% 之间）
    v = client._retry_backoff("t", MCPError("t", "限流", 429), 1)
    assert BACKOFF_429[0] * JITTER_RANGE[0] <= v <= BACKOFF_429[0] * JITTER_RANGE[1]
    assert client._retry_backoff("t", MCPError("t", "限流", 429), 2) is None
    v5 = client._retry_backoff("t", MCPError("t", "x", 500), 1)
    assert BACKOFF_NETWORK[0] * JITTER_RANGE[0] <= v5 <= BACKOFF_NETWORK[0] * JITTER_RANGE[1]
    assert client._retry_backoff("t", MCPError("t", "x", 500), 3) is None
    vn = client._retry_backoff("t", MCPError("t", "MCP 连接失败：ssl error"), 1)
    assert BACKOFF_NETWORK[0] * JITTER_RANGE[0] <= vn <= BACKOFF_NETWORK[0] * JITTER_RANGE[1]
    assert client._retry_backoff("t", MCPError("t", "业务错误（code=400）", 400), 1) is None


# ---------------------------------------------------------------------------
# 去重与缓存
# ---------------------------------------------------------------------------


def test_identical_read_deduplicated_within_ttl():
    """相同输入的只读查询：TTL 内只发一次真实请求。"""
    client, session = make_scripted_client(
        [_ok_result({"success": True, "code": 200, "data": {"datetime": "T1"}})]
    )
    with client:
        a = client.now_time()
        b = client.now_time()
    assert a == b
    assert session.attempts == 1
    assert client.cache_hits == 1
    # 去重命中也被审计（但标记为未发起请求）
    dedup = [c for c in client.calls if not c.get("mcp_call", True)]
    assert len(dedup) == 1 and "去重命中" in dedup[0]["note"]


def test_cache_expiry_refetches():
    """TTL 过期后重新发起真实请求。"""
    client, session = make_scripted_client(
        [_ok_result({"success": True, "code": 200, "data": {"datetime": "T1"}})]
    )
    with client:
        client.now_time()
        # 手动把缓存时间戳改到 31 秒前（超过 30s TTL）
        key = next(iter(client._read_cache))
        client._read_cache[key] = (time.monotonic() - 31, client._read_cache[key][1])
        client.now_time()
    assert session.attempts == 2


def test_failed_call_not_cached():
    """失败结果绝不进缓存：下一次仍然真实请求（不会把失败当成功复用）。"""
    # 前 3 次尝试全部瞬时失败 -> 第一次 now_time 抛错；第 4 次起成功
    client, session = make_scripted_client(fail_first_n=3)
    with client:
        with pytest.raises(MCPError):
            client.now_time()
        t = client.now_time()  # 必须真实再请求
    assert t["datetime"] == "T"
    assert session.attempts == 4  # 3 次失败 + 1 次成功


def test_calculate_price_never_cached():
    """算价绝不缓存：相同入参两次也重新请求（必须按当前方案实时核验）。"""
    payload = {"success": True, "code": 200, "data": {"price": 2250, "originalPrice": 2250, "discount": 0}}
    client, session = make_scripted_client([_ok_result(payload)])
    scene = scene_for_mode("takeaway", "1420762")
    from burger_quest.models import MenuItem, PlanItem

    items = [PlanItem(MenuItem(code="1440", name="麦辣鸡腿堡"), 1)]
    with client:
        q1 = client.calculate_price(scene, items, [])
        q2 = client.calculate_price(scene, items, [])
    assert q1.total_cents == q2.total_cents == 2250
    assert session.attempts == 2  # 两次真实请求


def test_coupons_never_cached():
    """优惠券查询绝不缓存（券池/有效性实时变）。"""
    client, session = make_scripted_client(
        [_ok_result({"success": True, "code": 200, "data": []})]
    )
    scene = scene_for_mode("takeaway", "1420762")
    with client:
        client.store_coupons(scene)
        client.store_coupons(scene)
    assert session.attempts == 2


def test_cacheable_ttl_excludes_coupons_and_price():
    """可缓存集合不包含优惠券与算价。"""
    from burger_quest.mcp_base import CACHEABLE_TTL

    assert "calculate-price" not in CACHEABLE_TTL
    assert "query-store-coupons" not in CACHEABLE_TTL
    assert "query-my-coupons" not in CACHEABLE_TTL
    assert CACHEABLE_TTL["now-time-info"] <= 60
    assert CACHEABLE_TTL["list-nutrition-foods"] <= 600


# ---------------------------------------------------------------------------
# 审计与安全
# ---------------------------------------------------------------------------


def test_audit_fields_complete():
    """审计记录包含序号/耗时/用途/尝试次数，且递增。"""
    client, _ = make_scripted_client(
        [_ok_result({"success": True, "code": 200, "data": {"datetime": "T"}})]
    )
    with client:
        client.now_time()
        client.now_time()  # 去重命中
    recs = client.calls
    assert len(recs) == 2
    assert recs[0]["seq"] == 1 and recs[1]["seq"] == 2
    assert recs[0]["purpose"] == "time_context"
    assert recs[0]["duration_ms"] is not None and recs[0]["duration_ms"] >= 0
    assert recs[0]["attempts"] == 1
    assert recs[0]["mcp_call"] is True and recs[1]["mcp_call"] is False


def test_audit_never_records_token():
    """审计参数脱敏：无 token/authorization/headers。"""
    client, _ = make_scripted_client(
        [_ok_result({"success": True, "code": 200, "data": {"datetime": "T"}})]
    )
    with client:
        client.now_time()
    for c in client.calls:
        args = c["args"]
        assert not any(k.lower() in ("token", "authorization", "headers") for k in args)


def test_cli_shows_call_audit(capsys):
    """CLI 输出包含 MCP 调用审计摘要。"""
    rc = cli.main(["demo", "预算35，想吃辣，两个人"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "MCP 调用审计" in out
    assert "真实请求" in out and "分工具" in out


def test_json_output_includes_call_summary(tmp_path):
    """--json 输出包含调用摘要；单任务真实请求 ≤15（优化前基线 25）。"""
    import json

    p = tmp_path / "r.json"
    rc = cli.main(["demo", "预算35，想吃辣", "--json", str(p)])
    assert rc == 0
    payload = json.loads(p.read_text(encoding="utf-8"))
    assert payload["mcp_calls_summary"]["total_calls"] <= 15


# ---------------------------------------------------------------------------
# 失败不静默
# ---------------------------------------------------------------------------


def test_price_failure_no_silent_zero():
    """算价失败不出方案，也没有虚假零价。"""
    from burger_quest.mcp_mock import MockMCPClient

    class BrokenPrice(MockMCPClient):
        def calculate_price(self, scene, items, coupon_ids, purpose=""):
            # 与真实客户端一致：失败也进审计（ok=False）
            self._record("calculate-price", {"storeCode": scene.store_code}, False,
                         note="模拟价格服务不可用", purpose=purpose)
            raise MCPError("calculate-price", "模拟价格服务不可用", code=500)

    c = BrokenPrice()
    r = run_quest("预算35，想吃辣", c)
    assert r.chosen is None
    assert all(o.total_cents is None for o in r.options)
    assert any("价格查询失败" in w for w in r.warnings)
    # 审计记录了失败
    failed = [x for x in c.calls if x["ok"] is False]
    assert any(x["tool"] == "calculate-price" for x in failed)


def test_partial_tool_failure_isolated():
    """部分工具失败被隔离：详情失败不影响菜单/价格链路。"""
    from burger_quest.mcp_mock import MockMCPClient

    class BrokenDetail(MockMCPClient):
        def meal_detail(self, scene, code):
            raise MCPError("query-meal-detail", "详情服务不可用")

    c = BrokenDetail()
    r = run_quest("预算35，想吃辣，两个人", c)
    # 名称来自算价 productList，详情失败不影响出方案
    assert r.chosen is not None
    assert r.chosen.price_checked
