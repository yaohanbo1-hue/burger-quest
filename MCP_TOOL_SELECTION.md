# 麦当劳 MCP 工具全量梳理与选型报告

> 调研时间：2026-10-18（真实 `tools/list` + 逐个实测）
> 官方工具清单：**35 项条目、去重后 30 个唯一工具**（官方清单存在重复列项）
> 本项目当前接入：**11 个只读工具**（`TOOLS_USED`），另有 10 个登记为预留（`TOOLS_RESERVED`）
> 配套证据：`API_CAPABILITY_AUDIT.md`（接口能力与阻塞项）

---

## 1. 全量工具清单（按业务域）

参数说明：官方 `list_tools` 对**所有工具**返回的 `inputSchema` 均为空，参数只能依据
工具 `description` 与实测调用确定；下表「参数」列为 description 中明示的字段。

### 1.1 点餐主链路（10 个）

| 工具 | 功能 | 关键参数 | 适用场景 | 本项目 |
| --- | --- | --- | --- | --- |
| `now-time-info` | 服务器时间/时段 | 无 | 判断早/正餐时段（菜单随时段变化） | ✅ 接入 |
| `query-nearby-stores` | 按位置查门店 | `beType`(1到店/5得来速)、`searchType`(2按位置)、`city`+`keyword`（**必须同时非空**） | 用户给地址 | ✅ 接入 |
| `query-meals` | 门店菜单骨架 | `storeCode`、`orderType`、`beType` | 选品 | ✅ 接入 |
| `query-meal-detail` | 餐品/套餐详情 | `code`、`storeCode`、`orderType`、`beType`（外送需 `beCode`） | 看套餐组成/替换项/特调 | ✅ 接入 |
| `calculate-price` | 权威算价 | `items[].productCode/quantity/couponId/couponCode`（**无替换参数**） | 任何价格结论 | ✅ 接入 |
| `list-nutrition-foods` | 营养成分全表 | 无 | 低卡/高蛋白偏好 | ✅ 接入 |
| `query-store-coupons` | 门店可用券 | `storeCode`、`orderType`、`beType` | 下单可用券 | ✅ 接入 |
| `query-my-coupons` | 卡包券 | 无 | 「我有什么券」 | ✅ 接入 |
| `delivery-query-addresses` | 配送地址列表 | 无 | 外送前置 | ✅ 预留（外送链路依赖保存地址） |
| `delivery-query-stores` | 地址可配送门店 | `addressId`、`beType` | 外送选店 | ✅ 预留（同上） |

### 1.2 优惠券与券资产（3 个，另有 2 个见上）

| 工具 | 功能 | 参数 | 本项目 |
| --- | --- | --- | --- |
| `available-coupons` | 可领取的麦麦省券 | 无 | ✅ 接入（**只查不领**；无券时 `code=499 data=null`） |
| `auto-bind-coupons` | 一键领取全部可领券 | 无 | ❌ **写操作**（改变账户券资产），不接 |
| `query-survey-coupon` | 按订单号查 CSAT 答卷与关联奖券 | `orderId`（描述隐含） | ❌ 与点餐规划无关 |

### 1.3 积分与麦麦商城（8 个）

| 工具 | 功能 | 参数 | 本项目 |
| --- | --- | --- | --- |
| `query-my-account` | 积分账户（可用/累计/已用/过期） | 无 | ✅ **本轮接入**（只取数值，丢弃 `accountId`） |
| `mall-points-products` | 积分/现金商品列表 | 无 | ❌ 实测返回品鉴会/读书会等活动票（`shopId=5`），非餐品券 |
| `mall-product-detail` | 商城商品详情 | `spuId`/`shopId` | ❌ 同上，且兑换走写接口 |
| `mall-create-order` | 兑换下单 | 商品+`shopId` | ❌ **写操作**（扣积分/现金） |
| `mall-order-list` | 商城订单列表 | 无 | ❌ 与点餐规划无关 |
| `query-lottery-info` | 积分抽奖活动信息 | 无 | ❌ 与点餐规划无关（信息类） |
| `draw-lottery` | 参加抽奖 | 无 | ❌ **写操作**（原子扣积分） |
| `query-my-prizes` | 我的抽奖奖品 | 无 | ❌ 与点餐规划无关 |

### 1.4 活动与派对（4 个）

| 工具 | 功能 | 参数 | 本项目 |
| --- | --- | --- | --- |
| `campaign-calendar` | 当月营销活动日历（含订阅状态） | `specifiedDate`(可选) | ✅ 接入（只读；Markdown 返回需专用解析） |
| `query-party-city` | 派对可举办城市 | `spuId` | ❌ 生日/主题派对场景，与点餐规划无关 |
| `query-party-store` | 城市内可办派对门店 | 城市 | ❌ 同上 |
| `query-party-store-session` | 指定门店/日期的场次 | 门店+日期 | ❌ 同上（预约属写操作范畴） |

### 1.5 订单与交易（4 个）

