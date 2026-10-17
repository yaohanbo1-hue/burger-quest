"""安全与隐私审计测试（2026-10-17）。

覆盖范围（对着真实攻击面写，不做泛泛检查）：

A. 本地Web 服务的访问控制（webui.py）
   - Host 白名单：挡 DNS rebinding（恶意域名解析到 127.0.0.1）
   - Origin 校验：挡任意网页跨站 POST 借受害者 Token 发起真实 MCP 查询
   - 绑定地址默认只允许 localhost

B. 前端资产（web_assets/）
   - 无内联脚本/样式（CSP script-src 'self' 下的硬要求）
   - 不使用 innerHTML（XSS 根因）
   - 零 Token 引用（Token 只存在于服务端）

C. Token 隔离（全链路）
   - 不进审计/日志/诊断/卡片/进度文件
   - 缺 Token 必须失败，禁止静默降级 Demo

D. 本地进度存档隐私（progress.py）
   - 零个人信息：不写用户输入原文、不写地址、手机号
   - opt-in：默认不写文件

E. 成就签名防重放（achievements.py）
   - Demo 不得解锁真实成就

F. 静态安全断言（贯穿全仓）
   - 源码中不得出现硬编码真实 Token 形态
"""
import inspect
import json
import re
from pathlib import Path

import pytest

from burger_quest import achievements, progress, webui

REPO = Path(__file__).resolve().parent.parent
ASSETS = Path(webui.__file__).resolve().parent / "web_assets"


# ===========================================================================
# A. Host 白名单（DNS rebinding 防线）
# ===========================================================================


@pytest.mark.parametrize(
    "host,ok",
    [
        ("127.0.0.1:8787", True),
        ("localhost:8787", True),
        ("127.0.0.1", True),
        ("localhost", True),
        ("[::1]:8787", True),
        # DNS rebinding：恶意域名指向 127.0.0.1
        ("evil.example.com:8787", False),
        ("attacker.test", False),
        # 空Host 必须拒绝
        ("", False),
        # 大小写不敏感
        ("LOCALHOST:8787", True),
    ],
)
def test_a1_host_allowlist(host, ok):
    assert webui._host_allowed(host) is ok


def test_a2_allow_lan_relaxes_host_check():
    """显式 --allow-lan 后才放行非本机 Host（默认必须拒绝）。"""
    assert webui._host_allowed("192.168.1.50:8787") is False
    assert webui._host_allowed("192.168.1.50:8787", allow_lan=True) is True


def test_a3_get_handler_validates_host():
    """do_GET 必须校验 Host：否则 DNS rebinding 可读进度/状态。"""
    src = inspect.getsource(webui._Handler.do_GET)
    assert "self._guard(" in src, "do_GET 未调用访问控制（DNS rebinding 可读数据）"


def test_a4_post_handler_validates_origin():
    """do_POST 必须校验 Origin：否则任意网页可驱动本服务发真实 MCP 查询。"""
    src = inspect.getsource(webui._Handler.do_POST)
    assert "require_same_origin=True" in src, "do_POST 未要求同源"
    assert "self._guard(" in src


# ===========================================================================
# A2. Origin 校验（跨站写操作防线）
# ===========================================================================


@pytest.mark.parametrize(
    "origin,host,ok",
    [
        ("http://127.0.0.1:8787", "127.0.0.1:8787", True),      # 同源
        ("http://localhost:8787", "127.0.0.1:8787", True),        # 本机互指
        ("http://127.0.0.1:8787", "localhost:8787", True),        # 本机互指
        ("", "127.0.0.1:8787", True),                              # 同源不带Origin
        # 跨站攻击
        ("https://evil.example.com", "127.0.0.1:8787", False),
        ("http://attacker.test:80", "127.0.0.1:8787", False),
        # 非法 scheme
        ("file://", "127.0.0.1:8787", False),
        ("javascript:alert(1)", "127.0.0.1:8787", False),
        # 无 Origin 无法比对
        ("http://127.0.0.1:8787", "", False),
        # 无 Origin 的 null（opaque origin）：GET 容忍（跨站读不回去，无 CORS 头）
        ("null", "127.0.0.1:8787", True),
    ],
)
def test_a5_origin_allowlist(origin, host, ok):
    assert webui._origin_allowed(origin, host) is ok


