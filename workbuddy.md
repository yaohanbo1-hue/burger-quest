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
python -m pytest tests/ -q          # 99 passed
python -m burger_quest demo "我想吃辣，预算35，外送到南山科技园，一个人" \
  --card-html examples/cards/demo_spicy.html --card-md examples/cards/demo_spicy.md
python -m burger_quest demo "两个人想吃点好的，预算100，堂食"   # 丰盛场景
python -m burger_quest demo "减肥中，想吃低卡的，预算40，到店自取"  # 低卡场景
python -m burger_quest demo "预算5块，想吃汉堡"                # 预算不足 -> 如实失败
python -m burger_quest demo "在演示门店吃，到店自取，预算35"      # 多家候选 -> 列出不选
python -m burger_quest stores "南山科技园" --demo               # 门店查询
```

实测结果：辣味外送场景产出「麦辣鸡腿堡 + 立减券 + 配送费 = ¥27，剩余预算 ¥8」；两人丰盛场景产出「双套餐 = ¥68，叠券 ¥63」；低卡场景整餐约 540 千卡；预算不足场景明确失败且不编造价格；多家候选场景列出 DEMO-001/DEMO-002 并暂停。

### 真实 MCP 联调实测（2026-10-13，官方 Token）

1. **SDK 版本漂移修复**（mcp 1.27.2）：导出名 `streamable_http_client`、headers 改经 `httpx2.AsyncClient`、transport yield 2 元组、401/429 状态码被 SDK 吞掉后用 event hook 还原；
2. **工具参数经 `list_tools` 官方 Schema + 真实调用验证**：`query-nearby-stores` 需 `beType`+`searchType=2`+`city/keyword`；`query-meals`/`query-meal-detail`/`query-store-coupons`/`calculate-price` 需 `orderType`+`beType`；外送另需 `beCode`（走配送地址链路）；餐品详情参数是 `code`；券挂在 `items[].couponId`；
3. **关键返回结构实测**：`query-meals` **只返回编码和标签**（无名称无价格）→ 服务端重构为「短名单 → 详情补名 → 一次算价发现单价 → 组合验价」四段式；`calculate-price` **金额单位为分**；`list-nutrition-foods` 返回自定义分隔文本；
4. **端到端实测**：多候选门店（5 家真实门店列出并暂停）；到店完整链路（112 编码 → 10 详情 → 10 价格，产出「龙焰鸡腿堡 ¥24」）；多人丰盛（¥52）；外送无地址（明确说明并停止）；
5. **仍未验证**：优惠券字段/单位（账户无券）、外送全链路（账户无地址）、套餐换选与特制选项——已写入 MCP_INTEGRATION.md §3.6 与 README「已知限制」。

## 5.5 修复记录（第一阶段审计后，2026-10-13）

审计发现真实 MCP 模式存在 P0 缺陷（异步客户端被同步调用，100% 崩溃），本阶段已修复：

1. **P0 同步/异步不匹配**：`HttpMCPClient` 重写为「后台事件循环线程 + 同步门面」，`MCPClient` 接口保持同步，Mock 与引擎零改动；
2. **门店解析重写**：显式编码优先（单次查询复用菜单）→ 位置查询附近门店 → 多候选全部列出等用户指定（不编造、不默默选择）→ 无位置明确要求补充（真实模式）；
3. **CLI 重构**：`quest` 移除 `--fixture`（彻底区分 demo/真实）；新增 `stores` 子命令；候选门店打印与重跑指引；`--fixture` 缺失友好报错；帮助文本重写；
4. **SDK 联调修复**（实测 mcp 1.27.2）：双版本导出名兼容、headers 经 `httpx2.AsyncClient` 传递、transport 2/3 元组兼容、event hook 还原被 SDK 吞掉的 401/429 状态码；
5. **联调后架构重构**：因官方 `query-meals` 只返回编码，服务端增加「短名单 → `query-meal-detail` 补名 → 一次 `calculate-price` 价格发现（分）→ 组合验价」链路；新增外送场景（配送地址 → 可配送门店拿 beCode；无地址明确说明）；Mock 与 fixtures 全面对齐真实返回格式；
6. **文档去魅**：README/MCP_INTEGRATION/skill.md/demo.md 移除 DEMO-001 真实模式误导；`query-meal-detail` 转为「已接入」；MCP_INTEGRATION.md 增加 §3.5 联调实测记录与 §3.6 仍未验证项；README 增加「已知限制」；
7. **测试扩充**：30 → 99 个（真实客户端同步门面与错误映射、联调格式适配器、门店与外送流程、CLI 全流程；修复一个空断言测试）。

## 5.6 MCP 集成专项复查记录（阶段 5，2026-10-14）

用户要求「专门审查并完善麦当劳 MCP 集成」，本轮完成：

1. **调用链审计**：`cli/service → MCPClient(同步接口) → HttpMCPClient(同步门面 + 后台事件循环线程) → 官方 mcp SDK ClientSession.call_tool → mcp_base.adapt_* 归一化`；代码审计确认无任何裸 HTTP 自研协议（唯一 `httpx2` import 是给官方 SDK 工厂构造 AsyncClient 传 Authorization）；
2. **SDK 版本漂移复核**：实测环境 SDK 已从 1.27.2 升到 **2.3.0**——逐项复核兼容层（导出名/工厂签名/transport 元组/call_tool 位置参数全部仍匹配），新发现并处理 `list_tools()` 返回 `ListToolsResult` 模型（1.x 是裸 list）；
3. **工具发现实测**：经项目客户端用真实 Token `list_tools` 发现官方 **35 个工具**，项目使用的 8 个全部在列；逐个核对官方输入 Schema 与代码传参一致（`code` 参数名、`items[].productCode/quantity/couponId`、`beType+searchType` 组合）；另发现官方 Schema 约定「到店自提场景传 beCode 会报错」，客户端按空 beCode 不传已规避；
4. **错误通道补缺**：新增 `_call()` 协议级 `is_error/isError` 检查——业务错误（信封 success=false）与协议错误两条通道都转可读 MCPError 并记审计，绝不让未知形态静默流入适配器；实测无效门店编码 `DEMO-001` 被服务端明确拒绝（「storeCode门店编码必须全部由数字组成」）；
5. **只读冒烟测试入口**（新增 `burger_quest/smoke.py` + `burger-quest smoke` 子命令）：连接 → 工具发现 → now-time-info → query-nearby-stores → query-meals → query-meal-detail → calculate-price → 安全自检（仅只读工具）八步；Token 只从环境变量读、绝不打印、不接受命令行参数；退出码 0/1/2；`--demo` 离线可重复执行；真实实测 8/8 通过（35 工具 / 5 家候选门店 / 110 编码 / 鸡薯双全盒 ¥15.90）；
6. **测试扩充**：99 → **126 个**（isError 协议错误、非 JSON 文本、空 content、字符串 content、价格为字符串/小数的单位防御、缺总价不出方案、envelope 无 data 键、success=false 无 message、错误类型字段丢弃不编造、list_tools 两种 SDK 形态、营养文本坏行跳过；冒烟测试 9 个用例：全通过/某步失败/无门店不编造/仅只读红线/连接失败/缺 Token/离线 demo/CLI 接线/JSON 输出）；
7. **端到端复测**（SDK 2.3.0 + 真实 Token）：`quest ... --store-code 1420762` → 110 编码 → 10 详情 → 10 价格 →「龙焰鸡腿堡 ¥24.00，剩 ¥6.00」，160 条营养，0 券如实说明，无回归；
8. **阻塞项不变**（已在 MCP_INTEGRATION.md §3.6 登记）：优惠券字段与金额单位（账户无券）、外送全链路（账户无地址）、套餐换选/特制选项。

## 5.7 套餐规划引擎升级记录（2026-10-14）

用户要求升级规划引擎，目标体验：「预算 35 元，想吃辣，两个人，希望尽量高蛋白，不想点太多饮料」→ 真实菜单检索 → 候选组合 → 权威核验 → 最多三个可比较方案。

1. **解析层（nlparser）**：新增饮料偏好——硬避免（「不要饮料/不点饮料/不想喝饮料」→ avoid_categories）与软上限（「不想点太多饮料/少点饮料」→ max_drink_qty = 人数-1，至少 1）；软上限优先于硬避免判定（「不想点太多」不是「不要」）；补充句尾位置模式（「……，在深圳市南山区科技园」）+ 停用词/疑问词过滤（「店里」「有什么」不再误判为位置）；
2. **模型层（models）**：Constraints 增加 avoid_categories / max_drink_qty / has_nutrition_preference()；PlanOption 增加 unmet（不完全符合）、nutrition_complete、nutrition_missing、coupon_note；
3. **规划层（planner）重写**：
   - 候选生成支持品类硬避免与饮料槽位封顶；**修复真 bug**：某品类池为空时原逻辑会丢弃整个模板的候选（短名单无饮品时多人模板全灭），改为跳过该槽位；
   - 紧预算多人新增 share_light 模板与最便宜主食候选（价格也是候选维度）；
   - 营养覆盖度跟踪：有偏好且零覆盖 → 排除候选（不猜测）；部分覆盖 → 保留但标注 + 降权；
   - 优惠效力核验：coupon_expected_discount 与权威折扣比对，不符即 coupon_note 备注「不默认优惠可叠加」；coupon_sets 扩展到前 3 张可用券的两两组合（同源券也可试探，是否叠加由 authority 决定）；
   - 最多 3 个可比较方案（预算内 Top3；无预算内则至多 2 个最接近参考）；同分决胜优先总价更低；
   - 失败诊断四分类（无价餐品 / 品类排除后无候选 / 算价全失败 / 预算筛除 / 营养缺失），每类附可操作建议；
4. **展示层**：CLI 新增【方案对比】（排序 + ✅符合/⚠️不完全符合）；卡片 HTML/Markdown 增加「不完全符合」区块；RPG 营养行标注数据完整性；`--json` 输出 unmet/nutrition_complete 等字段；
5. **测试 126 → 148**：18 个边界案例（预算刚好相等 remaining=0 / 超一分钱参考不篡价 / 算价全失败 / 券门槛未达不提交 / 券提交无效果不计优惠 / 双券权威只认一张 / 营养零覆盖排除 / 部分覆盖降权 / 无可行解可解释 / 品类排除干净 / 饮料硬避免与软上限 / 高蛋白用真实数据 / Top3 / 权威总价≠菜单价求和 / 确定性 / RPG 不改数字 / 预算内优先排序）+ 4 个离线演示固化（examples/planner_demos.md）；
6. **真实 MCP 复测**：用户原话 → 5 家真实门店列出暂停 → `--store-code 1420762` → 麦辣鸡腿汉堡三件套 ¥33.50 权威总价、预算内剩 ¥1.50、蛋白 24g 如实标注未达 25g、主食 1 份少于 2 人明示、0 饮料符合软上限；
7. 离线演示全部可重复：`python -m burger_quest demo "<需求>"`，无需 Token。

## 6. 交付物清单

- 引擎源码 `burger_quest/`（models / nlparser / mcp_base / mcp_http / mcp_mock / planner / rpg / cards / service / smoke / cli，共 12 个模块）
- 测试 `tests/`（126 个用例：解析、规划铁律、真实客户端同步门面与错误映射、协议级 isError、非法返回结构、联调格式适配器、门店与外送流程、CLI 全流程、只读冒烟测试、卡片安全、故障路径）
- 文档：README.md、MCP_INTEGRATION.md、skill.md、examples/demo.md
- 比赛必交：CONTEST_DECLARATION.md（官方原文）、mcp-config.example.json（仅占位符）、workbuddy.md（本文件）
- 示例：examples/cards/ 三张演示成就卡（HTML/Markdown/JSON）

## 7. 安全确认

- 仓库内无任何真实 Token / 密钥 / 个人信息（`mcp-config.example.json` 仅 `${MCD_MCP_TOKEN}` 占位符，`.gitignore` 忽略 `.env` 与 `mcp-config.json`）；
- 未接入任何写操作工具（create-order / auto-bind-coupons / mall-* 等）；
- 所有输出含非官方项目声明。

_本文件由参赛者使用 WorkBuddy 开发本项目时整理生成。_

## 5.8 MCP 调用效率与可靠性优化（2026-10-14）

先测基线再优化（用户明确要求不许凭猜测重构）：

1. **基线实测**（tools/measure_calls.py，插桩包装客户端方法）：演示模式 24 次/任务；真实模式 17 次/3.5s。拆解发现 query-meal-detail 10 次占真实耗时 ~50%，组合验价券试探最多 8 次/组合；
2. **调用插桩**（mcp_base._record）：seq/duration_ms/purpose/attempts/mcp_call 五字段；args 经 _safe_args 脱敏（无 token/authorization）；summarize_calls() 汇总；CLI 输出末尾新增【MCP 调用审计】，--json 增加 mcp_calls_summary；
3. **去重与缓存**：HttpMCPClient._call 内置 TTL 缓存（CACHEABLE_TTL：时间 30s/门店 2min/菜单详情 5min/营养 10min）；**算价与优惠券硬性排除**；失败不进缓存；
4. **超时与重试**：分工具超时（默认 30s/算价 45s）；_retry_backoff 决策表——401 不重试、429 最多 2 次（2s/4s 降速）、5xx 与瞬时网络错误最多 3 次（0.5/1/2s）、业务错误与 isError 不重试；_map_sdk_error 增加消息文本 5xx 识别；
5. **链路削减**：service 算价探测前置——一次 calculate-price 同时拿名称（productList.productName）与单价（subtotal），缺名称才回退 query-meal-detail；planner 券试探封顶（最优单券→最优双券→全失败回退无券；无券基线在同餐品下被叠券方案严格支配故不再无条件探）；
6. **优化后实测**：演示 11-15 次（最坏 15，-40%）；真实 **7 次/1.8s**（-59%/-43%）；详情 10→0 次；方案质量不变（同场景同权威总价）；
7. **测试 148 → 173**：新增 tests/test_mcp_efficiency.py 25 个用例（调用上限/详情归零/用途区分/券试探封顶/超时/分工具超时值/瞬时重试成功/持续失败有限次数/401 不重试/429 降速/业务错误不重试/5xx 重试/决策表/去重/TTL 过期/失败不进缓存/算价不缓存/券不缓存/可缓存集合排除/审计字段/脱敏/CLI 审计展示/JSON 摘要/失败不静默/部分工具失败隔离）；
8. 修复两处流程适配测试（详情回退路径、productList 缺名称场景改为新链路语义）。

## 6. 交付物清单

## 5.9 中文需求解析升级（2026-10-14）

先探针实测再修（不凭猜）：把用户要求的表达全集过一遍现行解析器，抓到 3 个真 bug + 4 项静默丢失。评估结论（开发要求 1）：**规则式足够，无需大模型/NLP 依赖**——问题是规则作用域污染与缺失否定语义。

修复的 P0（数据污染）：
1. 「热量控制在400千卡以内」的 400 被误读为预算 ¥400 且污染地点 -> 预算模式增加营养单位后瞻守卫（千卡/大卡/kcal/克/g…）+ 营养主语回看（热量/蛋白质/糖…），地点清洗拒绝数字开头与营养单位片段；
2. 「不吃辣」被解析成想吃辣 -> 新增 avoid_tastes（否定词窗口 + 「不/别/免+口味」直连），避免性约束优先于正向偏好，冲突时明示；
3. 多条件句「两个人不吃辣预算35」同样中招 -> 随 1/2 一并修复。

新增能力：
- 模糊预算中文区间：「二三十」= 20~30（注意汉语结构是双数字在「十」前）、「三十五」= 确切 35、「十来块」= 10~15；按上限设置预算 + fuzzy_budget 标记 + note 说明区间；
- 多预算表述取最严（min）并提示；
- 「偏清淡」-> veggie（启发式映射并注明）；
- 门店名作地点（麦当劳X餐厅/麦当劳（X店））；「X附近」；
- 「少糖」识别但明示不可执行（官方营养数据无糖字段）——不假装支持；
- 心情词扩充（回血/周五/放松）。

结构化与可调试（开发要求 2/7）：
- Constraints.sources 区分 explicit（用户明确限制）/ inferred（系统推断软偏好，如低卡 600 千卡、少饮料人数-1）/ default（系统默认，如到店自取）；
- Constraints.to_debug_dict(include_raw=False)：结构化调试表示，**默认不回显原始输入**（只给长度）；CLI --json 增加 constraints_debug 字段。

规划引擎联动：avoid_tastes 是硬约束——候选池排除命中避免口味的餐品，「不吃辣，想吃脆的」实测 3 方案全无辣且保留酥脆偏好；排空时给可解释失败。

测试 173 -> 198：tests/test_nlparser_upgrade.py 25 个用例；旧测试全过。真实 MCP 复测：「不吃辣，大概二三十元，两个人，外送到深圳市南山区科技园」-> ¥30 上限、麦乐鸡经典中套餐 ¥25 预算内、无辣、营养缺失明示不猜测。

## 5.10 成就卡视觉升级（2026-10-14）

设计方向「复古 RPG 游戏界面 + 麦当劳红黄配色 + 汉堡冒险主题」，直接改 cards.py（不另建前端）：

1. **结构**：头部（像素 M 徽标 + 项目名 + NON-OFFICIAL FAN PROJECT 标签 + LIVE/DEMO 模式徽章）→ 任务标题 + LV 等级 + 角色称号 → 任务背景 → 推荐装备（图标/绰号/真实名/数量/单价/**商品编码+来源溯源**）→ 价格结算四段（原价/已核实优惠/应付总价权威/剩余预算）→ 预算使用率进度条 → 属性面板 → 智囊团备注/不完全符合 → 页脚（需求/来源/时间/免责声明）；
2. **诚实性**：总价只认 calculate-price（未核验显「未知」不用菜单价冒充）；营养只用 option.nutrition 已汇总真实数据，零覆盖显「未知（无官方营养记录——不猜测数值）」——LIVE 示例卡的龙焰鸡腿堡正是此场景（营养库无此条目）；
3. **纯本地**：无外部 CDN/字体/图片/@import/url()（测试断言）；emoji 图标本地渲染；等宽字体做复古终端感；硬边框+错位阴影+斜纹进度条做像素风；
4. **适配**：@media max-width 480px 移动端（390px 截图验证）；@media print 打印（去阴影/避免断页）；
5. **Markdown 同步升级**：徽章行、装备表格（含溯源编码列）、价格表格、预算 ASCII 进度条、模式标签；同样转义防注入；
6. **安全测试扩充**（test_cards.py 4→20）：恶意 HTML/script/img onerror/引号/&/尖括号在编码、名称、分类、标签、券名、理由、unmet 全字段转义；属性上下文不断裂；结构断言（LIVE/DEMO、溯源、四段价格、未核验未知、营养未知/部分缺失标注、装饰不改数据、无外部资源、移动端打印、Markdown 结构）；
7. **示例卡片**（实际运行数据生成）：examples/cards/ 下 demo_spicy / demo_share / demo_light（DEMO）+ live_real（真实 MCP，Edge 无头截图 live_real.png / demo_spicy.png / demo_spicy_mobile.png 已验证渲染，未伪造截图）。

测试 198 → 214 全绿。

## 5.11 skill.md 打磨（2026-10-14）

目标：让 Skill 在 WorkBuddy 中稳定、自然地调用 mcd-mcp 连接器。先读现有 skill.md/MCP_INTEGRATION/代码，核对差异后重写：

1. **角色定位**（要求 2）：明确「友好的 RPG 游戏主持人 + 谨慎的点餐规划助手」双重身份；事实优先于剧情、谨慎优先于热情、简洁优先于演出（不为剧情追问角色设定）；
2. **调用链与实际代码对齐**（要求 1/3）：原文档写「逐个 query-meal-detail 拿名称」已过时——现行 service.py 是「一次 calculate-price 探测同时从 productList 拿名称+单价，详情仅回退」；券试探从「无券/单券/双券都探」改为「最优单券→最优双券→全失败回退无券」；文档同步更新并加效率提示（单任务 7-15 次调用）；
3. **连接器使用**（要求 4）：明确使用 WorkBuddy 已配置的 mcd-mcp 连接器，**绝不在对话中索要 Token**、不把 Token 写进文件；
4. **诚实性**（要求 5/6/7）：不虚构工具 Schema/菜单/价格/优惠/门店；LIVE/DEMO 必须分明，不得把演示数据说成实时查询；实时查询失败要明确告知无法验证数据，话术模板给出三种后续选项；
5. **追问纪律**（要求 8/9）：模糊/缺失/冲突的处理表格——地点模糊追问地址、人数缺失可按 1 人规划并注明、预算缺失不追问、冲突明示处理方式；**RPG 设定缺失不追问**；
6. **操作边界**（要求 10）：红线章节列明 create-order/cancel-order/auto-bind-coupons/available-coupons/draw-lottery 等写操作不调用（TOOLS_RESERVED 补登记 query-lottery-info/draw-lottery/query-my-prizes 三个官方真实工具，与文档红线一致）；
7. **事实与文案分离**（要求 11）：输出格式固定结构 + LIVE/DEMO 标注 + 未核验/缺失显「未知」；
8. **五个对话示例**（要求 12）：预算受限（¥12→最接近 ¥13 超 ¥1）/两人用餐（¥91-5=¥86）/营养偏好（低卡 540 千卡）/地点模糊（简洁追问）/MCP 暂时不可用（诚实失败+三个选项）。**示例数字全部用引擎实际输出校正**（初版凭印象写的 ¥20/¥37 与引擎不符，已按实测修正——不编数字）；
9. **文档一致性测试**（test_skill_docs.py，20 用例）：工具名必须真实存在、必备章节齐全、红线条款在场、解析规则与 nlparser 一致（10 条抽查）、三个数值示例与引擎输出一致、调用链描述与 service.py 顺序一致、frontmatter 有效。测试抓到两处真实问题（draw-lottery 未登记、query-nearby-stores 带参数书写），已修复。

联调状态：**当前 WorkBuddy 会话未配置 mcd-mcp 连接器**（list_installed_plugins 仅 GitHub 且禁用）——按要求完成离线测试并标记联调阻塞，未伪造真实调用。此前经 Python SDK + 真实 Token 的端到端验证记录见 MCP_INTEGRATION.md §3.5/§3.7。

测试 214 → 234 全绿。

## 5.12 安全加固 + 安装可复现（2026-10-14）

**安全加固（审计四领域，修复 1 个真实漏洞）**：
- 凭据与隐私核查通过：Token 仅环境变量/构造传入；错误消息/审计 args/卡片/JSON/摘要全路径不泄露（FAKE_TOKEN 断言）；无静默持久化；.gitignore 补 *.log/*.tmp/*.bak；示例配置仅占位符；仓库无硬编码 Token（正则扫描）。
- **修复：Markdown 成就卡 XSS 缺口**——story（含用户地点）与 constraints.describe() 在 HTML 卡有转义但 MD 卡漏转义，多数 MD 渲染器透传内联 HTML。已补 html.escape + 回归测试（test_security.py 21 用例）。
- MCP 返回视为不可信数据：注入文本（ignore previous instructions/泄露 Token/script）只被 extract_api_json 当数据提取；携带注入指令时 Token 零泄露。
- 业务规则：价格未知≠0、券失败必明示、无库存断言、真实模式失败不降级 Mock（CLI 退出码 2 无演示数据泄漏）。
- 操作边界：完整流程只调只读工具（WRITE_TOOLS 允许 list）、重试不针对写操作。

**安装可复现（修复 1 个真实 bug）**：
- **修复：fixtures/demo_data.json 在包外**，FIXTURE_PATH 指向 `<site-packages>/../fixtures/`，pip install 后 demo 模式必坏。已移入包内 `burger_quest/data/demo_data.json` + pyproject package-data 声明。
- pyproject：补 keywords/classifiers/urls、package-data；requirements.txt 改为指向 `pip install -e ".[dev]"`（消除与 pyproject 的重复声明，单一事实来源）。
- **独立 venv 实测**：① 可编辑安装 `pip install -e .[dev]` → 数据在包内；② 构建 wheel 检查包含 burger_quest/data/demo_data.json；③ 第二个干净 venv 装 wheel，从 $HOME（仓库外）跑 `burger-quest demo` 与 `python -m burger_quest --version` 均正常，255 测试全过。
- README：安装节重写（三种入口+依赖分层），修复章节编号错位（成就卡预览 4.2→4.4、6.5→7 顺延），目录树补 data/，测试数 234→255。

测试 234 → 255 全绿。

## 5.13 CI + 演示材料 + README 展示优化（2026-10-15）

**GitHub Actions CI（.github/workflows/ci.yml）**：
- test job：Python 3.10-3.13 矩阵（fail-fast: false）→ install -e ".[dev]" → compileall → pytest 全量 → 打包健全性（wheel 必须含演示数据）→ git diff --check；
- quality job：secret scan（无硬编码 Token/示例配置仅占位符/参赛声明在位）→ skill 文档一致性 → 离线演示可复现；
- 不用个人 Token、不访问真实 MCP、不跳过失败测试；
- **三轮真实迭代才跑绿**（都是有价值的问题）：① API 增量推送漏了 2 个 commit 导致 wheel 断言失败 → 写了 tools/api_push.py 按 git blob 内容哈希全量同步（幂等）；② checkout 默认浅克隆没有 HEAD~ → fetch-depth: 2；③ 空白检查抓到卡片模板占位符空展开留下行尾空格 → render_html 统一消除 + 回归测试；
- 最终 run 38741854075：5 job 全绿后才加 CI 徽章（未伪造）。

**安装可复现加固**：演示数据移入包内（修复 pip install 后 demo 必坏的真 bug）；requirements.txt 改为指向 pyproject（单一事实来源）；独立 venv 三重实测（可编辑安装/wheel 内容/仓库外目录跑 CLI）。

**演示材料**：examples/run_demos.py 统一入口（8 场景，逐场景校验退出码与验证重点，实测 4/4 核心符合预期）；examples/demo.md 重写（用户输入/预期输出/验证重点/退出码四要素，失败场景列为核心场景）；examples/pitch.md（30 秒口播稿 + 三版介绍文案，忠于实现）。

**README 展示**：首屏成就卡预览图（真实运行产物）；架构图与数据流改为 Mermaid（与现行「算价探测前置」链路一致，替换过时 ASCII 图）；示例数字按当前引擎输出校正；补凭据安全提示与贡献小节；GitHub Topics 经 API 设置（mcp/model-context-protocol/python/ai-agent/mcdonalds/game/skill/chatbot/rpg）。

测试 255 → 257；CI 全绿；远端 main 与本地 HEAD 内容一致（api_push dry-run 0 差异）。

## 5.14 最终交付验收（2026-10-15，全部实跑）

**第一部分 · 真实 MCP 冒烟（只读，Token 经环境变量，日志脱敏）**：
- `burger-quest smoke` 8/8 PASS：连接 / list_tools（35 个工具，项目 8 个在位）/ now-time-info / query-nearby-stores（5 家候选）/ query-meals（114 编码）/ query-meal-detail（鸡薯双全盒）/ calculate-price（¥15.90，单位分）/ 安全自检（仅只读工具）；
- 真实端到端：`quest 预算50 想吃辣 --store-code 1420762` → 3 个可比较方案（龙焰鸡腿堡×2 ¥48 / 单堡 ¥24 / 麦辣鸡翅4块中套餐 ¥36.50），全部 calculate-price 权威核验；营养库无「龙焰鸡腿堡」记录 → 属性面板明确「未知」不猜测；
- 未调用任何写操作（create-order/cancel-order/auto-bind-coupons/draw-lottery 等）。

**第二部分 · 参赛文件**：README/CONTEST_DECLARATION/MCP_INTEGRATION/mcp-config.example.json/源码/测试/workbuddy.md 均在 GitHub 可访问；**CONTEST_DECLARATION.md 与官方仓库字节级一致**（已比对 M-China/mcd-developer-innovation-challenge，538 字符 sha256 匹配）；mcp-config.example.json 仅 `${MCD_MCP_TOKEN}` 占位符。

**第三部分 · 自动化验收**：pytest 257 passed；run_demos.py 8/8 场景行为符合预期；CLI --help exit 0 / 缺 Token exit 2 不回退；compileall + JSON 配置合法；git diff --check 干净；Token 扫描 0 命中；真实模式不降级 Mock 3 项专项测试通过；**干净克隆验收**（codeload 拉取官方 main tarball → 新 venv `pip install -e .[dev]` → 257 测试 → demo ¥34/剩¥1 → run_demos 全通过）。

**第四部分 · GitHub 与赛事状态**：
- 仓库公开 ✅；创建时间 2026-10-13（在 2025-12-25~2026-10-25 允许窗口内）；
- **报名已确认成功**：官方 Issue #633 有 M-China-Official 回复「您的作品已成功参赛」（2026-10-13T11:59:16Z）；
- 截止时间：报名及排名 2026-10-25 23:59；最终排名以 2026-10-26 00:00 采集数据为准；奖品信息提交截止 2026-11-14；
- 排行榜：RANKING.md（更新时间 2026-10-15 21:00）暂未列出本项目——官方定时采集，当前 Star=1（>0），等待下一次采集入榜；
- CI 真实成功记录：最近 4 次运行（493508de/507a8b6/d24b3ab/b695652f）全部 success，最新一次即当前远端 main。

**第五部分 · 发布**：全部改动已提交并推送（远端 main 与本地 HEAD 内容一致，api_push dry-run 0 差异；未 force push）。

## 5.15 最后一轮发布审计与修复（2026-10-15）

1. **文档流程矛盾修复**（MCP_INTEGRATION.md）：调用链图重画——「calculate-price 价格发现（名称+单价）」提前为第 4 步，「query-meal-detail 仅回退补名」降为 4b 步；调用量口径统一（旧 28 次明确标注为优化前基线，现行 7-15 次）；§3.5 端到端实测标注「优化前历史记录」；§3.7 复测更新为「0 次详情」；§2 工具表改为「8 核心 + 2 外送专用」并注明 query-nearby-stores 仅到店场景、外送门店来自 delivery-query-stores；
2. **README 场景歧义修复**：§4.2 首个示例改为到店场景（复制即用必然成立），新增「到店 vs 外送」工具链对照表（外送须已保存配送地址，没有则明确提示不伪造）；测试数统一 214→257；
3. **LIVE 历史快照语义**（cards.py/cli.py/service.py）：新增 snapshot 参数与 `--card-snapshot` 旗标——快照卡带「LIVE SNAPSHOT · 真实 MCP 历史实测」紫色徽章 + 生成时间 + 「金额仅代表该次查询返回，不代表当前价格/优惠/库存」声明；**红线守卫：演示数据即使加该标记也只显示 DEMO 徽章**（专项测试 + CLI 警告）；live_real 卡片用真实 MCP 重新生成为快照卡（2026-10-15 22:29 实测，龙焰鸡腿堡×2 ¥48，未改任何真实数据），预览图同步重生成；
4. **验证**：263 测试全绿（新增 6 个快照用例）/ 8 场景演示 / CLI 各路径 / diff --check / Token 扫描 0 / README 无死链缺图 / CONTEST_DECLARATION 与官方字节级一致且 git 历史仅初始提交；
5. **CI 复核**：公开仓库 ci.yml 在位，最新运行（9ac15a1，当前 main）success——共 5 次连续成功。
