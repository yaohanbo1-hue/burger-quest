# MCP 集成说明（MCP_INTEGRATION.md）

> 本文档说明 Burger Quest（麦门异世界）实际使用的麦当劳中国官方 MCP Server、工具、调用链与业务价值。
> **所有工具参数与返回结构均于 2026-10-13 用官方 Token 联调实测**（`list_tools` 官方 Schema + 真实调用验证），实测记录见 §3.5。
>
> **文档性质说明（2026-10-21）**：本文是开发期（2026-10-13 ~ 10-20）的集成实测与验证记录。文中提及的参考引擎及其自动化测试套件曾随开发过程存在并全部跑绿；2026-10-21 本仓库转型为**纯 Skill 交付**（只保留 SKILL.md 与证据文档），引擎与测试不再随仓库分发——记录完整保留，作为「26 个只读工具全部经过真实 Token 端到端验证」的证据。

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

## 2. 实际使用的工具（引擎 11 只读 + Skill 支线 15 只读 + 9 预留，覆盖官方 35 个）

引擎通过 `burger_quest/mcp_base.py` 的 `MCPClient` 接口调用以下工具。
**场景不同，实际使用的工具并不必然完全相同**（以代码 `TOOLS_USED` 注册表为权威，共 11 个）。

除引擎外，WorkBuddy Skill（`SKILL.md` §10）经 `mcd-mcp` 连接器**直调 15 个支线只读工具**
（`TOOLS_SKILL_READONLY`，2026-10-19 真实 Token 实测，契约见 §3.15）——两层合计 **26 个只读工具**
+ `TOOLS_RESERVED` 9 个（写操作 7 + 外送链路 2），官方 35 个工具全覆盖、零虚构：

- **11 个引擎在用只读工具**（`TOOLS_USED`）：7 个常规主链路 + 1 个详情回退 + 3 个扩展只读（`campaign-calendar` / `available-coupons` / `query-my-account`，只在对应功能里调用）。常规主链路**并非每个任务都会用到全部**——6 个场景通用（`now-time-info` / `query-meals` / `query-store-coupons` / `query-my-coupons` / `list-nutrition-foods` / `calculate-price`）、1 个仅到店场景（`query-nearby-stores`；外送门店查询未接入，见下）、1 个按需回退（`query-meal-detail`，菜单骨架缺名称时补名，实测通常 0 次）；
- **15 个 Skill 支线只读工具**（`TOOLS_SKILL_READONLY`，**不由引擎调用**）：麦享会宝库（积分/商城/抽奖）、派对据点侦查、团餐公会、冒险日志（历史订单）四条支线；参数契约与实测结果见 §3.15 与 `SKILL.md` §10；
- **1 个外送专用工具**（`delivery-query-addresses`，登记在 `TOOLS_RESERVED`）：仅外送场景使用，且需要账户已保存配送地址；账户无保存地址时程序明确提示并停止。
- **预留未接入**：`delivery-query-stores`（地址→可配送门店）官方 Schema 已确认（必填 `beType`+`addressId`），但账户无保存地址、端到端不可用，登记于 `TOOLS_RESERVED`。程序明确提示「外送门店查询能力未接入，请使用官方 App 下单」，不查门店、不发起算价、不伪造外送价格。

| 工具 | 用途 | 在调用链中的位置 |
| --- | --- | --- |
| `now-time-info` | 当前时间信息（时段上下文） | 第 1 步，失败不阻塞 |
| `query-nearby-stores` | 按 city/keyword 查询附近门店（searchType=2，**仅到店场景**） | 第 2 步，确定作战据点（多家候选时列出待选） |
| `query-meals` | 查询门店可售餐品**骨架**（分类/编码/标签） | 第 3 步，**真实菜单的唯一来源** |
| `calculate-price` | 按商品列表（可含优惠券）计算金额、配送费、优惠与应付总价 | 第 4 步**价格发现**（名称+单价）+ 第 8 步**组合验价**，**价格与总价的唯一权威来源** |
| `query-meal-detail` | 按编码查餐品详情（名称/套餐 rounds） | 第 4b 步，**仅回退补名**（`productList` 缺名称时；实测通常 0 次） |
| `query-store-coupons` | 当前门店可用优惠券列表 | 第 5 步，券池来源之一 |
| `query-my-coupons` | 我的卡包优惠券 | 第 5 步，券池来源之二 |
| `list-nutrition-foods` | 常见餐品营养数据（能量/蛋白质/脂肪/碳水/钠/钙） | 第 6 步，低卡/高蛋白筛选依据 |
| *（外送专用）* `delivery-query-addresses` | 用户已保存的配送地址（取 addressId/beCode） | 第 2 步，**仅外送场景**；无地址 → 明确说明并停止 |
| `delivery-query-stores` | 地址→可配送门店（必填 `beType`+`addressId`） | **预留未接入**（账户无保存地址，端到端不可用；§2） |

> 注：`query-nearby-stores` 是**到店场景**的位置查询（`beType=1`）；外送场景的门店来自
> 已保存地址仅用于提示用户（本项目不做外送门店查询），到店场景用 `query-nearby-stores`，两者不可互相替代。

### 预留但不接入的工具（TOOLS_RESERVED，9 个）

按比赛规则「真实下单或其他具有实际影响的操作必须等待用户明确确认」，以下工具一律不接入，仅在代码 `TOOLS_RESERVED` 中登记边界：

- **写操作（7）**：`auto-bind-coupons`（领券改变账户券资产；`available-coupons` 已于 2026-10-18 以**只读查询**接入，不领取）、`delivery-create-address`（写地址簿）、`create-order` / `cancel-order`（真实订单）、`mall-create-order`（扣积分/现金兑换）、`party-order-create`（真实派对预约）、`draw-lottery`（原子扣积分/次数）；
- **外送链路（2）**：`delivery-query-addresses` / `delivery-query-stores`——契约已确认但账户无保存地址，端到端不可用（`delivery-query-addresses` 的「无地址」诚实失败路径已如实验证）。

> 注：`query-order` / `order-list` / `mall-order-list` / `mall-order-detail` / `query-lottery-info` / `query-my-prizes` 等**只读**工具已由 Skill 支线覆盖（§3.15），不再属于「预留」——预留清单只保留写操作与端到端不可用项。

## 3. 调用链（一次完整任务，联调实测）

