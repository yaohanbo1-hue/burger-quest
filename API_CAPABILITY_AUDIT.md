# 官方 MCP 接口能力报告（套餐改价 / 优惠券状态）

> 审计时间：2026-10-18 第二轮深度审计
> 审计对象：麦当劳中国官方 MCP Server（`https://mcp.mcd.cn`，SDK `mcp` 2.3.0）
> 证据来源：`tools/list` 实时 Schema + 工具 description 原文 + 裸 HTTP JSON-RPC 原始响应 + 本仓库实现
> 复现方式见文末「复核步骤」

---

## 1. 结论摘要

| 能力 | 状态 | 依据 |
| --- | --- | --- |
| 查询套餐组成（组/选项/默认项） | ✅ 官方支持 | `query-meal-detail` 返回 `rounds[].choices[]` |
| 查询可替换项及其差价（`diffPrice`） | ✅ 官方支持（只读展示） | `rounds[].choices[].diffPrice` / `supportModify` |
| 查询特制选项（去冰/加酱等，`modification.values`） | ✅ 官方支持（只读展示） | 详情返回 + 工具 description 的展示规则 |
| **把替换结果传入官方算价（即「套餐改价」）** | ❌ **官方未提供** | `calculate-price` 的 items 仅 `productCode/quantity/couponId/couponCode`，无任何替换/特调字段 |
| 按「单品组合」重新算价 | ✅ 可用（语义不同） | `calculate-price` 对每个 `productCode` 单独计价；套餐价 ≠ 单品价之和 |
| 查询可领取券列表 | ✅ 支持，但空态用**业务错误码**表达 | 无券时 `{"success": false, "code": 499, "message": "No coupons available", "data": null}` |
| 领取优惠券（`auto-bind-coupons`） | ⚠️ 存在但**本项目不调用** | 会改变账户状态；仅预留接口说明 |

---

## 2. 已验证事实（可复核）

### 2.1 `calculate-price` 的 items 字段（官方工具 description 原文）

```
Input:
- items: 商品列表（数组）
  - productCode: 商品编码，必填
  - quantity: 商品数量，必填，大于等于1
  - couponId: 优惠券ID，选填（如果用户要使用优惠券）
  - couponCode: 优惠券…（选填，与 couponId 成对提交）
```

- `list_tools` 返回的 `inputSchema` 为 **空对象**（官方未声明 JSON Schema），因此上述 description 是唯一权威参数说明。
- **没有任何** `choices` / `modification` / `options` / `replace` / `subItems` 字段。

### 2.2 `query-meal-detail` 的官方展示规则（description 原文）

```
Output展示规则（必须遵守）：
- 若 data.supportModify=true，在商品名称后面标注【可特调】
- 若 choice.supportModify=true，在该 choice 名称后面标注【可特调】
- 不要主动展开特调选项列表，仅当用户主动询问时才展示 modification 内容
- 展示完详情后，总结当前默认选中的搭配（isDefault=1的choices），并引导用户：
  "如需查看或修改特调选项，请回复'查看特调'"
```

→ 官方把「特调/替换」定位为**展示能力**；「修改」指向官方点餐页，而非 MCP 接口。

### 2.3 详情真实返回结构（2026-10-18 实测，门店 1420762、套餐 9900011121）

```json
rounds: [
  {"id": 1, "name": "麦香鱼", "quantity": 1,
   "choices": [{"code": "1600", "name": "麦香鱼", "isDefault": 1, "diffPrice": "+ ¥0",
                "supportModify": true,
                "modification": {"items": [{"minValues": 0, "maxValues": 2,
                  "values": [{"name": "麦香鱼酱", "price": 0, "selectedQuantity": 1},
                             {"name": "吉士", "price": 0, "selectedQuantity": 1}]}]}}]},
  {"id": 4, "name": "选择饮料",
   "choices": [{"code": "3505", "name": "锡兰红茶", "isDefault": 1, "diffPrice": "+ ¥0"},
               {"code": "3071", "name": "无糖可口可乐中杯", "diffPrice": "- ¥1.5", "supportModify": true},
               {"code": "3111", "name": "无糖可口可乐大杯", "diffPrice": "+ ¥0.5"}]}
]
```

