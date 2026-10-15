# 可复现演示流程（examples/demo.md）

本文档记录演示场景的完整命令与预期输出。**任何人按步骤执行均可复现**，所有数字均为当前引擎实测输出（2026-10-14，257 个测试全绿版本）。

> 📌 **数据模式声明**：本文档全部场景使用包内演示数据（`burger_quest/data/demo_data.json`，结构与麦当劳 MCP 真实返回兼容，**数值非实时**），输出中会明确标注 `DEMO · 离线演示数据`。**不得**将本文件的输出描述为「刚刚查询到的真实菜单」。真实 MCP 联调实测记录见 [MCP_INTEGRATION.md](../MCP_INTEGRATION.md) §3.5/§3.7。

## 0. 环境准备与演示顺序

```bash
# 1) 克隆并安装（Python >= 3.10；演示数据随包分发）
git clone https://github.com/yaohanbo1-hue/burger-quest.git
cd burger-quest
pip install -e ".[dev]"        # 或 pip install .

# 2) 跑测试确认环境正常（预期 257 passed）
python -m pytest tests/ -q

# 3) 一键运行全部演示场景（本目录的统一入口，跨平台）
python examples/run_demos.py            # 全部 8 个场景
python examples/run_demos.py --quick    # 只跑 4 个核心场景
python examples/run_demos.py --cards    # 运行并重新生成示例成就卡
```

**推荐演示顺序**：场景 1（预算受限辣味）→ 场景 2（两人用餐）→ 场景 3（营养偏好）→ 场景 4（预算不足的诚实失败）→ 附加场景按兴趣选看。

## 1. 场景一：预算受限的辣味偏好（外送）

```bash
python -m burger_quest demo "我想吃辣，预算35，外送到南山科技园，一个人" \
  --card-html examples/cards/demo_spicy.html \
  --card-md examples/cards/demo_spicy.md
```

- **用户输入**：辣味、预算 ¥35、外送、南山科技园、1 个人；
- **预期输出**（节选）：
  - 推荐装备：麦辣鸡腿堡（¥21.00）+ 麦辣鸡翅 2 块（¥12.00）+ 满30减5券 + 麦辣堡立减3元券；
  - 价格结算：原价 ¥33.00 − 已核实优惠 ¥8.00 + 配送费 ¥9.00 = **应付 ¥34.00**（calculate-price 权威结果），剩余预算 ¥1.00；
  - 属性面板：整餐热量约 840 千卡、蛋白质约 41g（演示营养数据汇总）；
- **验证重点**：多条件解析（预算/人数/用餐方式/位置/口味）互不干扰；双券叠加效果经权威核验；配送费单独列示；总价绝不等于菜单价加总（33 − 8 + 9 = 34 由官方算价工具返回）；
- **退出码**：0（生成方案）。

## 2. 场景二：两人用餐的预算规划（丰盛偏好）

```bash
python -m burger_quest demo "两个人想吃点好的，预算100，堂食" \
  --card-html examples/cards/demo_share.html --card-md examples/cards/demo_share.md
```

- **用户输入**：2 个人、「想吃点好的」（丰盛偏好）、预算 ¥100、堂食；
- **预期输出**（节选）：
  - 推荐装备：麦辣鸡腿堡套餐（¥32.00）+ 巨无霸套餐（¥36.00）+ 麦乐鸡 5 块 ×2（¥23.00）；
  - 价格结算：原价 ¥91.00 − 优惠 ¥5.00 = **应付 ¥86.00**，剩余预算 ¥14.00；
- **验证重点**：多人模板给足主食份数（2 份主食够 2 人分食，会在理由中明示）；丰盛偏好在预算内优先更丰富组合；优惠经官方核验后才计入；
- **退出码**：0。

## 3. 场景三：营养偏好组合搜索（低卡）

```bash
python -m burger_quest demo "减肥中，想吃低卡的，预算40，到店自取" \
  --card-html examples/cards/demo_light.html --card-md examples/cards/demo_light.md
```

- **用户输入**：「减肥中，想吃低卡的」、预算 ¥40、到店自取；
- **预期输出**（节选）：
  - 推荐装备：双层吉士堡（¥13.00）+ 玉米杯（小）（¥7.00）+ 零度可乐（大）（¥10.00）；
  - 价格结算：原价 ¥30.00 − 优惠 ¥5.00 = **应付 ¥25.00**，剩余 ¥15.00；
  - 属性面板：整餐热量约 540 千卡，满足 ≤600 千卡偏好；
