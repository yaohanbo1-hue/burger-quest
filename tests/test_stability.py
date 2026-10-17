"""真实 MCP 请求稳定性测试（2026-10-17 稳定性增强新增）。

覆盖：Retry-After 尊重与封顶、退避抖动区间、会话重试预算、连续网络失败断路、
副作用工具禁重试、诊断日志脱敏（Token 零泄露）、日志默认关闭、非法响应仍明确失败。
不访问网络、不使用真实 Token（假会话 + monkeypatch）。
"""
import json
import os
import types

import pytest

from burger_quest.mcp_base import MCPError, OrderScene
from burger_quest.mcp_http import (
    JITTER_RANGE,
    MAX_CONSECUTIVE_NETWORK_FAILURES,
    MAX_SESSION_RETRIES,
    NON_IDEMPOTENT_TOOLS,
    RETRY_AFTER_CAP,
    HttpMCPClient,
)
from test_http_client import make_client

SCENE = OrderScene(store_code="1420762", order_type=1, be_type=1)


def _network_error(tool="query-meals", msg="MCP 连接失败：network unreachable"):
    return MCPError(tool, msg)


def _rate_limit_error(tool="query-meals", msg="触发麦当劳 MCP 限流（429）"):
    return MCPError(tool, msg, code=429)


# ---------------------------------------------------------------------------
# Retry-After：尊重服务端，但封顶
# ---------------------------------------------------------------------------


def test_retry_after_honored_when_present():
    client = HttpMCPClient(token="x")
    client._http_retry_after = 7.0
    v = client._retry_backoff("query-meals", _rate_limit_error(), 1)
    assert v is not None
    assert 7.0 * JITTER_RANGE[0] <= v <= 7.0 * JITTER_RANGE[1]


def test_retry_after_capped():
    client = HttpMCPClient(token="x")
    client._http_retry_after = 999.0  # 服务端给了超大值：封顶，不无限等
    v = client._retry_backoff("query-meals", _rate_limit_error(), 1)
    assert RETRY_AFTER_CAP * JITTER_RANGE[0] <= v <= RETRY_AFTER_CAP * JITTER_RANGE[1]


def test_retry_after_absent_uses_builtin():
    from burger_quest.mcp_http import BACKOFF_429

    client = HttpMCPClient(token="x")
    client._http_retry_after = None
    v = client._retry_backoff("query-meals", _rate_limit_error(), 1)
    assert BACKOFF_429[0] * JITTER_RANGE[0] <= v <= BACKOFF_429[0] * JITTER_RANGE[1]


def test_retry_after_non_numeric_ignored():
    """Retry-After 是 HTTP-date 形态时忽略（事件钩子只认秒形态），走内置退避。"""
    from burger_quest.mcp_http import BACKOFF_429

    client = HttpMCPClient(token="x")
    client._http_retry_after = None  # date 形态在 hook 里就被丢弃
    v = client._retry_backoff("query-meals", _rate_limit_error(), 1)
    assert BACKOFF_429[0] * JITTER_RANGE[0] <= v <= BACKOFF_429[0] * JITTER_RANGE[1]


# ---------------------------------------------------------------------------
# 抖动区间
# ---------------------------------------------------------------------------


def test_jitter_stays_within_bounds():
    client = HttpMCPClient(token="x")
    vals = [client._retry_backoff("t", MCPError("t", "x", 500), 1) for _ in range(50)]
    assert all(BACKOFF_NETWORK_MIN := 0.5 * JITTER_RANGE[0] <= v <= 1.0 * JITTER_RANGE[1] for v in vals)
    # 抖动应产生多个不同取值（恒定值说明没抖动）
    assert len(set(round(v, 6) for v in vals)) > 1


# ---------------------------------------------------------------------------
# 会话重试预算（防重试风暴）
# ---------------------------------------------------------------------------


def test_session_retry_budget_caps_total_retries():
    client = HttpMCPClient(token="x")
    client._session_retries = MAX_SESSION_RETRIES
    assert client._retry_backoff("query-meals", _network_error(), 1) is None


def test_session_retry_budget_shared_across_tools():
    """预算按实例累计：一个工具烧掉预算后，其它工具也不再重试。"""
    client = HttpMCPClient(token="x")
    # 第一次：允许重试（预热预算到满）
    client._session_retries = MAX_SESSION_RETRIES - 1
    first = client._retry_backoff("query-meals", _network_error(), 1)
    assert first is not None
    client._session_retries += 1  # 模拟 _invoke_with_retry 的记账
    assert client._retry_backoff("list-nutrition-foods", _network_error("list-nutrition-foods"), 1) is None


# ---------------------------------------------------------------------------
# 连续网络失败断路
# ---------------------------------------------------------------------------


def test_consecutive_network_failure_opens_circuit():
    client = HttpMCPClient(token="x")
    client._consecutive_network_failures = MAX_CONSECUTIVE_NETWORK_FAILURES
    assert client._retry_backoff("query-meals", _network_error(), 1) is None


def test_business_error_does_not_trip_circuit():
    """429/业务错误不算网络失败——不会误断路。"""
    client = HttpMCPClient(token="x")
    v = client._retry_backoff("query-meals", _rate_limit_error(), 1)
    assert v is not None
    assert client._consecutive_network_failures == 0


def test_consecutive_counter_resets_after_success():
    """成功后断路计数复位（瞬时抖动不永久判死刑）。"""
    client, _ = make_client({"now-time-info": {"success": True, "code": 200, "data": {"datetime": "T"}}})
    client._consecutive_network_failures = 2
    with client:
        client.now_time()
    assert client._consecutive_network_failures == 0


