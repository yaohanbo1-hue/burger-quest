# 麦当劳 MCP 工具全量梳理与选型报告

> 调研时间：2026-10-18 首版，2026-10-19 支线扩容修订（真实 `tools/list` + 逐个实测）
> 官方工具清单：**35 个唯一工具**（`list_tools` 实测快照见 `tests/fixtures/official_tools_schema.json`，2026-10-17 落盘，35/35 均有 `input_schema`）
> 本项目覆盖：**11 个引擎只读工具**（`TOOLS_USED`）+ **15 个 Skill 支线只读工具**（`TOOLS_SKILL_READONLY`）+ **9 个预留/不接**（`TOOLS_RESERVED`）= 35 个全覆盖，零遗漏
> 配套证据：`API_CAPABILITY_AUDIT.md`（接口能力与阻塞项）；支线参数契约详见 `MCP_INTEGRATION.md` §3.12 与 `SKILL.md` §10

---

## 1. 全量工具清单（按业务域）

参数说明：官方 `list_tools` 对**全部 35 个工具**都返回了非空 `input_schema`（其中 21 个明示 `required`），
参数以官方 Schema 为第一依据；下表「参数」列摘录官方 Schema 必填项与实测确认的可选项。

### 1.1 点餐主链路（11 个）

| 工具 | 功能 | 关键参数（官方 Schema 必填 + 实测可选） | 适用场景 | 本项目 |
| --- | --- | --- | --- | --- |
| `now-time-info` | 服务器时间/时段 | 无 | 判断早/正餐时段（菜单随时段变化） | ✅ 接入 |
| `query-nearby-stores` | 按位置查门店 | 必填 `beType`(1到店/5得来速)、`searchType`(2按位置)；可选 `city`、`keyword`（**两者至少一个**） | 用户给地址 | ✅ 接入 |
| `query-meals` | 门店菜单骨架 | 必填 `storeCode`、`orderType`(1到店/2外送)、`beType` | 选品 | ✅ 接入 |
| `query-meal-detail` | 餐品/套餐详情 | 必填 `storeCode`、`orderType`、`beType`、`code`（**外送需 `beCode`**） | 看套餐组成/替换项/特调 | ✅ 接入 |
| `calculate-price` | 权威算价 | 必填 `storeCode`、`orderType`、`beType`；`items[].productCode/quantity/couponId/couponCode`（**无替换参数**） | 任何价格结论 | ✅ 接入 |
| `list-nutrition-foods` | 营养成分全表 | 无 | 低卡/高蛋白偏好 | ✅ 接入 |
| `query-store-coupons` | 门店可用券 | 必填 `storeCode`、`orderType`、`beType` | 下单可用券 | ✅ 接入 |
| `query-my-coupons` | 卡包券 | 无 | 「我有什么券」 | ✅ 接入 |
| `delivery-query-addresses` | 配送地址列表 | 无 | 外送前置 | 🔒 预留（外送链路依赖保存地址） |
| `delivery-query-stores` | 地址可配送门店 | 必填 `beType`、`addressId` | 外送选店 | 🔒 预留（需 `addressId`；账户无保存地址，端到端不可用） |

### 1.2 优惠券与券资产（3 个，另有 2 个见上）

| 工具 | 功能 | 参数 | 本项目 |
| --- | --- | --- | --- |
| `available-coupons` | 可领取的麦麦省券 | 无 | ✅ 接入（**只查不领**；无券时 `code=499 data=null`） |
| `auto-bind-coupons` | 一键领取全部可领券 | 无 | ⛔ **写操作**（改变账户券资产），不接 |
| `query-survey-coupon` | 按订单号查 CSAT 答卷与关联奖券 | 必填 `orderId` | 📖 Skill 支线（§10.4 冒险日志：历史订单满意度复盘） |

### 1.3 积分与麦麦商城（9 个）

| 工具 | 功能 | 参数 | 本项目 |
| --- | --- | --- | --- |
| `query-my-account` | 积分账户（可用/累计/已用/过期） | 无 | ✅ **接入**（只取数值，丢弃 `accountId`） |
| `mall-points-products` | 积分/现金商品列表 | 可选 `catRuleIds`（**类目规则链**） | 📖 Skill 支线（§10.1 麦享会宝库） |
| `mall-product-detail` | 商城商品详情 | 必填 `spuId` | 📖 Skill 支线（同上，兑换走写接口故只读详情） |
| `mall-order-list` | 商城订单列表 | 可选 `lastId`/`size` | 📖 Skill 支线（同上，兑换记录复盘） |
| `mall-order-detail` | 商城订单详情 | 必填 `orderId` | 📖 Skill 支线（同上） |
| `mall-create-order` | 兑换下单 | 必填 `skuId`、`spuCategory` | ⛔ **写操作**（扣积分/现金） |
| `query-lottery-info` | 积分抽奖活动信息 | 无 | 📖 Skill 支线（§10.1 抽奖情报） |
| `draw-lottery` | 参加抽奖 | 无 | ⛔ **写操作**（原子扣积分） |
| `query-my-prizes` | 我的抽奖奖品 | 可选 `pageNum`/`pageSize` | 📖 Skill 支线（§10.1 奖品盘点） |

