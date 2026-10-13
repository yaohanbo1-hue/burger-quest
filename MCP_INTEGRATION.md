# MCP 集成说明（MCP_INTEGRATION.md）

> 本文档说明 Burger Quest（麦门异世界）实际使用的麦当劳中国官方 MCP Server、工具、调用链与业务价值。
> **所有工具参数与返回结构均于 2026-10-13 用官方 Token 联调实测**（`list_tools` 官方 Schema + 真实调用验证），实测记录见 §3.5。

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

引擎通过 `burger_quest/mcp_base.py` 的 `MCPClient` 接口调用以下工具。

| 工具 | 用途 | 在调用链中的位置 |
| --- | --- | --- |
| `now-time-info` | 当前时间信息（时段上下文） | 第 1 步，失败不阻塞 |
| `delivery-query-addresses` | 用户已保存的配送地址（取 addressId/beCode） | 第 2 步（仅外送） |
| `delivery-query-stores` | 某配送地址可配送的门店（含 beCode） | 第 2 步（仅外送） |
| `query-nearby-stores` | 按 city/keyword 查询附近门店（searchType=2） | 第 2 步，确定作战据点（多家候选时列出待选） |
| `query-meals` | 查询门店可售餐品**骨架**（分类/编码/标签） | 第 3 步，**真实菜单的唯一来源** |
| `query-meal-detail` | 按编码查餐品详情（名称/套餐 rounds） | 第 4 步，**名称的唯一来源** |
| `query-store-coupons` | 当前门店可用优惠券列表 | 第 5 步，券池来源之一 |
| `query-my-coupons` | 我的卡包优惠券 | 第 5 步，券池来源之二 |
| `list-nutrition-foods` | 常见餐品营养数据（能量/蛋白质/脂肪/碳水/钠/钙） | 第 6 步，低卡/高蛋白筛选依据 |
| `calculate-price` | 按商品列表（可含优惠券）计算金额、配送费、优惠与应付总价 | 第 7/9 步，**价格与总价的唯一权威来源** |

### 预留但 MVP 不主动调用的工具

`available-coupons` / `auto-bind-coupons`（领券动作，会改变用户账户状态）、`delivery-create-address`（写操作）、`create-order` / `cancel-order` / `query-order`（订单操作）、`mall-*` / `query-lottery-info` / `draw-lottery`（商城与抽奖，涉及积分扣减）。这些工具具有实际影响，按比赛要求「真实下单或其他具有实际影响的操作必须等待用户明确确认」，本仓库不接入，仅在代码 `TOOLS_RESERVED` 中登记边界。

## 3. 调用链（一次完整任务，联调实测）

```
用户一句话
  │
  ├─(0) nlparser 解析：预算/口味/营养偏好/门店/用餐方式/心情/丰盛偏好
  │
  ├─(1) now-time-info ────────────────► 时间上下文（失败 → warning，不阻塞）
  │
  ├─(2) 门店与场景确定：
  │      ├─ 外送场景：delivery-query-addresses → delivery-query-stores（拿 beCode）
  │      │    └─ 无保存地址 → 明确说明并停止（不假装能外送）
  │      ├─ 到店场景：query-nearby-stores(beType=1, searchType=2, city/keyword)
  │      │    ├─ 0 家 → 明确告知，请补充地址或提供编码（不猜门店）
  │      │    ├─ 1 家 → 使用
  │      │    └─ 多家 → 列出全部候选，等用户用 --store-code 指定（不编造、不默默选择）
  │      └─ 用户给了门店编码 → 到店场景直接用
  │
  ├─(3) query-meals(storeCode, orderType, beType) ► 菜单骨架（只有编码/分类/标签）
  │      └─ 失败 → 明确提示「菜单查询失败」，停止生成（不编造餐品）
  │
  ├─(4) 短名单（每分类 Top3、共 ≤10 个）逐个 query-meal-detail(code) ► 餐品名称
  │      └─ 拿不到名称 → 跳过；全部失败 → 停止（不编造餐品名）
  │
  ├─(5) query-store-coupons + query-my-coupons ► 真实券池（失败 → 按无券计算并提示）
  │
  ├─(6) list-nutrition-foods ─────────► 营养数据（失败 → 不展示营养，不影响方案）
  │
  ├─(7) calculate-price（短名单全部商品各 1 件）► 价格发现：
  │      从 productList 的 subtotal 取每件商品真实单价（单位：分）
  │      └─ 失败 → 不出方案（没有权威价格就不编造）
  │
  ├─(8) planner 生成候选组合（模板：单人饱腹/轻食低卡/省钱/多人分享/丰盛飨宴）
  │      └─ 预算预筛（真实单价 + 配送费估算 + 25% 余量，控 MCP 调用次数）
  │
  ├─(9) calculate-price × N ──────────► 每个候选组合验价（无券 / 单券 / 门店券×我的券）
  │      └─ 全部失败 → 不出方案（不用单价加总冒充权威总价）
  │
  ├─(10) 预算过滤 + 排序 → PlanOption[]
  │
  └─(11) rpg 文案 + cards 分享卡（含非官方声明与数据来源）
```

