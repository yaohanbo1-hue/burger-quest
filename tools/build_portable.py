#!/usr/bin/env python3
"""构建 Windows 便携发布包（zip，解压即用，无需安装 Python）。

设计（2026-10-17，第 6 项「Windows 发布包」）：
- 目标用户：不懂技术的 Windows 用户——双击「启动游戏.bat」就能玩；
- 便携 = 自带 Python 运行时（独立发行版拷贝，剥离测试/编译部件），
  不依赖目标机器上的任何 Python；
- 只装运行时依赖（mcp SDK）；测试/质量工具（pytest/mypy/bandit 等）
  一律不进产物；
- Token 绝不打入产物：构建前对暂存区跑 CI 同款 secret scan，
  且显式检查 .env / mcp-config.json 不存在；
- 产物：dist/BurgerQuest-portable-win64-<version>.zip + 同名 .sha256
  + 校验发布包.bat（ certutil 比对）。

用法：
    python tools/build_portable.py            # 构建 + 暂存区自测 + 打包
    python tools/build_portable.py --no-zip   # 只构建暂存区（调试用）

构建后必须做「干净环境验证」（换一台路径解压运行），见脚本末尾注释。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DIST = REPO / "dist"
STAGING = DIST / "portable" / "BurgerQuest"
PYTHON_SRC = Path(os.environ.get("PORTABLE_PYTHON", r"C:\Users\hambu\.workbuddy\binaries\python\versions\3.13.12"))


def _version() -> str:
    sys.path.insert(0, str(REPO))
    from burger_quest import __version__

    return __version__


def log(msg: str) -> None:
    print(f"[portable] {msg}", flush=True)


# ---------------------------------------------------------------------------
# 1. 拷贝独立 Python 运行时（剥离不需要的部件）
# ---------------------------------------------------------------------------

# 拷贝时排除：__pycache__（可再生）、Lib\test（CPython 自测）、include/libs
# （编译 C 扩展才需要，运行时不碰）、Scripts（pip 生成的 exe 启动器带绝对
# 路径，换路径即坏；python -m pip 本身不依赖 Scripts）
_EXCLUDE_DIR_NAMES = {"__pycache__", "test", "testtools"}
_EXCLUDE_TOP = {"include", "libs", "Scripts", "tcl", "tkinter"}


def stage_python(dest: Path) -> None:
    if not PYTHON_SRC.exists():
        raise SystemExit(f"找不到独立 Python 发行版：{PYTHON_SRC}（可用 PORTABLE_PYTHON 环境变量指定）")
    log(f"拷贝 Python 运行时 {PYTHON_SRC} -> {dest}")
    for item in PYTHON_SRC.iterdir():
        if item.name in _EXCLUDE_TOP:
            continue
        if item.is_dir():
            shutil.copytree(item, dest / item.name, ignore=shutil.ignore_patterns(*_EXCLUDE_DIR_NAMES))
        else:
            shutil.copy2(item, dest / item.name)
    exe = dest / "python.exe"
    if not exe.exists():
        raise SystemExit("拷贝后的运行时缺少 python.exe")
    log(f"运行时就绪：{_dir_size_mb(dest):.1f} MB")


def _dir_size_mb(path: Path) -> float:
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return total / 1024 / 1024


# ---------------------------------------------------------------------------
# 2. 安装本项目 + 运行时依赖
# ---------------------------------------------------------------------------


def install_app(python_dir: Path) -> None:
    log("安装 burger-quest + 运行时依赖（mcp SDK，版本锁定）到内置 Python……")
    cmd = [
        str(python_dir / "python.exe"),
        "-m",
        "pip",
        "install",
        "--no-warn-script-location",
        "--disable-pip-version-check",
        "--constraint",
        str(REPO / "tools" / "portable-constraints.txt"),
        "mcp==2.3.0",  # 与 MCP_INTEGRATION.md 联调实测同版本；闭包由 constraints 锁死
        str(REPO),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        log("pip install 失败：")
        print(proc.stdout[-3000:])
        print(proc.stderr[-3000:])
        raise SystemExit(1)
    # pip 会在 Scripts\ 生成带绝对路径的 exe 启动器（burger-quest.exe），
    # 换路径即坏，且产物用 python -m burger_quest 入口，删掉
    scripts = python_dir / "Scripts"
    if scripts.exists():
        shutil.rmtree(scripts)
    slim_site_packages(python_dir / "Lib" / "site-packages")
    site = python_dir / "Lib" / "site-packages"
    if not (site / "burger_quest").is_dir():
        raise SystemExit("安装后未找到 burger_quest 包")
    log(f"应用安装完成，site-packages {_dir_size_mb(site):.1f} MB")


# mcp 2.3.0 把服务端/传输附属依赖也声明为硬依赖。本产物只走 Streamable HTTP
# 客户端。2026-10-17 实测确认的导入链（删了直接 ModuleNotFoundError，
# 由 verify_imports 硬验证兜底，绝不带伤出货）：
#   pywin32 家族（win32/ + pywin32_system32/ + pywin32.pth + pythoncom.py）——
#     pywin32.pth 执行 import pywin32_bootstrap（在 win32/ 里），由它把
#     pywin32_system32 挂上 sys.path，mcp.os.win32.utilities 才 import
#     得到 pywintypes。这一套互相咬合，必须整套保留。
#   cryptography / cffi / pycparser —— mcp.server.request_state 在链上
#   uvicorn / starlette / sse-starlette / jsonschema 等 —— 保持安装（体积可接受）
# 可安全删除：pip/setuptools/wheel（终端用户不装包）、pywin32 的 COM 服务端
# 扩展（win32com/win32comext/pythonwin/adodbapi/isapi 不在 pywintypes 链上，
# 由导入验证门确认）。
_SLIM_DIRS = (
    "pip",
    "setuptools",
    "wheel",
    "pkg_resources",
    "pythonwin",
    "win32com",
    "win32comext",
    "adodbapi",
    "isapi",
)


def slim_site_packages(site: Path) -> None:
    removed_mb = 0.0
    for name in _SLIM_DIRS:
        for target in (site / name, site / f"{name}.dist-info"):
            if target.exists():
                removed_mb += _dir_size_mb(target)
                shutil.rmtree(target)
    if removed_mb:
        log(f"瘦身：移除服务端/COM/pip 部件 {removed_mb:.1f} MB（导入链随后硬验证）")


def verify_imports(python_dir: Path) -> None:
    """瘦身后的硬验证：真实客户端导入链 + httpx 传输层 + 引擎全链路。"""
    log("导入链硬验证（瘦身后）……")
    code = (
        "from mcp import ClientSession;"
        "import httpx2, pydantic, anyio, jsonschema, jwt;"  # pyjwt 的导入名是 jwt
        "import burger_quest.mcp_http as mh;"
        "factory = mh._import_stream_client();"  # 生产环境的双版本工厂解析器
        "assert factory is not None;"
        "c = mh.HttpMCPClient(token='verify-not-real');"
        "kwargs = c._transport_kwargs(factory, {'Authorization': 'Bearer x'});"
        "assert kwargs, '传输层参数构造失败';"
        "print('IMPORT_CHAIN_OK')"
    )
    proc = subprocess.run(
        [str(python_dir / "python.exe"), "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=STAGING,
        timeout=120,
    )
    if proc.returncode != 0 or "IMPORT_CHAIN_OK" not in (proc.stdout or ""):
        print(proc.stdout[-2000:])
        print(proc.stderr[-2000:])
        raise SystemExit("瘦身后导入链验证失败——构建中止（绝不发布带伤产物）")
    log("导入链验证通过（ClientSession + streamablehttp_client + httpx2 + 引擎）")


# ---------------------------------------------------------------------------
# 3. 暂存区自测（不访问网络：demo 离线可跑）
# ---------------------------------------------------------------------------


def self_test(python_dir: Path) -> None:
    log("暂存区自测（demo 离线场景）……")
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    env.pop("MCD_MCP_TOKEN", None)  # 确保不借用开发机 Token
    proc = subprocess.run(
        [str(python_dir / "python.exe"), "-m", "burger_quest", "demo", "我想吃辣，预算35"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=STAGING,
        timeout=120,
    )
    if proc.returncode != 0:
        print(proc.stdout[-2000:])
        print(proc.stderr[-2000:])
        raise SystemExit("暂存区 demo 自测失败")
    if "麦辣" not in proc.stdout and "汉堡" not in proc.stdout:
        raise SystemExit("demo 输出异常（未见餐品）")
    log("自测通过：demo 正常出方案（未借用开发机 Token）")


# ---------------------------------------------------------------------------
# 4. Secret scan（CI 同款口径 + 显式文件检查）
# ---------------------------------------------------------------------------

_TOKEN_PAT = re.compile(r"Bearer\s+(?!\$\{)([A-Za-z0-9_\-]{16,})")
_TOKEN_WORDS = ("fake", "test", "example", "placeholder", "your", "xxx")
_FORBIDDEN_FILES = {".env", "mcp-config.json", "token.txt", ".env.local"}


def secret_scan(root: Path) -> None:
    log("产物 secret scan（CI 同款口径 + 禁用文件检查）……")
    hits = []
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix not in {".py", ".json", ".txt", ".bat", ".cfg", ".html", ".js", ".css"}:
            continue
        if any(x in p.parts for x in (".git", "__pycache__", "dist_check")):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in _TOKEN_PAT.finditer(text):
            v = m.group(1)
            if any(w in v.lower() for w in _TOKEN_WORDS):
                continue
            hits.append((str(p.relative_to(root)), v[:6] + "..."))
        for bad in _FORBIDDEN_FILES:
            if p.name == bad:
                hits.append((str(p.relative_to(root)), f"禁入文件 {bad}"))
    if hits:
        raise SystemExit(f"产物中发现疑似敏感内容：{hits}")
    log("secret scan 通过：无硬编码 Token、无 .env / mcp-config.json")


# ---------------------------------------------------------------------------
# 5. 写入启动器与说明
# ---------------------------------------------------------------------------

_LAUNCHER = """@echo off
chcp 65001 >nul
setlocal EnableExtensions
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d %~dp0