| 工具 | 功能 | 参数 | 本项目 |
| --- | --- | --- | --- |
| `create-order` | 创建订单 | 门店/方式/商品 | ❌ **写操作**（真实资金/订单） |
| `cancel-order` | 取消订单 | 订单号 | ❌ **写操作** |
| `query-order` | 订单详情/进度 | 订单号 | ❌ 与「规划」无关；含个人信息 |
| `order-list` | 历史订单 | 无 | ❌ 实测本账户为空 `{}`；字段结构未知，且订单通常含地址/手机号，**隐私风险** |

### 1.6 外送地址与团餐（4 个）

| 工具 | 功能 | 参数 | 本项目 |
| --- | --- | --- | --- |
| `delivery-create-address` | 创建配送地址 | 地址字段 | ❌ **写操作**（写用户地址簿） |
| `query-promotions` | 企业团餐促销规则（满减/满折） | 门店+`beType=6` | ❌ 仅企业团餐，当前项目不做团餐 |
| `query-meal-assistance` | 企业团餐助餐服务 | 门店+`beType=6` | ❌ 同上 |
| `now-time-info`… | （见 1.1） | | |

---

## 2. 选型结论

### 2.1 已接入（11 个，全部只读）

主链路 7：`now-time-info` / `query-nearby-stores` / `query-meals` / `query-store-coupons` /
`query-my-coupons` / `list-nutrition-foods` / `calculate-price`
详情回退 1：`query-meal-detail`
扩展只读 3：`campaign-calendar` / `available-coupons` / `query-my-account`

**选型理由（本轮新增 `query-my-account`）**

1. 与核心场景强相关：点餐规划的核心矛盾是「预算」，而积分是用户已有资产——展示可用积分可以
   把「要不要用积分换券」纳入决策，而不是让用户自己去 App 里翻。
2. 只读、无副作用：不扣分、不兑换、不改账户。
3. 成本极低：无参数、单次调用、返回字段少。
4. 与已有工具互补：券（卡包/门店/可领取）回答「有什么优惠」，积分回答「还能换什么优惠」。

### 2.2 明确不接入（19 个）及理由

| 类别 | 工具 | 理由 |
| --- | --- | --- |
| 写操作（8） | `create-order`、`cancel-order`、`auto-bind-coupons`、`draw-lottery`、`mall-create-order`、`delivery-create-address`、`query-order`(查)、`mall-*`(兑换) | 产生真实资金/订单/券资产/积分变动。比赛规则要求此类操作须用户明确确认，第一阶段一律不接 |
| 与场景无关（7） | `query-party-city`、`query-party-store`、`query-party-store-session`、`query-lottery-info`、`query-my-prizes`、`mall-order-list`、`query-survey-coupon` | 派对预约/抽奖/商城订单/满意度问卷，与「点餐规划」无交集 |
| 场景不匹配（2） | `query-promotions`、`query-meal-assistance` | 仅企业团餐（`beType=6`），当前项目覆盖到店/外送 |
| 价值低/风险（2） | `mall-points-products`、`order-list` | 前者实测为活动票（非餐品券）且兑换是写操作；后者本账户无数据、结构未知，订单通常含地址/手机号 |

### 2.3 预留（2 个）

`delivery-query-addresses` / `delivery-query-stores`：外送链路必需，但当前账户无保存地址，
无法端到端验证；已在 `TOOLS_RESERVED` 登记，代码路径存在但未被主流程触发。

---

## 3. 接入时的注意事项（实测踩坑）

1. **Schema 全部未声明**：`list_tools` 的 `inputSchema` 恒为空对象，参数只能靠 description +
   实测。任何新工具接入前必须先真实调用一次并核对返回结构。
2. **空态可能用业务错误码表达**：`available-coupons` 无券返回 `success:false, code:499`，
   不是网络错误。判断「空」必须精确匹配 code + message，不能用「含暂无」泛化。
3. **SDK 会先于业务语义报错**：`data:null` 不符合官方声明的 `outputSchema`（array）时，
   `mcp` SDK 抛 `RuntimeError`。需要 `session.send_request` 绕过校验才能拿到原始结构。
4. **返回格式不统一**：`campaign-calendar` 返回 **Markdown**（非 JSON 信封），
   `list-nutrition-foods` 返回自定义文本，`available-coupons` / `query-my-coupons` 同时给
   structuredContent 与 Markdown content。
5. **数值是字符串**：积分字段（`availablePoint: "0"`）、价格（分）都需显式转换与容错。
6. **账户标识必须丢弃**：`query-my-account` 返回 `accountId`；适配层直接丢弃，绝不进模型/
   日志/UI。订单类工具同理（地址、手机号）。
7. **参数陷阱**：`query-nearby-stores` 的 `city` 与 `keyword` 必须**同时非空**（报 600058）；
   `query-meal-detail` 的编码参数名是 `code`；`calculate-price` 的 `couponId` 与 `couponCode`
   必须成对提交（否则 600010）。
8. **不能伪造替换参数**：`calculate-price` 无套餐替换字段，「套餐改价」不可实现，
   单品组合重算必须明确标注语义不同（见 `API_CAPABILITY_AUDIT.md`）。
9. **限流与配额**：官方限 600 次/分钟/Token；本项目已做 TTL 缓存、去重、有限重试与熔断，
   新增工具应复用既有 `_call` 链路而不是另开调用通道。