```
用户一句话
  │
  ├─(0) nlparser 解析：预算/口味/营养偏好/门店/用餐方式/心情/丰盛偏好
  │
  ├─(1) now-time-info ────────────────► 时间上下文（失败 → warning，不阻塞）
  │
  ├─(2) 门店与场景确定：
  │      ├─ 外送场景：delivery-query-addresses → 明确提示外送门店查询未接入（不做算价）
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
  ├─(4) calculate-price（短名单全部商品各 1 件，不带券）► 价格发现（2026-10-14 优化后）：
  │      从 productList 同时取 productName（名称）与 subtotal（真实单价，单位：分）
  │      └─ 失败 → 不出方案（没有权威价格就不编造）
  │
  ├─(4b) 仅当 productList 未返回名称的商品：逐个 query-meal-detail(code) 回退补名
  │      └─ 拿不到名称 → 跳过；全部失败 → 停止（不编造餐品名）
  │      （实测通常 0 次调用；优化前此步为全量 ≤10 次，见 §3.8 优化记录）
  │
  ├─(5) query-store-coupons + query-my-coupons ► 真实券池（失败 → 按无券计算并提示）
  │
  ├─(6) list-nutrition-foods ─────────► 营养数据（失败 → 不展示营养，不影响方案）
  │
  ├─(7) planner 生成候选组合（模板：单人饱腹/轻食低卡/省钱/多人分享/丰盛飨宴）
  │      └─ 预算预筛（真实单价 + 配送费估算 + 25% 余量，控 MCP 调用次数）
  │
  ├─(8) calculate-price × N ──────────► 每个候选组合验价（券试探：最优单券→最优双券→全拒回退无券）
  │      └─ 全部失败 → 不出方案（不用单价加总冒充权威总价）
  │
  ├─(9) 预算过滤 + 排序 → PlanOption[]
  │
  └─(10) rpg 文案 + cards 分享卡（含非官方声明与数据来源）
```

**调用量控制**（2026-10-14 优化后实测）：基础 5-6 次（时间/门店/菜单/价格发现/券×2/营养）+ 组合验价
（候选 ≤6 个 × 每组合 ≤2 次券试探）≈ **单任务 7-15 次**（演示最坏 15、真实典型 7），远低于 600 次/分钟限流。
优化前为「≤10 次详情补名 + 每组合 ≤8 次券试探 ≈ 28 次/任务」——新旧差异与优化手段详见 §3.8；
调用量数据开发期由插桩脚本实测（该脚本随引擎一同停止分发，记录保留在此）。

> **缓存边界**：`calculate-price` 与优惠券查询**绝不缓存**（价格/券池实时变，必须当次核验）；
> 只读查询（时间 30s / 门店 2min / 菜单与详情 5min / 营养 10min）做任务内去重与短 TTL 缓存。

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
| `calculate-price` | `storeCode`、`orderType`、`beType`、`items[]`（`productCode`+`quantity`） | `items[].couponId` **+ `items[].couponCode`**（券挂商品上、两字段必须成对提交，见 3.9）、`beCode` |
| `list-nutrition-foods` / `now-time-info` | 无 | — |
| `delivery-query-addresses` | 无 | — |
| ~~`delivery-query-stores`~~ | — | 已移除 |

### 返回结构（实测）

1. **文本信封**：字段说明 + `## Original Response` + `{"success":true,"code":200,...,"data":...}` + 传参规则说明；`success=false` 时 `message` 是业务错误（如「外送场景下，beType=2/5/6时必传beCode」）→ 客户端转成可读 MCPError；
2. **query-meals 只返回编码**：`data.categories[].meals[].code + tags[]`，**无名称、无价格**——名称与单价改由一次 `calculate-price` 探测的 `productList` 同时获取（2026-10-14 优化），`query-meal-detail` 仅在 `productList` 缺名称时回退；
3. **query-meal-detail**：`data={code,name,rounds[],modification,supportModify}`；套餐的 rounds 含换选项（choices[].diffPrice）；
4. **calculate-price**：`data={price,originalPrice,discount,productList[{productCode,productName,quantity,subtotal}],takeWayList[]}`，**金额单位为分**（实测麦辣鸡腿汉堡 2250 = ¥22.50）；`productList` 自带餐品名称；
5. **list-nutrition-foods**：`data` 是自定义文本 `[N]{productName,...,energyKcal,...}:` + CSV 行，按表头列序解析；
6. **query-my-coupons**：无券时返回纯文本「暂无可用优惠券」（无 JSON）→ 适配器返回空列表。

### 端到端实测（真实 Token，2026-10-13；**优化前历史记录**，调用链与次数已被 §3.8 优化改变）

| 场景 | 命令 | 实测结果 |
| --- | --- | --- |
| 多候选门店 | `quest "我想吃辣，预算35，在深圳市南山区科技园吃"` | 找到 5 家真实门店，列出名称+编码并暂停，未查询菜单 |
| 到店完整链路 | `quest "我想吃辣，预算35" --store-code 1420762` | 112 个菜单编码 → 10 个详情 → 10 个真实价格；产出「龙焰鸡腿堡 ¥24，剩 ¥11」 |
| 多人丰盛 | `quest "两个人想吃点好的，预算100，堂食" --store-code 1420762` | 学生专享汉堡小食随心选 + 麦乐鸡经典中套餐 = ¥52 |
| 外送无地址 | `quest "外送到深圳市南山区科技园，预算40"` | 明确说明「需要先在官方 App 保存配送地址」，停止且不编造 |

## 3.6 仍未验证项（需要特定账户状态）

1. **优惠券的门槛/折扣金额单位与真实优惠实效**：联调账户无可用券（`query-store-coupons` 返回空、`query-my-coupons` 为「暂无可用优惠券」）。已验证的是**提交契约**——官方要求 `couponId` + `couponCode` 成对提交，只传 ID 报 600010（见 §3.9）；**未验证**的是券的门槛/折扣字段单位（当前按「元」解析）与「有券时优惠是否真实生效」。需要一张有效门店券或有券账户才能结案；
2. **外送全链路**：需要账户已保存配送地址（联调账户无地址，已实测「无地址」分支的明确说明）；
3. **套餐换选 / 特制选项**（`rounds` 换项、`modification` 特制键）：MVP 不展示，接口已预留。

## 3.7 集成复查记录（2026-10-14，阶段 5：MCP 集成专项审查）

本次复查用真实 Token 经**项目自己的客户端**（`HttpMCPClient`，官方 mcp Python SDK 薄封装）
重新做了一轮工具发现与链路验证，并修补了两处协议层缺口。

### 官方 SDK 核实（无自研协议）

- 代码审计确认：`burger_quest/` 内**没有任何**裸 HTTP 实现（无 `requests`/`urllib`/`aiohttp`）；
  唯一 HTTP 相关 import 是 `httpx2`（SDK 自带），仅用于给官方 `streamable_http_client`
  工厂构造带 Authorization 的 `AsyncClient`——传输仍由官方 SDK 完成；
- 实测环境 SDK 已从联调时的 **1.27.2 升级到 2.3.0**，逐项复核兼容层：

