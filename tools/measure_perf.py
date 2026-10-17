#!/usr/bin/env python3
"""性能基线测量：启动 / 端到端任务 / 解析 / 渲染 / 测试套件。

用法：
    python tools/measure_perf.py                 # 全量测量并打印报告
    python tools/measure_perf.py --json out.json # 结果另存 JSON
    python tools/measure_perf.py --skip-tests    # 跳过最慢的测试套件测量

测量口径（2026-10-17 建立，第 7 项「性能与技术债治理」）：
- 冷启动：子进程 `python -m burger_quest --version`，5 次取中位数
  （导入全部引擎模块的真实成本，非 pyinstaller 式假启动）；
- demo 端到端：子进程跑完整任务，墙钟时间 + 子进程峰值工作集
  （psapi GetProcessMemoryInfo 轮询采样，OS 维护的 PeakWorkingSetSize，
  无需第三方库；采样间隔 20ms）；
- nlparser 微基准：进程内解析 20 条典型语句，报中位数与 p95
  （纯 CPU，任何回归都会立刻显形）；
- 成就卡渲染：直接测 cards.render_html（service.run_quest 内部调用它，
  单独测量可把「渲染成本」与「引擎成本」分开归因）；
- 测试套件：pytest 墙钟时间 + 从输出解析真实用例数（不硬编码）。

所有测量只读：不碰网络、不碰真实 Token、不修改项目行为。
"""
from __future__ import annotations

import argparse
import ctypes
import json
import re
import statistics
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = sys.executable

# 20 条覆盖各类约束的典型输入（与 tests/test_nlparser.py 的场景同源，不另造）
SAMPLE_TEXTS = [
    "我想吃辣，预算35，外送到南山科技园，一个人",
    "预算5块，想吃汉堡",
    "两个人吃，想吃麦辣鸡腿堡，不喝可乐，到店",
    "低卡，高蛋白，预算50",
    "少糖，两个人，外送到北京市朝阳区三里屯",
    "心情不好，想吃点甜的，预算20",
    "十二个人聚餐，预算300，到店",
    "想吃巨无霸套餐，加薯条，到店自取",
    "预算二三十元，想吃辣，外送",
    "高蛋白，不吃辣，预算40，一个人",
    "清爽一点，预算25，外送到公司",
    "想吃板烧鸡腿堡，两杯可乐，预算30",
    "不喝可乐，不吃辣，预算60，三个人",
    "周末聚餐，丰盛一点，预算100，到店",
    "夜宵，想吃麦辣鸡翅，预算15",
    "低卡沙拉，预算35，一个人",
    "双人套餐，预算80，外送到南山科技园",
    "想吃安格斯厚牛堡，预算45",
    "减肥，高蛋白，预算30",
    " budget30, 想吃辣",  # 中英混排：容错路径也在基准内
]

class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


_PROCESS_QUERY_INFORMATION = 0x0400
_PSAPI = ctypes.WinDLL("psapi")
_KERNEL32 = ctypes.WinDLL("kernel32")
_KERNEL32.OpenProcess.restype = wintypes.HANDLE
_KERNEL32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
_PSAPI.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESS_MEMORY_COUNTERS), wintypes.DWORD]


def peak_working_set_mb(pid: int) -> float:
    """查询指定进程的峰值工作集（MB）。进程已退出/打不开时返回 0。"""
    handle = _KERNEL32.OpenProcess(_PROCESS_QUERY_INFORMATION, False, pid)
    if not handle:
        return 0.0
    try:
        counters = _PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        if not _PSAPI.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return 0.0
        return counters.PeakWorkingSetSize / 1024 / 1024
    finally:
        _KERNEL32.CloseHandle(handle)


def measure_cold_start(runs: int = 5) -> dict:
    times = []
    for _ in range(runs):
        t0 = time.perf_counter()
        proc = subprocess.run(
            [PY, "-m", "burger_quest", "--version"], capture_output=True, text=True, cwd=REPO, timeout=60
        )
        times.append((time.perf_counter() - t0) * 1000)
        assert proc.returncode == 0, proc.stderr[-500:]
    return {"runs_ms": [round(t, 1) for t in times], "median_ms": round(statistics.median(times), 1)}