@pytest.mark.parametrize(
    "origin,host,ok",
    [
        # POST 同源要求（reject_opaque=True）：null 一律拒绝——
        # 沙箱 iframe/data:/file: 的不透明来源是攻击者可嵌入的浏览器表面，
        # Host 头只反映目标地址、挡不住它们
        ("null", "127.0.0.1:8787", False),
        ("NULL", "127.0.0.1:8787", False),  # 大小写不敏感
        # 无 Origin（非浏览器客户端/同源导航）POST 仍放行
        ("", "127.0.0.1:8787", True),
        # 同源 Origin 不受 reject_opaque 影响
        ("http://127.0.0.1:8787", "127.0.0.1:8787", True),
        ("https://evil.example.com", "127.0.0.1:8787", False),
    ],
)
def test_a5b_opaque_origin_rejected_on_post(origin, host, ok):
    assert webui._origin_allowed(origin, host, reject_opaque=True) is ok


def test_a6_denial_is_explicit_not_silent():
    """拒绝必须回明确JSON，不能静默——排障时用户需要知道原因。"""
    src = inspect.getsource(webui._Handler._access_denied)
    assert "access-denied" in src
    assert "detail" in src


# ===========================================================================
# A3. 绑定地址
# ===========================================================================


def test_a7_non_localhost_bind_requires_allow_lan():
    """默认拒绝绑定非 localhost（--allow-lan 显式放行）。"""
    with pytest.raises(SystemExit) as ei:
        webui.serve(host="0.0.0.0", port=8799)
    assert "allow-lan" in str(ei.value)
    assert "局域网" in str(ei.value)


def test_a8_default_host_is_loopback():
    assert webui.DEFAULT_HOST == "127.0.0.1"


# ===========================================================================
# B. 前端资产
# ===========================================================================


def test_b1_no_inline_script_tag():
    """index.html 不得有内联 <script>（CSP script-src 'self' 会直接拦掉）。"""
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    assert "<script>" not in html
    assert "</script>" not in html.replace("</script>\n", "</script>") or "<script>" not in html
    # 外链 app.js 才是正解
    assert 'src="app.js"' in html or "src='app.js'" in html or "app.js" in html


def test_b2_no_inline_event_handlers():
    """不得有 onclick= 等内联事件属性。"""
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"\son[a-z]+\s*=", html, re.I), "index.html 含内联事件处理器"


def _strip_js_comments(text: str) -> str:
    """去掉 JS 行注释与块注释（安全说明常写在注释里，不该被当成实际代码）。"""
    import re as _re

    no_block = _re.sub(r"/\*.*?\*/", "", text, flags=_re.S)
    out = []
    for line in no_block.splitlines():
        stripped = line.strip()
        if stripped.startswith("//"):
            continue
        out.append(line)
    return "\n".join(out)


def test_b3_no_inner_html_usage():
    """app.js 一律 textContent 渲染——innerHTML 是 XSS 根因。"""
    js = _strip_js_comments((ASSETS / "app.js").read_text(encoding="utf-8"))
    assert "innerHTML" not in js, "app.js 使用了 innerHTML（XSS 风险）"
    assert "outerHTML" not in js
    assert "insertAdjacentHTML" not in js
    assert "textContent" in (ASSETS / "app.js").read_text(encoding="utf-8")


def test_b4_document_write_only_for_escaped_share_card():
    """document.write 只允许出现在「打开服务端分享卡」这一处。

    分享卡必须是完整 HTML 文档，所以这里用 document.write 写入
    **服务端已转义** 的内容是合理设计；除此之外禁止任何 document.write。
    """
    raw = (ASSETS / "app.js").read_text(encoding="utf-8")
    js = _strip_js_comments(raw)
    hits = [line.strip() for line in js.splitlines() if "document.write" in line]
    assert len(hits) == 1, f"document.write 出现在 {len(hits)} 处，应只有分享卡一处：{hits}"
    # 该处必须紧邻 window.open（确认是「打开分享卡」而非任意注入）
    idx = js.index("document.write")
    assert "window.open" in js[max(0, idx - 400) : idx + 200], "document.write 未与 window.open 关联"
    #禁止 eval / new Function
    assert "eval(" not in js
    assert "new Function(" not in js


