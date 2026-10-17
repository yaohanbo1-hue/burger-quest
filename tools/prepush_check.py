#!/usr/bin/env python3
"""推送前本地预检：与 CI 逐字相同的检查口径。

背景（2026-10-17 踩坑，CI run 39039347513 红、本地全绿）：
CI 的 Whitespace check 跑 `git diff --check HEAD~`（检查**上一个提交**），
而本地提交前习惯跑 `git diff --check`（检查**未暂存改动**）——范围根本不同。
提交自带的行尾/空白问题，本地那条命令永远看不到，到了 CI 才炸。

本脚本把 CI 会跑的关键快检在本地按**同一命令**先跑一遍：

    git diff --check HEAD~    # 与 CI Whitespace check 逐字一致
    compileall                # 与 CI Syntax check 一致

再加两个 CI 没有但便宜的冒烟：
    最近提交的测试数 sanity   # 防「改了测试目录却没跑」
    git status 提示           # 防漏提交/漏推送

用法：
    python tools/prepush_check.py            # 快检（默认）
    python tools/prepush_check.py --full     # 追加全量 pytest（约 1 分钟）

退出码：0 = 全部通过；1 = 有失败项（细节见输出）。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _run(cmd: list[str], timeout: int = 120) -> tuple[int, str]:
    """跑命令，返回 (退出码, 合并输出)。stderr 合并进来——git 的提示走 stderr。"""
    try:
        proc = subprocess.run(
            cmd, cwd=REPO, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace"
        )
    except FileNotFoundError:
        return 127, f"命令不存在：{cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"命令超时（{timeout}s）：{' '.join(cmd)}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def check_whitespace() -> bool:
    """与 CI 的 Whitespace check 逐字一致：检查上一个提交的 diff。"""
    code, out = _run(["git", "diff", "--check", "HEAD~"])
    if code != 0:
        print("❌ 空白检查失败（git diff --check HEAD~，CI 同款命令）：")
        print(out.strip() or "(无细节输出)")
        print("   常见成因：行尾空格、文件末尾多余空行、缩进里的 Tab。")
        print("   修复后 amend 或补一个提交，再跑本脚本。")
        return False
    print("✅ 空白检查通过（git diff --check HEAD~，与 CI 同口径）")
    return True


def check_syntax() -> bool:
    """与 CI 的 Syntax check 一致：compileall 全目录。"""
    code, out = _run([sys.executable, "-m", "compileall", "-q", "burger_quest", "tests", "tools"])
    if code != 0:
        print("❌ 语法检查失败（compileall）：")
        print(out.strip()[:2000])
        return False
    print("✅ 语法检查通过（compileall burger_quest tests tools）")
    return True


def check_repo_state() -> bool:
    """仓库状态提示：漏提交/漏推送在 CI 上是查不出来的，但会让你以为推过了。"""
    code, out = _run(["git", "status", "--short", "--branch"])
    ok = True
    dirty = [ln for ln in out.splitlines() if ln and not ln.startswith("##")]
    ahead = "ahead" in out
    if dirty:
        print(f"⚠️ 工作区有 {len(dirty)} 个未提交改动（推送只包含已提交内容）：")
        for ln in dirty[:10]:
            print(f"   {ln}")
        ok = False
    if ahead:
        print("⚠️ 本地领先远端（尚未推送）：git push")
        ok = False
    if ok:
        print("✅ 仓库状态干净（无未提交改动，无未推送提交）")
    return ok


def check_tests(full: bool) -> bool:
    if not full:
        print("ℹ️ 跳过全量测试（--full 可启用；CI 会跑全量）")
        return True
    code, out = _run([sys.executable, "-m", "pytest", "-q"], timeout=300)
    tail = "\n".join(out.strip().splitlines()[-3:])
    if code != 0:
        print("❌ 全量测试失败：")
        print(tail)
        return False
    print(f"✅ 全量测试通过：{tail.strip().splitlines()[-1] if tail.strip() else ''}")
    return True


def main() -> int:
    full = "--full" in sys.argv[1:]
    print("== 推送前预检（CI 同口径）==")
    results = [
        check_whitespace(),
        check_syntax(),
        check_repo_state(),
        check_tests(full),
    ]
    print("==" + (" 全部通过，可以推送 " if all(results) else " 存在未通过项，勿推送 ").center(38, "=") + "==")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