### 2.4 价格实测对比（同一门店，全部来自 `calculate-price` 权威返回）

| 场景 | 提交内容 | 官方应付 |
| --- | --- | --- |
| 套餐价 | `items=[{productCode: 9900011121, quantity: 1}]` | **¥36.00** |
| 单品组合（饮料换无糖可乐中杯） | `items=[麦香鱼, 小杯玉米杯, 苹果片, 无糖可乐中杯]` 各 1 | **¥42.50** |

→ 两者都是官方真实报价，但**语义不同**：前者是套餐价，后者是 4 件单品单独计价。

### 2.5 `available-coupons` 的空态表达（裸 HTTP 原始响应）

```json
// 无券账户（成功调用，业务层用 499 表示「无券」）
{"success": false, "code": 499, "message": "No coupons available", "data": null}
content 文本：「### 麦麦省优惠券列表：\n\n暂无可领取的优惠券」
// 对照：query-my-coupons 用成功信封表达空
{"success": true, "code": 200, "data": {"coupons": [], "totalCount": 0}}
```

→ `code=499` 是**语义化空态**，不是网络/鉴权/服务端错误；SDK 因 `data=null` 不符合其
`outputSchema`（array）先抛 `RuntimeError`，属**客户端校验先于业务语义**的误报。

---

## 3. 尚未验证的假设

| 假设 | 说明 | 验证方式 |
| --- | --- | --- |
| 官方点餐页支持「套餐内换项并按套餐价重算」 | 页面端可能调用未公开的内部接口 | 需官方确认；MCP 层无对应工具/参数 |
| `modification.values` 的 `price` 字段是否为加价 | 实测值为 0（麦香鱼酱/吉士），无正例 | 需要一个带加价特调项的套餐样本 |
| 有券账户的 `available-coupons` 返回结构 | 本账户无券，只见 `data=null` | 需有可领券账户实测 |

---

## 4. 需要官方确认的问题

1. `calculate-price` 是否有未在 description 中列出的「套餐选项/替换」参数？若有，字段名与语义是什么？
2. 特调项（`modification.values`）产生的加价如何进入算价？是否只能通过官方点餐页完成？
3. `available-coupons` 在**有券**账户下的 `data` 结构（字段名、券状态枚举）是什么？
4. `code=499 / "No coupons available"` 是否为稳定的语义化空态码（可长期依赖）？

---

## 5. 本项目的处理边界

- **提供**：套餐组成、可替换项（含真实差价）、特制选项的**只读展示**（`detail` 命令 / Web）；
  单品组合重配并按官方算价对比（`--reconfigure`），输出中**明确标注「非套餐改价」**。
- **不提供**：伪造「套餐改价」按钮或价格。理由见 §2.1（官方无参数）。
- **不调用**：`auto-bind-coupons`（领取）、下单/取消/抽奖等任何改变账户状态的工具。

---

## 6. 复核步骤

```powershell
# 1) 查看官方工具清单与 description（含 calculate-price 的 items 字段说明）
.\.venv\Scripts\python.exe -c "from burger_quest.mcp_http import HttpMCPClient; c=HttpMCPClient(); c.connect(); [print(t.name if hasattr(t,'name') else t.get('name'), '|', (t.description if hasattr(t,'description') else t.get('description'))[:120]) for t in c.list_tools()]; c.close()"

# 2) 套餐详情与替换项（真实门店/套餐编码请替换）
.\.venv\Scripts\python.exe -m burger_quest detail 9900011121 --store-code 1420762
.\.venv\Scripts\python.exe -m burger_quest detail 9900011121 --store-code 1420762 --reconfigure "4=3071"

# 3) 券三态
.\.venv\Scripts\python.exe -m burger_quest coupons --store-code 1420762
```
