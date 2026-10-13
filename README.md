# 🍔 Burger Quest · 麦门异世界

> 输入预算、心情、口味和用餐偏好，RPG 游戏主持人替你从**麦当劳中国官方 MCP Server 的真实菜单**里，组出一份不超预算的点餐方案，并生成一张可以分享的任务成就卡。

**非麦当劳官方项目** —— 本项目为「麦当劳程序员创意开发大赛」参赛作品，由参赛者独立开发。餐品信息、价格及供应状态以麦当劳官方渠道的实时结果为准。

---

## 1. 项目介绍

「麦门异世界」是一个游戏化点餐规划 Skill：把「今天吃什么」变成一场 RPG 冒险。

- 你（冒险者）说出预算、口味、心情、人数、用餐方式；
- AI（游戏主持人）调用麦当劳中国官方 MCP Server 查询**真实可售菜单、真实价格、真实优惠券和营养数据**；
- 主持人生成任务书：任务名称、故事背景、推荐装备（真实餐品）、选择理由、总价与剩余预算；
- 一键生成分享成就卡（HTML / Markdown）。

### 三条铁律（贯穿全部代码与测试）

1. **不编造餐品**：进入方案的每一项都来自 `query-meals` 返回的真实菜单；
2. **不编造价格**：总价只来自 `calculate-price` 的权威返回，菜单价缺失的餐品直接出局；
3. **不编造优惠与库存**：优惠券必须来自 `query-store-coupons` / `query-my-coupons`，库存状态以 MCP 返回为准。

MVP **只生成方案，不自动下单**；`create-order` 等具有实际影响的操作需要用户明确确认后才可调用（本仓库未接入）。

## 2. 目标用户

- 选择困难症患者：一句话需求 → 直接可执行的点餐方案；
- 预算敏感用户：预算内多组合对比，剩余预算一目了然；
- 关注营养的用户：低卡 / 高蛋白偏好筛选，热量与蛋白质来自 `list-nutrition-foods` 真实数据；
- 任何想在 AI 对话里「玩着点餐」的开发者和玩家。

## 3. 安装

```bash
# 要求 Python >= 3.10
git clone https://github.com/yaohanbo1-hue/burger-quest.git
cd burger-quest
pip install -r requirements.txt
```

引擎核心零第三方依赖；`mcp` 官方 SDK 仅真实模式需要。

## 4. 快速开始

### 4.1 演示模式（离线，无需 MCP Token）

```bash
python -m burger_quest demo "我想吃辣，预算35，外送到南山科技园，一个人" \
  --card-html examples/cards/demo_spicy.html \
  --card-md examples/cards/demo_spicy.md
```

输出（节选）：

```
🍔 无名冒险者的第一道门槛 · 精打细算的游侠·辣味偏好（麦乐送远征）

【推荐装备】
  ⚔️ 烈焰骑士的招牌巨剑 —— 麦辣鸡腿堡（¥21.00）
  🎟️ 券灵加持 —— 麦辣鸡腿堡立减3元（演示券）

【价格结算】
  原价合计：¥21.00
  优惠减免：-¥3.00
  配送费：¥9.00
  应付总价：¥27.00（calculate-price 权威结果）
  剩余预算：¥8.00
```

> ⚠️ 演示模式使用 `fixtures/demo_data.json` 中的**演示数据**（结构与 MCP 返回兼容，数值非实时），用于离线体验与自动化测试。

### 4.2 真实模式（调用麦当劳 MCP）

```bash
# 1. 到 https://open.mcd.cn/mcp 手机号登录，控制台申请 MCP Token
# 2. Token 只通过环境变量提供（切勿写进代码或提交仓库）
export MCD_MCP_TOKEN="你的Token"

# 3. 直接按位置规划（自动用官方 query-nearby-stores 查询附近门店）
python -m burger_quest quest "我想吃辣，预算35，外送到南山科技园"
```

行为约定（与演示模式彻底分离）：