| 2.3.0 实测 | 兼容层处理 |
| --- | --- |
| 导出名只剩 `streamable_http_client` | `_import_stream_client()` 先试旧名再试新名，实测取到新名 |
| 工厂签名 `(url, http_client, terminate_on_close, max_sse_event_size)`，无 `headers` | `_transport_kwargs` 按签名分流，实测走 `http_client` 分支 |
| transport yield `(read, write)` 2 元组 | 只取前两个值，实测通过 |
| `list_tools()` 返回 `ListToolsResult` 模型（1.x 是裸 list） | `list_tools()` 双形态兼容（`.tools` / list） |
| `call_tool` 返回 `CallToolResult`，`.content` 不变 | `parse_tool_text` 继续可用 |

### 工具发现（`list_tools` 实测，35 个工具）

官方清单共 **35 个工具**；项目 `TOOLS_USED`（11 个）与 `TOOLS_RESERVED`（10 个）全部在列。
本次逐个核对了 11 个在用工具的官方输入 Schema，与 `mcp_http.py` 实际传参**完全一致**
（含 `query-meal-detail` 的编码参数名是 `code`、`calculate-price` 的
`items[].productCode/quantity/couponId`、`query-nearby-stores` 的 `beType+searchType` 组合）。
另发现官方 Schema 明确约定：**到店自提场景（orderType=1）传 beCode 会报错**——
客户端按 `scene.be_code` 为空即不传的方式已天然规避。

### 两条错误通道（本次补齐的缺口）

| 通道 | 形态 | 处理 |
| --- | --- | --- |
| 业务错误 | 文本信封 `success=false`（实测：缺参 → code=400「缺少参数」；`DEMO-001` → 「storeCode门店编码必须全部由数字组成」） | `check_envelope` → 可读 MCPError（原有） |
| 协议错误 | SDK/协议层 `isError=True` / JSON-RPC 异常（实测：未知工具名 → `McpError: unknown tool`） | **本次新增**：`_call()` 检查 `is_error`/`isError`，有错即抛 MCPError 并记审计，绝不让未知形态静默流入适配器 |

原则不变：**不允许「任何未知格式都猜一个价格/名称」后继续往下走**——算价缺总价即
`ok=False`，服务层据此停止出方案。

### 只读冒烟测试入口（新增）

```bash
export MCD_MCP_TOKEN="你的Token"
python -m burger_quest smoke            # 真实服务：连接→工具发现→时间→门店→菜单→详情→算价
python -m burger_quest smoke --demo     # 离线演示（无需 Token，可重复执行，CI 友好）
python -m burger_quest smoke --store-code 1420762 --json result.json
```

- 八步只读检查，逐步输出 PASS/FAIL；退出码 0=全过 / 1=有失败 / 2=配置错误；
- Token 只从环境变量读，**绝不打印**、不接受命令行参数；
- 末尾「安全自检」断言全过程只调用了只读工具（写操作工具入账即判失败）；
- 实测（2026-10-14，SDK 2.3.0 + 真实 Token）：**8/8 通过**——35 工具全发现、
  5 家候选门店、110 个菜单编码、`9900016076 -> "我就喜欢"鸡薯双全盒 ¥15.90`；
- Mock 测试同步覆盖：全通过 / 某步失败 / 无门店时下游不编造 / 仅只读红线 / 连接失败 / 缺 Token。

### 端到端复测（2026-10-14，SDK 2.3.0）

`quest "我想吃辣，预算30，在深圳市南山区科技园，一个人，到店自取" --store-code 1420762`
→ 110 个菜单编码 → **0 次详情**（名称与单价取自一次 calculate-price 探测的 productList）→
「龙焰鸡腿堡 ¥24.00，剩 ¥6.00」，160 条营养数据，0 张券如实说明。SDK 升级未破坏任何链路。

## 3.8 调用效率与可靠性优化（2026-10-14，实测基线后实施）

基线实测（开发期插桩脚本，单任务 **演示 24 次 / 真实 17 次 3.5s**）：
其中 `query-meal-detail` 10 次占真实耗时约 50%，组合验价的券试探最多 8 次/组合。

### 优化手段（均不以牺牲价格真实性为代价）

| 手段 | 做法 | 效果 |
| --- | --- | --- |
| 名称/单价一次拿 | 先对短名单发一次 `calculate-price`，从 `productList` 取 `productName` 与 `subtotal`；`productList` 缺名称才回退 `query-meal-detail` | 详情 10 次 → 0-2 次 |
| 券试探封顶 | 每个组合最多 2 次券试探（预期折扣最优单券 → 最优双券）；全部被权威核验拒绝才回退无券；无券方案在同餐品下被叠券方案严格支配，不再无条件探基线。**最优性边界：不保证全局最优**——理论上可能遗漏更优的第三张券或非最高折扣组合；引擎追求「预算内真实可付的较优解」而非穷举，实际折扣一律以权威核验为准 | 每组合 ≤8 次 → ≤2 次 |
| 任务内去重 + 短 TTL 缓存 | 相同输入的只读查询在 TTL 内直接复用；TTL：时间 30s / 门店 2min / 菜单与详情 5min / 营养 10min | 重复查询归零 |
| 分工具超时 | 默认 30s，`calculate-price` 45s（最重） | 单次调用不卡死任务 |
| 有限退避重试 | 瞬时网络错误/5xx 最多 3 次（0.5s/1s/2s）；429 最多 2 次（2s/4s，降速尊重限流）；**401 不重试**（直接提示检查 Token）；业务错误与协议错误不重试 | 临时故障不废掉整个任务 |
| 调用审计 | 每次调用记录 seq/耗时/用途/成功状态/尝试次数；去重命中标记 `mcp_call=False`；args 脱敏 | 输出末尾【MCP 调用审计】可查 |

### 硬性边界（绝不为了少调用而破坏真实性）

- `calculate-price` **绝不缓存**——每个最终方案都必须按当前餐品实时核验；
- 优惠券查询**绝不缓存**（券池/有效性实时变）；
- 失败结果**绝不进缓存**（不会把失败当成功复用）；
- 优惠券不默认可叠加：预期折扣与权威折扣不符时如实备注，实际优惠以权威返回为准；
- 失败不静默转换：算价失败不出方案，没有虚假零价/空券/“查询成功”。

### 优化后实测（同场景对比）

| 指标 | 优化前 | 优化后 |
| --- | --- | --- |
| 演示模式单任务 | 24 次 | 11-15 次（最坏 15，-40%） |
| 真实模式单任务 | 17 次 / 3.5s | **7 次 / 1.8s**（-59% / -43%） |
| 详情查询 | 10 次 | 0 次（真实模式实测省 10 次） |
| 方案质量 | — | 不变（同场景同权威总价） |

