"""脱敏诊断日志（可选开启，默认关闭，2026-10-17 稳定性增强新增）。

用途：真实 MCP 调用排障时，把详细诊断信息（工具、尝试次数、HTTP 状态、
错误类型、耗时、trace）写进日志文件，而**面向用户的错误提示保持简洁**。

开启方式（默认不写任何文件，不影响现有行为）：
    set BURGER_QUEST_LOG=burger-quest-diag.log   # Windows
    export BURGER_QUEST_LOG=burger-quest-diag.log

安全约定（硬约束）：
- **绝不明文记录 Token**：键名含 token/authorization/headers/cookie 的字段
  整体打码；消息中的 Bearer 凭据打码；MCD_MCP_TOKEN 的值若出现在任何字段
  中也会被替换为 ***（双保险，测试锁定）；
- 只记录排障必要的元数据（工具名/用途/尝试/状态/耗时），不记录请求参数
  与响应体（可能含个人信息）；
- 单行 JSON（JSONL），写失败静默忽略（日志不可用不影响主流程）。
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime

_LOCK = threading.Lock()
_ENV_VAR = "BURGER_QUEST_LOG"

# 键名命中即整体打码（不区分大小写）
_SENSITIVE_KEYS = ("token", "authorization", "headers", "cookie", "secret", "password")
# 消息里的 Bearer 凭据打码（兜底）
_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9_\-.]{8,}", re.IGNORECASE)


def _scrub_fields(fields: dict) -> dict:
    out = {}
    for key, value in fields.items():
        if any(s in key.lower() for s in _SENSITIVE_KEYS):
            out[key] = "***"
        else:
            out[key] = value
    return out


def _scrub_text(text: str) -> str:
    token = os.environ.get("MCD_MCP_TOKEN") or ""
    if token and len(token) >= 8:
        text = text.replace(token, "***")
    return _BEARER_RE.sub("Bearer ***", text)


def log_event(event: str, **fields) -> None:
    """写一条诊断日志（JSONL）。未配置 BURGER_QUEST_LOG 时为零操作。

    event：事件名（如 mcp_attempt / mcp_retry / mcp_retry_give_up）；
    fields：任意可 JSON 序列化的上下文字段（敏感键自动打码）。
    """
    path = os.environ.get(_ENV_VAR)
    if not path:
        return
    record = {
        "ts": datetime.now().isoformat(timespec="milliseconds"),
        "event": event,
        **_scrub_fields(fields),
    }
    try:
        line = json.dumps(record, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        line = json.dumps({"ts": record["ts"], "event": event, "note": "unserializable"}, ensure_ascii=False)
    line = _scrub_text(line)
    try:
        with _LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError:
        pass  # 日志不可用不影响主流程（测试/只读目录等场景）


def log_path() -> str:
    """当前诊断日志路径（空 = 未启用）。供文档/诊断命令展示。"""
    return os.environ.get(_ENV_VAR) or ""