set "PY=python\\python.exe"
if not exist "%PY%" (
    echo [错误] 未找到内置 Python 运行时（%PY%）。
    echo 请确认整个 BurgerQuest 文件夹已完整解压。
    pause
    exit /b 1
)

rem 带参数调用 = 一次性执行（脚本/验收用）：启动游戏.bat 1
rem 不带参数 = 交互菜单（双击场景）
set "ONESHOT=%~1"
if defined ONESHOT (
    set "choice=%ONESHOT%"
    goto run
)

:menu
cls
echo ============================================================
echo    Burger Quest 麦门异世界 - 便携版
echo    （本文件夹自带 Python 运行时，无需安装任何软件）
echo ============================================================
echo.
echo    [1] 演示模式   无需 Token，立即体验完整流程
echo    [2] 真实模式   调用麦当劳官方 MCP（需 Token）
echo    [3] 网页界面   浏览器里玩（推荐新手）
echo    [4] 连接诊断   检查网络 / Token / 官方服务状态
echo    [5] 命令帮助   查看全部可用命令
echo    [0] 退出
echo.
set "choice="
set /p choice=请选择（0-5）:
if "%choice%"=="" goto menu

:run
if "%choice%"=="0" exit /b 0
if "%choice%"=="1" goto act1
if "%choice%"=="2" goto act2
if "%choice%"=="3" goto act3
if "%choice%"=="4" goto act4
if "%choice%"=="5" goto act5
echo 无效选择：%choice%
if defined ONESHOT exit /b 1
pause
goto menu