（复测脚本随引擎停止分发；以上为开发期实测记录。）

## 3.9 优惠券双字段契约（2026-10-16 真实联调发现）

真实只读联调中，官方 `calculate-price` 对只提交 `couponId` 的算价请求返回：

```
code=600010  使用优惠券需要couponId和couponCode
```

即 **官方要求 `items[].couponId` 与 `items[].couponCode` 同时提交**，缺一即整单拒绝（业务失败，不是静默忽略）。
只传 ID 的实现会让**有券账户的优惠永远不生效**。

当前实现：

| 层 | 处理 |
| --- | --- |
| `models.Coupon` | 新增 `code` 字段承载 `couponCode` |
| `adapt_coupons` | 从券响应解析 `couponCode` / `coupon_code`；**取不到就留空，绝不伪造** |
| `resolve_coupon_pair` | 统一 `Coupon` 对象 / `(id, code)` 元组 / 裸 id 三种形态 |
| `build_calc_payload` | 两字段齐备才写 `couponCode`；券只挂第一件商品 |
| `mcp_mock` | 忠实还原官方 600010——只传 ID 必须失败，**演示数据不比真实服务端宽松** |
| `demo_data.json` | 3 张演示券全部补 `couponCode` |
| 回归测试 | `tests/test_coupon_contract.py`（20 条协议契约测试） |

诚实性边界：**本组修复只验证「协议字段被正确提交」，未验证「优惠在真实账户上实际生效」**——
联调账户无可用券（`query-store-coupons` 空、`query-my-coupons` 返回「暂无可用优惠券」），
因此券的门槛/折扣金额单位（当前按「元」解析）仍属未验证项，见 §3.6 第 1 条。

## 3.10 失败路径矩阵（10 类，第二波收尾）

真实链路的失败路径原先分散在 5 个测试文件里，**漏掉任何一条都不会被发现**。
现将用户要求的 10 类固化为显式矩阵（`tests/test_failure_matrix.py`，28 条用例）：

| 编号 | 类别 | 关键断言 |
| --- | --- | --- |
| F01 | 非法参数 | 缺门店编码时入参原样为空，不伪造默认门店 |
| F02 | 缺字段 | 菜单条目缺 `code` 丢弃；算价缺 `productName` 不产生名称 |
| F03 | 业务错误 | `success=false` 信封（实测 600057 打烊）转可读 MCPError；无 message 给兜底文案 |
| F04 | 协议级错误 | `isError=True` / SDK 1.x `is_error` 别名均识别，不当成功响应解析 |
| F05 | 部分工具失败 | 详情/营养失败被隔离，规划照常且失败进审计 |
| F06 | 价格缺失 | 缺 `price` → `ok=False`；缺 `subtotal` → 无价格（非 0 元）；0/负价排除 |
| F07 | 响应结构变化 | 字段类型错误丢弃该条；营养坏行跳过；非 JSON 文本不抛未捕获异常 |
| F08 | 超时 | 转 MCPError 且文案含「超时」；网络错误不重试成成功；**401「无效」二字不被误判为商品错误** |
| F09 | 401 未授权 | code=401 + Token 提示，不重试（仅 1 次调用）；doctor 归类 401 并给出申请链接；**缺 Token 必须失败且不自动降级 Demo** |
| F10 | 429 限流 | code=429；重试次数少于网络/5xx；doctor 给出退避建议 |

三个元测试保证不退化：矩阵必须 10 类齐全、登记用例名必须真实存在（改名即失败）、每条用例必须有实际断言（防空壳充数）。

## 3.11 全链路体检记录（2026-10-17，真实 Token）

体检范围：环境变量读取 → HTTP 请求 → Bearer Token → Streamable HTTP → 会话管理 → 工具发现 → 工具调用 → 响应解析 → 业务层；连接超时 / 读取超时 / 401 / 403 / 429 / 5xx / 网络断开 / 无效 JSON / 工具不存在 / 参数校验错误 / 服务端业务错误逐项核对。原则：有合法 Token 才做只读真实验证，无条件如实标 BLOCKED，不伪造 PASS；真实模式失败不回退 Demo、不把模拟响应当真实成功。

### 五层验证状态（2026-10-17 15:44 真实 Token 实测）

| 层级 | 状态 | 证据 |
| --- | --- | --- |
| L1 服务可访问 | ✅ PASS | 未认证 POST `https://mcp.mcd.cn` → HTTP 403，TCP+TLS 1.4s 完成（服务可达 + 认证层正常；403 是网关对无 Token 请求的预期响应） |
| L2 认证成功 | ✅ PASS | doctor：Streamable HTTP 会话建立，Bearer Token 通过认证 |
| L3 工具发现成功 | ✅ PASS | `list_tools` 35 个工具，项目依赖的 11 个全部在位（SDK 2.3.0） |
| L4 单工具真实调用成功 | ✅ PASS | `now-time-info` 返回服务器时间 2026-10-17T15:44:45 |
| L5 业务流程真实跑通 | ✅ PASS | `quest "我想吃辣，预算35…" --store-code 1420762`：114 个菜单编码 → 价格发现（0 次详情）→ 组合验价 → 龙焰鸡腿堡 ¥24.00（calculate-price 权威）/ 预算 ¥35 剩 ¥11；8 次调用 2.5s；分享卡正常生成 |

### 失败路径真实核验（同一 Token，全部只读）

| 场景 | 真实结果 |
| --- | --- |
| 无效 Token | HTTP 401 → 「MCP Token 无效或已过期（401）」，退出码 2（重构 `_map_sdk_error` 后专项回归通过） |
| 无效门店编码 DEMO-001 | 官方业务错误 code=400「storeCode门店编码必须全部由数字组成」→ 可读 MCPError，明确停止（未编造餐品） |
| 外送无保存地址 | 明确说明需先在官方 App 保存配送地址，停止（不伪造外送能力） |
| 位置命中多家门店 | 5 家真实门店全部列出并暂停，不默默选择 |
| 预算内无解 | 如实失败 + 可操作建议（顺带修复诊断措辞，见下表 #6） |

### 本次体检修复（均不以牺牲价格真实性为代价）

