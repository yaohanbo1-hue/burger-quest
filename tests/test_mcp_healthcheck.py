"""全链路体检回归（2026-10-17）。

覆盖本次体检新增/加固的链路行为（mcp_http / doctor）：
1. 无信封响应：白名单工具（query-my-coupons / delivery-query-addresses）保持宽松，
   其余工具明确失败（MCPError「响应解析失败」），不静默降级为空结果；
2. 超时分离：ConnectTimeout / ReadTimeout / PoolTimeout / WriteTimeout 单独映射，
   消息区分「连接超时」（换网络）与「读取超时」（服务端慢，稍后重试）；
3. 403 单独映射（实测无 Authorization 的 POST -> HTTP 403，与 401 Token 失效处置不同）；
4. 显式传输层超时：httpx2.AsyncClient 必须带 connect/read/write/pool，
   且 read >= 最重工具外层等待（calculate-price 45s）；
5. doctor：L1 HTTP 探针分类（401/403=PASS 服务可达；5xx/网络错误=FAIL）、
   探针绝不携带 Token、L1 先于 Token 检查、五层状态汇总、新错误分类；
6. 真实模式失败不回退 Demo（cli quest 缺 Token -> exit 2）。

不访问网络、不使用真实 Token（假会话 + monkeypatch）；真实服务侧的
只读联调验证记录见 MCP_INTEGRATION.md §3.11。
"""
import contextlib
import sys
import types
import urllib.error

import pytest

from burger_quest import cli, doctor
from burger_quest.mcp_base import MCPError, OrderScene
from burger_quest.mcp_http import (
    CONNECT_TIMEOUT,
    PLAIN_TEXT_TOOLS,
    READ_TIMEOUT,
    TOOL_TIMEOUTS,
    HttpMCPClient,
    _map_sdk_error,
)
from test_http_client import make_client

SCENE = OrderScene(store_code="1420762", order_type=1, be_type=1)


def _raw_text_client(text: str, tool: str = "query-meals"):
    """构造对 tool 返回纯文本（无信封）的假会话客户端。"""
    client, _ = make_client(
        raw_results={tool: types.SimpleNamespace(content=[types.SimpleNamespace(text=text)])}
    )
    return client


# ---------------------------------------------------------------------------
# 1. 无信封响应：白名单宽松 + 其余明确失败
# ---------------------------------------------------------------------------


def test_plain_text_allowlist_is_exactly_the_documented_tools():
    """白名单必须且只能包含联调实测的纯文本返回工具（防误扩）。

    campaign-calendar 于 2026-10-18 实测返回 Markdown 文本（非 JSON 信封）。
    """
    assert PLAIN_TEXT_TOOLS == {"query-my-coupons", "delivery-query-addresses", "campaign-calendar"}


@pytest.mark.parametrize("tool", ["query-meals", "query-nearby-stores", "calculate-price", "now-time-info"])
def test_no_envelope_structured_tools_fail_loudly(tool):
    """其余工具返回纯文本（网关错误页/服务异常）：明确 MCPError，不静默降级。"""
    client = _raw_text_client("<html>502 Bad Gateway</html>", tool)
    with client, pytest.raises(MCPError) as ei:
        if tool == "query-meals":
            client.query_meals(SCENE)
        elif tool == "query-nearby-stores":
            client.nearby_stores("科技园", city="深圳市")
        elif tool == "calculate-price":
            from burger_quest.models import MenuItem, PlanItem

            client.calculate_price(SCENE, [PlanItem(MenuItem(code="1440", name="堡"), 1)], [])
        else:
            client.now_time()
    assert "响应解析失败" in str(ei.value)
    assert "502" in str(ei.value)  # 保留返回开头片段便于定位
    assert any(c["ok"] is False and c.get("note") == "无 JSON 信封" for c in client.calls)


def test_query_my_coupons_plain_text_stays_empty():
    """白名单：query-my-coupons 无券时官方返回纯文本——维持宽松（空列表）。"""
    client = _raw_text_client("暂无可用优惠券", "query-my-coupons")
    with client:
        assert client.my_coupons() == []


def test_delivery_addresses_plain_text_stays_empty():
    """白名单：delivery-query-addresses 无地址时可能返回纯文本——维持宽松（空列表）。"""
    client = _raw_text_client("您还没有保存配送地址", "delivery-query-addresses")
    with client:
        assert client.delivery_addresses() == []


# ---------------------------------------------------------------------------
# 2. 超时分离
# ---------------------------------------------------------------------------


class _FakeReadTimeout(Exception):
    pass


class _FakeConnectTimeout(Exception):
    pass


class _FakePoolTimeout(Exception):
    pass