### 1.4 活动与派对（6 个）

| 工具 | 功能 | 参数（官方 Schema 必填 + 实测可选） | 本项目 |
| --- | --- | --- | --- |
| `campaign-calendar` | 当月营销活动日历（含订阅状态） | 可选 `specifiedDate` | ✅ 接入（只读；Markdown 返回需专用解析） |
| `query-party-city` | 派对可举办城市 | 必填 `spuId` | 📖 Skill 支线（§10.2 派对据点侦查·第一链） |
| `query-party-store` | 城市内可办派对门店 | 必填 `code`（城市 code）；实测需配合 `latitude`+`longitude`+`spuId` | 📖 Skill 支线（§10.2·第二链） |
| `query-party-store-date` | 门店可预约日期 | 必填 `spuId`、`storeCode` | 📖 Skill 支线（§10.2·第三链） |
| `query-party-store-session` | 指定门店/日期的场次 | 必填 `spuId`、`storeCode`、`dateStr`（**注意是 `dateStr` 不是 `date`**） | 📖 Skill 支线（§10.2·第四链，返回场次价格(分)/余位） |
| `party-order-create` | 派对预约下单 | 必填 `partyType` 等 | ⛔ **写操作**（真实预约/资金） |

### 1.5 订单与交易（5 个）

| 工具 | 功能 | 参数 | 本项目 |
| --- | --- | --- | --- |
| `create-order` | 创建订单 | 必填 `storeCode`、`orderType`、`beType` | ⛔ **写操作**（真实资金/订单） |
| `cancel-order` | 取消订单 | 必填 `orderId`、`cancelReasonCode` | ⛔ **写操作** |
| `query-order` | 订单详情/进度 | 必填 `orderId` | 📖 Skill 支线（§10.4 冒险日志） |
| `order-list` | 历史订单 | 可选分页 | 📖 Skill 支线（§10.4 冒险日志；隐私红线见下） |

### 1.6 外送地址与团餐（4 个）

| 工具 | 功能 | 参数 | 本项目 |
| --- | --- | --- | --- |
| `delivery-create-address` | 创建配送地址 | 必填 `address`/`addressDetail`/`city`/`contactName`/`phone` | ⛔ **写操作**（写用户地址簿） |
| `query-promotions` | 企业团餐促销规则（满减/满折） | 必填 `storeCode`、`orderType`、`beType`（团餐=6） | 📖 Skill 支线（§10.3 团餐公会，实测有数据） |
| `query-meal-assistance` | 企业团餐助餐服务 | 必填 `storeCode` | 📖 Skill 支线（§10.3；门店不支持返回 610501） |
| `now-time-info` | （见 1.1） | | |

---

## 2. 选型结论

### 2.1 引擎接入（11 个，全部只读）

主链路 7：`now-time-info` / `query-nearby-stores` / `query-meals` /
`query-store-coupons` / `query-my-coupons` / `list-nutrition-foods` / `calculate-price`
详情回退 1：`query-meal-detail`
扩展只读 3：`campaign-calendar` / `available-coupons` / `query-my-account`

**选型理由（`query-my-account`）**

1. 与核心场景强相关：点餐规划的核心矛盾是「预算」，而积分是用户已有资产——展示可用积分可以
   把「要不要用积分换券」纳入决策，而不是让用户自己去 App 里翻。
2. 只读、无副作用：不扣分、不兑换、不改账户。
3. 成本极低：无参数、单次调用、返回字段少。
4. 与已有工具互补：券（卡包/门店/可领取）回答「有什么优惠」，积分回答「还能换什么优惠」。

### 2.2 Skill 支线只读（15 个，2026-10-19 实测扩容）

引擎（Python）不调用这 15 个工具——它们由 WorkBuddy Agent 经 `mcd-mcp` 连接器**直调**，
覆盖 SKILL.md §10 的四条支线任务（麦享会宝库 / 派对据点侦查 / 团餐公会 / 冒险日志）。

**扩容理由**：比赛要求「让创意真正解决现实问题」，只做点餐主线只能覆盖一个场景；
积分商城、派对侦查、团餐、历史订单复盘都是官方开放的真实只读能力，参数契约已全部实测打通
（详见 `SKILL.md` §10 与 `MCP_INTEGRATION.md` §3.12）。选型标准与主线一致：