| # | 问题（体检发现） | 修复 |
| --- | --- | --- |
| 1 | 连接超时与读取超时不分，笼统报「连接失败」（排查方向相反） | `httpx2.AsyncClient` 显式 `Timeout(connect=10/read=60/write=30/pool=10)`；read 必须 ≥ 最重工具外层等待（calculate-price 45s）——SDK 默认 5s 读取超时会提前误杀慢但正常的权威算价 |
| 2 | 403 无专门映射（实测无 Authorization 的 POST 正是 403，与 401 处置不同） | 单独映射 403 + WAF/权限排查建议；`doctor` 分类器同步 |
| 3 | 无信封纯文本响应（网关错误页/服务异常）被静默降级为空菜单/空门店——把协议故障误报成业务空结果 | 白名单（`query-my-coupons` / `delivery-query-addresses`，联调实测的空态纯文本）外强制 `MCPError`「响应解析失败」，保留返回开头片段 |
| 4 | 无 Token 时 doctor 完全看不到网络结论 | L1 增加 HTTP 级探针（**绝不携带 Token**）并提前到 Token 检查前；缺 Token 也给出「服务可达」实证 |
| 5 | 诊断无层级视角，用户分不清「通没通/连没连/能不能用」 | doctor 步骤打 L1–L4 级别标签 + 末尾五层验证状态汇总（L5 指向 `smoke`/`quest`，未执行如实标记） |
| 6 | 预算空结果措辞不实：全部候选止步预估筛时说「其余逐一用权威价格核验」（真实运行复现，暗示了没做的核验） | 按真实路径分化：全部预筛跳过 → 明示「未进入权威核验」；预筛+核验失败并存 → 两类数量如实并列 |

测试：**372 → 406**（新增 `tests/test_mcp_healthcheck.py` 35 例，更新 1 例随行为变更的旧用例），全量绿；真实链路复跑确认（无效 Token 401 / DEMO-001 业务错误 / 外送无地址 / 多家候选 / 预算无解均按预期）。

### 仍未验证项（状态更新）

1. **优惠券门槛/单位与真实生效**：本账户 `query-store-coupons` / `query-my-coupons` 2026-10-17 真实复测仍返回 0 张——维持未验证，需有券账户；
2. **外送全链路**：账户无保存地址（「无地址」诚实失败路径已验证）——维持未验证；
3. **多券提交语义**（双券试探是否被官方接受）：无券可试——维持未验证。注意：第三轮代码分析发现的「`build_calc_payload` 只提交 `coupons[0]`、mock 却按全组券算折扣」的演示/真实分叉**已于 2026-10-17 当日修复**（券按商品序号一一挂载 + `coupon_sets` 容量守卫 + mock 从 payload 实际提交的券推导折扣，修复记录见 §3.12 与 workbuddy.md §5.19）；**仍未验证的是服务端对同单多券的接受性**（需有券账户），验证前不得把演示叠券效果当作真实效果。

安全记录：本次体检 Token 仅经**进程级环境变量**传递（`MCD_MCP_TOKEN=… python -m …`），未写入任何文件、日志、卡片或审计；doctor/smoke/quest 输出与 JSON 诊断零 Token 泄露（有专项测试断言）。

## 4. 业务价值

1. **把「查菜单」变成「做决策」**：MCP 提供数据，Burger Quest 提供决策——预算约束下的多组合对比、优惠券叠加验算、营养偏好筛选，直接给出可执行方案；
2. **可信的价格叙事**：所有金额可追溯（`data_sources` + `mcp_calls` 审计），把 MCP 的权威计算转化为用户看得懂的「原价/优惠/配送费/应付/剩余预算」五段式结算；
3. **游戏化提升完成率**：RPG 任务书把点餐决策包装成冒险任务，分享卡（含非官方声明）自带传播属性；
4. **错误透明**：MCP 无响应/数据缺失/预算无解三种失败路径都有明确文案，绝不伪造查询结果——这是接入第三方交易类 MCP 时的必要工程设计。

## 5. 在 WorkBuddy 中接入（推荐路径）

比赛官方推荐使用 WorkBuddy 开发与运行 Skill。WorkBuddy 作为 MCP Client 直连麦当劳 MCP Server，Skill（SKILL.md）指导 AI 按上述调用链编排工具，**无需自行重复实现 MCP Client**：

1. WorkBuddy → 左侧边栏【专家·技能·连接器】→【连接器】→【自定义连接器】→【配置 MCP】；
2. 粘贴 `mcp-config.example.json` 内容，把 `${MCD_MCP_TOKEN}` 替换为真实 Token（仅保存在你的 WorkBuddy 连接器配置中）；
3. 启用 `mcd-mcp` 连接器，安装本仓库 `SKILL.md` 为技能；
4. 对话框中直接描述需求，如：「用麦门异世界帮我规划：预算 40，想吃辣，外送到南山科技园」。

独立运行的 Python 引擎（`python -m burger_quest quest`）使用官方 `mcp` Python SDK 做同样的 Streamable HTTP 调用，用于可复现演示、自动化测试与脱离宿主环境的集成。

### Skill 与代码的一致性守卫

`SKILL.md`（WorkBuddy 技能入口）与实测结论保持同步——开发期由文档一致性测试（校验文档提到的工具名均真实存在、必备章节齐全、解析规则与引擎一致、数值示例与引擎输出一致）自动强制，防止文档与代码漂移：

- 文档中提到的每个 MCP 工具名都必须在 `TOOLS_USED` / `TOOLS_SKILL_READONLY` / `TOOLS_RESERVED` 真实清单内（防虚构工具名）；
- 引擎使用的 11 个工具 + 外送入口 1 个 + 支线任务 15 个工具都必须在文档中说明；
- 必备章节（角色定位/前置检查/解析/调用链/规划/输出/卡片/错误处理/红线/对话示例）齐全；
- 文档声称的解析规则与 `nlparser` 实际行为一致（10 条抽查）；
- 文档中三个数值示例与引擎实际输出一致（示例可执行、不编数字）；
- 调用链描述与 `service.py` 实际顺序一致（算价探测前置取名称与单价、详情仅作回退）。

改工具名、改调用链或改解析规则时，该测试会失败——文档与代码不允许漂移。

## 6. 数据处理与隐私

- MCP Token 只从环境变量或宿主连接器配置读取，不落库、不写日志、不进 Git（`mcp-config.example.json` 仅含占位符）；
- 引擎不存储任何用户数据；任务输出中的审计记录会自动剔除 token/authorization 字段；
- 涉及用户账户的工具（我的券、积分、订单）只读，MVP 不做任何写操作。

## 3.12 工具契约审计（2026-10-17，官方 Schema 实证）

审计方法：**以官方定义为准，不靠工具名猜参数**。两个权威来源：
1. 官方仓库 README（`M-China/mcd-mcp-server`）：端点 `https://mcp.mcd.cn`、Streamable HTTP、`Authorization: Bearer`、限流 600 次/分钟、错误码 401/429——与本文档 §1 一致；
2. **实时 `list_tools` Schema 快照**：2026-10-17 用真实 Token 经项目自有客户端拉取官方 35 个工具的完整 `inputSchema`，**verbatim 落盘** `docs/official_tools_schema.json`（未做任何修改），开发期的测试套件据此做回归。