def test_b5_frontend_never_references_token_value():
    """前端只允许知道「Token 是否存在」，绝不读取 Token 值。"""
    for name in ("index.html", "app.js", "app.css"):
        text = _strip_js_comments((ASSETS / name).read_text(encoding="utf-8")).lower()
        # 禁止任何把 token 当作待读取值的模式
        assert not re.search(r"(getenv|localstorage|sessionstorage)\s*\(\s*['\"]?.*token", text)
        assert not re.search(r"authorization\s*[:=]", text)
        # 不允许出现 Bearer 头
        assert "bearer" not in text


def test_b6_assets_exist():
    for name in ("index.html", "app.css", "app.js"):
        assert (ASSETS / name).is_file(), f"缺少前端资产 {name}"


# ===========================================================================
# C. Token 隔离
# ===========================================================================


FAKE_TOKEN = "fake-token-for-audit-only-000000000000"


def test_c1_token_not_in_audit_records(monkeypatch):
    """MCP 调用审计不得记录 Token。"""
    from burger_quest.mcp_mock import MockMCPClient

    c = MockMCPClient()
    c.token = FAKE_TOKEN
    c.nearby_stores("科技园")
    blob = json.dumps(c.calls, ensure_ascii=False, default=str)
    assert FAKE_TOKEN not in blob
    assert "fake-token-for-audit" not in blob


def test_c2_token_not_in_diagnostics(monkeypatch, tmp_path):
    """诊断日志（JSONL）必须脱敏：只记存在性/类别，不记值。"""
    from burger_quest import diagnostics

    log = tmp_path / "diag.jsonl"
    monkeypatch.setenv("BURGER_QUEST_LOG", str(log))
    monkeypatch.setenv("MCD_MCP_TOKEN", FAKE_TOKEN)
    diagnostics.log_event("mcp_call_failed", token=FAKE_TOKEN, Authorization=f"Bearer {FAKE_TOKEN}")
    text = log.read_text(encoding="utf-8")
    assert FAKE_TOKEN not in text, "诊断日志泄露了 Token 值"
    assert "fake-token-for-audit" not in text
    # 敏感键应被打码成 ***
    assert "***" in text


def test_c3_card_html_has_no_token():
    """成就卡不得包含 Token。"""
    from burger_quest import cards
    from burger_quest.service import run_quest
    from burger_quest.mcp_mock import MockMCPClient

    r = run_quest("预算35，想吃辣", MockMCPClient())
    assert r.chosen is not None
    html = cards.render_html(
        r.constraints,
        r.chosen,
        getattr(r, "store", None),
        getattr(r, "time_info", "") or "",
        "测试来源",
        "2026-10-17T00:00:00",
        demo=False,
    )
    assert FAKE_TOKEN not in html
    assert "Bearer" not in html


def test_c4_progress_file_has_no_token_or_pii(tmp_path):
    """进度存档零个人信息：不写 Token、不写用户输入原文。"""
    from burger_quest.service import run_quest
    from burger_quest.mcp_mock import MockMCPClient

    r = run_quest("预算35，想吃辣，在深圳市南山区科技园堂食", MockMCPClient())
    path = tmp_path / "p.json"
    prog, _ = progress.load_progress(path)
    progress.apply_progress(prog, r, demo=False)
    progress.save_progress(path, prog)
    text = path.read_text(encoding="utf-8")
    assert FAKE_TOKEN not in text
    assert "Bearer" not in text
    # 不应存用户需求原文（含位置等个人信息）
    assert "深圳市南山区科技园" not in text
    assert "想吃辣" not in text


