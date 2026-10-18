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