**首个实证差异**：README 工具表列 33 个工具，实时服务实为 35 个（多 `query-promotions` / `query-survey-coupon`，README 最后提交 2026-10-01 早于服务上线）——以实时 Schema 为准。项目 `TOOLS_USED`(11) + `TOOLS_SKILL_READONLY`(15) + `TOOLS_RESERVED`(9) 全部在官方 35 个内，无虚构工具名（2026-10-19 三层口径）。

### 统一契约清单（引擎在用工具，三态标注；Skill 支线 15 个工具见 §3.15）

三态定义：**官方定义已核对** = 快照中有 Schema 且本文表逐项核对；**本地测试覆盖** = 测试套件含正常+异常响应用例；**真实服务已验证** = 2026-10-17 用真实 Token 实际调用。

| 工具 | 官方必填参数 | 官方关键可选/枚举 | 引擎传参 | 官方定义已核对 | 本地测试覆盖 | 真实服务已验证 |
| --- | --- | --- | --- | --- | --- | --- |
| `now-time-info` | 无 | — | `{}` | ✅ | ✅ | ✅ 返回服务器时间 |
| `query-nearby-stores` | beType, searchType | city, keyword；**beType 枚举 [1,5]**、searchType [1,2] | beType=1, searchType=2, city, keyword | ✅ | ✅ | ✅ 5 家真实候选 |
| `query-meals` | storeCode, orderType, beType | beCode(外送必传), reservationDate(预约场景)；beType [1,2,5,6]，orderType [1,2] | 必填齐 + beCode 仅外送 | ✅ | ✅ | ✅ 118 个编码 |
| `query-meal-detail` | storeCode, orderType, beType, **code** | beCode, reservationDate | 必填齐 + beCode 仅外送 | ✅ | ✅ | ✅ 鸡薯双全盒 |
| `query-store-coupons` | orderType, beType, storeCode | beCode, reservationDate | 必填齐 + beCode 仅外送 | ✅ | ✅ | ✅ 调用成功，0 张券 |
| `query-my-coupons` | 无 | **page/pageSize 类型为 string** | `{}`（用官方默认分页） | ✅ | ✅ | ✅ 调用成功，空券包 |
| `list-nutrition-foods` | 无 | — | `{}` | ✅ | ✅ | ✅ 160 条 |
| `calculate-price` | storeCode, orderType, beType | **items[]（商品级字段：productCode/quantity/couponId/couponCode/modification/roundList）**、beCode、gmServiceCode、needTableware、reservationDate、withOrder | 必填齐 + items + beCode 仅外送 | ✅ | ✅ | ✅ ¥15.90/¥24.00；600010 双字段契约实测 |
| `delivery-query-addresses` | 无 | — | `{}` | ✅ | ✅ | ✅ 调用成功，账户无地址 |
| ~~`delivery-query-stores`~~ | — | — | — | — | — | ⛔ 已移除（2026-10-18，未验证） |

### 契约核对结论（逐工具，无猜测）

1. **字段传递全部正确**：门店编码（storeCode，官方要求全数字）、餐品编码（`code`，非 mealCode）、orderType/beType 组合、取餐方式（到店自取/堂食均走 orderType=1+beType=1）、配送方式（本项目不覆盖外送，见 `delivery-query-stores` 已移除的说明）——均与官方 Schema 及描述一致；
2. **beCode 传递规则锁定**：官方明示「到店自提场景 orderType=1 时 beCode 是无效参数，传了会报错」「麦乐送 orderType=2 必传 beCode」——引擎按 `scene.be_code` 为空即不传，天然合规；`tests/test_tool_contract.py` 对两条链路做端到端断言；
3. **官方已定义、MVP 不传的能力**（登记备忘，非缺陷）：`reservationDate`（官方：非预约场景不传）、`needTableware`、`withOrder`、`gmServiceCode`、`modification`/`roundList`（套餐换选/特制，§3.6 已登记）、`beType=5` 得来速与 `beType=6` 团餐（引擎未接入，`query-nearby-stores` 的 beType 枚举也只有 [1,5]——外送门店需走 `delivery-query-stores`，该工具未验证已移除）；
4. **修复：优惠券 per-item 契约**（此前 §3.9 未覆盖的维度）。官方 Schema 把 `couponId`/`couponCode` 定义在 **items[] 商品级**——每件商品可各带一张券。旧 `build_calc_payload` 只往第一件商品挂 `coupons[0]`，双券试探的第二张被静默丢弃（payload 与单券试探完全相同，白烧一次权威调用，方案却宣称「提交 2 张券」）。已修：第 i 张券挂第 i 件商品；`coupon_sets` 增加**容量守卫**（商品数 < 券数时不生成挂不下的试探，不静默丢券）；mock 改为从 payload 实际提交的券推导折扣，恢复「演示不比真实宽松」不变量。**服务端对同单多券的接受性待有券账户验证**（§3.6）；
5. **防御性解析加固**：`delivery-query-addresses` 返回容器官方未定义（无参数 Schema、真实账户无地址只验证过空态）→ 用 `_as_list` 兼容 list/`list`/`items`/`data`/`records`/`result`/`addresses` 容器并丢弃无 addressId 条目；餐品详情成功但无名称 → 服务层如实计数警告（原来静默跳过）；null/空列表/类型变化（categories 为 dict、营养文本缺表头、算价 data=null 等）全部有测试锁定「明确为空或丢弃坏条目，绝不猜测填空」；
6. **仍待验证（无法从官方定义确认）**：① 优惠券 `percent` 字段语义（88 → 0.88 还是 8.8 折，无官方文档且无券账户，当前按 `p/100 if p>1 else p` 启发式并已在代码注释标注）；② 同单多券的服务端接受性（见上）；③ `query-my-coupons` 分页参数为 string 的取值边界（默认分页未触顶，未实测）。

### 契约回归（开发期 37 例自动化断言）

- 快照守卫：35 工具、出处元数据（server/SDK/时间）、`TOOLS_USED`/`TOOLS_RESERVED` ⊆ 官方清单；
- 端到端传参契约：完整 quest（到店+外送两条链路）捕获每个工具的实际入参，对照官方 Schema 断言「必填齐备 / 无官方外参数（防拼错）/ 枚举合法 / 类型正确」，调用流非空断言防真空校验；
- 三态台账守卫：在用工具必须登记「官方定义/本地测试/真实验证」状态，未真实验证必须写明阻塞原因——新工具不允许绕过审计。

## 3.13 稳定性配置：超时 / 重试 / 限流 / 追踪 / 日志（2026-10-17）

真实 MCP 请求的稳定性参数，单一事实来源在 `burger_quest/mcp_http.py`，本节是配置说明。

