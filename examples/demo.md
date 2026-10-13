# 可复现演示流程（examples/demo.md）

本文档记录演示场景的完整命令与预期输出，任何人按步骤执行均可复现。
所有 demo 场景使用离线 Mock 数据（`fixtures/demo_data.json`，结构与真实 MCP 返回兼容），**不需要 MCP Token**；
真实模式的联调实测记录见 [MCP_INTEGRATION.md](../MCP_INTEGRATION.md) §3.5。

## 环境准备

```bash
cd burger-quest
pip install -r requirements.txt   # 引擎核心零依赖；mcp SDK 仅真实模式需要
python -m pytest tests/ -q        # 预期：99 passed
```

## 场景 1：辣味外送（单人紧预算）

```bash
python -m burger_quest demo "我想吃辣，预算35，外送到南山科技园，一个人" \
  --card-html examples/cards/demo_spicy.html \
  --card-md examples/cards/demo_spicy.md \
  --json examples/cards/demo_spicy.json
```

预期要点：

- 解析结果：预算 ¥35、1 人、外送、位置南山科技园、口味辣味；
- 演示模式走配送地址流程（fixtures 内置一个演示地址）→ 默认演示门店；
- 数据链：16 个菜单编码 → 10 个餐品详情 → 10 个真实（演示）价格；
- 推荐装备：麦辣鸡腿堡（¥21.00）+ 麦辣鸡腿堡立减3元（演示券）；
- 价格结算：原价 ¥21.00 − 优惠 ¥3.00 + 配送费 ¥9.00 = **应付 ¥27.00**，剩余预算 ¥8.00；
- 应付总价标注「calculate-price 权威结果」；数据来源注明演示数据。

## 场景 2：两人丰盛（多人分享）

```bash
python -m burger_quest demo "两个人想吃点好的，预算100，堂食" \
  --card-html examples/cards/demo_share.html --card-md examples/cards/demo_share.md
```

预期要点：识别「丰盛偏好」→ 优先套餐；装备为麦辣鸡腿堡套餐 + 巨无霸套餐，原价 ¥68.00，满30减5 后 **¥63.00**。

## 场景 3：低卡轻食（营养偏好）

```bash
python -m burger_quest demo "减肥中，想吃低卡的，预算40，到店自取" \
  --card-html examples/cards/demo_light.html --card-md examples/cards/demo_light.md
```

预期要点：识别低卡偏好（≤600 千卡）→ 双层吉士堡 + 玉米杯 + 零度可乐（无糖标签命中），整餐约 540 千卡，原价 ¥30.00，叠券后 ¥25.00。

## 场景 4：预算不足（错误处理）

```bash
python -m burger_quest demo "预算5块，想吃汉堡"
```

预期要点：**明确失败**——「没有组合能落在 ¥5.00 预算内（…未编造任何价格）」，退出码 1，不生成任何方案。

## 场景 5：多家候选门店（不默默选择）

```bash
python -m burger_quest demo "在演示门店吃，到店自取，预算35"
```

预期要点：「演示门店」同时匹配两家演示门店 → 列出候选（DEMO-001 / DEMO-002）并暂停，提示用 `--store-code` 重新运行；**不查询菜单、不生成任何价格**，退出码 1。

## 场景 6：外送但无保存地址（错误处理）

```bash
python -m burger_quest demo "外送到一个没有地址的地方，预算35"
```

预期要点：明确说明「外送（麦乐送）需要先在麦当劳官方 App/小程序保存配送地址；当前账户没有可用地址」，退出码 1，不编造门店与价格。
（演示 fixtures 内置一个演示配送地址；将 `delivery_addresses` 清空即可复现本场景。）

## 附 1：查看引擎使用的 MCP 工具

```bash
python -m burger_quest tools
```

预期输出 8 个实际调用工具（now-time-info / delivery-query-addresses / delivery-query-stores / query-nearby-stores / query-meals / query-meal-detail / query-store-coupons / query-my-coupons / list-nutrition-foods / calculate-price）与预留工具清单（available-coupons / auto-bind-coupons / create-order / cancel-order / query-order 等）。

## 附 2：查询附近门店（获取门店编码）

```bash
python -m burger_quest stores "南山科技园" --demo    # 演示模式
python -m burger_quest stores "深圳市南山区科技园"    # 真实模式（需 MCD_MCP_TOKEN）
```

预期要点：输出门店列表（名称 + 编码 + 地址），并提示用 `--store-code` 继续。真实模式实测（2026-10-13）：「深圳市南山区科技园」返回 5 家真实门店。

> 备注：demo 场景未提供位置时使用默认演示门店（DEMO-001）并在提示中明确声明；真实模式没有位置信息时会要求补充，不会假设门店。