# ---------------------------------------------------------------------------
# 副作用工具禁重试
# ---------------------------------------------------------------------------


def test_non_idempotent_tools_never_retried():
    assert "create-order" in NON_IDEMPOTENT_TOOLS and "auto-bind-coupons" in NON_IDEMPOTENT_TOOLS
    client = HttpMCPClient(token="x")
    for tool in NON_IDEMPOTENT_TOOLS:
        # 即使是「可重试」的网络错误形态，副作用工具也不许重试
        assert client._retry_backoff(tool, _network_error(tool), 1) is None
        assert client._retry_backoff(tool, MCPError(tool, "x", 500), 1) is None


def test_read_tools_still_retryable():
    """只读/计算类工具保持可重试（稳定性不误伤正常路径）。"""
    client = HttpMCPClient(token="x")
    assert client._retry_backoff("query-meals", _network_error(), 1) is not None
    assert client._retry_backoff("calculate-price", MCPError("calculate-price", "x", 503), 1) is not None


# ---------------------------------------------------------------------------
# 诊断日志：默认关闭 + 脱敏
# ---------------------------------------------------------------------------


def test_log_disabled_by_default(tmp_path, monkeypatch):
    """未配置 BURGER_QUEST_LOG：零文件副作用。"""
    monkeypatch.delenv("BURGER_QUEST_LOG", raising=False)
    from burger_quest import diagnostics

    diagnostics.log_event("test_event", token="should-not-be-written")
    assert not list(tmp_path.iterdir())


def test_log_scrubs_token_and_sensitive_keys(tmp_path, monkeypatch):
    """日志脱敏：Token 值/ Bearer / 敏感键不得明文出现。"""
    fake_token = "FAKE-TOKEN-abcdef123456"
    monkeypatch.setenv("MCD_MCP_TOKEN", fake_token)
    log_file = tmp_path / "diag.log"
    monkeypatch.setenv("BURGER_QUEST_LOG", str(log_file))
    from burger_quest import diagnostics

    diagnostics.log_event(
        "mcp_attempt_failed",
        trace="abcd1234", tool="query-meals", attempt=1,
        error=f"连接失败，凭证 {fake_token} 疑似问题",
        headers={"Authorization": f"Bearer {fake_token}"},
        authorization=f"Bearer {fake_token}",
    )
    content = log_file.read_text(encoding="utf-8")
    assert "mcp_attempt_failed" in content and "abcd1234" in content  # 排障信息在
    assert fake_token not in content                           # Token 值不在
    assert "Bearer FAKE" not in content                         # Bearer 凭据不在
    assert '"headers": "***"' in content and '"authorization": "***"' in content


def test_failed_calls_write_diagnostic_log(tmp_path, monkeypatch):
    """失败调用的诊断日志：含 trace/工具/尝试次数/错误类型，无 Token。"""
    fake_token = "FAKE-TOFILE-987654321"
    monkeypatch.setenv("MCD_MCP_TOKEN", fake_token)
    log_file = tmp_path / "diag2.log"
    monkeypatch.setenv("BURGER_QUEST_LOG", str(log_file))
    client, _ = make_client(errors={"query-meals": Exception("HTTP 500 boom")}, timeout=5)
    with client, pytest.raises(MCPError):
        client.query_meals(SCENE)
    lines = [json.loads(x) for x in log_file.read_text(encoding="utf-8").splitlines()]
    events = {ln["event"] for ln in lines}
    assert "mcp_attempt_failed" in events
    failed = next(ln for ln in lines if ln["event"] == "mcp_attempt_failed")
    assert failed["tool"] == "query-meals" and failed["trace"] == client.trace_id
    assert fake_token not in log_file.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 非法响应：仍然明确失败（不为稳定性牺牲真实性）
# ---------------------------------------------------------------------------


def test_invalid_response_still_fails_loudly_with_log(tmp_path, monkeypatch):
    """无信封响应：明确失败并留日志——重试再多也不把非法响应当成功。"""
    monkeypatch.setenv("MCD_MCP_TOKEN", "FAKE-TOKEN-xyz")
    log_file = tmp_path / "diag3.log"
    monkeypatch.setenv("BURGER_QUEST_LOG", str(log_file))
    client, _ = make_client(
        raw_results={"query-meals": types.SimpleNamespace(content=[types.SimpleNamespace(text="<html>502</html>")])}
    )
    scene = SCENE
    with client, pytest.raises(MCPError) as ei:
        client.query_meals(scene)
    assert "响应解析失败" in str(ei.value)
    events = {json.loads(x)["event"] for x in log_file.read_text(encoding="utf-8").splitlines()}
    assert "mcp_no_envelope" in events


def test_protocol_error_still_fails_loudly(tmp_path, monkeypatch):
    monkeypatch.setenv("MCD_MCP_TOKEN", "FAKE-TOKEN-xyz")
    log_file = tmp_path / "diag4.log"
    monkeypatch.setenv("BURGER_QUEST_LOG", str(log_file))
    client, _ = make_client(
        raw_results={"query-meals": types.SimpleNamespace(is_error=True, content=[types.SimpleNamespace(text="denied")])}
    )
    scene = SCENE
    with client, pytest.raises(MCPError):
        client.query_meals(scene)
    events = {json.loads(x)["event"] for x in log_file.read_text(encoding="utf-8").splitlines()}
    assert "mcp_protocol_error" in events