### 超时（连接与读取分离）

| 参数 | 值 | 理由 |
| --- | --- | --- |
| `CONNECT_TIMEOUT` | 10s | TCP/TLS 握手。实测 TLS 约 1.4s；不通即网络问题，10s 足够判定 |
| `READ_TIMEOUT` | 60s | 等待服务端出数据。**必须 ≥ 最重工具的外层等待**（`calculate-price` 45s）——SDK 默认 5s 读取超时会提前误杀慢但正常的权威算价 |
| `WRITE_TIMEOUT` | 30s | 请求体很小（商品列表），30s 充裕 |
| `POOL_TIMEOUT` | 10s | 连接池获取 |
| 外层等待 `_submit` | 默认 30s / `calculate-price` 45s | 任务级总截止；基线实测最慢单次 663ms |

### 重试（有上限、防风暴、尊重服务端）

| 维度 | 规则 |
| --- | --- |
| 可重试 | 瞬时网络错误 / 5xx（最多 3 次尝试，退避 0.5/1/2s）；429（最多 2 次尝试，退避 2/4s） |
| 不重试 | 401/403（确定性凭据/权限）、业务错误（信封 success=false）、协议错误（isError） |
| **Retry-After** | 服务端 429/503 响应头给出时按它等待，封顶 `RETRY_AFTER_CAP=30s`（秒形态；HTTP-date 形态忽略走内置退避） |
| **抖动** | 所有退避 × 随机系数 50%–100%（只减不增，避免多客户端叠加限流） |
| **会话预算** | 单客户端实例累计最多 `MAX_SESSION_RETRIES=15` 次重试——杜绝多个失败请求逐个重试的风暴 |
| **连续失败断路** | 连续 `MAX_CONSECUTIVE_NETWORK_FAILURES=3` 次网络类失败后停止重试、快速失败（网络整体不可达时避免每个调用耗 10s 连接超时）；一次成功即复位 |
| **副作用禁重试** | `NON_IDEMPOTENT_TOOLS`（create-order / cancel-order / auto-bind-coupons / draw-lottery / mall-create-order / party-order-create / delivery-create-address）一律单次尝试——重试下单/支付/领券会造成重复副作用。引擎 MVP 只读不会调用，此清单是硬护栏 |

### 追踪与日志

- **trace_id**：每个客户端实例生成（8 位 hex），进入调用审计与诊断日志，用于关联一次任务的全部调用（MCP 协议层无自定义 request id 出口，不自造协议字段）；
- **诊断日志**（默认关闭）：设置环境变量 `BURGER_QUEST_LOG=<文件路径>` 后，每次尝试/重试/放弃写入一行 JSON（时间、trace、工具、尝试次数、HTTP 状态、错误类型、耗时）。**脱敏硬约束**：token/authorization/headers/cookie 类键整体打码、Bearer 凭据打码、MCD_MCP_TOKEN 值双保险替换——测试锁定零泄露；写失败静默忽略，不影响主流程；
- 面向用户的错误提示保持简洁（原因 + 下一步建议），详细诊断只在日志。

### 传输层协议边界

Streamable HTTP 的 SSE 流由官方 SDK 在传输层完成聚合；客户端只消费最终 `content`（文本块按序拼接后提取信封 JSON），**不自研 SSE/JSON-RPC 解析，不把三种响应形态混为一谈**（`parse_tool_text` 注释与测试锁定）。

## 3.14 餐品与价格专项审计（2026-10-17，真实 Token）

### 真实证据（门店 1420762，2026-10-17 17:01，只读）

- `query-meals`：116 个菜单编码；分类/tags 为官方标签体系（tags 分布：单品 20、麦金卡 15、套餐 14、上新 11、100%纯牛肉 11…）；
- `calculate-price`（3 件各 1 件）：`originalPrice=7960 == sum(subtotal)=7960`、`price=7960`、`discount=0`、无 `deliveryFee` 字段（到店）；**金额单位为分**（1590=¥15.90），与引擎解释一致；
- `calculate-price`（同品 quantity=2）：`subtotal=3180=2×1590` → 行小计语义确认，`extract_product_prices` 的 ÷数量 归一化正确；
- **新发现字段 `takeWayList`**（可取餐方式：堂食/外带取餐柜）：官方返回但引擎未使用（场景由 orderType/beType 表达）——登记备忘，非缺陷；
- 账户券池为空：`query-store-coupons`/`query-my-coupons` 均 0 张，`discount=0`——优惠展示链路本次无可验样本（待有券账户）。

### 审计结论（价格链路）

1. **单位与转换**：`_to_cents_official` 整数=分、小数/带点字符串=元，防御官方口径变更的 100 倍误差；测试固化 12 种输入（含 bool/空串/大整数/负值）；
2. **价格四段语义**：原价（originalPrice）/ 已核实优惠（discount）/ 配送费（官方返回时）/ 应付总价（price=用户最终支付）——全部只认 calculate-price 权威返回，缺失总价即 ok=False 停止出方案；
3. **行小计归一**：quantity>1 时 ÷数量；缺数量按 1、quantity=0 除零保护、缺小计跳过——「忠实提取」，0/负价由服务层结合语境排除并明示；
4. **空/零/负价格**：排除 + 明示（0 元不免费进组合、不充当预算空间）；全部无价格 → 诚实失败，措辞「价格暂不可验证」；
5. **套餐价与单价的区分**：官方按商品行返回行小计——套餐类餐品（三件套/双全盒/单人餐）该单价即套餐组合价；真实 tags 只有「套餐/单品」标签、组合名（三件套等）不进标签，**不按名称推断价格类型**，改为在卡片/命令行用「单价语义说明」讲清官方口径；
6. **价格适用范围**（本次新增展示）：卡片/命令行/Markdown 三处一致标注「门店编码 + 用餐渠道 + 生成时间；价格随门店/时间/渠道/规格变化」；演示数据明示「非实时、不构成任何真实价格」；
7. **无价餐品不进预算**：planner 池过滤 `price_cents is not None`，测试锁定无价餐品不出现在任何方案、排除时明示；
8. **规格/可选项**：`query-meal-detail` 的 rounds（换选项）与 modification（特制）仍未解析展示（§3.6 已登记），接口层已预留——不猜测、不编造。

测试：新增 `tests/test_price_display.py` 32 例（单位转换 12 种/四段语义/行小计归一/空零负/预算边界相等与超 1 分/无价餐品排除/排序确定性/三处适用范围提示/演示数据明示）；全量 **475 通过**。

### 状态语义（2026-10-18 第三轮加固）

**活动日历（campaign-calendar）**