:act1
%PY% -m burger_quest demo "我想吃辣，预算35，外送到南山科技园，一个人"
goto after
:act2
%PY% -m burger_quest quest "我想吃辣，预算35"
goto after
:act3
%PY% -m burger_quest web
goto after
:act4
%PY% -m burger_quest doctor
goto after
:act5
%PY% -m burger_quest --help
goto after

:after
if defined ONESHOT exit /b 0
echo.
echo （按任意键返回菜单）
pause >nul
goto menu
"""

_README_TXT = """Burger Quest · 麦门异世界 —— Windows 便携版
==============================================

这是什么
--------
一个游戏化点餐规划工具：说一句话（预算/口味/人数/用餐方式），
它调用麦当劳中国官方 MCP Server 查询真实菜单与权威价格，
给出最多 3 个可比较的点餐方案，并生成一张可分享的成就卡。

本文件夹自带完整的 Python 运行时，**不需要安装 Python**，
**不需要联网下载任何东西**，解压即用。

怎么开始
--------
双击「启动游戏.bat」（或纯英文名的 START.bat，内容完全相同），按提示选：

  1  演示模式（推荐第一次用）：完全离线，用内置演示数据跑完整流程；
  2  真实模式：调用官方 MCP 拉取真实菜单/价格（需要 Token，见下）；
  3  网页界面：自动打开本地网页，浏览器里点按钮就能玩；
  4  连接诊断：排查网络/Token/官方服务是否正常；
  0  退出。