- **验证重点**：低卡偏好按「单人单餐 ≤600 千卡」筛选，并明确提示「饮食偏好，不构成医疗或减重建议」；热量数字全部来自演示营养数据，**匹配不到的餐品会标注「未知」而不会估算**；
- **退出码**：0。

## 4. 场景四：预算不足（诚实失败）

```bash
python -m burger_quest demo "预算5块，想吃汉堡"
```

- **用户输入**：预算 ¥5、想吃汉堡；
- **预期输出**（节选）：
  - ⚠️ 「没有组合能落在 ¥5.00 预算内（2 个候选组合因估算价超出预算被跳过，其余逐一用权威价格核验；**未编造任何价格**）。可尝试：适当提高预算，或减少人数/品类要求」；
  - ❌ 未生成方案——引擎不会编造餐品、价格或优惠；
- **验证重点**：**失败场景比成功场景更能体现产品原则**——明确说明原因、给出可操作的调整建议、退出码为 1、不输出任何虚假成功套餐；
- **退出码**：1（预期失败）。

## 5. 附加场景（按兴趣选看）

| 场景 | 命令 | 验证重点 |
| --- | --- | --- |
| A · 多条件综合 | `demo "预算 35 元，想吃辣，两个人，希望尽量高蛋白，不想点太多饮料"` | 模糊预算+蛋白偏好+饮料软上限；最多 3 个可比较方案 |
| B · 超预算参考 | `demo "预算12块，想吃辣"` | 给出最接近真实方案（¥13）并标注超预算 ¥1，不篡改价格 |
| C · 品类硬避免 | `demo "不要饮料，预算20"` | 组合完全不含饮品 |
| D · 多家候选门店 | `demo "在演示门店吃，到店自取，预算35"` | 列出候选门店并暂停，不默默选择 |
| E · 否定口味 | `demo "不吃辣，想吃脆的，预算50"` | 「不吃辣」是硬约束，不被「脆」覆盖；方案无辣味餐品 |
| F · 地点模糊 | `quest "帮我规划一顿麦当劳"`（**需 MCD_MCP_TOKEN**） | 简洁追问地址/门店编码，不假设门店；未配置 Token 时明确报错（退出码 2），不回退演示数据 |

## 6. 示例成就卡（实际运行生成）

核心场景的 HTML/Markdown 成就卡由上述命令实际生成，位于 `examples/cards/`：

| 卡片 | 数据模式 | 文件 |
| --- | --- | --- |
| 辣味外送成就卡 | DEMO | [demo_spicy.html](cards/demo_spicy.html) · [demo_spicy.md](cards/demo_spicy.md) · [预览图](cards/demo_spicy.png) |
| 多人丰盛成就卡 | DEMO | [demo_share.html](cards/demo_share.html) · [demo_share.md](cards/demo_share.md) |
| 低卡轻食成就卡 | DEMO | [demo_light.html](cards/demo_light.html) · [demo_light.md](cards/demo_light.md) |
| 真实 MCP 成就卡 | **LIVE SNAPSHOT**（历史快照） | [live_real.html](cards/live_real.html) · [live_real.md](cards/live_real.md) · [预览图](cards/live_real.png) |

`live_real.*` 为真实 MCP Token 联调时的实际输出（门店 1420762），卡片带 `LIVE SNAPSHOT · 真实 MCP 历史实测` 徽章、生成时间与商品编码溯源，并明确声明「金额仅代表该次查询时的返回结果，不代表当前价格、优惠或库存」；DEMO 卡则全程标注演示数据。重新生成快照卡：`quest "…" --store-code <编码> --card-snapshot --card-html examples/cards/live_real.html`。

## 7. 附：查看引擎使用的 MCP 工具

```bash
python -m burger_quest tools
```

输出 8 个实际调用工具与预留工具清单（create-order / cancel-order / auto-bind-coupons 等写操作**不接入**）。

> 备注：demo 场景未提供位置时使用默认演示门店（DEMO-001）并在提示中明确声明；真实模式没有位置信息时会要求补充，不会假设门店。