1. **只读**：全部不改变账户/资金/订单状态；
2. **契约已实测**：每个工具的入参都经官方 Schema + 端到端真实调用双重确认，无猜测参数；
3. **隐私可控**：订单类工具返回的地址/手机号/`accountId` 一律脱敏或丢弃（§3.6）；
4. **写操作留给用户**：兑换、预约、领奖的最终动作明确不代做（§2.3）。

**实测关键发现（2026-10-19，真实 Token）**

- `mall-points-products` **传 `catRuleIds=1>4`** 才返回积分兑换餐品券（实测 24 张）；
  不传只返回活动票类——「不传参」与「传对类目」是两个完全不同的数据面。
- 派对链五段全通：`query-party-city`（279 城市）→ `query-party-store`（城市 code + 经纬度，
  实测 40 门店）→ `query-party-store-date`（5 个可约日期）→ `query-party-store-session`
  （`dateStr` 日期，返回场次价格(分)/余位 `leftNum`）。
- `query-promotions` 传 `beType=6` 有真实团餐促销数据；`query-meal-assistance` 门店不支持时
  返回业务码 610501（需按空态处理，不是网络错误）。

### 2.3 明确不接入（9 个）及理由

| 类别 | 工具 | 理由 |
| --- | --- | --- |
| 写操作（7） | `create-order`、`cancel-order`、`auto-bind-coupons`、`draw-lottery`、`mall-create-order`、`party-order-create`、`delivery-create-address` | 产生真实资金/订单/券资产/积分变动。比赛规则要求此类操作须用户明确确认，一律不接；最终动作引导用户在官方渠道完成 |
| 外送链路（2） | `delivery-query-addresses`、`delivery-query-stores` | 契约已确认但账户无保存地址，端到端不可用；已验证「无地址」诚实失败路径，待有地址账户补充验证 |

---

## 3. 接入时的注意事项（实测踩坑）

1. **Schema 以官方为准、边界靠实测**：`list_tools` 快照显示 35/35 工具均有 `input_schema`
   （21 个明示 `required`，见 `tests/fixtures/official_tools_schema.json`）——参数契约第一依据是
   官方 Schema；但官方 Schema 不写「至少填一个」「二选一」这类边界语义（如
   `query-nearby-stores` 的 `city`/`keyword`），也有的字段只在 description 里说明，
   所以新工具接入的正确姿势是：**先读 Schema，再真实调用一次核对返回结构**。
2. **空态可能用业务错误码表达**：`available-coupons` 无券返回 `success:false, code:499`，
   不是网络错误；`query-meal-assistance` 门店不支持返回 610501。判断「空」必须精确匹配
   code + message，不能用「含暂无」泛化。
3. **SDK 会先于业务语义报错**：`data:null` 不符合官方声明的 `outputSchema`（array）时，
   `mcp` SDK 抛 `RuntimeError`。需要 `session.send_request` 绕过校验才能拿到原始结构。
4. **返回格式不统一**：`campaign-calendar` 返回 **Markdown**（非 JSON 信封），
   `list-nutrition-foods` 返回自定义文本，`available-coupons` / `query-my-coupons` 同时给
   structuredContent 与 Markdown content。
5. **数值是字符串**：积分字段（`availablePoint: "0"`）、价格（分）都需显式转换与容错。
6. **账户标识必须丢弃**：`query-my-account` 返回 `accountId`；适配层直接丢弃，绝不进模型/
   日志/UI。订单类工具同理（地址、手机号）——支线任务的话术模板同样遵守该红线
   （`SKILL.md` §10.4）。
7. **参数陷阱**：`query-nearby-stores` 的 `city` 与 `keyword` 必须**同时非空**（报 600058）；
   `query-meal-detail` 的编码参数名是 `code`；`calculate-price` 的 `couponId` 与 `couponCode`
   必须成对提交（否则 600010）；`query-party-store-session` 的日期参数是 `dateStr`。
8. **类目规则参数**：`mall-points-products` 的 `catRuleIds` 是「类目规则链」（如 `1>4`），
   不同链路返回完全不同的商品面——不传参只能看到活动票，积分换券必须显式传类目。
9. **不能伪造替换参数**：`calculate-price` 无套餐替换字段，「套餐改价」不可实现，
   单品组合重算必须明确标注语义不同（见 `API_CAPABILITY_AUDIT.md`）。
10. **限流与配额**：官方限 600 次/分钟/Token；本项目已做 TTL 缓存、去重、有限重试与熔断，
    新增工具应复用既有 `_call` 链路而不是另开调用通道；支线任务由宿主连接器直调时，
    应遵循 SKILL.md §10.5 的「一次问清、批量取数、失败明说」效率约定。
