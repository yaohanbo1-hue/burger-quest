"""Web 服务端到端测试（2026-10-17）。

与 test_security_audit.py 的分工：那组测**判定函数**（_host_allowed/
_origin_allowed 纯函数），本组测**接线与真实攻击面**——用真实
ThreadingHTTPServer + 真实 urllib 请求验证：

1. _guard 真的接进了 do_GET/do_POST（不是写了没调）；
2. 恶意 Host / 跨站 Origin / Origin:null 在真实线路上拿到 403，
   且**业务代码一次都没被触达**（app 桩记录调用）；
3. 同源请求能正常走完业务路径（含 MCPError 降级）；
4. 每个响应都带安全响应头（CSP/nosniff/no-referrer/no-store）；
5. 静态资源、404、payload 超限、非法 JSON 的真实行为。

设计要点：
- 端口用 0（ephemeral），避免与在跑的服务冲突；
- app 用桩（_StubApp），绝不碰真实 MCP；
- 每次请求后重置调用记录，断言「被拒时业务零调用」。
"""
import json
import threading
import urllib.error
import urllib.request

import pytest

from burger_quest import webui
from burger_quest.mcp_base import MCPError

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


class _StubApp:
    """记录调用的假 WebApp。run_quest 模拟真实失败路径（MCPError 降级）。"""

    def __init__(self):
        self.quest_calls: list[tuple[str, str | None]] = []
        self.stores_calls: list[str] = []
        self.status_calls = 0
        self.progress_calls = 0

    def status(self) -> dict:
        self.status_calls += 1
        return {"levels": [], "token_present": False, "overall": "ok"}

    def progress_summary(self) -> dict:
        self.progress_calls += 1
        return {"achievements": [], "story": {}}

    def run_quest(self, text: str, store_code):
        self.quest_calls.append((text, store_code))
        raise MCPError("mcd-mcp", "桩不执行真实查询", code=401)

    def nearby_stores(self, query: str):
        self.stores_calls.append(query)
        raise MCPError("mcd-mcp", "桩不执行真实查询", code=401)

    def progress_lines(self, result) -> list:
        return []


@pytest.fixture()
def srv():
    app = _StubApp()
    server = webui._Server(("127.0.0.1", 0), app)
    server.stub_app = app  # 测试专用引用：断言「被拒时业务零调用」
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _url(srv, path: str) -> str:
    host, port = srv.server_address[0], srv.server_address[1]
    return f"http://{host}:{port}{path}"