class _FakeWriteTimeout(Exception):
    pass


class _FakeRemoteProtocolError(Exception):
    pass


def test_map_sdk_error_read_vs_connect_timeout():
    """读取超时与连接超时必须分开：排查方向相反（服务端慢 vs 网络不通）。"""
    read_err = _map_sdk_error("tool", _FakeReadTimeout("The read operation timed out"))
    assert "读取超时" in str(read_err) and read_err.code == 408
    connect_err = _map_sdk_error("tool", _FakeConnectTimeout("The connect operation timed out"))
    assert "连接超时" in str(connect_err) and connect_err.code == 408
    pool_err = _map_sdk_error("tool", _FakePoolTimeout("pool timeout"))
    assert "连接池超时" in str(pool_err)
    write_err = _map_sdk_error("tool", _FakeWriteTimeout("write timeout"))
    assert "写入超时" in str(write_err)


def test_map_sdk_error_remote_protocol_error():
    """HTTP 层内容非法（RemoteProtocolError，含无效 JSON）：映射为响应协议错误。"""
    e = _map_sdk_error("tool", _FakeRemoteProtocolError("peer closed connection without proper TLS"))
    assert "响应协议错误" in str(e)


def test_map_sdk_error_403():
    """403 单独映射：与 401（Token 失效）排查方向不同（WAF/权限 vs 重新申请）。"""
    e = _map_sdk_error("tool", Exception("Server returned an error response"), http_status=403)
    assert e.code == 403
    assert "403" in str(e) and ("WAF" in str(e) or "权限" in str(e))


def test_timeout_errors_are_retryable_but_403_is_not():
    """读取/连接超时属瞬时错误可有限重试；403 确定性错误不重试。"""
    client = HttpMCPClient(token="x")
    read_timeout = _map_sdk_error("tool", _FakeReadTimeout("timed out"))
    connect_timeout = _map_sdk_error("tool", _FakeConnectTimeout("timed out"))
    forbidden = _map_sdk_error("tool", Exception("forbidden"), http_status=403)
    assert client._retry_backoff("tool", read_timeout, 1) is not None  # 瞬时：退避重试
    assert client._retry_backoff("tool", connect_timeout, 1) is not None
    assert client._retry_backoff("tool", forbidden, 1) is None  # 确定性：不重试


# ---------------------------------------------------------------------------
# 3. 显式传输层超时
# ---------------------------------------------------------------------------


def test_transport_kwargs_sets_explicit_timeouts(monkeypatch):
    """httpx2.AsyncClient 必须显式携带 connect/read/write/pool 超时。

    SDK 默认 read=5s 会提前误杀慢但正常的 calculate-price（外层等待 45s）。
    """
    captured: dict = {}

    class FakeTimeout:
        def __init__(self, **kwargs):
            captured["timeout"] = kwargs

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

    monkeypatch.setitem(sys.modules, "httpx2", types.SimpleNamespace(Timeout=FakeTimeout, AsyncClient=FakeAsyncClient))
    client = HttpMCPClient(token="x")

    def new_sdk_factory(url, http_client=None, terminate_on_close=True, max_sse_event_size=None):  # noqa: ARG001
        return None

    kwargs = client._transport_kwargs(new_sdk_factory, {"Authorization": "Bearer x"})
    assert "http_client" in kwargs
    timeout = captured["timeout"]
    assert timeout["connect"] == CONNECT_TIMEOUT
    assert timeout["read"] == READ_TIMEOUT
    assert timeout["read"] >= TOOL_TIMEOUTS["calculate-price"]  # read 不得早于最重工具外层等待
    assert timeout["write"] > 0 and timeout["pool"] > 0
    # Authorization 仍然只走这个客户端头，不进入任何审计/输出
    assert captured["client"]["headers"]["Authorization"] == "Bearer x"


def test_transport_kwargs_without_timeout_class_still_works(monkeypatch):
    """httpx2 没有 Timeout 类的极端情况：放弃显式配置而非崩溃。"""
    captured: dict = {}

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

    monkeypatch.setitem(sys.modules, "httpx2", types.SimpleNamespace(Timeout=None, AsyncClient=FakeAsyncClient))
    client = HttpMCPClient(token="x")

    def new_sdk_factory(url, http_client=None, terminate_on_close=True):  # noqa: ARG001
        return None

    kwargs = client._transport_kwargs(new_sdk_factory, {"Authorization": "Bearer x"})
    assert "http_client" in kwargs and "timeout" not in captured["client"]


# ---------------------------------------------------------------------------
# 4. doctor：L1 HTTP 探针 / 五层汇总 / 新分类
# ---------------------------------------------------------------------------