命令行用户也可以直接：

  python\\python.exe -m burger_quest demo "我想吃辣，预算35"
  python\\python.exe -m burger_quest --help

配置 Token（只用真实模式时需要）
--------------------------------
1. 到 https://open.mcd.cn/mcp 用手机号登录，控制台申请 MCP Token；
2. 任选一种方式提供给程序（二选一）：
   a. 在本文件夹新建文本文件 `.env`，内容一行：
         MCD_MCP_TOKEN=你的Token
   b. 或设置 Windows 用户环境变量 MCD_MCP_TOKEN（重启本 bat 生效）。
注意：
- Token 只保存在你自己电脑上，程序不会把它发送到任何其他地方
  （除了麦当劳官方 MCP 服务端本身），也不会写进任何输出文件；
- 没有 Token 时真实模式会明确失败并提示，**不会偷偷切换成演示数据**；
- 演示模式永远不需要 Token。

重要说明
--------
- 建议把文件夹解压到短路径（如 D:\\BurgerQuest），解压过程中请勿中断；
- 程序只「出方案」，不会替你下单；涉及真实资金的操作一概不碰；
- 餐品、价格、优惠券、营养数据均来自麦当劳官方 MCP 的实时返回；
  官方服务不可用时，程序会如实告知，不会编造数据；
- 网页界面默认只监听 127.0.0.1（仅本机可访问），Token 只在服务端，
  不会下发到浏览器；
- 这是非麦当劳官方项目的参赛作品，餐品信息以官方渠道实时结果为准。

文件清单
--------
  启动游戏.bat          双击开始（菜单入口）
  START.bat             同上，纯英文名——个别解压工具不认 UTF-8 文件名，
                        解压后中文名变乱码时，双击这个一样能玩
  使用说明.txt          本文件
  README.txt            同上，纯英文名
  python\\               内置 Python 运行时（请勿单独移动其中的文件）