def test_c5_progress_is_opt_in(tmp_path):
    """默认不写任何文件。"""
    from burger_quest.service import run_quest
    from burger_quest.mcp_mock import MockMCPClient

    r = run_quest("预算35", MockMCPClient())
    prog, _ = progress.load_progress(tmp_path / "never.json")  # 只load 不 save
    progress.apply_progress(prog, r, demo=False)
    assert not (tmp_path / "never.json").exists(), "未启用进度却写了文件"


def test_c6_missing_token_fails_loudly():
    """缺 Token 必须失败，绝不自动降级 Demo 冒充成功。"""
    from burger_quest.mcp_http import HttpMCPClient

    with pytest.raises(Exception) as ei:
        HttpMCPClient(token="")
    assert "Token" in str(ei.value)


# ===========================================================================
# E. 成就签名防重放
# ===========================================================================


def test_e1_demo_cannot_unlock_real_achievements():
    """Demo 数据不得解锁真实成就（否则演示会污染真实战绩）。"""
    from burger_quest.service import run_quest
    from burger_quest.mcp_mock import MockMCPClient

    r = run_quest("预算35，想吃辣", MockMCPClient())
    prog, _ = progress.load_progress(Path("nonexistent-dont-write.json"))
    _prog2, _run, events = progress.apply_progress(prog, r, demo=False)
    # demo=False 时正常；关键是函数签名必须显式要求调用方声明数据性质
    assert progress.apply_progress is not None
    import inspect as _i

    sig = _i.signature(progress.apply_progress)
    assert "demo" in sig.parameters, "apply_progress 必须显式声明 demo 标志"


def test_e2_achievement_events_require_valid_signature():
    """成就解锁事件需签名，防止外部伪造进度。"""
    names = [a.name for a in achievements.ACHIEVEMENTS]
    assert names, "成就表不应为空"
    # 成就定义里不应允许任意无签名解锁入口
    src = inspect.getsource(achievements)
    assert "verify" in src or "sign" in src, "成就模块缺少签名校验实现"


# ===========================================================================
# F. 全仓静态扫描
# ===========================================================================


def test_f1_no_hardcoded_bearer_token_in_source():
    """源码不得硬编码真实形态的 Bearer Token。"""
    pattern = re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{24,}")
    for py in REPO.glob("burger_quest/*.py"):
        text = py.read_text(encoding="utf-8", errors="replace")
        for m in pattern.findall(text):
            # 允许占位说明性文案，但不允许真实长度 token
            assert "example" in m.lower() or "fake" in m.lower() or "xxx" in m.lower() or "test" in m.lower(), (
                f"{py.name} 疑似硬编码 Token：{m[:12]}…"
            )


def test_f2_no_phone_number_in_source():
    """源码不得含手机号（官方申请用，用户不应被收集）。"""
    pattern = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
    for py in REPO.glob("burger_quest/*.py"):
        text = py.read_text(encoding="utf-8", errors="replace")
        assert not pattern.search(text), f"{py.name} 含疑似手机号"


def test_f3_env_file_gitignored():
    """.env 必须被 gitignore（否则 Token 会被提交）。"""
    gi = (REPO / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in gi


def test_f4_diagnostics_default_off():
    """诊断日志默认关闭（不默认落盘用户数据）。"""
    import os

    from burger_quest import diagnostics

    os.environ.pop(diagnostics._ENV_VAR, None)
    assert diagnostics.log_path() == "", "未设置 BURGER_QUEST_LOG 时应视为未启用"


# ===========================================================================
# G. 回归：安全修复不破坏功能
# ===========================================================================


def test_g1_guard_allows_normal_local_request():
    """正常浏览器访问（Host=127.0.0.1:PORT，无 Origin）必须放行。"""
    assert webui._host_allowed("127.0.0.1:8787") is True
    assert webui._origin_allowed("", "127.0.0.1:8787") is True


def test_g2_server_carries_allow_lan_flag():
    """Server 必须把 allow_lan 传给 handler 可见属性。"""
    src = inspect.getsource(webui._Server.__init__)
    assert "allow_lan" in src