def _fake_socket_connect(monkeypatch):
    @contextlib.contextmanager
    def fake_create_connection(*_args, **_kwargs):
        yield None

    monkeypatch.setattr("socket.create_connection", fake_create_connection)


def _patch_urlopen(monkeypatch, outcome):
    """把 urllib.request.urlopen 换成预制结果；捕获请求对象用于断言不携带 Token。"""
    captured: dict = {}

    def fake_urlopen(request, timeout=None):
        captured["request"] = request
        captured["timeout"] = timeout
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return captured


@pytest.mark.parametrize("code", [400, 401, 403, 404, 405])
def test_endpoint_probe_auth_rejection_is_l1_pass(monkeypatch, code):
    """L1 探针：401/403 等服务端响应 = 服务可达 + 认证层正常（无 Token 的 PASS 证据）。"""
    _fake_socket_connect(monkeypatch)
    captured = _patch_urlopen(monkeypatch, urllib.error.HTTPError("https://mcp.mcd.cn", code, "x", {}, None))
    ok, detail, _hint = doctor._probe_endpoint()
    assert ok and f"HTTP {code}" in detail
    # 探针绝不携带 Token/Authorization
    assert "Authorization" not in captured["request"].headers
    assert captured["request"].data in (b"{}", None)


def test_endpoint_probe_5xx_is_fail(monkeypatch):
    """L1 探针：5xx = 服务端异常，FAIL + 下一步建议。"""
    _fake_socket_connect(monkeypatch)
    _patch_urlopen(monkeypatch, urllib.error.HTTPError("https://mcp.mcd.cn", 503, "x", {}, None))
    ok, detail, hint = doctor._probe_endpoint()
    assert not ok and "503" in detail and hint


def test_endpoint_probe_network_error_is_fail(monkeypatch):
    """L1 探针：URLError（DNS/连接失败）= FAIL，不得谎报可达。"""
    _fake_socket_connect(monkeypatch)
    _patch_urlopen(monkeypatch, urllib.error.URLError(OSError("getaddrinfo failed")))
    ok, _detail, hint = doctor._probe_endpoint()
    assert not ok and "mcp.mcd.cn" in hint


def test_endpoint_probe_ssl_error_has_dedicated_hint(monkeypatch):
    import ssl

    _fake_socket_connect(monkeypatch)
    _patch_urlopen(monkeypatch, urllib.error.URLError(ssl.SSLError("certificate verify failed")))
    ok, detail, hint = doctor._probe_endpoint()
    assert not ok and "TLS" in detail and "证书" in hint


def test_network_step_reports_l1_level(monkeypatch):
    """L1 步骤必须带级别标签（供五层汇总与用户按层定位）。"""
    _fake_socket_connect(monkeypatch)
    _patch_urlopen(monkeypatch, urllib.error.HTTPError("https://mcp.mcd.cn", 403, "x", {}, None))

    class _Report:
        def __init__(self):
            self.steps = []

        def add(self, name, ok, detail="", hint="", level=""):
            self.steps.append({"step": name, "ok": ok, "detail": detail, "hint": hint, "level": level})

    report = _Report()
    assert doctor._check_network(report) is True
    assert report.steps[-1]["level"] == "L1"
    assert report.steps[-1]["ok"] is True


def test_l1_checked_before_token(monkeypatch):
    """L1 服务可达性不需要 Token：必须排在 Token 检查之前（无 Token 也能拿到 L1 结论）。"""
    monkeypatch.delenv("MCD_MCP_TOKEN", raising=False)
    ran = {}

    def fake_network(report):
        ran["network"] = True
        report.add("网络：MCP 服务端点", True, "（测试替身）", level="L1")
        return True

    monkeypatch.setattr(doctor, "_check_network", fake_network)
    report = doctor.run_doctor()
    assert ran.get("network") is True
    token_step = next(s for s in report.steps if s["step"].startswith("Token"))
    assert token_step["ok"] is False
    assert report.exit_code == 2


def test_level_summary_marks_unexecuted_honestly(monkeypatch, capsys):
    """五层汇总：L1 PASS 实证；L2-L4 未执行如实标记；L5 指向 smoke/quest。"""
    monkeypatch.delenv("MCD_MCP_TOKEN", raising=False)

    def fake_network(report):
        report.add("网络：MCP 服务端点", True, "（测试替身）", level="L1")
        return True

    monkeypatch.setattr(doctor, "_check_network", fake_network)
    rc = doctor.main([])  # main 才会打印五层汇总
    out = capsys.readouterr().out
    assert rc == 2
    assert "✅ L1 服务可访问：PASS" in out
    assert "⬜ L2 认证成功" in out and "未执行" in out
    assert "L5 业务流程真实跑通" in out and "smoke" in out
    # 全程不得出现 Token 值
    assert "test-token" not in out