**调用量控制**：短名单 ≤10 次 `query-meal-detail` + 1 次价格发现 + 候选组合上限 6 个 × 每个最多 3 次验价 ≈ 28 次/任务，远低于 600 次/分钟限流。

## 3.5 联调实测记录（2026-10-13，官方 Token）

### SDK 与网络（mcp 1.27.2）

| 版本差异 | 处理方式（`mcp_http.py`） |
| --- | --- |
| 导出名：旧版 `streamablehttp_client` / 新版 `streamable_http_client` | `_import_stream_client()` 双兼容 |
| 请求头：旧版工厂收 `headers=` / 新版只收 `http_client=` | 按工厂签名分流；新版自建 `httpx2.AsyncClient` 带 Authorization |
| transport yield：旧版 3 元组 / 新版 2 元组 | 只取前两个值 |
| 新版 SDK 把 401/429 包装成 JSON-RPC 错误且不暴露状态码 | 自建 http_client 挂 event hook 记录 HTTP 状态码，还原 401/429 语义 |

实测：`https://mcp.mcd.cn` 网络可达；无效 Token 返回 HTTP 401 → 客户端输出「MCP Token 无效或已过期（401）」友好错误（退出码 2）。

### 工具参数（`list_tools` 官方 Schema + 真实调用验证）

| 工具 | 必填参数（实测） | 关键可选参数 |
| --- | --- | --- |
| `query-nearby-stores` | `beType`(1到店自提/5得来速)、`searchType`(2按位置) | `city`、`keyword`（至少一个） |
| `query-meals` | `storeCode`、`orderType`(1到店/2外送)、`beType`(1到店取餐/2麦乐送/5得来速/6团餐) | `beCode`（外送必传） |
| `query-meal-detail` | `storeCode`、`orderType`、`beType`、`code`（餐品编码） | `beCode` |
| `query-store-coupons` | `orderType`、`beType`、`storeCode` | `beCode` |
| `query-my-coupons` | 无 | `page`、`pageSize` |
| `calculate-price` | `storeCode`、`orderType`、`beType`、`items[]`（`productCode`+`quantity`） | `items[].couponId`（券挂商品上）、`beCode` |
| `list-nutrition-foods` / `now-time-info` | 无 | — |
| `delivery-query-addresses` | 无 | — |
| `delivery-query-stores` | `beType`(2麦乐送/6团餐)、`addressId` | — |

### 返回结构（实测）

1. **文本信封**：字段说明 + `## Original Response` + `{"success":true,"code":200,...,"data":...}` + 传参规则说明；`success=false` 时 `message` 是业务错误（如「外送场景下，beType=2/5/6时必传beCode」）→ 客户端转成可读 MCPError；
2. **query-meals 只返回编码**：`data.categories[].meals[].code + tags[]`，**无名称、无价格**——这是服务端采用「详情补名 + 算价发现」链路的原因；
3. **query-meal-detail**：`data={code,name,rounds[],modification,supportModify}`；套餐的 rounds 含换选项（choices[].diffPrice）；
4. **calculate-price**：`data={price,originalPrice,discount,productList[{productCode,productName,quantity,subtotal}],takeWayList[]}`，**金额单位为分**（实测麦辣鸡腿汉堡 2250 = ¥22.50）；`productList` 自带餐品名称；
5. **list-nutrition-foods**：`data` 是自定义文本 `[N]{productName,...,energyKcal,...}:` + CSV 行，按表头列序解析；
6. **query-my-coupons**：无券时返回纯文本「暂无可用优惠券」（无 JSON）→ 适配器返回空列表。

### 端到端实测（真实 Token，2026-10-13）

| 场景 | 命令 | 实测结果 |
| --- | --- | --- |
| 多候选门店 | `quest "我想吃辣，预算35，在深圳市南山区科技园吃"` | 找到 5 家真实门店，列出名称+编码并暂停，未查询菜单 |
| 到店完整链路 | `quest "我想吃辣，预算35" --store-code 1420762` | 112 个菜单编码 → 10 个详情 → 10 个真实价格；产出「龙焰鸡腿堡 ¥24，剩 ¥11」 |
| 多人丰盛 | `quest "两个人想吃点好的，预算100，堂食" --store-code 1420762` | 学生专享汉堡小食随心选 + 麦乐鸡经典中套餐 = ¥52 |
| 外送无地址 | `quest "外送到深圳市南山区科技园，预算40"` | 明确说明「需要先在官方 App 保存配送地址」，停止且不编造 |

## 3.6 仍未验证项（需要特定账户状态）

1. **优惠券字段与金额单位**：联调账户当时无可用券（`query-store-coupons` 返回空、`query-my-coupons` 无券）；`items[].couponId` 挂载方式与门槛/折扣字段按官方 Schema 与惯例实现（金额按「元」解析），有待有券账户验证；
2. **外送全链路**：需要账户已保存配送地址（联调账户无地址，已实测「无地址」分支的明确说明）；
3. **套餐换选 / 特制选项**（`rounds` 换项、`modification` 特制键）：MVP 不展示，接口已预留。

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
