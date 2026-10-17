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

## 2. 实际使用的工具（8 个核心 + 2 个外送场景专用）

引擎通过 `burger_quest/mcp_base.py` 的 `MCPClient` 接口调用以下工具。
**场景不同，实际使用的工具并不必然完全相同**：

- **8 个核心工具**：任何场景（到店/外送）都会用到；
- **2 个外送专用工具**（`delivery-query-addresses` / `delivery-query-stores`）：仅外送场景使用，且需要账户已保存配送地址。

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
| *（外送专用）* `delivery-query-stores` | 某配送地址可配送的门店（含 beCode） | 第 2 步，**仅外送场景** |

> 注：`query-nearby-stores` 是**到店场景**的位置查询（`beType=1`）；外送场景的门店来自
> `delivery-query-stores`（按已保存地址），两者不可互相替代。

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
调用量可用 `tools/measure_calls.py` 复测。

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
| `delivery-query-stores` | `beType`(2麦乐送/6团餐)、`addressId` | — |

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

官方清单共 **35 个工具**；项目 `TOOLS_USED`（8 个）与 `TOOLS_RESERVED`（8 个）全部在列。
本次逐个核对了 8 个在用工具的官方输入 Schema，与 `mcp_http.py` 实际传参**完全一致**
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

基线实测（`tools/measure_calls.py`，可复测）：单任务 **演示 24 次 / 真实 17 次 3.5s**；
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

复测方式：`python tools/measure_calls.py`（演示）或
`MCD_MCP_TOKEN=xxx python tools/measure_calls.py --real --store-code 1420762`（真实）。

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

### Skill 与代码的一致性守卫

`skill.md`（WorkBuddy 技能入口）与仓库代码保持同步，由 `tests/test_skill_docs.py` 自动校验：

- 文档中提到的每个 MCP 工具名都必须在 `TOOLS_USED` / `TOOLS_RESERVED` 真实清单内（防虚构工具名）；
- 引擎使用的 8 个工具 + 外送 2 个工具都必须在文档中说明；
- 必备章节（角色定位/前置检查/解析/调用链/规划/输出/卡片/错误处理/红线/对话示例）齐全；
- 文档声称的解析规则与 `nlparser` 实际行为一致（10 条抽查）；
- 文档中三个数值示例与引擎实际输出一致（示例可执行、不编数字）；
- 调用链描述与 `service.py` 实际顺序一致（算价探测前置取名称与单价、详情仅作回退）。

改工具名、改调用链或改解析规则时，该测试会失败——文档与代码不允许漂移。

## 6. 数据处理与隐私

- MCP Token 只从环境变量或宿主连接器配置读取，不落库、不写日志、不进 Git（`mcp-config.example.json` 仅含占位符）；
- 引擎不存储任何用户数据；任务输出中的审计记录会自动剔除 token/authorization 字段；
- 涉及用户账户的工具（我的券、积分、订单）只读，MVP 不做任何写操作。