@pytest.mark.parametrize(
    "message,expect_cat",
    [
        ("MCP 读取超时：已建立连接但服务端 60 秒内未返回数据", "TIMEOUT_READ"),
        ("MCP 连接超时：10 秒内无法完成 TCP/TLS 握手", "TIMEOUT_CONNECT"),
        ("MCP 连接池超时：10 秒内未拿到可用连接", "TIMEOUT_READ"),
        ("MCP 响应解析失败：官方返回不是预期的 API 信封 JSON", "RESPONSE"),
        ("MCP 服务拒绝访问（403）：可能是网络代理/WAF 拦截", "403"),
        ("MCP 响应协议错误：HTTP 层内容非法", "RESPONSE"),
    ],
)
def test_classify_new_categories(message, expect_cat):
    assert doctor._classify_error(MCPError("t", message)) == expect_cat


def test_hint_for_new_categories_is_actionable():
    for cat in ("TIMEOUT_CONNECT", "TIMEOUT_READ", "RESPONSE"):
        assert doctor._hint_for(cat) and len(doctor._hint_for(cat)) > 10


def test_doctor_json_output_includes_levels_without_secrets(monkeypatch, tmp_path):
    """--json 输出带级别字段且不含 Token 值。"""
    monkeypatch.setenv("MCD_MCP_TOKEN", "super-secret-token-value")

    def fake_token_fail(report):
        report.add("Token：MCD_MCP_TOKEN", False, "未配置（测试）", "测试建议")
        return False

    monkeypatch.setattr(doctor, "_check_network", lambda r: (r.add("网络：MCP 服务端点", True, "x", level="L1"), True)[1])
    monkeypatch.setattr(doctor, "_check_token", fake_token_fail)
    out = tmp_path / "doctor.json"
    rc = doctor.main(["--json", str(out)])
    import json

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert rc == 2
    assert any(s.get("level") == "L1" for s in payload["steps"])
    assert "super-secret-token-value" not in out.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 5. 真实模式不回退 Demo（红线）
# ---------------------------------------------------------------------------


def test_quest_without_token_fails_loudly_no_demo_fallback(monkeypatch, capsys):
    """cli quest 缺 Token：exit 2 且明示不会自动切换演示数据（红线守卫）。"""
    monkeypatch.delenv("MCD_MCP_TOKEN", raising=False)
    rc = cli.main(["quest", "我想吃辣，预算35"])
    assert rc == 2
    out = capsys.readouterr().out
    assert "MCD_MCP_TOKEN" in out
    assert "不会自动切换" in out or "demo" in out.lower()


# ---------------------------------------------------------------------------
# 6. 体检真实运行暴露的诊断措辞（预算空结果）
# ---------------------------------------------------------------------------


def test_budget_diagnosis_distinguishes_prescreened_from_verified():
    """全部候选止步预估筛时，不得声称「其余逐一用权威价格核验」（2026-10-17 体检实录）。

    真实运行复现：quest "预算50 想吃点好的 两个人" --store-code 1420762 ->
    全部候选因估算价超预算被预筛，calculate-price 仅 1 次（发现），
    旧文案却写「其余逐一用权威价格核验」——暗示了没做的核验。
    """
    from burger_quest.models import Constraints, MenuItem, PriceQuote
    from burger_quest.planner import build_plan

    menu = [
        MenuItem(code="M1", name="巨无霸", category="主食", price_cents=2500),
        MenuItem(code="S1", name="薯条", category="小食", price_cents=1300),
        MenuItem(code="D1", name="可乐", category="饮品", price_cents=1000),
    ]

    def no_price(items, coupons):
        return PriceQuote(ok=False, message="模拟权威核验失败")

    # 路径 A：全部候选被预估筛拦下（price_fn 都未被调用）
    c = Constraints(budget_cents=1000)
    options, warnings = build_plan(c, menu, [], [], no_price)
    assert not options
    assert any("全部候选组合的估算价均已超出预算，未进入权威核验" in w for w in warnings)
    assert not any("其余逐一用权威价格核验" in w for w in warnings)

    # 路径 B：预筛跳过 + 权威核验失败并存——两类数量如实并列
    c2 = Constraints(budget_cents=4800)
    options2, warnings2 = build_plan(c2, menu, [], [], no_price)
    assert not options2
    mixed = [w for w in warnings2 if "因估算价超出预算被跳过" in w]
    assert mixed and "进入权威核验但均失败" in mixed[0]
