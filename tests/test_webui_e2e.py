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