| state | 含义 |
| --- | --- |
| `ok` | 识别到官方结构且有活动（若部分条目格式不符，`reason` 会注明未解析条数） |
| `empty` | 识别到官方结构、无活动，且官方文本明确「暂无活动」 |
| `parse_error` | 无法确认活动列表是否完整（无结构标志，或结构在但解析不出活动）——**不当作有效空结果** |
| `error` | MCP / 网络 / 认证 / 限流 / 服务端调用失败 |

**优惠券（三类分区独立容错）**

| state | 含义 |
| --- | --- |
| `ok` | 响应契约正确，数据解析成功 |
| `empty` | 官方明确没有符合条件的券（精确匹配 `code=499` + `No coupons available`，或精确文本「暂无可领取的优惠券」） |
| `unknown` | 有返回但无法确认（如 `success=true` 且 `data=null`、文本无法判定） |
| `skipped` | 因缺少门店编码等原因未执行 |
| `error` | 实际请求失败或响应违反契约（`data` 非数组、券条目缺少必需字段等） |

顶层 `state` 聚合：全部分区 `error` → `error`；部分失败 → `partial`（成功分区照常展示，
失败分区不显示为 0 张）；否则 `ok`。某一分区失败不影响其余分区继续查询。

## 3.15 支线任务工具契约（2026-10-19，真实 Token 端到端实测）

引擎（Python）不调用本节工具——它们由 WorkBuddy Agent 经 `mcd-mcp` 连接器直调，
支撑 `SKILL.md` §10 的四条支线（麦享会宝库 / 派对据点侦查 / 团餐公会 / 冒险日志）。
契约来源同 §3.12：官方 `list_tools` Schema 快照 + 真实 Token 端到端调用，**无猜测参数**。

### 统一契约清单（15 个支线只读工具）

| 工具 | 官方必填参数 | 实测确认的可选参数与返回结构 | 支线 |
| --- | --- | --- | --- |
| `mall-points-products` | 无 | **`catRuleIds` 类目规则链**：`1>4`=积分兑换餐品券（实测 24 个，如 200 积分兑「18.8元2份麦辣鸡翅」）；`1>6>21`=主题类派对、`1>6>20`=生日类派对（¥45–198）；不传只返回活动票类。返回 `spuId`/`spuName`/`catName`/`point`/`price`/`shopId` | 10.1 / 10.2 |
| `mall-product-detail` | `spuId` | 返回 `skuList`（skuId/积分/价格）与售卖说明；**不需要 shopId** | 10.1 |
| `mall-order-list` | 无 | `lastId` 翻页 / `size` 每页条数（默认 10、上限 10） | 10.1 / 10.4 |
| `mall-order-detail` | `orderId` | 订单号必须来自列表返回，不编造 | 10.1 / 10.4 |
| `query-party-city` | `spuId` | 实测 279 个城市；字段 `code`/`name`/`latitude`/`longitude` | 10.2 |
| `query-party-store` | `code`（城市 code） | 实测需配合 `latitude`+`longitude`+`spuId`；返回门店 `code`/`name`/`address`/`distance`/`businessStatus`（实测深圳 40 家） | 10.2 |
| `query-party-store-date` | `spuId`、`storeCode` | 返回可预约日期 `[{date, storeCode}]`（实测 5 个） | 10.2 |
| `query-party-store-session` | `spuId`、`storeCode`、**`dateStr`**（不是 `date`） | 返回场次 `timeStart`/`timeEnd`/`partyMin`/`partyMax`/`price`（**单位：分**，13800=¥138）/`leftNum` 剩余名额 | 10.2 |
| `query-lottery-info` | 无 | 活动名/起止时间/单次消耗积分（`drawPoint`）/规则/资格判定（`drawDecision`）/奖品列表。只看不抽 | 10.1 |
| `query-my-prizes` | 无 | `pageNum`/`pageSize`（默认 1/10、最大 50）；按中奖时间倒序 + `hasMore` | 10.1 |
| `query-promotions` | `storeCode`、`orderType`、`beType` | 团餐 `beType=6`；实测 `orderType=1` 返回真实规则（`promotionId`/`promotionType`/`ruleCategory`/`beTypes`/起止时间）；`orderType=2` 需 `beCode` | 10.3 |
| `query-meal-assistance` | `storeCode` | 门店不支持时返回业务码 **610501**「仅支持企业团餐场景下使用助餐服务」——按空态处理，不是网络错误 | 10.3 |
| `order-list` | 无 | 历史订单（到店/外送，非商城）；实测空账户返回 `{}`——空是有效结果 | 10.4 |
| `query-order` | `orderId` | 假订单号实测返回 610303「订单号不存在！」；含地址/手机号，隐私红线见下 | 10.4 |
| `query-survey-coupon` | `orderId` | CSAT 答卷与关联奖券（标题/核销时间/核销状态/点餐方式）；官方明确未返回的原始答卷/会员号/券字段不得猜测 | 10.4 |

### 契约核对结论（支线，2026-10-19）

1. **类目规则链是数据面的开关**：`mall-points-products` 传不传 `catRuleIds` 返回完全不同的商品集合（活动票 vs 积分餐品券）——「无参数调用看到空/不对」先怀疑类目，不要当成没数据；
2. **派对链五段顺序即前置条件**：`spuId`（选商品）→ 城市 → 门店 → 日期 → 场次，任一步骤的入参都来自上一步返回（门店编码取自 `query-party-store` 的 `code` 字段、日期取自 `query-party-store-date`），不允许跨步编造；
3. **参数名坑已实测锁定**：场次日期是 `dateStr`（早期按 `date` 传全部 400）、派对城市入参是 `code`（非 `city`/`cityCode`/`cityId`/`cityName`）——这两个坑写进 `SKILL.md` §10.2 防止回归；
4. **金额单位一致**：派对场次 `price` 与主链路 `calculate-price` 同为「分」；
5. **空态与业务码**：`order-list` 空账户返回 `{}`（有效空）、`query-meal-assistance` 610501（门店不支持）、`query-order` 610303（订单号不存在）——都按「诚实空结果/明确错误」处理，不当成查询失败也不编造；
6. **隐私红线（比主链路更严）**：订单类返回常含配送地址/手机号——只展示用户问到的字段，地址/手机号不回显、不写入任何文件；`query-my-account` 的 `accountId` 同样绝不读取输出；
7. **写操作边界不变**：`mall-create-order` / `draw-lottery` / `party-order-create` 一律不调用（`TOOLS_RESERVED`），只告诉用户官方入口由本人操作。

### 支线调用量

四条支线合计通常 2–6 次调用（限流 600 次/分钟充裕）；相同参数的只读查询任务内去重；
支线可与主线串联（如「积分快过期」→ 查可兑餐品券 → 主线规划时优先用已持有券核验）。
