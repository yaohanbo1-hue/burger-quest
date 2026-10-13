# workbuddy.md —— WorkBuddy 开发上下文

> 本文件用于麦当劳程序员创意开发大赛「WorkBuddy 专项奖励」核验：记录 Burger Quest（麦门异世界）真实使用 WorkBuddy 开发的过程与上下文。

## 1. 开发环境

- 平台：WorkBuddy（Windows 桌面端，Git Bash 环境）
- 开发日期：2026-10-13
- 运行时：Python 3.13（引擎零依赖核心 + 官方 `mcp` SDK 真实模式）
- 参赛项目：麦当劳程序员创意开发大赛（Skill 方向）
- 开发方式：与 WorkBuddy 助手多轮对话完成需求分析 → 规范研究 → 架构设计 → 编码 → 测试 → 文档全流程

## 2. 需求输入（用户原始指令摘要）

> 基于麦当劳中国官方 MCP Server，开发一个具有游戏化体验的点餐规划 Skill。用户输入预算、心情、口味和用餐偏好后，AI 以 RPG 游戏主持人身份设计小任务，调用真实 MCP 工具查询菜单、价格、营养信息及可用优惠，生成符合约束的点餐方案。MVP 要求：自然语言解析、MCP 数据检索、套餐规划（价格计算工具校验，不得编造）、RPG 输出、分享卡片（含非官方声明）、错误处理。技术要求：先读官方规范、复用现有 MCP 连接能力、分层架构、可复现演示与测试、Token 不落地、脱敏配置示例、不自动下单。

## 3. 规范研究（先行步骤）

在 WorkBuddy 中通过网页检索完成：

1. 比赛规则：`tch.cloud.tencent.com/contest/48` 与官方仓库 `M-China/mcd-developer-innovation-challenge` 的 README —— 确认必交文件清单（README.md / CONTEST_DECLARATION.md / MCP_INTEGRATION.md / 源代码 / workbuddy.md）、报名方式（Issue）、排名规则（公开 Star 数）、WorkBuddy 专项奖励条件；
2. MCP 集成指南：`M-China/mcd-mcp-server` —— 确认接入地址 `https://mcp.mcd.cn`、Streamable HTTP + Bearer Token、限流 600 次/分钟、错误码 401/429、31 个工具的清单与描述；
3. 参赛声明原文：从官方仓库 raw 文件逐字获取，未做任何修改。

## 4. 关键设计决策

1. **双形态交付**：WorkBuddy Skill（skill.md，指导宿主 AI 编排 MCP 工具，不重复实现 MCP Client）+ 独立 Python 引擎（官方 mcp SDK 薄封装，用于可复现演示与 30 个自动化测试）；
2. **三层「不编造」防线**：无价格餐品出局 → calculate-price 为总价唯一来源 → 失败路径话术模板化；对应测试全部覆盖；
3. **金额用「分」（int）**：杜绝浮点误差，展示层才格式化；
4. **防御式字段适配**：官方未公布工具级 JSON Schema，客户端按多键名容错提取，取不到即声明缺失；
5. **调用量控制**：候选组合预筛 + 每组合最多 3 次验价 ≈ 25 次/任务，远离限流；
6. **演示模式与真实模式严格分离**：fixtures 显著标注「演示数据」，卡片与终端输出均体现数据来源。

## 5. 验证记录（可复现）

```bash
python -m pytest tests/ -q          # 80 passed
python -m burger_quest demo "我想吃辣，预算35，外送到南山科技园，一个人" \
  --card-html examples/cards/demo_spicy.html --card-md examples/cards/demo_spicy.md
python -m burger_quest demo "两个人想吃点好的，预算100，堂食"   # 丰盛场景
python -m burger_quest demo "减肥中，想吃低卡的，预算40，到店自取"  # 低卡场景
python -m burger_quest demo "预算5块，想吃汉堡"                # 预算不足 -> 如实失败
python -m burger_quest demo "外送到演示门店，预算35"            # 多家候选 -> 列出不选
python -m burger_quest stores "南山科技园" --demo               # 门店查询
```

实测结果：辣味外送场景产出「麦辣鸡腿堡 + 立减券 + 配送费 = ¥27，剩余预算 ¥8」；两人丰盛场景产出「双套餐 + 双小食 + 双饮品 = ¥98，叠券后 ¥93」；低卡场景整餐约 475 千卡；预算不足场景明确失败且不编造价格；多家候选场景列出 DEMO-001/DEMO-002 并暂停。

真实模式实测（2026-10-13，假 Token）：`https://mcp.mcd.cn` 网络可达；无效 Token 返回 HTTP 401，客户端输出「MCP Token 无效或已过期（401）」友好错误（退出码 2）；未配置 Token 时明确报错且不回落演示数据。SDK 版本兼容（导出名 / headers 传法 / transport 元数 / 401 状态码还原）均已按 mcp 1.27.2 实测处理。

## 5.5 修复记录（第一阶段审计后，2026-10-13）

审计发现真实 MCP 模式存在 P0 缺陷（异步客户端被同步调用，100% 崩溃），本阶段已修复：

1. **P0 同步/异步不匹配**：`HttpMCPClient` 重写为「后台事件循环线程 + 同步门面」，`MCPClient` 接口保持同步，Mock 与引擎零改动；
2. **门店解析重写**：显式编码优先（单次查询复用菜单）→ 位置查询附近门店 → 多候选全部列出等用户指定（不编造、不默默选择）→ 无位置明确要求补充（真实模式）；
3. **CLI 重构**：`quest` 移除 `--fixture`（彻底区分 demo/真实）；新增 `stores` 子命令；候选门店打印与重跑指引；`--fixture` 缺失友好报错；帮助文本重写；
4. **SDK 联调修复**（实测 mcp 1.27.2）：双版本导出名兼容、headers 经 `httpx2.AsyncClient` 传递、transport 2/3 元组兼容、event hook 还原被 SDK 吞掉的 401/429 状态码；
5. **文档去魅**：README/MCP_INTEGRATION/skill.md/demo.md 移除 DEMO-001 真实模式误导；`query-meal-detail` 从「实际使用」移至「预留」；新增「已知限制」节；
6. **测试扩充**：30 → 80 个（新增真实客户端同步门面、适配器、门店流程、CLI 全流程；修复一个空断言测试）。

## 6. 交付物清单

- 引擎源码 `burger_quest/`（models / nlparser / mcp_base / mcp_http / mcp_mock / planner / rpg / cards / service / cli，共 11 个模块）
- 测试 `tests/`（80 个用例：解析、规划铁律、真实客户端同步门面与错误映射、适配器、门店流程、CLI 全流程、卡片安全、故障路径）
- 文档：README.md、MCP_INTEGRATION.md、skill.md、examples/demo.md
- 比赛必交：CONTEST_DECLARATION.md（官方原文）、mcp-config.example.json（仅占位符）、workbuddy.md（本文件）
- 示例：examples/cards/ 三张演示成就卡（HTML/Markdown/JSON）

## 7. 安全确认

- 仓库内无任何真实 Token / 密钥 / 个人信息（`mcp-config.example.json` 仅 `${MCD_MCP_TOKEN}` 占位符，`.gitignore` 忽略 `.env` 与 `mcp-config.json`）；
- 未接入任何写操作工具（create-order / auto-bind-coupons / mall-* 等）；
- 所有输出含非官方项目声明。

_本文件由参赛者使用 WorkBuddy 开发本项目时整理生成。_