版本与完整性
------------
随包发布方应提供发布包 zip 的 SHA-256 值与「校验发布包.bat」。
解压后如需自验，可对 zip 运行校验脚本核对哈希。
"""


def write_launcher_and_docs(staging: Path) -> None:
    # 中文名 + ASCII 兜底名双写：zip 条目已带 UTF-8 标志位（资源管理器/7-Zip
    # 能正确解压出中文名），但 Info-ZIP、PowerShell 5.1 Expand-Archive 等
    # 工具不认该标志、按本地代码页解出乱码名。目标用户「不懂技术」，不能赌
    # 他用什么解压——ASCII 名保证任何工具解出来都能双击启动。
    (staging / "启动游戏.bat").write_text(_LAUNCHER, encoding="utf-8")
    (staging / "START.bat").write_text(_LAUNCHER, encoding="utf-8")
    (staging / "使用说明.txt").write_text(_README_TXT, encoding="utf-8")
    (staging / "README.txt").write_text(_README_TXT, encoding="utf-8")
    log("已写入 启动游戏.bat / START.bat / 使用说明.txt / README.txt")


# ---------------------------------------------------------------------------
# 6. 打包 + 哈希 + 校验脚本
# ---------------------------------------------------------------------------

_VERIFY_BAT = """@echo off
chcp 65001 >nul
setlocal
rem 校验发布包 zip 的 SHA-256 是否与随附 .sha256 一致（certutil 系统自带）
rem 无参数时自动找同目录的 *portable*.zip；也可把 zip 拖到本脚本上（参数=%1）
set "ZIP=%~1"
if not defined ZIP for %%F in ("%~dp0*portable*.zip") do set "ZIP=%%F"
if not defined ZIP (
    echo [错误] 找不到发布包 zip（应与本脚本同目录，或把 zip 拖到本脚本上）。
    pause
    exit /b 1
)
if not exist "%ZIP%" (
    echo [错误] 文件不存在：%ZIP%
    pause
    exit /b 1
)
if not exist "%ZIP%.sha256" (
    echo [错误] 找不到哈希文件 %ZIP%.sha256
    pause
    exit /b 1
)
echo 正在计算 %ZIP% 的 SHA-256 ...
rem certutil 输出固定三行：标题 / 哈希 / 成功信息。skip=1 跳过标题，
rem if not defined 只取第一行哈希（忽略后面的 CertUtil 成功行——若不用
rem 这个判断，for 会遍历所有行、最后一行把哈希覆盖成 "CertUtil:"）。
rem 不引入 findstr 过滤：实测本环境 for /f 内嵌 findstr 管道取不到值。
set "GOT="
for /f "skip=1 tokens=1 delims= " %%H in ('certutil -hashfile "%ZIP%" SHA256') do if not defined GOT set "GOT=%%H"
set "WANT="
for /f "tokens=1" %%H in ('type "%ZIP%.sha256"') do if not defined WANT set "WANT=%%H"
echo.
echo  期望：%WANT%
echo  实际：%GOT%
echo.
if /i "%GOT%"=="%WANT%" (
    echo [通过] 发布包完整，未被篡改，可以解压使用。
) else (
    echo [失败] 哈希不一致！文件可能损坏或被改动，请重新下载。
)
pause
"""


def normalize_records(staging: Path) -> None:
    """让 RECORD 如实描述「实际随包发布」的文件，保证构建可复现。

    pip 写的 RECORD 含两类本产物并不发布的条目：
    - `__pycache__/*.pyc`——zip 已排除（字节码现场再生，见 package()）；
    - `../../Scripts/*.exe`——带绝对路径的 pip 启动器，构建时即被删除
      （换路径就坏，且入口用 python -m，不需要它们）。

    其中 Scripts 启动器行是**实测唯一剩余的不确定性来源**：distlib 生成的
    exe 每次构建字节不同，RECORD 里记的哈希随之不同 → zip SHA 必然不同。
    剔除后 RECORD 就是 shipped files 的真清单，重复构建字节一致。
    """
    site = staging / "python" / "Lib" / "site-packages"
    fixed = 0
    for record in sorted(site.glob("*.dist-info/RECORD")):
        lines = record.read_text(encoding="utf-8").splitlines()
        kept = [
            ln for ln in lines if "__pycache__/" not in ln and not ln.startswith("../../Scripts/")
        ]
        if len(kept) != len(lines):
            record.write_text("\n".join(kept) + "\n", encoding="utf-8")
            fixed += 1
    if fixed:
        log(f"规范化 {fixed} 个 RECORD（剔除未随包的 .pyc / Scripts 启动器条目，保证可复现）")


def package(staging: Path) -> tuple[Path, str]:
    version = _version()
    zip_path = DIST / f"BurgerQuest-portable-win64-v{version}.zip"
    if zip_path.exists():
        zip_path.unlink()
    log(f"打包 {zip_path.name}（确定性：固定时间戳，重复构建字节一致）…")
    # 固定时间戳 + 排序条目 + 固定权限位：同样的内容在任何时间构建出同一个 zip
    # （发布物的可复现性：别人 rebuild 出来的 SHA-256 应该和公布的一致）
    fixed_date = (2026, 10, 17, 12, 0, 0)
    # 先让 RECORD 与「实际发布内容」一致（剔除 .pyc / 已删的 Scripts 启动器行），
    # 否则 RECORD 内每次构建都不同的哈希会让 zip SHA 失确定性
    normalize_records(staging)
    # 排除 __pycache__：.pyc 头部内嵌源文件 mtime/size，pip 每次安装时刻不同
    # → 编译出的字节码字节不同 → zip SHA 不同（实测 3 次构建 3 个哈希，差异
    # 875/875 全是 .pyc）。字节码可再生，首次启动现场编译只慢约 1 秒，
    # 换来确定性 + 体积减半，值得。
    entries = sorted(
        p
        for p in staging.rglob("*")
        if p.is_file() and "__pycache__" not in p.relative_to(staging).parts
    )
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for p in entries:
            info = zipfile.ZipInfo(p.relative_to(staging.parent).as_posix(), date_time=fixed_date)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16  # 统一 -rw-r--r--，不带构建机 umask 差异
            if p.suffix in {".bat", ".exe"}:
                info.external_attr = 0o755 << 16
            zf.writestr(info, p.read_bytes())
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    (DIST / f"{zip_path.name}.sha256").write_text(f"{digest}  {zip_path.name}\n", encoding="utf-8")
    (DIST / "校验发布包.bat").write_text(_VERIFY_BAT, encoding="utf-8")
    # 自校验：zip 可读且 CRC 全对
    with zipfile.ZipFile(zip_path) as zf:
        bad = zf.testzip()
        assert bad is None, f"zip 内文件损坏：{bad}"
    log(f"打包完成：{zip_path.stat().st_size / 1024 / 1024:.1f} MB，SHA-256 {digest[:16]}…")
    return zip_path, digest


def main() -> int:
    version = _version()
    log(f"版本 v{version}，仓库 {REPO}")
    if STAGING.parent.exists():
        shutil.rmtree(STAGING.parent)
    STAGING.mkdir(parents=True)

    stage_python(STAGING / "python")
    install_app(STAGING / "python")
    verify_imports(STAGING / "python")
    self_test(STAGING / "python")
    secret_scan(STAGING)
    write_launcher_and_docs(STAGING)

    if "--no-zip" in sys.argv:
        log(f"--no-zip：暂存区保留在 {STAGING}")
        return 0

    zip_path, digest = package(STAGING)
    log("完成。产物：")
    log(f"  {zip_path}")
    log(f"  {DIST / (zip_path.name + '.sha256')}")
    log(f"  {DIST / '校验发布包.bat'}")
    log("下一步（干净环境验证）：把 zip 解压到**另一个路径**，运行 启动游戏.bat 选 1/3，")
    log("  并执行 python\\python.exe -m burger_quest demo \"预算5块，想吃汉堡\"（应诚实失败退出码 1）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
