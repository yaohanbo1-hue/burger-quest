"""干净环境验证：对解压到异地的便携包跑真实使用路径。

用法：python tools/verify_portable.py D:\\portable_verify\\BurgerQuest
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else r"D:\portable_verify\BurgerQuest")
PY = ROOT / "python" / "python.exe"
BAT = ROOT / "启动游戏.bat"

results = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""), flush=True)


def run(args, timeout=180, cwd=ROOT):
    env_extra = {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    import os

    env = dict(os.environ, **env_extra)
    env.pop("MCD_MCP_TOKEN", None)
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, cwd=cwd, env=env)


# 0. 结构完整
check("启动游戏.bat 存在", BAT.exists())
check("START.bat（ASCII 兜底名）存在", (ROOT / "START.bat").exists())
check("使用说明.txt 存在", (ROOT / "使用说明.txt").exists())
check("README.txt（ASCII 兜底名）存在", (ROOT / "README.txt").exists())
check("内置 python.exe 存在", PY.exists())

# 1. 版本（异地路径的 Python 能跑，证明真便携）
p = run([str(PY), "-m", "burger_quest", "--version"])
check("--version 退出码 0", p.returncode == 0, p.stdout.strip().splitlines()[-1] if p.stdout.strip() else p.stderr[-200:])

# 2. demo 正常场景（应出方案）
p = run([str(PY), "-m", "burger_quest", "demo", "我想吃辣，预算35，外送到南山科技园，一个人"])
out = p.stdout or ""
check("demo 正常场景退出码 0", p.returncode == 0)
check("demo 输出含餐品与总价", ("麦辣" in out or "汉堡" in out) and ("¥" in out or "总价" in out))

# 2b. 真实模式导入链（mcp SDK/httpx/pydantic 在位；不联网，只验证可导入）
p = run([str(PY), "-c", "import burger_quest.mcp_http as m; print('mcp', m.sdk_version() if hasattr(m,'sdk_version') else 'ok')"])
check("真实模式依赖可导入（mcp SDK 链）", p.returncode == 0 and "ok" in (p.stdout or "") or p.returncode == 0, (p.stdout or p.stderr).strip()[-120:])

# 2c. tools 子命令（官方工具清单）
p = run([str(PY), "-m", "burger_quest", "tools"])
check("tools 列出 7 个官方工具", p.returncode == 0 and p.stdout.count("- ") >= 7)

# 3. demo 诚实失败（预算不足应退出码 1，不编造）
p = run([str(PY), "-m", "burger_quest", "demo", "预算5块，想吃汉堡"])
check("预算不足诚实失败退出码 1", p.returncode == 1, f"rc={p.returncode}")

# 4. 真实模式缺 Token：退出码 2 且不回退 demo
p = run([str(PY), "-m", "burger_quest", "quest", "我想吃辣，预算35"])
check("缺 Token 退出码 2", p.returncode == 2, f"rc={p.returncode}")
check("缺 Token 提示不含演示结果", "DEMO-001" not in (p.stdout or ""))

# 5. 菜单 bat（传参跳过交互，模拟双击选 1）；中文名与 ASCII 兜底名都要能跑
p = subprocess.run(["cmd", "/c", str(BAT), "1"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180, cwd=ROOT)
check("启动游戏.bat 选 1 可跑完", p.returncode == 0 and "麦门" in (p.stdout or ""))
p = subprocess.run(["cmd", "/c", str(ROOT / "START.bat"), "1"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180, cwd=ROOT)
check("START.bat 选 1 可跑完（乱码名场景兜底）", p.returncode == 0 and "麦门" in (p.stdout or ""))

# 6. 网页模式：启动 -> healthz -> 关闭
proc = subprocess.Popen(
    [str(PY), "-m", "burger_quest", "web", "--port", "8791"],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", cwd=ROOT,
)
try:
    ok = False
    for _ in range(40):
        time.sleep(0.5)
        try:
            with urllib.request.urlopen("http://127.0.0.1:8791/api/healthz", timeout=2) as r:
                ok = r.status == 200 and json.loads(r.read())["ok"] is True
                break
        except Exception:
            continue
    check("web 模式 healthz 200", ok)
    # 安全头 spot check
    with urllib.request.urlopen("http://127.0.0.1:8791/api/healthz", timeout=2) as r:
        csp = r.headers.get("Content-Security-Policy", "")
        check("web 响应带 CSP", "default-src 'none'" in csp)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()

# 7. 敏感文件与 Token 检查
bad_files = [p.name for p in ROOT.rglob("*") if p.name in {".env", "mcp-config.json", "token.txt"}]
check("无 .env / mcp-config.json / token.txt", not bad_files, str(bad_files))

import re

pat = re.compile(r"Bearer\s+(?!\$\{)([A-Za-z0-9_\-]{16,})")
hits = []
for p in ROOT.rglob("*"):
    if p.suffix not in {".py", ".json", ".txt", ".bat", ".html"} or not p.is_file():
        continue
    try:
        text = p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        continue
    for m in pat.finditer(text):
        v = m.group(1)
        if any(w in v.lower() for w in ("fake", "test", "example", "placeholder", "your", "xxx")):
            continue
        hits.append((str(p.relative_to(ROOT)), v[:6]))
check("无硬编码真实 Token 形态", not hits, str(hits[:3]))

print()
failed = [r for r in results if not r[1]]
print(f"== {len(results) - len(failed)}/{len(results)} 通过 ==")
if failed:
    print("失败项：", [r[0] for r in failed])
    sys.exit(1)
print("干净环境验证全部通过：解压即用、离线 demo、缺 Token 明确失败、web 模式正常、无敏感内容。")