| 情况 | 行为 |
| --- | --- |
| 未配置 `MCD_MCP_TOKEN` | 明确报错（退出码 2），**不会**静默切换到演示数据 |
| 找到多家附近门店 | 列出全部候选门店及编码并暂停，不默默选一家；选定后加 `--store-code` 重新运行 |
| 没有位置信息 | 明确要求补充地址或门店编码（不会假设门店） |
| 查不到门店 / 菜单失败 / 无法确定用餐方式 | 逐项说明原因并停止，不生成任何价格 |

```bash
# 先查附近门店，获取真实门店编码
python -m burger_quest stores "南山科技园"

# 指定门店运行
python -m burger_quest quest "我想吃辣，预算35" --store-code <编码>
```

> ⚠️ `DEMO-001` / `DEMO-002` 只存在于演示数据（fixtures），真实模式使用会查询失败；真实门店编码请用 `stores` 子命令获取。

### 4.3 作为 WorkBuddy Skill 使用

把本仓库的 `skill.md` 安装为 WorkBuddy 技能，并在 WorkBuddy 中配置麦当劳 MCP 连接器（`mcp-config.example.json` 提供模板），即可在对话框中直接说：

> 「用麦门异世界帮我规划：预算 40，想吃辣，外送到南山科技园」

详细用法见 [skill.md](skill.md) 与 [MCP_INTEGRATION.md](MCP_INTEGRATION.md)。

## 5. 使用示例

| 场景 | 输入 | 结果 |
| --- | --- | --- |
| 辣味外送 | `我想吃辣，预算35，外送到南山科技园，一个人` | 麦辣鸡腿堡 + 立减券 + 配送费 = ¥27，剩 ¥8 |
| 多人丰盛 | `两个人想吃点好的，预算100，堂食` | 麦辣鸡腿堡套餐 + 巨无霸套餐 = ¥68，叠券后 ¥63 |
| 低卡轻食 | `减肥中，想吃低卡的，预算40，到店自取` | 双层吉士堡 + 玉米杯 + 零度可乐，整餐约 540 千卡 |
| 预算不足 | `预算5块，想吃汉堡` | **如实失败**：明确说明没有组合落在预算内，不编造价格 |
| 多家门店 | `quest "想吃辣，预算35"`（位置附近有多家店） | 列出全部候选门店及编码并暂停，用 `--store-code` 指定后重跑 |

完整可复现演示流程见 [examples/demo.md](examples/demo.md)。

## 6. 测试

```bash
python -m pytest tests/ -v
```

99 个测试覆盖：自然语言解析、组合规划、**不编造铁律**（价格缺失出局 / 价格计算失败无方案 / 预算筛除）、真实 MCP 客户端（同步门面 / 401·429 状态码还原 / 超时 / SDK 缺失 / 业务错误，用假会话不访问网络）、防御式字段适配（联调实测格式：文本信封提取、菜单骨架、算价分单位、营养文本解析）、门店解析流程（多候选不默默选择 / 无位置要求补充 / 外送 beCode 链路 / 无效编码明确报错）、CLI 全流程（demo / quest / stores / tools / 帮助与退出码）、RPG 文案确定性、分享卡转义与必备声明。

> 说明：测试中的「真实模式」用假会话验证引擎链路，**不访问麦当劳服务器**；真实服务器的联调实测记录见 MCP_INTEGRATION.md。

## 6.5 已知限制（透明声明）

**已于 2026-10-13 用官方 Token 实测验证**（详见 MCP_INTEGRATION.md §3.5）：
- 网络链路、`mcp` SDK 1.27.2 双版本兼容、401/429 状态码还原；
- 7 个工具的真实参数名与返回结构（门店查询 / 菜单骨架 / 餐品详情 / 价格发现 / 算价验价 / 营养文本）；
- 多候选门店列出暂停、外送无地址时的明确说明。

**仍未验证（需要特定账户状态）**：
- 优惠券字段与金额单位（联调账户当时无可用券；`items[].couponId` 挂载方式按官方 Schema 实现）；
- 外送全链路（需要账户已保存配送地址）；
- `query-meal-detail` 的特制选项（modification）与套餐换选项——MVP 不展示，接口已预留。

