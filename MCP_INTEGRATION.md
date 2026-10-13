# MCP 集成说明（MCP_INTEGRATION.md）

> 本文档说明 Burger Quest（麦门异世界）实际使用的麦当劳中国官方 MCP Server、工具、调用链与业务价值。

## 1. MCP Server

| 项目 | 值 |
| --- | --- |
| 服务名称 | 麦当劳中国 MCP Server |
| 接入地址 | `https://mcp.mcd.cn` |
| 传输协议 | Streamable HTTP |
| 认证方式 | 请求头 `Authorization: Bearer <MCP_TOKEN>` |
| Token 申请 | <https://open.mcd.cn/mcp>（手机号登录 → 控制台 → 激活） |
| 限流 | 每 Token 每分钟最多 600 次请求，超出返回 429 |
| 错误码 | 401（Token 无效/过期/缺失）、429（限流） |
| 服务范围 | 中国大陆地区（不含港澳台） |
| 官方文档 | <https://github.com/M-China/mcd-mcp-server> |

## 2. 实际使用的工具（8 个）

引擎通过 `burger_quest/mcp_base.py` 的 `MCPClient` 接口调用以下工具。官方文档发布工具清单与描述，未公布每个工具的详细 JSON Schema，因此客户端对字段做**防御式适配**（按多个常见键名尝试，取不到留空并提示，绝不编造）。

| 工具 | 用途 | 在调用链中的位置 |
| --- | --- | --- |
| `now-time-info` | 当前时间信息（用于时段上下文） | 第 1 步，失败不阻塞 |
| `query-nearby-stores` | 按地址查询附近门店 | 第 2 步，确定作战据点 |
| `query-meals` | 查询当前门店可售餐品（分类/编码/标签/价格） | 第 3 步，**真实菜单的唯一来源** |
| `query-meal-detail` | 按餐品编码查询套餐组成与可替换选项 | 预留扩展（MVP 在规划中以单品/套餐编码为单位） |
| `query-store-coupons` | 当前门店可用优惠券列表 | 第 4 步，券池来源之一 |
| `query-my-coupons` | 我的卡包优惠券 | 第 4 步，券池来源之二 |
| `list-nutrition-foods` | 常见餐品营养数据（能量/蛋白质/脂肪/碳水/钠/钙） | 第 5 步，低卡/高蛋白筛选依据 |
| `calculate-price` | 按商品列表（可含优惠券）计算金额、配送费、优惠与应付总价 | 第 6 步，**总价的唯一权威来源** |

### 预留但 MVP 不主动调用的工具

`available-coupons`（领券动作）、`auto-bind-coupons`（一键领券，会改变用户账户状态）、`create-order` / `cancel-order` / `query-order`（订单操作）、`mall-*` / `query-lottery-info` / `draw-lottery`（商城与抽奖，涉及积分扣减）。这些工具具有实际影响，按比赛要求「真实下单或其他具有实际影响的操作必须等待用户明确确认」，本仓库不接入，仅在代码 `TOOLS_RESERVED` 中登记边界。

## 3. 调用链（一次完整任务）

```
用户一句话
  │
  ├─(0) nlparser 解析：预算/口味/营养偏好/门店/用餐方式/心情/丰盛偏好
  │
  ├─(1) now-time-info ────────────────► 时间上下文（失败 → warning，不阻塞）
  │
  ├─(2) query-nearby-stores(address) ─► 门店列表，取最近一家（可在需求中指定门店编码）
  │      └─ 失败 → 明确提示「未确定门店」，停止生成（不猜门店）
  │
  ├─(3) query-meals(storeCode) ───────► 真实可售菜单
  │      └─ 失败 → 明确提示「菜单查询失败」，停止生成（不编造餐品）
  │      └─ 价格缺失的餐品 → 从组合中排除并提示（不估算价格）
  │
  ├─(4) query-store-coupons + query-my-coupons ► 真实券池（失败 → 按无券计算并提示）
  │
  ├─(5) list-nutrition-foods ─────────► 营养数据（失败 → 不展示营养，不影响方案）
  │
  ├─(6) planner 生成候选组合（模板：单人饱腹/轻食低卡/省钱/多人分享/丰盛飨宴）
  │      └─ 预算预筛（估算价 + 配送费 + 25% 余量，控 MCP 调用次数）
  │
  ├─(7) calculate-price × N ──────────► 每个候选组合验价（无券 / 单券 / 门店券×我的券）
  │      └─ 全部失败 → 不出方案（不用菜单价加总冒充权威价）
  │
  ├─(8) 预算过滤 + 排序 → PlanOption[]
  │
  └─(9) rpg 文案 + cards 分享卡（含非官方声明与数据来源）
```

**调用量控制**：候选组合上限 6 个 × 每个最多 3 次验价 ≈ 18 次 `calculate-price`，加其余工具约 25 次/任务，远低于 600 次/分钟限流。

## 4. 业务价值

1. **把「查菜单」变成「做决策」**：MCP 提供数据，Burger Quest 提供决策——预算约束下的多组合对比、优惠券叠加验算、营养偏好筛选，直接给出可执行方案；
2. **可信的价格叙事**：所有金额可追溯（`data_sources` + `mcp_calls` 审计），把 MCP 的权威计算转化为用户看得懂的「原价/优惠/配送费/应付/剩余预算」五段式结算；
3. **游戏化提升完成率**：RPG 任务书把点餐决策包装成冒险任务，分享卡（含非官方声明）自带传播属性；
4. **错误透明**：MCP 无响应/数据缺失/预算无解三种失败路径都有明确文案，绝不伪造查询结果——这是接入第三方交易类 MCP 时的必要工程设计。

## 5. 在 WorkBuddy 中接入（推荐路径）

比赛官方推荐使用 WorkBuddy 开发与运行 Skill。WorkBuddy 作为 MCP Client 直连麦当劳 MCP Server，Skill（skill.md）指导 AI 按上述调用链编排工具，**无需自行重复实现 MCP Client**：

1. WorkBuddy → 左侧边栏【专家·技能·连接器】→【连接器】→【自定义连接器】→【配置 MCP】；
2. 粘贴 `mcp-config.example.json` 内容，把 `${MCD_MCP_TOKEN}` 替换为真实 Token（仅保存在你的 WorkBuddy 连接器配置中）；
3. 启用 `mcd-mcp` 连接器，安装本仓库 `skill.md` 为技能；
4. 对话框中直接描述需求，如：「用麦门异世界帮我规划：预算 40，想吃辣，外送到南山科技园」。

独立运行的 Python 引擎（`python -m burger_quest quest`）使用官方 `mcp` Python SDK 做同样的 Streamable HTTP 调用，用于可复现演示、自动化测试与脱离宿主环境的集成。

## 6. 数据处理与隐私

- MCP Token 只从环境变量或宿主连接器配置读取，不落库、不写日志、不进 Git（`mcp-config.example.json` 仅含占位符）；
- 引擎不存储任何用户数据；任务输出中的审计记录会自动剔除 token/authorization 字段；
- 涉及用户账户的工具（我的券、积分、订单）只读，MVP 不做任何写操作。
