# 可复现演示流程（examples/demo.md）

本文档记录四个演示场景的完整命令与预期输出，任何人按步骤执行均可复现。
所有演示使用离线 Mock 数据（`fixtures/demo_data.json`），**不需要 MCP Token**；
真实模式见每节末尾的对照命令。

## 环境准备

```bash
cd burger-quest
pip install -r requirements.txt   # 引擎核心零依赖；mcp SDK 仅真实模式需要
python -m pytest tests/ -q        # 预期：30 passed
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
- 推荐装备：麦辣鸡腿堡（¥21.00）+ 麦辣鸡腿堡立减3元（演示券）；
- 价格结算：原价 ¥21.00 − 优惠 ¥3.00 + 配送费 ¥9.00 = **应付 ¥27.00**，剩余预算 ¥8.00；
- 应付总价标注「calculate-price 权威结果」；数据来源注明演示数据。

真实模式（需 `export MCD_MCP_TOKEN=...`）：

```bash
python -m burger_quest quest "我想吃辣，预算35，外送到南山科技园，一个人"
```

## 场景 2：两人丰盛（多人分享）

```bash
python -m burger_quest demo "两个人想吃点好的，预算100，堂食" \
  --card-html examples/cards/demo_share.html --card-md examples/cards/demo_share.md
```

预期要点：识别「丰盛偏好」→ 优先套餐与预算充分利用；装备为麦辣鸡腿堡套餐 + 巨无霸套餐 + 玉米杯×2 + 热红茶×2，原价 ¥98.00，满30减5 后 ¥93.00。

## 场景 3：低卡轻食（营养偏好）

```bash
python -m burger_quest demo "减肥中，想吃低卡的，预算40，到店自取" \
  --card-html examples/cards/demo_light.html --card-md examples/cards/demo_light.md
```

预期要点：识别低卡偏好（≤600 千卡）→ 麦香鱼 + 玉米杯 + 热红茶，整餐约 475 千卡，应付 ¥30.00。

## 场景 4：预算不足（错误处理）

```bash
python -m burger_quest demo "预算5块，想吃汉堡"
```

预期要点：**明确失败**——「没有组合能落在 ¥5.00 预算内（候选组合因估算价超出预算被跳过；未编造任何价格）」，退出码 1，不生成任何方案。

## 场景 5：多家候选门店（不默默选择）

```bash
python -m burger_quest demo "外送到演示门店，预算35"
```

预期要点：「演示门店」同时匹配两家演示门店 → 列出候选（DEMO-001 / DEMO-002）并暂停，提示用 `--store-code` 重新运行；**不查询菜单、不生成任何价格**，退出码 1。

## 附 1：查看引擎使用的 MCP 工具

```bash
python -m burger_quest tools
```

预期输出 7 个实际调用工具（now-time-info / query-nearby-stores / query-meals / query-store-coupons / query-my-coupons / list-nutrition-foods / calculate-price）与预留工具清单（query-meal-detail / available-coupons / auto-bind-coupons / create-order / cancel-order / query-order）。

## 附 2：查询附近门店（获取门店编码）

```bash
python -m burger_quest stores "南山科技园" --demo    # 演示模式
python -m burger_quest stores "南山科技园"           # 真实模式（需 MCD_MCP_TOKEN）
```

预期要点：输出门店列表（名称 + 编码 + 地址），并提示用 `--store-code` 继续。

> 备注：场景 2 / 3 未提供位置，演示模式会使用默认演示门店（DEMO-001）并在提示中明确声明；真实模式没有位置信息时会要求补充，不会假设门店。
