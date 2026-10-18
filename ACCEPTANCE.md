# 候选版本总验收报告

> 验收时间：2026-10-18（第 8 项「候选版本总验收」+ 当日最终审计纠偏）
> 验收对象：`main` @ `a740d74`（最终审计纠偏：文档与真实状态对齐）
> 验收方式：**全部本地复跑 + CI 实证**，不引用未经本次验证的数字

## 结论：✅ 通过，可作候选版本交付

| # | 验收项 | 结果 | 证据 |
| --- | --- | --- | --- |
| 1 | 全量测试 | ✅ 657 passed | 本地 `pytest -q`（58.6s） |
| 2 | 覆盖率 | ✅ 91.44% ≥ 85% 阈值 | `--cov-fail-under=85` 实跑 |
| 3 | CI 全流水线 | ✅ 7/7 job 绿 | run 39083738429（对 `a740d74`：Windows 3.11/3.12 + Linux 3.10/3.11/3.12/3.13 + Quality gates，共 7 个 job） |
| 4 | 类型检查 | ✅ mypy 0 error | 本地复跑（曾 45） |
| 5 | 安全扫描 | ✅ bandit 0 finding | 本地复跑（曾 12；2 处 `nosec` 均带理由） |
| 6 | 依赖漏洞 | ✅ pip-audit 无已知漏洞 | 本地复跑 |
| 7 | 空白检查 | ✅ 通过 | `git diff --check HEAD~`（CI 同口径） |
| 8 | 文档一致性 | ✅ 通过 | README 数字同步为 657/91.44%；skill.md 与代码一致性由 `test_skill_docs.py` 在套件内强制；其余文档无陈旧计数 |
| 9 | 敏感扫描 | ✅ 0 真实命中 | CI 同口径正则 + 手机号形态全仓扫描；1 个命中为**已验证误报**（见下） |
| 10 | 干净环境（发布包） | ✅ 19/19 | 异路径解压 + Info-ZIP 最坏情况双路验证 |
| 11 | 真实/模拟状态分离 | ✅ 有据可查 | MCP_INTEGRATION.md §3.12 三态台账（10 工具逐一标注） |
| 12 | 遗留项登记 | ✅ 已登记 | TECH_DEBT.md D1-D6（带优先级与建议动作） |

## 1. 测试与质量门禁（本地复跑实录）

```
pytest -q --cov                        657 passed · 91.44%（阈值 85%）
mypy burger_quest                      Success: no issues found in 20 source files
bandit -r burger_quest                 0 finding（B105 nosec ×3 均带理由）
pip-audit                              No known vulnerabilities found
git diff --check HEAD~                 通过
```

逐文件覆盖率低位项（已登记技术债，见 TECH_DEBT.md §2）：
doctor 83% / smoke 87% / achievements 87% / cli 88% / mcp_http 90% / webui 84%。

## 2. 敏感扫描说明（1 个已验证误报）

全仓扫描（Bearer Token 形态 + 11 位手机号形态）命中 1 处：

- `tests/fixtures/official_tools_schema.json` 含 `18616646686`。

**判定为误报，不做修改**，理由：
1. 该文件是 2026-10-17 用真实 Token 拉取的官方 `list_tools` Schema **verbatim
   快照**，手机号出自官方对 `phone` 字段的文档示例（"如'18616646686'"），
   非任何真实用户数据；
2. `tests/test_tool_contract.py::test_snapshot_provenance` 强制该文件与官方
   响应逐字一致——改动即破坏契约审计的权威来源。

## 3. 干净环境验证（Windows 便携发布包）

- 产物：`dist/BurgerQuest-portable-win64-v0.1.0.zip`（23.7 MB，自带 Python
  运行时，解压即用，无需安装任何软件）；
- **确定性**：连续两次构建 SHA-256 逐字节一致
  （`fbfdc79315252372e8d038aef838558ae2cf20284e70e887571d43f0e27d250a`，
  排除 .pyc + 规范化 RECORD 后达成，可复现构建）；
- **19/19 项通过**（`tools/verify_portable.py`，异路径解压后实跑）：
  结构完整 / `--version` / demo 出方案 / 预算不足诚实失败 rc=1 /
  缺 Token rc=2 且不回退 demo / 中文名与 ASCII 名 bat 均可跑 /
  web healthz + CSP / 无 .env/mcp-config/Token 形态；
- **最坏情况兜底**：Info-ZIP `unzip` 不认 zip 的 UTF-8 标志位、中文名解出
  乱码时，纯 ASCII 兜底名 `START.bat` 实测可完整跑通 demo；
- **校验链**：`校验发布包.bat` 肯定测试通过（完好包通过 / 篡改 1 字节明确报失败 /
  拖拽传参正常）。

## 4. 真实与模拟状态分离

**铁律执行情况**：所有 657 个测试均用假会话（mock/录制响应）驱动，
**不访问网络也能全绿**；真实 Token 从未进入代码、测试或产物。

真实服务验证（2026-10-13/17，官方 Token，记录于 MCP_INTEGRATION.md §3.5/§3.12）：

| 类别 | 状态 |
| --- | --- |
| 官方 Schema 快照（35 工具 verbatim） | ✅ 2026-10-17 拉取落盘 |
| 9/10 在用工具真实调用 | ✅ 含 401/429 还原、600010 双字段契约、算价分单位 |
| `delivery-query-stores` | ⬜ 账户无保存地址，待验证 |
| 优惠券真实优惠实效 | ⬜ 账户无可用券；**已验证提交契约**（couponId+couponCode 成对，只传 ID 报 600010），未验证有券时优惠是否真实生效 |
| 服务端同单多券接受性 | ⬜ per-item 挂载已修（容量守卫），待有券账户验证 |

未验证项均已在前端文档（README §7）与集成文档中**显式标注**，不以任何形式
伪装成已验证。

## 5. 交付物清单

| 交付物 | 位置 |
| --- | --- |
| 引擎源码（20 模块，mypy 零 error） | `burger_quest/` |
| 测试套件（657 例，91.44%） | `tests/` |
| Windows 便携发布包（23.7MB + sha256 + 校验 bat） | `dist/`（**gitignore 排除，仅本机验证产物；当前未随仓库公开发布，评审不可从 GitHub 页面下载**） |
| 发布包构建/验证脚本 | `tools/build_portable.py`、`tools/verify_portable.py`、`tools/portable-constraints.txt` |
| 性能基线与回归阈值 | `PERFORMANCE.md` + `perf-baseline.json` + `tools/measure_perf.py` |
| 技术债台账 | `TECH_DEBT.md` |
| CI 四门禁 + 预检脚本 | `.github/workflows/ci.yml`、`tools/prepush_check.py` |
| 联调实测记录（真实 Token） | `MCP_INTEGRATION.md` |

## 6. 已知限制（随交付声明）

1. 真实模式端到端延迟依赖官方服务端与网络，非本项目可控（PERFORMANCE.md §4）；
2. 便携发布包仅覆盖 Windows（项目目标用户即 Windows 非技术用户）；
3. **便携发布包当前未公开发布**：产物位于被 `.gitignore` 排除的 `dist/`，仅本机验证产物；仓库无 Release、远端无 `dist/` 目录，评审不可从公开 GitHub 页面下载。如需随评审分发，需另行约定发布方式（如 GitHub Releases）或按 `tools/build_portable.py` 本地构建；
4. 优惠券「有券时优惠是否真实生效」未结案（无有券账户），已在所有面向用户的
   文档中显式标注；
5. doctor.py 真实网络失败分支覆盖率 83%（技术债 D1，排障工具主路径已覆盖）。
