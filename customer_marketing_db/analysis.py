#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
客户营销数据库示例分析查询。
用法: python3 analysis.py   （需先运行 create_db.py 生成 marketing.db）
"""

import sqlite3
import unicodedata
from pathlib import Path

# 与 create_db.py 同源: 固定读本目录下的 marketing.db, 不随 cwd 漂移。
DB_PATH = str(Path(__file__).resolve().parent / "marketing.db")

QUERIES = [
    ("1. 客户年龄段分布", """
        SELECT age_group AS 年龄段, COUNT(*) AS 客户数,
               ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM customers), 1) AS 占比pct
        FROM customers GROUP BY age_group ORDER BY 客户数 DESC
    """),
    ("2. 各地区风险等级分布", """
        SELECT region AS 地区, risk_level AS 风险等级, COUNT(*) AS 客户数
        FROM customers GROUP BY region, risk_level ORDER BY region, 客户数 DESC
    """),
    ("3. 总资产 TOP10 客户（2026-06-30 快照）", """
        SELECT c.customer_id, c.name, c.region, c.risk_level,
               ROUND(SUM(a.amount), 2) AS 总资产
        FROM assets a JOIN customers c ON c.customer_id = a.customer_id
        WHERE a.stat_date = '2026-06-30'
        GROUP BY c.customer_id ORDER BY 总资产 DESC LIMIT 10
    """),
    ("4. 各产品类型资产规模（2026-06-30 快照）", """
        SELECT product_type AS 产品类型, COUNT(DISTINCT customer_id) AS 客户数,
               ROUND(SUM(amount), 2) AS 总资产
        FROM assets WHERE stat_date = '2026-06-30'
        GROUP BY product_type ORDER BY 总资产 DESC
    """),
    ("5. 营销活动效果对比", """
        SELECT c.campaign_name AS 活动, ct.channel AS 渠道,
               COUNT(*) AS 触达数,
               SUM(CASE WHEN ct.status = '已接通' THEN 1 ELSE 0 END) AS 接通数,
               ROUND(100.0 * SUM(CASE WHEN ct.status = '已接通' THEN 1 ELSE 0 END) / COUNT(*), 1) AS 接通率pct,
               SUM(CASE WHEN ct.outcome = '购买' THEN 1 ELSE 0 END) AS 购买数,
               ROUND(100.0 * SUM(CASE WHEN ct.outcome = '购买' THEN 1 ELSE 0 END) / COUNT(*), 1) AS 购买率pct
        FROM contacts ct JOIN campaigns c ON c.campaign_id = ct.campaign_id
        GROUP BY c.campaign_id ORDER BY 购买率pct DESC
    """),
    ("6. 2025 年月度交易额趋势", """
        SELECT substr(trans_date, 1, 7) AS 月份, COUNT(*) AS 笔数,
               ROUND(SUM(amount), 2) AS 交易总额
        FROM transactions
        WHERE trans_date >= '2025-01-01' AND trans_date < '2026-01-01'
        GROUP BY 月份 ORDER BY 月份
    """),
]


def disp_width(s):
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in str(s))


def pad(s, width):
    return str(s) + " " * max(0, width - disp_width(s))


def print_table(rows, headers):
    if not rows:
        print("(无结果)")
        return
    widths = [max(disp_width(h), *(disp_width(r[i]) for r in rows)) for i, h in enumerate(headers)]
    fmt = lambda row: "  ".join(pad(v, widths[i]) for i, v in enumerate(row))
    print(fmt(headers))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print(fmt(r))
    print(f"共 {len(rows)} 行")


def main():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    for title, sql in QUERIES:
        print(f"\n=== {title} ===")
        cur.execute(sql)
        headers = [d[0] for d in cur.description]
        print_table([tuple(r) for r in cur.fetchall()], headers)
    conn.close()


if __name__ == "__main__":
    main()
