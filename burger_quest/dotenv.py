"""安全的 .env 加载器（零第三方依赖，2026-10-16 Windows 支持新增）。

设计约定（安全优先）：
- **只读**：本模块只解析已存在的 .env 文件，绝不创建/写入/修改任何文件；
- **不覆盖**：已存在于进程环境变量的变量优先，.env 只补缺（避免意外遮蔽
  用户显式设置的值，例如 CI 里注入的临时 Token）；
- **不打印**：加载器本身不输出任何值；调用方（doctor）也只报告「存在/缺失」；
- **有限搜索**：从当前目录向上最多 3 层找 .env，避免误读无关项目的配置；
- **宽容解析**：忽略注释/空行/畸形行；值可带单双引号（自动剥离）；
- 典型用途：Windows 用户把 MCD_MCP_TOKEN 放在仓库根的 .env（已被 .gitignore
  排除）中免每次设环境变量。**绝不要求把 Token 写进源码或提交到 Git。**
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

ENV_FILENAME = ".env"
MAX_UPLEVEL = 3  # 从 cwd 向上最多找 3 层


def _parse_env_text(text: str) -> dict[str, str]:
    """解析 .env 文本为 {KEY: VALUE}；畸形行忽略（不抛异常）。"""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):  # 兼容 `export KEY=VALUE` 写法
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key or " " in key:  # 键必须是非空且无空格
            continue
        # 剥离成对引号
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key] = value
    return out


def find_env_file(start: Optional[Path] = None) -> Optional[Path]:
    """从 start（默认 cwd）向上最多 MAX_UPLEVEL 层查找 .env；找不到返回 None。"""
    base = (start or Path.cwd()).resolve()
    for candidate in [base, *base.parents][: MAX_UPLEVEL + 1]:
        env_path = candidate / ENV_FILENAME
        if env_path.is_file():
            return env_path
    return None


def load_dotenv(start: Optional[Path] = None) -> dict[str, str]:
    """把 .env 中的变量补进 os.environ（不覆盖已有值）。

    返回实际新设置 {KEY: VALUE}（供诊断报告「从 .env 加载了 N 个变量」，
    值本身不进报告）。文件不存在或解析失败返回空 dict——.env 是可选项。
    """
    env_path = find_env_file(start)
    if env_path is None:
        return {}
    try:
        parsed = _parse_env_text(env_path.read_text(encoding="utf-8", errors="ignore"))
    except OSError:
        return {}
    applied: dict[str, str] = {}
    for key, value in parsed.items():
        if key in os.environ:  # 进程环境变量优先，不覆盖
            continue
        os.environ[key] = value
        applied[key] = value
    return applied