## 7. 架构（分层）

```
用户一句话
   │
   ▼
nlparser.py     自然语言解析：预算/口味/营养偏好/门店/用餐方式/心情/丰盛偏好
   │  Constraints
   ▼
mcp_base.py     MCP 客户端接口 + 防御式适配（联调实测格式：文本信封提取/菜单骨架/算价分单位）
   ├─ mcp_http.py   真实客户端（同步门面 + 后台事件循环，官方 mcp SDK）
   └─ mcp_mock.py   演示客户端（fixtures，离线可跑）
   │
   ▼
service.py      编排层（官方工具链，联调实测）：
   │  门店解析（附近门店/配送地址，多候选列出不默选）→ query-meals 菜单骨架（只有编码）
   │  → 短名单 query-meal-detail 补名称 → 一次 calculate-price 价格发现（单位：分）
   ▼
planner.py      套餐规划：候选组合模板 → 优惠券匹配 → calculate-price 权威核验 → 预算过滤
   │  PlanOption[]
   ▼
rpg.py          RPG 文案：任务名/故事/装备绰号/价格叙事（只包装事实，不改数值）
   │
   ▼
cards.py        分享卡渲染：HTML / Markdown（含项目名 + 非官方声明 + 数据来源）
```

业务逻辑（planner）、工具调用（mcp_*）、游戏文案（rpg）、展示层（cards）严格分离，可单测、可替换。

**真实数据流要点**（2026-10-13 联调实测）：官方 `query-meals` 只返回餐品编码与标签，**没有名称和价格**——名称来自 `query-meal-detail`，价格只来自 `calculate-price`（金额单位为分）。服务端因此采用「短名单 → 详情补名 → 一次算价发现全部单价 → 组合验价」的四段式链路，所有价格均可追溯到权威工具返回。

## 8. 安全与合规

- **Token 不进仓库**：只从环境变量 `MCD_MCP_TOKEN` 读取；`mcp-config.example.json` 仅含占位符；`.gitignore` 显式忽略 `.env` / `mcp-config.json`；
- **审计链路**：每次任务输出 `data_sources` 与 `mcp_calls`，可追溯每个数字的来源；
- **无实际影响操作**：MVP 不接入下单；涉及真实资金/库存的工具（`create-order` 等）在 `TOOLS_RESERVED` 中显式预留；
- **非官方声明**：所有输出（终端 + 卡片）均带非官方项目声明。

## 9. 目录结构

```
burger-quest/
├── README.md                 本文件
├── CONTEST_DECLARATION.md    参赛声明（官方文件，内容未改动）
├── MCP_INTEGRATION.md        MCP 集成说明（工具/调用链/业务价值/SDK 兼容）
├── skill.md                  WorkBuddy Skill 入口
├── workbuddy.md              WorkBuddy 开发上下文（专项奖励材料）
├── mcp-config.example.json   MCP 配置模板（仅占位符）
├── requirements.txt
├── pyproject.toml
├── burger_quest/             引擎源码
│   ├── models.py             数据模型（金额用「分」，杜绝浮点误差）
│   ├── nlparser.py           自然语言解析
│   ├── mcp_base.py           MCP 接口 + 防御式适配
│   ├── mcp_http.py           真实 MCP 客户端（同步门面 + 后台事件循环）
│   ├── mcp_mock.py           演示 MCP 客户端
│   ├── planner.py            套餐规划引擎
│   ├── rpg.py                RPG 文案层
│   ├── cards.py              分享卡渲染
│   ├── service.py            编排层（门店解析/错误处理）
│   └── cli.py                命令行入口
├── fixtures/demo_data.json   演示数据（明确标注非实时）
├── tests/                    99 个测试
└── examples/                 可复现演示与示例卡片
```

## 10. 参赛信息

- 比赛：麦当劳程序员创意开发大赛（<https://tch.cloud.tencent.com/contest/48>）
- 官方 MCP 使用指南：<https://github.com/M-China/mcd-mcp-server>
- 本作品由 WorkBuddy 辅助开发（见 [workbuddy.md](workbuddy.md)）