def measure_demo_e2e() -> dict:
    """demo 完整任务的墙钟时间 + 子进程峰值工作集（20ms 轮询采样）。"""
    t0 = time.perf_counter()
    proc = subprocess.Popen(
        [PY, "-m", "burger_quest", "demo", "我想吃辣，预算35，外送到南山科技园，一个人"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=REPO,
    )
    peak_mb = 0.0
    samples = 0
    while proc.poll() is None:
        peak_mb = max(peak_mb, peak_working_set_mb(proc.pid))
        samples += 1
        time.sleep(0.02)
    out, err = proc.communicate()
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert proc.returncode == 0, err[-800:]
    assert "麦辣" in (out or ""), "demo 输出异常"
    return {
        "wall_ms": round(elapsed_ms, 1),
        "peak_rss_mb": round(peak_mb, 1),
        "rss_samples": samples,
    }


def measure_nlparser() -> dict:
    sys.path.insert(0, str(REPO))
    from burger_quest import nlparser  # noqa: E402

    latencies = []
    for _ in range(3):  # 3 轮取每轮中位数的最小值，排除抖动
        round_ms = []
        for text in SAMPLE_TEXTS:
            t0 = time.perf_counter()
            c = nlparser.parse(text)
            round_ms.append((time.perf_counter() - t0) * 1000)
            assert c is not None
        latencies.append(statistics.median(round_ms))
    per_call = []
    for text in SAMPLE_TEXTS:
        t0 = time.perf_counter()
        nlparser.parse(text)
        per_call.append((time.perf_counter() - t0) * 1000)
    per_call.sort()
    p95 = per_call[max(0, int(len(per_call) * 0.95) - 1)]
    return {"median_ms": round(min(latencies), 3), "p95_ms": round(p95, 3), "samples": len(SAMPLE_TEXTS)}


def measure_card_render() -> dict:
    sys.path.insert(0, str(REPO))
    from burger_quest import cards  # noqa: E402
    from burger_quest.mcp_mock import MockMCPClient  # noqa: E402
    from burger_quest.service import run_quest  # noqa: E402

    client = MockMCPClient()
    result = run_quest("我想吃辣，预算35", client)
    assert result.chosen is not None, "demo 未产出方案，无法测渲染"
    c = result.constraints
    t0 = time.perf_counter()
    html = cards.render_html(
        c,
        result.chosen,
        result.store,
        result.time_info,
        "性能基线测量",
        "2026-10-17T12:00:00",
        demo=True,
    )
    render_ms = (time.perf_counter() - t0) * 1000
    assert html.lstrip().startswith(("<!DOCTYPE", "<html")), "渲染产物异常"
    return {"wall_ms": round(render_ms, 2), "html_kb": round(len(html.encode("utf-8")) / 1024, 1)}


def measure_test_suite() -> dict:
    t0 = time.perf_counter()
    proc = subprocess.run([PY, "-m", "pytest", "-q"], capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=REPO, timeout=900)
    elapsed = time.perf_counter() - t0
    assert proc.returncode == 0, proc.stdout[-500:]
    # pytest -q 汇总行形如 "635 passed in 12.34s"；从真实输出解析，不硬编码
    m = re.search(r"(\d+)\s+passed", proc.stdout)
    count = int(m.group(1)) if m else -1
    return {"wall_s": round(elapsed, 1), "cases": count}


def main() -> int:
    ap = argparse.ArgumentParser(description="性能基线测量")
    ap.add_argument("--json", default=None, help="把结果另存为 JSON")
    ap.add_argument("--skip-tests", action="store_true", help="跳过测试套件测量（最慢的一项）")
    args = ap.parse_args()

    print("== 性能基线测量（只读，不联网）==", flush=True)
    report = {}
    report["cold_start"] = measure_cold_start()
    print(f"冷启动 --version：中位 {report['cold_start']['median_ms']} ms（{report['cold_start']['runs_ms']}）")
    report["demo_e2e"] = measure_demo_e2e()
    print(f"demo 端到端：{report['demo_e2e']['wall_ms']} ms，峰值内存 {report['demo_e2e']['peak_rss_mb']} MB")
    report["nlparser"] = measure_nlparser()
    print(f"nlparser 单次解析：中位 {report['nlparser']['median_ms']} ms / p95 {report['nlparser']['p95_ms']} ms")
    report["card_render"] = measure_card_render()
    print(f"成就卡渲染：{report['card_render']['wall_ms']} ms（产物 {report['card_render']['html_kb']} KB）")
    if not args.skip_tests:
        report["test_suite"] = measure_test_suite()
        print(f"测试套件（{report['test_suite']['cases']} 例）：{report['test_suite']['wall_s']} s")
    report["python"] = sys.version.split()[0]
    report["platform"] = sys.platform

    print("\n== Markdown 表格（可直接贴文档）==")
    print("| 指标 | 数值 |")
    print("| --- | --- |")
    print(f"| 冷启动 `--version`（中位） | {report['cold_start']['median_ms']} ms |")
    print(f"| demo 端到端（墙钟） | {report['demo_e2e']['wall_ms']} ms |")
    print(f"| demo 端到端（峰值工作集） | {report['demo_e2e']['peak_rss_mb']} MB |")
    print(f"| nlparser 单次解析（中位/p95） | {report['nlparser']['median_ms']} / {report['nlparser']['p95_ms']} ms |")
    print(f"| 成就卡渲染 | {report['card_render']['wall_ms']} ms |")
    if "test_suite" in report:
        print(f"| 测试套件（{report['test_suite']['cases']} 例） | {report['test_suite']['wall_s']} s |")
    print(f"| 测量平台 | Python {report['python']} / {report['platform']} |")

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON 已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
