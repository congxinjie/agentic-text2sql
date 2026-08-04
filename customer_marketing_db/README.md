# customer_marketing_db

小型客户营销数据库（SQLite），全部为**虚构数据**（随机生成姓名 + 结构化字段），无任何真实个人信息，规避合规问题。固定随机种子，重复运行数据完全一致。

## 用法

```bash
python3 create_db.py   # 建库并灌入虚构数据 -> marketing.db
python3 analysis.py    # 运行 6 个示例营销分析查询
```

## 表结构

| 表 | 字段 | 说明 |
|----|------|------|
| customers | customer_id, name, age_group, region, risk_level | 客户主档：年龄段、地区、风险等级 |
| assets | asset_id, customer_id, product_type, amount, stat_date | 客户资产：产品类型（活期/定期/货币基金/股票基金/理财/保险）、金额、季度统计日 |
| transactions | transaction_id, customer_id, trans_type, amount, trans_date | 交易流水：买入/卖出/转入/转出 |
| campaigns | campaign_id, campaign_name, channel, start_date, end_date, budget | 营销活动：渠道（短信/电话/邮件/APP推送/微信）与预算 |
| contacts | contact_id, customer_id, campaign_id, contact_date, channel, status, outcome | 触达记录：接通状态（已接通/未接通/拒接）与结果（购买/有意向/无意向/无响应） |

## 数据规模（种子=42）

- 200 名客户（年龄 18-25 至 56+ 五档，六个地区，三档风险等级）
- ~1500 条资产记录（每人 1-4 种产品 × 3 个季度统计日）
- 2000 条交易流水（2024-01 ~ 2026-07）
- 10 场营销活动（2025-01 ~ 2026-08）
- ~1000 条触达记录

## 外键关系

```
customers 1─* assets
customers 1─* transactions
customers 1─* contacts *─1 campaigns
```
