"""本地 Web 界面（2026-10-17 新增；Python 标准库实现，零第三方依赖）。

架构决策（评估结论）：
- 选型 **stdlib http.server**，不引入前端框架/Node 工具链——本项目引擎核心
  零依赖，界面只是 CLI 的第二个入口；维护成本最低，pip install 即可用；
- **只监听 localhost**（默认 127.0.0.1）：手机/平板如需访问，应连同一 Wi-Fi
  后用 `--allow-lan` 显式放行（默认拒绝，避免不必要的局域网暴露）；
- **Token 只存在于后端**：从环境变量/.env 读取，只出现在服务端 MCP 调用里；
  任何响应（HTML/JSON/Header/日志）都不含 Token 值——测试锁定；
- 所有 MCP 查询在服务端完成（复用 service.run_quest），浏览器只拿结果 JSON；
- 渲染安全：服务端文本一律 html.escape / JSON 编码；前端默认 textContent
  渲染，仅分享卡预览使用服务端已转义的卡片 HTML（cards.py 保证）；
- CSP：script-src 'self'（静态 app.js，无内联脚本）、style-src 'self'、
  frame-ancestors 'none'、base-uri 'none'、form-action 'self'。

状态机（对应用户可见的 7 种状态）：loading / success / no_result /
partial / permission / connection / rate_limited。
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from .mcp_base import MCPError
from .models import format_yuan

ASSETS_DIR = Path(__file__).resolve().parent / "web_assets"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
MAX_BODY_BYTES = 64 * 1024  # 请求体上限（防滥用）

# ---------------------------------------------------------------------------
# 状态映射（错误 -> 用户可读状态；文案不掩盖真实故障）
# ---------------------------------------------------------------------------
STATE_META = {
    "loading": {"icon": "⏳", "label": "加载中", "hint": "正在通过官方水晶球求证……"},
    "success": {"icon": "✅", "label": "成功", "hint": "已获得权威核验的结果"},
    "no_result": {"icon": "🫥", "label": "无结果", "hint": "查询成功但没有满足条件的结果，原因见详情"},
    "partial": {"icon": "🧩", "label": "部分数据", "hint": "部分信息缺失（已逐项标注，未编造）"},
    "permission": {"icon": "🔒", "label": "权限不足", "hint": "Token 无效或无权限（401/403）"},
    "connection": {"icon": "📡", "label": "连接失败", "hint": "网络不可达或服务端异常（5xx/超时）"},
    "rate_limited": {"icon": "⏳", "label": "服务限流", "hint": "触发 600 次/分钟限流（429），稍后重试"},
}


def error_state(e: Exception) -> str:
    """异常 -> 状态键（映射表单一事实来源）。"""
    if isinstance(e, MCPError):
        msg = str(e)
        if e.code == 429 or "429" in msg or "限流" in msg:
            return "rate_limited"
        if e.code in (401, 403) or "401" in msg or "403" in msg or "Token" in msg:
            return "permission"
        return "connection"
    return "connection"


def result_state(result) -> str:
    """QuestResult -> 状态键（成功/无结果/部分数据）。"""
    if result.chosen is None:
        return "no_result"
    partial_markers = ("未返回真实价格", "非正价格", "无营养数据", "未返回名称", "查询失败", "排除")
    if any(m in " ".join(result.warnings) for m in partial_markers):
        return "partial"
    if getattr(result, "chosen", None) is not None and result.chosen.nutrition_missing:
        return "partial"
    return "success"


# ---------------------------------------------------------------------------
# 结果序列化（只含前端需要的字段；绝不含 Token）
# ---------------------------------------------------------------------------


def serialize_result(result) -> dict:
    """QuestResult -> 可 JSON 化 dict（前端 textContent 渲染，天然防注入）。"""
    c = result.constraints
    out = {
        "state": result_state(result),
        "quest_name": result.quest_name,
        "story": result.story,
        "store": None,
        "store_candidates": [{"code": s.code, "name": s.name, "address": s.address} for s in result.store_candidates],
        "time_info": result.time_info,
        "constraints": c.describe(),
        "warnings": list(result.warnings),
        "data_sources": {k: (v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)) for k, v in result.data_sources.items()},
        "mcp_calls_summary": _calls_summary(result.mcp_calls),
        "price_states": dict(getattr(result, "price_states", {}) or {}),
        "options": [],
        "chosen_index": None,
        "progress_lines": [],
    }
    if result.store is not None:
        out["store"] = {"code": result.store.code, "name": result.store.name, "address": result.store.address}
    for i, o in enumerate(result.options):
        out["options"].append(
            {
                "title": o.title,
                "items": [
                    {
                        "name": pi.item.name,
                        "code": pi.item.code,
                        "qty": pi.qty,
                        "price": pi.item.price_text(),
                    }
                    for pi in o.items
                ],
                "original": format_yuan(o.original_cents),
                "discount": format_yuan(o.discount_cents) if o.discount_cents else None,
                "delivery_fee": format_yuan(o.delivery_fee_cents) if o.delivery_fee_cents else None,
                "total": format_yuan(o.total_cents),
                "total_cents": o.total_cents,
                "budget": format_yuan(c.budget_cents) if c.budget_cents is not None else None,
                "within_budget": o.within_budget,
                "remaining": format_yuan(o.remaining_cents) if o.remaining_cents is not None else None,
                "reasons": list(o.reasons),
                "unmet": list(o.unmet),
                "nutrition": o.nutrition or {},
                "nutrition_missing": list(o.nutrition_missing),
            }
        )
        if o is result.chosen:
            out["chosen_index"] = i
    # 游戏进度（本地虚拟进度，含虚拟声明）
    from .achievements import VIRTUAL_NOTICE

    out["progress_lines"] = [VIRTUAL_NOTICE]
    return out


def _calls_summary(calls: list) -> dict:
    real = [c for c in calls if isinstance(c, dict) and c.get("mcp_call", True)]
    by_tool: dict[str, int] = {}
    for c in real:
        by_tool[c.get("tool", "?")] = by_tool.get(c.get("tool", "?"), 0) + 1
    return {"total": len(real), "failed": sum(1 for c in real if c.get("ok") is False), "by_tool": by_tool}


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------


def _read_asset(name: str) -> bytes:
    return (ASSETS_DIR / name).read_bytes()


class _Handler(BaseHTTPRequestHandler):
    server_version = "BurgerQuest/1.0"
    # 不打印默认访问日志里的请求行（避免任何敏感信息进入日志；诊断走 diagnostics 模块）
    def log_message(self, fmt, *args):  # noqa: A003
        return

    # -- 工具 -----------------------------------------------------------------
    def _send_json(self, payload: dict, status: int = 200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._security_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_asset(self, name: str, content_type: str):
        try:
            body = _read_asset(name)
        except OSError:
            self._send_json({"error": "asset-missing"}, status=404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self._security_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _security_headers(self):
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")

    def _read_body(self) -> Optional[dict]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            self._send_json({"error": "payload-too-large"}, status=413)
            return None
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json({"error": "invalid-json"}, status=400)
            return None
        return data if isinstance(data, dict) else {}

    # -- 路由 -----------------------------------------------------------------
    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/":
            self._send_asset("index.html", "text/html; charset=utf-8")
        elif path == "/app.css":
            self._send_asset("app.css", "text/css; charset=utf-8")
        elif path == "/app.js":
            self._send_asset("app.js", "text/javascript; charset=utf-8")
        elif path == "/api/status":
            self._send_json(self.server.app.status())
        elif path == "/api/progress":
            self._send_json(self.server.app.progress_summary())
        elif path == "/api/healthz":
            self._send_json({"ok": True})
        else:
            self._send_json({"error": "not-found"}, status=404)

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        body = self._read_body()
        if body is None:
            return
        if path == "/api/quest":
            self._handle_quest(body)
        elif path == "/api/stores":
            self._handle_stores(body)
        elif path == "/api/card":
            self._handle_card(body)
        else:
            self._send_json({"error": "not-found"}, status=404)

    # -- 端点 -----------------------------------------------------------------
    def _handle_quest(self, body: dict):
        text = str(body.get("text") or "").strip()
        store_code = str(body.get("store_code") or "").strip() or None
        if not text:
            self._send_json({"state": "no_result", "error": "请输入需求文本"}, status=400)
            return
        if len(text) > 500:
            self._send_json({"state": "no_result", "error": "需求文本过长（≤500 字）"}, status=400)
            return
        try:
            result = self.server.app.run_quest(text, store_code)
        except MCPError as e:
            self._send_json({"state": error_state(e), "error": str(e), "state_meta": STATE_META[error_state(e)]})
            return
        payload = serialize_result(result)
        payload["progress_lines"] = self.server.app.progress_lines(result)
        self._send_json(payload)

    def _handle_stores(self, body: dict):
        text = str(body.get("query") or "").strip()
        if not text:
            self._send_json({"state": "no_result", "stores": [], "error": "请输入地址关键词"}, status=400)
            return
        # 复用 nlparser 的位置解析（与 CLI 同一套规则，不另造解析）
        from . import nlparser

        c = nlparser.parse(text)
        if not c.location:
            self._send_json(
                {
                    "state": "no_result",
                    "stores": [],
                    "error": "未能从输入中识别位置：请包含地址（如「在深圳市南山区科技园」）",
                }
            )
            return
        try:
            stores = self.server.app.nearby_stores(c.location)
        except MCPError as e:
            self._send_json({"state": error_state(e), "stores": [], "error": str(e), "state_meta": STATE_META[error_state(e)]})
            return
        self._send_json(
            {
                "state": "success" if stores else "no_result",
                "stores": [{"code": s.code, "name": s.name, "address": s.address} for s in stores],
                "hint": None if stores else "该位置未返回门店；可补充更具体地址（含城市/商圈）",
            }
        )

    def _handle_card(self, body: dict):
        """生成分享卡（服务端转义后的完整 HTML 文档；Content-Disposition 引导下载/新窗打开）。"""
        text = str(body.get("text") or "").strip()
        store_code = str(body.get("store_code") or "").strip() or None
        if not text:
            self._send_json({"error": "missing-text"}, status=400)
            return
        try:
            result = self.server.app.run_quest(text, store_code)
        except MCPError as e:
            self._send_json({"state": error_state(e), "error": str(e)})
            return
        if not result.card_html:
            self._send_json({"state": "no_result", "error": "未生成方案，无法生成分享卡"})
            return
        body_bytes = result.card_html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Disposition", 'inline; filename="burger-quest-card.html"')
        self._security_headers()
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, app):
        super().__init__(address, _Handler)
        self.app = app


class WebApp:
    """Web 界面应用对象（CLI/测试共用；持有 MCP 客户端工厂与可选进度）。"""

    def __init__(self, progress_path: Optional[str] = None):
        self._progress_path = progress_path
        self._lock = threading.Lock()

    # -- 客户端（每次查询独立会话，服务端管理生命周期） -----------------------
    def _client(self):
        from .mcp_http import HttpMCPClient

        return HttpMCPClient()

    def status(self) -> dict:
        """连接状态（只报存在性，绝不返回 Token 值；供前端状态灯）。"""
        from .doctor import run_doctor

        report = run_doctor()
        levels = []
        for level, name in (("L1", "服务可访问"), ("L2", "认证成功"), ("L3", "工具发现"), ("L4", "单工具调用"), ("L5", "业务流程")):
            steps = [s for s in report.steps if s.get("level") == level]
            if level == "L5":
                levels.append({"level": level, "name": name, "ok": None, "detail": "由任务查询实时验证"})
            elif not steps:
                levels.append({"level": level, "name": name, "ok": None, "detail": "未执行（前置步骤未通过）"})
            else:
                ok = all(s["ok"] for s in steps)
                detail = steps[0].get("detail") or ""
                levels.append({"level": level, "name": name, "ok": ok, "detail": detail})
        token_present = any(s["step"].startswith("Token") and s["ok"] for s in report.steps)
        return {
            "levels": levels,
            "token_present": token_present,
            "overall": "ok" if report.passed else ("config" if any(not s["ok"] for s in report.steps if "Token" in s["step"] or "SDK" in s["step"]) else "failed"),
        }

    def run_quest(self, text: str, store_code: Optional[str]):
        from .service import run_quest

        with self._client() as client:
            return run_quest(text, client, store_code=store_code)

    def nearby_stores(self, query: str):
        with self._client() as client:
            return client.nearby_stores(query)

    def progress_lines(self, result) -> list:
        """本地虚拟进度结算（opt-in；未启用时返回说明行）。"""
        from . import progress as progress_mod

        if not self._progress_path:
            return ["📴 未启用本地进度：启动时加 --progress <路径> 可记录成就与剧情（默认不写文件）"]
        try:
            prog, _notes = progress_mod.load_progress(self._progress_path)
            prog, _run, events = progress_mod.apply_progress(prog, result, demo=False)
            progress_mod.save_progress(self._progress_path, prog)
            return events or ["本次无新进度"]
        except OSError as e:
            return [f"进度文件不可用：{e}"]

    def progress_summary(self) -> dict:
        """进度查看（GET /api/progress）：未启用时只返回说明，不写文件。"""
        from . import progress as progress_mod
        from .achievements import ACHIEVEMENTS
        from . import lore

        if not self._progress_path:
            return {"enabled": False, "note": "未启用本地进度：启动时加 --progress <路径>"}
        prog, _notes = progress_mod.load_progress(self._progress_path)
        level, title = prog.level()
        unlocked = prog.unlocked_ids()
        chapters = [c.name for c in lore.unlocked_chapters(unlocked)]
        return {
            "enabled": True,
            "xp": prog.xp,
            "level": level,
            "title": title,
            "unlocked": len(unlocked),
            "total": len(ACHIEVEMENTS),
            "stores": len(prog.stores),
            "chapters": chapters,
            "note": "本地虚拟进度，与麦当劳官方无关",
        }


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, progress_path: Optional[str] = None, allow_lan: bool = False):
    """启动本地 Web 界面（阻塞）。默认只监听 127.0.0.1。"""
    if host not in ("127.0.0.1", "localhost", "::1") and not allow_lan:
        raise SystemExit(
            f"拒绝绑定 {host}：默认仅监听 localhost。确认风险后加 --allow-lan 显式放行"
            "（局域网内任何设备都可访问本服务）。"
        )
    app = WebApp(progress_path=progress_path)
    try:
        httpd = _Server((host, port), app)
    except OSError as e:
        raise SystemExit(f"无法监听 {host}:{port}（{e}）。可换端口：--port <其他端口>") from e
    print(f"🖥️  Burger Quest 本地界面已启动：http://{host}:{port}")
    print("    仅监听本机；Token 只从环境变量/.env 读取，不会发送到浏览器。")
    print("    停止：Ctrl+C（无后台任务，干净退出）。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0