def _req(srv, path, method="GET", data=None, host=None, origin=None, raw_body=None):
    """发真实 HTTP 请求；返回 (status, headers_dict, body_bytes)。403 等不抛。"""
    headers = {}
    if host is not None:
        headers["Host"] = host
    if origin is not None:
        headers["Origin"] = origin
    body = raw_body
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(_url(srv, path), data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _port(srv) -> int:
    return srv.server_address[1]


# ===========================================================================
# E1. 静态资源与基础路由（真实线路）
# ===========================================================================


def test_e1_index_served(srv):
    status, headers, body = _req(srv, "/")
    assert status == 200
    assert "text/html" in headers.get("Content-Type", "")
    assert b"<html" in body.lower() or b"<!doctype" in body.lower()


def test_e1_js_and_css_served(srv):
    for path, ctype in (("/app.js", "javascript"), ("/app.css", "css")):
        status, headers, body = _req(srv, path)
        assert status == 200, f"{path} 未返回 200"
        assert ctype in headers.get("Content-Type", "")
        assert body, f"{path} 内容为空"


def test_e1_unknown_path_404(srv):
    status, _, body = _req(srv, "/nope")
    assert status == 404
    assert json.loads(body)["error"] == "not-found"


def test_e1_healthz(srv):
    status, _, body = _req(srv, "/api/healthz")
    assert status == 200
    assert json.loads(body) == {"ok": True}


# ===========================================================================
# E2. 安全响应头（每个响应都要带）
# ===========================================================================


@pytest.mark.parametrize("path", ["/", "/app.js", "/api/healthz", "/nope"])
def test_e2_security_headers_on_every_response(srv, path):
    _, headers, _ = _req(srv, path)
    csp = headers.get("Content-Security-Policy", "")
    assert "default-src 'none'" in csp, f"{path} 缺 CSP"
    assert "frame-ancestors 'none'" in csp, f"{path} CSP 未禁嵌套"
    assert headers.get("X-Content-Type-Options") == "nosniff"
    assert headers.get("Referrer-Policy") == "no-referrer"
    assert headers.get("Cache-Control") == "no-store"


# ===========================================================================
# E3. Host 白名单接线（DNS rebinding）
# ===========================================================================


def test_e3_evil_host_get_rejected_and_app_untouched(srv):
    """恶意 Host 的 GET：403 且业务零调用（证明 _guard 真的接进了 do_GET）。"""
    app = srv.stub_app
    status, _, body = _req(srv, "/api/status", host="evil.example.com")
    assert status == 403
    assert json.loads(body)["error"] == "access-denied"
    assert app.status_calls == 0, "恶意 Host 的请求竟触达了业务代码"


def test_e3_valid_host_get_reaches_app(srv):
    app = srv.stub_app
    status, _, body = _req(srv, "/api/status")
    assert status == 200
    assert app.status_calls == 1
    assert json.loads(body)["overall"] == "ok"


# ===========================================================================
# E4. Origin 同源接线（跨站代打 / opaque origin）
# ===========================================================================


def test_e4_cross_origin_post_rejected_and_app_untouched(srv):
    """跨站 POST：403 且 run_quest 零调用——攻击者借受害者浏览器打不动本服务。"""
    app = srv.stub_app
    status, _, body = _req(
        srv, "/api/quest", method="POST", data={"text": "辣堡"},
        origin="https://evil.example.com",
    )
    assert status == 403
    assert json.loads(body)["error"] == "access-denied"
    assert app.quest_calls == [], "跨站 POST 竟触达了业务代码"


def test_e4_opaque_origin_post_rejected(srv):
    """Origin: null（沙箱 iframe/data:/file:）POST：拒绝。

    Host 头只反映目标地址，攻击页面 fetch http://127.0.0.1:<port> 时
    Host 必然合法——null origin 是攻击者可嵌入的浏览器表面，必须挡。
    """
    app = srv.stub_app
    status, _, _ = _req(
        srv, "/api/quest", method="POST", data={"text": "辣堡"}, origin="null",
    )
    assert status == 403
    assert app.quest_calls == []


def test_e4_same_origin_post_reaches_app(srv):
    """同源 POST（无 Origin，模拟同源导航/脚本客户端）：放行并走完降级路径。"""
    app = srv.stub_app
    status, _, body = _req(srv, "/api/quest", method="POST", data={"text": "我想吃辣"})
    assert status == 200
    assert app.quest_calls == [("我想吃辣", None)], "同源 POST 未正确透传参数"
    payload = json.loads(body)
    assert payload["state"] != "no_result", "MCPError 未被转为明确失败态"


def test_e4_matching_origin_post_reaches_app(srv):
    """Origin 与 Host 一致（浏览器同源 fetch 会带）：放行。"""
    app = srv.stub_app
    status, _, _ = _req(
        srv, "/api/quest", method="POST", data={"text": "辣堡"},
        origin=f"http://127.0.0.1:{_port(srv)}",
    )
    assert status == 200
    assert app.quest_calls == [("辣堡", None)]


def test_e4_localhost_loopback_origin_allowed(srv):
    """用户手输 localhost、页面由 127.0.0.1 加载（或反之）：容忍互指。"""
    app = srv.stub_app
    status, _, _ = _req(
        srv, "/api/quest", method="POST", data={"text": "辣堡"}, origin="http://localhost",
    )
    assert status == 200
    assert app.quest_calls == [("辣堡", None)]


def test_e4_evil_host_post_rejected(srv):
    """恶意 Host + 同源 Origin：Host 防线先拦（DNS rebinding 场景）。"""
    app = srv.stub_app
    status, _, _ = _req(
        srv, "/api/quest", method="POST", data={"text": "辣堡"},
        host="evil.example.com", origin=f"http://127.0.0.1:{_port(srv)}",
    )
    assert status == 403
    assert app.quest_calls == []


# ===========================================================================
# E5. 业务路径细节（真实线路）
# ===========================================================================


def test_e5_quest_empty_text_400(srv):
    app = srv.stub_app
    status, _, body_ = _req(srv, "/api/quest", method="POST", data={"text": "   "})
    assert status == 400
    assert app.quest_calls == []


def test_e5_quest_overlong_text_400(srv):
    app = srv.stub_app
    status, _, _ = _req(srv, "/api/quest", method="POST", data={"text": "x" * 501})
    assert status == 400
    assert app.quest_calls == []


def test_e5_stores_missing_location_reports_clearly(srv):
    """输入无位置信息：明确失败，不透传异常、不猜位置。"""
    status, _, body = _req(srv, "/api/stores", method="POST", data={"query": "随便说说"})
    assert status == 200
    payload = json.loads(body)
    assert payload["state"] == "no_result"
    assert payload["stores"] == []
    assert "位置" in payload["error"]


def test_e5_card_without_result_is_explicit(srv):
    """run_quest 返回无 card_html 的结果：明确告知，不发空卡片。"""
    import types

    app = srv.stub_app
    app.run_quest = lambda text, store_code: types.SimpleNamespace(card_html="")
    status, _, body = _req(srv, "/api/card", method="POST", data={"text": "辣堡"})
    assert status == 200
    payload = json.loads(body)
    assert payload["state"] == "no_result"
    assert "分享卡" in payload["error"]


def test_e5_card_html_downloaded_with_safe_headers(srv):
    """有卡片时：完整 HTML 下载 + 安全头 + Content-Length 正确。"""
    import types

    app = srv.stub_app
    app.run_quest = lambda text, store_code: types.SimpleNamespace(
        card_html="<html><body>卡</body></html>"
    )
    status, headers, body = _req(srv, "/api/card", method="POST", data={"text": "辣堡"})
    assert status == 200
    assert "text/html" in headers.get("Content-Type", "")
    assert "burger-quest-card.html" in headers.get("Content-Disposition", "")
    assert headers.get("Content-Length") == str(len(body))
    assert b"<html>" in body
    assert "frame-ancestors 'none'" in headers.get("Content-Security-Policy", "")


def test_e5_invalid_json_400(srv):
    status, _, body = _req(srv, "/api/quest", method="POST", raw_body=b"{not json")
    assert status == 400
    assert json.loads(body)["error"] == "invalid-json"


def test_e5_oversized_payload_413(srv):
    status, _, body = _req(srv, "/api/quest", method="POST", raw_body=b"x" * (webui.MAX_BODY_BYTES + 10))
    assert status == 413
    assert json.loads(body)["error"] == "payload-too-large"


def test_e5_unknown_post_path_404(srv):
    status, _, body = _req(srv, "/api/unknown", method="POST", data={"x": 1})
    assert status == 404
    assert json.loads(body)["error"] == "not-found"


# ===========================================================================
# E6. 此前未覆盖区域清偿（技术债：webui.py 65% → 80%+）
#    /api/status、/api/progress 两个路由此前零请求；_host_header_name 的
#    IPv6/畸形分支、error_state/result_state 的映射分支无测试。
# ===========================================================================


def test_e6_status_endpoint_reaches_app_and_no_token_leak(srv):
    status, headers, body = _req(srv, "/api/status")
    assert status == 200
    payload = json.loads(body)
    assert payload["overall"] == "ok"
    assert srv.stub_app.status_calls == 1, "状态端点没调到 app"
    text = body.decode("utf-8")
    assert "Bearer" not in text and "MCD_MCP_TOKEN" not in text, "状态载荷疑似泄 Token"


def test_e6_progress_endpoint_reaches_app(srv):
    status, _, body = _req(srv, "/api/progress")
    assert status == 200
    assert json.loads(body)["achievements"] == []
    assert srv.stub_app.progress_calls == 1, "进度端点没调到 app"


@pytest.mark.parametrize(
    "host,expected",
    [
        ("", ""),
        ("127.0.0.1", "127.0.0.1"),
        ("127.0.0.1:8080", "127.0.0.1"),
        ("localhost:8787", "localhost"),
        ("[::1]:8080", "[::1]"),
        ("[::1]", "[::1]"),
        ("[bad", "[bad"),  # 括号不闭合：原样返回，不炸
    ],
)
def test_e6_host_header_name(host, expected):
    assert webui._host_header_name(host) == expected


def test_e6_error_state_mapping():
    assert webui.error_state(MCPError("m", "触发 600 次/分钟限流", code=429)) == "rate_limited"
    assert webui.error_state(MCPError("m", "429 Too Many Requests")) == "rate_limited"
    assert webui.error_state(MCPError("m", "Token 无效", code=401)) == "permission"
    assert webui.error_state(MCPError("m", "403 拒绝")) == "permission"
    assert webui.error_state(MCPError("m", "服务端爆炸", code=500)) == "connection"
    assert webui.error_state(ValueError("非 MCP 异常也归为连接类")) == "connection"


class _FakeChosen:
    nutrition_missing = False


class _FakeResult:
    def __init__(self, chosen, warnings=()):
        self.chosen = chosen
        self.warnings = list(warnings)


def test_e6_result_state_branches():
    assert webui.result_state(_FakeResult(None)) == "no_result"
    assert webui.result_state(_FakeResult(_FakeChosen(), ["未返回真实价格"])) == "partial"
    assert webui.result_state(_FakeResult(_FakeChosen(), ["门店查询失败"])) == "partial"

    class _NoNutrition:
        nutrition_missing = True

    assert webui.result_state(_FakeResult(_NoNutrition())) == "partial"
    assert webui.result_state(_FakeResult(_FakeChosen())) == "success"


class _FakeDoctorReport:
    def __init__(self, steps, passed):
        self.steps = steps
        self.passed = passed


def test_e6_webapp_status_token_missing_is_config(monkeypatch):
    """Token 缺失 → overall=config（配置问题）而非 failed，且绝不回吐 Token 值。"""
    from burger_quest import doctor as doctor_mod
    from burger_quest.webui import WebApp

    report = _FakeDoctorReport(
        steps=[
            {"level": "L1", "step": "服务可访问", "ok": True, "detail": "200 OK"},
            {"level": "L2", "step": "Token 配置", "ok": False, "detail": "缺少 MCD_MCP_TOKEN"},
        ],
        passed=False,
    )
    monkeypatch.setattr(doctor_mod, "run_doctor", lambda: report)
    st = WebApp().status()
    levels = {lv["level"]: lv for lv in st["levels"]}
    assert levels["L1"]["ok"] is True
    assert levels["L2"]["ok"] is False
    assert levels["L3"]["ok"] is None  # 前置未过：未执行
    assert levels["L5"]["ok"] is None and "实时验证" in levels["L5"]["detail"]
    assert st["overall"] == "config"
    assert st["token_present"] is False


def test_e6_webapp_status_all_green(monkeypatch):
    from burger_quest import doctor as doctor_mod
    from burger_quest.webui import WebApp

    report = _FakeDoctorReport(
        steps=[
            {"level": "L1", "step": "服务可访问", "ok": True, "detail": "200"},
            {"level": "L2", "step": "Token 配置", "ok": True, "detail": "已配置"},
            {"level": "L3", "step": "工具发现", "ok": True, "detail": "7 个工具"},
            {"level": "L4", "step": "单工具调用", "ok": True, "detail": "calculate-price 正常"},
        ],
        passed=True,
    )
    monkeypatch.setattr(doctor_mod, "run_doctor", lambda: report)
    st = WebApp().status()
    assert st["overall"] == "ok"
    assert st["token_present"] is True
    assert all(lv["ok"] for lv in st["levels"] if lv["level"] != "L5")


# ===========================================================================
# E7. serialize_result（/api/quest 响应序列化器）：真实管线 + 无 Token 泄漏
# ===========================================================================


def test_e7_serialize_result_json_safe_and_no_token_leak():
    """前端对响应做 JSON.stringify：任何非 JSON 类型都会让界面白屏；
    序列化器同时是「Token 不下发」承诺的执行点，逐字段钉死。"""
    from burger_quest.mcp_mock import MockMCPClient
    from burger_quest.service import run_quest

    result = run_quest("我想吃辣，预算35，外送到南山科技园，一个人", MockMCPClient())
    payload = webui.serialize_result(result)
    text = json.dumps(payload, ensure_ascii=False)  # 不可序列化会在这里炸
    assert "Bearer" not in text and "MCD_MCP_TOKEN" not in text, "响应疑似夹带 Token"
    assert payload["state"] in {"success", "partial", "no_result"}
    assert payload["options"], "演示流程应产出候选方案"
    assert payload["chosen_index"] is not None, "推荐项未标记"
    opt = payload["options"][payload["chosen_index"]]
    assert opt["total"].startswith("¥"), f"总价未按人民币格式：{opt['total']}"
    assert opt["items"] and opt["items"][0]["name"], "方案缺少餐品行"
    if payload["store"] is not None:
        assert {"code", "name", "address"} <= set(payload["store"])
    assert isinstance(payload["store_candidates"], list)
    # 游戏进度必须带虚拟声明（合规红线：虚拟进度不得伪装真实成就）
    assert any("虚拟" in ln for ln in payload["progress_lines"])


def test_e7_calls_summary_counts_real_calls_only():
    calls = [
        {"mcp_call": True, "tool": "query-meals", "ok": True},
        {"mcp_call": True, "tool": "query-meals", "ok": False},
        {"mcp_call": False, "tool": "mock-internal", "ok": True},  # 非真实调用不计入
        {"tool": "calculate-price", "ok": True},  # 缺 mcp_call 键视为真实调用
    ]
    s = webui._calls_summary(calls)
    assert s["total"] == 3
    assert s["failed"] == 1
    assert s["by_tool"] == {"query-meals": 2, "calculate-price": 1}


# ===========================================================================
# E8. WebApp 业务方法（本地进度落盘/失败）+ serve 的绑定安全守卫
# ===========================================================================


def _demo_result():
    from burger_quest.mcp_mock import MockMCPClient
    from burger_quest.service import run_quest

    return run_quest("我想吃辣，预算35", MockMCPClient())


def test_e8_progress_lines_without_path_is_explicit():
    lines = webui.WebApp(progress_path=None).progress_lines(_demo_result())
    assert any("未启用" in ln for ln in lines), "未配置进度时必须明说，不得静默"


def test_e8_progress_lines_records_to_disk(tmp_path):
    pf = tmp_path / "progress.json"
    app = webui.WebApp(progress_path=str(pf))
    lines = app.progress_lines(_demo_result())
    assert lines and lines[0]
    assert pf.exists(), "启用 --progress 后必须落盘"
    # 同一任务二次执行：防刷分分支（重复挑战不计经验）
    lines2 = app.progress_lines(_demo_result())
    assert lines2, "第二次执行不得静默失败"


def test_e8_progress_lines_unwritable_path_reports_clearly(tmp_path):
    # 父路径是普通文件：save_progress 的原子写在落临时文件时必炸
    # （NotADirectoryError ⊂ OSError）——用户把 --progress 指到坏路径的真实场景
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x", encoding="utf-8")
    app = webui.WebApp(progress_path=str(blocker / "progress.json"))
    lines = app.progress_lines(_demo_result())
    assert any("进度文件不可用" in ln for ln in lines), "IO 失败必须如实上报"


def test_e8_serve_refuses_non_localhost_without_allow_lan():
    with pytest.raises(SystemExit) as ei:
        webui.serve(host="0.0.0.0", port=0, allow_lan=False)
    assert ei.value.code != 0
    msg = str(ei.value)
    assert "localhost" in msg and "allow-lan" in msg, "守卫提示须说明风险与显式放行方式"


def test_e8_serve_reports_port_conflict():
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        with pytest.raises(SystemExit) as ei:
            webui.serve(host="127.0.0.1", port=port, allow_lan=False)
        assert "无法监听" in str(ei.value), "端口占用必须给出可操作提示"
    finally:
        s.close()
