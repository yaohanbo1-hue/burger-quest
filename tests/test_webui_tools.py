"""Web 工具箱 API 与 available-coupons 状态分类测试（离线，无 Token）。"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from burger_quest import webui
from burger_quest.mcp_base import MCPError
from burger_quest.mcp_http import HttpMCPClient

# ===========================================================================
# A. available-coupons 四类状态（纯逻辑，不联网）
# ===========================================================================


class _FakeRaw:
    """只提供 _call_raw 的假 client，用于驱动 available_coupons 的分类逻辑。"""

    def __init__(self, result):
        self._result = result

    def _call_raw(self, tool, arguments, timeout=30.0):
        return self._result


def _result(payload, text=""):
    return SimpleNamespace(structured_content=payload, content=[SimpleNamespace(text=text)] if text else [])


def _classify(payload, text=""):
    fake = _FakeRaw(_result(payload, text))
    return HttpMCPClient.available_coupons(fake)  # type: ignore[arg-type]


def test_coupon_official_no_coupons_is_empty_not_error():
    """官方 code=499 / No coupons available 是「明确无券」，不是错误。"""
    q = _classify({"success": False, "code": 499, "message": "No coupons available", "data": None})
    assert q.state == "empty"
    assert q.coupons == []
    assert "499" in q.note


def test_coupon_success_null_data_is_unknown():
    q = _classify({"success": True, "code": 200, "message": "ok", "data": None})
    assert q.state == "unknown"
    assert q.coupons == []


def test_coupon_success_list_is_ok():
    payload = {
        "success": True,
        "code": 200,
        "data": [{"couponId": "C1", "couponName": "满 30 减 5", "threshold": 30, "value": 5}],
    }
    q = _classify(payload)
    assert q.state == "ok"
    assert len(q.coupons) == 1
    assert q.coupons[0].name == "满 30 减 5"


def test_coupon_other_business_error_raises():
    with pytest.raises(MCPError) as ei:
        _classify({"success": False, "code": 500, "message": "服务端异常", "data": None})
    assert ei.value.code == 500


def test_coupon_without_structured_uses_text_semantics():
    q = _classify(None, text="### 麦麦省优惠券列表：\n\n暂无可领取的优惠券")
    assert q.state == "empty"
    q2 = _classify(None, text="一些无法判定的文本")
    assert q2.state == "unknown"


# ===========================================================================
# B. Web API 路由（真实 HTTP + 桩 App）
# ===========================================================================


class _StubToolsApp:
    def campaign(self):
        return {
            "state": "ok",
            "generated_at": "2026-10-18T12:00:00",
            "counts": {"live": 1, "upcoming": 0, "past": 0},
            "campaigns": [{"date": "2026-10-18", "title": "测试活动", "content": "内容", "image_url": "", "status": "live"}],
        }

    def coupons(self, store_code=None):
        sections = [{"name": "可领取（麦麦省）", "state": "empty", "note": "官方明确无券", "coupons": []}]
        sections.append(
            {"name": "门店适用", "state": "skipped" if not store_code else "ok", "note": "未提供门店编码", "coupons": []}
        )
        return {"state": "ok", "generated_at": "2026-10-18T12:00:00", "sections": sections}

    def meal_detail(self, code, store_code, reconfigure=None):
        return {
            "state": "ok",
            "generated_at": "2026-10-18T12:00:00",
            "code": code,
            "store_code": store_code,
            "pricing_mode": "single_item_combination",
            "groups": [{"index": 1, "name": "主食", "quantity": 1, "choices": []}],
            "combo_price": {"ok": True, "total_cents": 3600, "message": ""},
            "reconfigure_result": None if not reconfigure else {"ok": True, "total_cents": 4250, "items": [], "notes": []},
        }


@pytest.fixture()
def tools_srv():
    server = webui._Server(("127.0.0.1", 0), _StubToolsApp())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _req(srv, path, method="GET", data=None):
    host, port = srv.server_address[0], srv.server_address[1]
    body = json.dumps(data).encode() if data is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(f"http://{host}:{port}{path}", data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def test_api_campaign_returns_state_and_time(tools_srv):
    status, body = _req(tools_srv, "/api/campaign")
    assert status == 200 and body["state"] == "ok"
    assert body["generated_at"] and body["counts"]["live"] == 1


def test_api_coupons_distinguishes_sections(tools_srv):
    status, body = _req(tools_srv, "/api/coupons")
    assert status == 200
    states = {s["name"]: s["state"] for s in body["sections"]}
    assert states["可领取（麦麦省）"] == "empty"
    assert states["门店适用"] == "skipped"
    status2, body2 = _req(tools_srv, "/api/coupons?store_code=1420762")
    assert {s["name"]: s["state"] for s in body2["sections"]}["门店适用"] == "ok"


def test_api_detail_requires_code_and_store(tools_srv):
    status, body = _req(tools_srv, "/api/detail", method="POST", data={"code": "", "store_code": ""})
    assert status == 400 and body["state"] == "error"


def test_api_detail_marks_single_item_combination(tools_srv):
    status, body = _req(
        tools_srv, "/api/detail", method="POST", data={"code": "9900011121", "store_code": "1420762"}
    )
    assert status == 200
    assert body["pricing_mode"] == "single_item_combination"
    assert body["combo_price"]["total_cents"] == 3600


def test_api_detail_passes_reconfigure(tools_srv):
    status, body = _req(
        tools_srv,
        "/api/detail",
        method="POST",
        data={"code": "9900011121", "store_code": "1420762", "reconfigure": {"4": "3071"}},
    )
    assert status == 200
    assert body["reconfigure_result"]["total_cents"] == 4250


def test_api_detail_rejects_bad_reconfigure(tools_srv):
    status, body = _req(
        tools_srv,
        "/api/detail",
        method="POST",
        data={"code": "9900011121", "store_code": "1420762", "reconfigure": {"x": "y"}},
    )
    assert status == 400


# ===========================================================================
# C. 券状态加固（精确空态、data 类型、字段缺失）
# ===========================================================================


def test_coupon_code_499_with_other_message_is_business_error():
    """只有 code=499 且 message 精确匹配才算无券；否则是真实业务错误。"""
    with pytest.raises(MCPError) as ei:
        _classify({"success": False, "code": 499, "message": "系统繁忙", "data": None})
    assert ei.value.code == 499


def test_coupon_text_with_generic_none_word_is_unknown():
    """含「暂无」但不是官方无券文案 → unknown（不得当成 empty）。"""
    q = _classify(None, text="暂无可用数据，请检查网络（这不是无券文案）")
    assert q.state == "unknown"


def test_coupon_data_dict_or_str_is_contract_error():
    for bad in ({"coupons": []}, "oops"):
        with pytest.raises(MCPError) as ei:
            _classify({"success": True, "code": 200, "data": bad})
        assert "契约" in str(ei.value)


def test_coupon_entries_missing_required_fields_is_contract_error():
    with pytest.raises(MCPError) as ei:
        _classify({"success": True, "code": 200, "data": [{}]})
    assert "契约" in str(ei.value)


# ===========================================================================
# D. 三类券独立容错（部分失败仍展示成功分区）
# ===========================================================================


class _PartialClient:
    def __init__(self, fail: set[str] | None = None):
        self.fail = fail or set()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def available_coupons(self):
        if "available" in self.fail:
            raise MCPError("available-coupons", "available boom", code=500)
        from burger_quest.models import CouponQuery

        return CouponQuery(state="empty", note="官方明确无券")

    def my_coupons(self):
        if "mine" in self.fail:
            raise MCPError("query-my-coupons", "mine boom", code=500)
        return []

    def store_coupons(self, scene):
        if "store" in self.fail:
            raise MCPError("query-store-coupons", "store boom", code=500)
        return []


def test_webapp_coupons_available_fails_others_still_run(monkeypatch):
    monkeypatch.setattr(webui.WebApp, "_client", lambda self: _PartialClient({"available"}))
    out = webui.WebApp().coupons("1420762")
    assert out["state"] == "partial"
    states = {s["name"]: s["state"] for s in out["sections"]}
    assert states["可领取（麦麦省）"] == "error"
    assert states["我的卡包"] == "ok"
    assert states["门店 1420762 适用"] == "ok"


def test_webapp_coupons_mine_fails_others_still_run(monkeypatch):
    monkeypatch.setattr(webui.WebApp, "_client", lambda self: _PartialClient({"mine"}))
    out = webui.WebApp().coupons("1420762")
    states = {s["name"]: s["state"] for s in out["sections"]}
    assert states["我的卡包"] == "error"
    assert states["可领取（麦麦省）"] == "empty"
    assert out["state"] == "partial"


def test_webapp_coupons_store_fails_and_missing(monkeypatch):
    monkeypatch.setattr(webui.WebApp, "_client", lambda self: _PartialClient({"store"}))
    out = webui.WebApp().coupons("1420762")
    states = {s["name"]: s["state"] for s in out["sections"]}
    assert states["门店 1420762 适用"] == "error"

    monkeypatch.setattr(webui.WebApp, "_client", lambda self: _PartialClient())
    out2 = webui.WebApp().coupons(None)
    states2 = {s["name"]: s["state"] for s in out2["sections"]}
    assert states2["门店适用"] == "skipped"


def test_webapp_coupons_all_fail_is_error(monkeypatch):
    monkeypatch.setattr(webui.WebApp, "_client", lambda self: _PartialClient({"available", "mine", "store"}))
    out = webui.WebApp().coupons("1420762")
    assert out["state"] == "error"
    assert all(s["state"] == "error" for s in out["sections"])


# ===========================================================================
# E. 积分账户（query-my-account）：只保留数值，accountId 绝不进模型/输出
# ===========================================================================


def test_adapt_points_parses_strings_and_drops_account_id():
    from burger_quest.mcp_base import adapt_points

    acc = adapt_points(
        {
            "availablePoint": "120",
            "accumulativePoint": "300",
            "usedPoint": "180",
            "nextMouthExpirePoint": "10",
            "currency": "麦享会积分",
            "accountId": "SECRET-ACCOUNT-ID",
        }
    )
    assert acc.available == 120 and acc.accumulative == 300 and acc.used == 180
    assert acc.currency == "麦享会积分"
    assert acc.unknown_fields == ()
    assert "SECRET" not in str(acc)  # 账户标识不得进入模型


def test_adapt_points_missing_or_bad_fields_are_none_not_zero():
    from burger_quest.mcp_base import adapt_points

    missing = adapt_points({"currency": "x"})
    assert missing.available is None and missing.accumulative is None
    assert "availablePoint" in missing.unknown_fields

    bad = adapt_points({"availablePoint": "abc", "accumulativePoint": None})
    assert bad.available is None and bad.accumulative is None


def test_webapp_account_ok_and_error(monkeypatch):
    class _AccClient:
        def __init__(self, fail=False):
            self.fail = fail

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def my_account(self):
            if self.fail:
                raise MCPError("query-my-account", "boom", code=500)
            from burger_quest.models import PointsAccount

            return PointsAccount(available=42, accumulative=100, used=58, expiring_next_month=0, currency="麦享会积分")

    monkeypatch.setattr(webui.WebApp, "_client", lambda self: _AccClient())
    ok = webui.WebApp().account()
    assert ok["state"] == "ok" and ok["available"] == 42
    assert "accountId" not in ok

    monkeypatch.setattr(webui.WebApp, "_client", lambda self: _AccClient(fail=True))
    err = webui.WebApp().account()
    assert err["state"] == "error"
