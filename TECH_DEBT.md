# 技术债台账

> 建立时间：2026-10-17（第 7 项「性能与技术债治理」）
> 盘点口径：pytest-cov 逐文件覆盖 + 人工代码走查；数字为当前值，非历史快照。
> 原则：**只记真债，不造 KPI**。每条注明触发场景与建议动作；不为了数字写
> 「覆盖表演」测试（断言必须有行为约束，见 tests/ 各文件 docstring）。

## 1. 已清偿记录

### 2026-10-17（第 7 项「性能与技术债治理」）

| 债务 | 清偿方式 | 结果 |
| --- | --- | --- |
| `__main__.py` 0% 覆盖（`python -m burger_quest` 入口从未被测） | `tests/test_cli.py` 用 runpy 以 `__main__` 身份真实执行入口，并验证退出码透传（缺 Token → rc=2） | 0% → **100%** |
| `webui.py` 仅 65%（安全相关分支与两个端点零请求） | `tests/test_webui_e2e.py` 新增 E6/E7/E8 三组：`/api/status`、`/api/progress` 真实线路；`_host_header_name` IPv6/畸形分支；`error_state`/`result_state` 全映射；`serialize_result` 无 Token 泄漏 + JSON 可序列化；`progress_lines` 落盘/IO 失败；`serve` 非 localhost 绑定守卫与端口占用提示 | 65% → **84%** |
| 测试总数停滞（635） | 上述新增 22 例，全部有行为断言 | 635 → **657** |
| 总覆盖率 89.7% | 同上 | → **91.44%** |

### 2026-10-19（支线 Skill 扩容 + CLI 子系统命令补测试）

| 债务 | 清偿方式 | 结果 |
| --- | --- | --- |
| `cli.py` 仅 **52%**：detail/account/campaign/coupons 四个子系统命令（各约 130 行真实逻辑）零直接测试 | `tests/test_cli_commands.py` 新增 16 例：套餐组成与权威参考价 / 单品无 rounds / 不存在编码诚实失败 / 重配参数三种错误路径（格式、组序号）/ 重配指定不存在选项保留默认项 / 四命令 `--json` 落盘结构 / 三类券分区与 skipped 态 / accountId 不出现 | 52% → **84%** |
| 测试总数停滞（703） | `tests/test_cli_commands.py` 16 例 + `tests/test_skill_docs.py` 支线文档断言 2 例 + `tests/test_tool_contract.py` 三层注册表互斥守卫 1 例 | 703 → **722** |
| 总覆盖率 85.83% | 同上 | → **89.97%** |

## 2. 在账债务（按优先级）

### P2 — 有价值、可延后

**D1. `doctor.py` 83%（37 行未覆盖）**
- 缺的是真实网络路径分支：DNS 失败、TLS 握手、连接超时、httpx 未安装降级。
- 触发场景：用户网络异常时的诊断输出质量。
- 建议：起本地假 TCP 服务（socket 接受即 RST / 接受不响应）覆盖超时与拒连；
  或录制一次真实失败的响应做 fixture。预计 +10% 覆盖，工作量半天。
- 风险承受度：高。doctor 是排障工具，主路径（成功/Token 缺失）已覆盖。

**D2. `cli.py` 84%（86 行，2026-10-19 复核刷新）**
- 已覆盖（2026-10-19）：detail/account/campaign/coupons 四命令的主路径与
  主要错误路径（见 §1 清偿记录）；剩余未覆盖为 web 子命令启动参数分支
  （--host/--port/--allow-lan 组合）、部分 `--json` 输出边角与错误码映射边角。
- 建议：参数化测试扫一遍 argparse 组合；web 启动用端口占用法（E8 已有先例）。
- 风险承受度：中。CLI 是主入口，边角分支影响 help 文本与错误提示质量。

**D3. `mcp_http.py` 87%（57 行，2026-10-19 复核刷新）**
- 缺：传输层双版本工厂（streamablehttp_client 新旧签名）的错误分支、
  部分 4xx/5xx 状态码精细分支。
- 建议：用假 socket 层注入特定 HTTP 状态；新旧 SDK 分支已由
  `verify_imports` 在便携包构建时硬验证，测试侧可放宽。
- 风险承受度：中。

### P3 — 已知边界，暂不处理

**D4. `smoke.py` 87%（22 行）/ `achievements.py` 87%（23 行）**
- smoke 缺的是真实 Token 冒烟的失败分支（联调时手动跑过，自动化需要录制品）；
  achievements 缺的是连续挑战/剧情线的少见组合。
- 动作：记录在案，不排期。

**D5. 中英混排与生僻输入的解析质量**
- `nlparser` 对「 budget30, 想吃辣」这类中英混排是容错路径（已在基准样本内），
  但更乱的输入（纯拼音、错别字）没有系统化测试。当前策略是「不猜：
  解析不出就进 warnings 让用户补充」，行为已有测试钉死。
- 动作：维持现状。过度投资解析容错对评审收益低。

**D6. 便携发布包仅覆盖 Windows**
- 项目目标用户即 Windows 非技术用户；macOS/Linux 不在范围。
  `tools/build_portable.py` 的 psapi 内存查询是 Windows 专属（已有 try 兜底）。
- 动作：接受。

## 3. 性能基线

见 `PERFORMANCE.md`（冷启动/demo 端到端/nlparser/渲染/测试套件五项基线 +
回归阈值）。复现：`python tools/measure_perf.py`。

## 4. 防新增债务的机制（已就位）

- CI 四门禁：覆盖率 ≥85%（`--cov-fail-under`）、mypy 零 error、
  bandit 零中高危、pip-audit 无已知漏洞——任何一项红即合并不了。
- `tools/prepush_check.py`：推送前本地跑 CI 同口径（`git diff --check HEAD~`），
  消灭「本地绿 CI 红」的口径差。
- 覆盖率先例：新增生产代码须带测试，否则 `fail_under` 会拦下净覆盖率下滑。
