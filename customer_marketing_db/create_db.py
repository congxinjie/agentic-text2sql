#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
创建客户营销数据库 marketing.db
全部使用虚构数据（随机生成的中文姓名 + 结构化字段），避免个人信息合规问题。
固定随机种子(42)，重复运行生成完全一致的数据，便于复现。

表结构:
    customers    客户: 客户ID、姓名、年龄段、地区、风险等级
    assets       资产: 客户、产品类型、金额、统计日期
    transactions 交易: 客户、交易类型、金额、交易日期
    campaigns    营销活动: 活动名称、渠道、起止日期、预算
    contacts     触达: 客户、活动、触达日期、渠道、状态、结果
"""

import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

# 建库输出固定落在本文件所在目录(customer_marketing_db/marketing.db), 不随 cwd 漂移。
DB_PATH = str(Path(__file__).resolve().parent / "marketing.db")
rng = random.Random(42)  # 固定种子，数据可复现

# ---------------- 虚构数据素材 ----------------
SURNAMES = list("王李张刘陈杨赵黄周吴徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭")
GIVEN_NAMES = list("伟芳娜敏静磊军洋勇艳杰涛明超霞平刚桂英建华文辉丽强玉兰建国志强晓明海燕国庆")

AGE_GROUPS = [("18-25", 0.15), ("26-35", 0.30), ("36-45", 0.25),
              ("46-55", 0.20), ("56+", 0.10)]
REGIONS = [("华东", 0.30), ("华南", 0.20), ("华北", 0.20),
           ("西南", 0.12), ("东北", 0.10), ("西北", 0.08)]
RISK_LEVELS = [("低风险", 0.40), ("中风险", 0.40), ("高风险", 0.20)]

# 产品类型 -> (最小金额, 最大金额)
PRODUCTS = {
    "活期存款": (1_000, 200_000),
    "定期存款": (10_000, 1_000_000),
    "货币基金": (5_000, 300_000),
    "股票基金": (1_000, 500_000),
    "银行理财": (10_000, 1_000_000),
    "保险": (1_000, 100_000),
}
ASSET_STAT_DATES = ["2025-06-30", "2025-12-31", "2026-06-30"]

TRANS_TYPES = [("买入", 0.45), ("卖出", 0.35), ("转入", 0.10), ("转出", 0.10)]

# (名称, 渠道, 开始, 结束, 预算)
CAMPAIGNS = [
    ("新春理财节", "短信", "2025-01-10", "2025-02-10", 80_000),
    ("基金定投推广", "电话", "2025-03-01", "2025-04-15", 50_000),
    ("新客开户有礼", "APP推送", "2025-05-01", "2025-06-30", 120_000),
    ("保险保障月", "邮件", "2025-07-01", "2025-08-31", 60_000),
    ("金秋资产配置专场", "电话", "2025-09-15", "2025-10-31", 70_000),
    ("年终回馈活动", "短信", "2025-11-20", "2025-12-31", 100_000),
    ("稳健理财升级季", "微信", "2026-01-15", "2026-03-15", 90_000),
    ("高端客户专属沙龙", "电话", "2026-02-01", "2026-03-31", 150_000),
    ("夏日理财节", "短信", "2026-06-01", "2026-07-31", 80_000),
    ("定投赢未来", "邮件", "2026-07-01", "2026-08-31", 55_000),
]

CONTACT_STATUS = [("已接通", 0.60), ("未接通", 0.25), ("拒接", 0.15)]
CONTACT_OUTCOME = [("购买", 0.15), ("有意向", 0.20), ("无意向", 0.30), ("无响应", 0.35)]

SCHEMA = """
DROP TABLE IF EXISTS contacts;
DROP TABLE IF EXISTS campaigns;
DROP TABLE IF EXISTS transactions;
DROP TABLE IF EXISTS assets;
DROP TABLE IF EXISTS customers;

CREATE TABLE customers (
    customer_id INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    age_group   TEXT NOT NULL,
    region      TEXT NOT NULL,
    risk_level  TEXT NOT NULL
);

CREATE TABLE assets (
    asset_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id  INTEGER NOT NULL REFERENCES customers(customer_id),
    product_type TEXT NOT NULL,
    amount       REAL NOT NULL,
    stat_date    TEXT NOT NULL
);

CREATE TABLE transactions (
    transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id    INTEGER NOT NULL REFERENCES customers(customer_id),
    trans_type     TEXT NOT NULL,
    amount         REAL NOT NULL,
    trans_date     TEXT NOT NULL
);

CREATE TABLE campaigns (
    campaign_id   INTEGER PRIMARY KEY,
    campaign_name TEXT NOT NULL,
    channel       TEXT NOT NULL,
    start_date    TEXT NOT NULL,
    end_date      TEXT NOT NULL,
    budget        REAL NOT NULL
);

CREATE TABLE contacts (
    contact_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id  INTEGER NOT NULL REFERENCES customers(customer_id),
    campaign_id  INTEGER NOT NULL REFERENCES campaigns(campaign_id),
    contact_date TEXT NOT NULL,
    channel      TEXT NOT NULL,
    status       TEXT NOT NULL,
    outcome      TEXT NOT NULL
);

CREATE INDEX idx_assets_customer      ON assets(customer_id);
CREATE INDEX idx_transactions_customer ON transactions(customer_id);
CREATE INDEX idx_contacts_customer    ON contacts(customer_id);
CREATE INDEX idx_contacts_campaign    ON contacts(campaign_id);
"""


def weighted(pairs):
    """按 (值, 权重) 列表做加权随机选择。"""
    values = [v for v, _ in pairs]
    weights = [w for _, w in pairs]
    return rng.choices(values, weights=weights, k=1)[0]


def gen_name():
    given = rng.choice(GIVEN_NAMES) + (rng.choice(GIVEN_NAMES) if rng.random() < 0.4 else "")
    return rng.choice(SURNAMES) + given


def gen_customers(n=200):
    rows = []
    for i in range(n):
        rows.append((10_001 + i, gen_name(), weighted(AGE_GROUPS),
                     weighted(REGIONS), weighted(RISK_LEVELS)))
    return rows


def gen_assets(customers):
    rows = []
    for cid, *_ in customers:
        n_products = rng.choices([1, 2, 3, 4], weights=[0.30, 0.35, 0.25, 0.10], k=1)[0]
        products = rng.sample(list(PRODUCTS.keys()), n_products)
        for p in products:
            lo, hi = PRODUCTS[p]
            base = rng.uniform(lo, hi)
            for i, d in enumerate(ASSET_STAT_DATES):
                # 后期统计日资产小幅增长（±8% 漂移）
                drift = 1.0 + i * rng.uniform(0.0, 0.08)
                rows.append((cid, p, round(base * drift, 2), d))
    return rows


def gen_transactions(customers, n=2000):
    rows = []
    start, end = date(2024, 1, 1), date(2026, 7, 31)
    span = (end - start).days
    for _ in range(n):
        cid = rng.choice(customers)[0]
        ttype = weighted(TRANS_TYPES)
        amount = min(2_000_000, max(100, round(rng.lognormvariate(9.2, 1.2), 2)))
        d = start + timedelta(days=rng.randint(0, span))
        rows.append((cid, ttype, amount, d.isoformat()))
    return rows


def gen_contacts(customers, campaigns):
    rows = []
    for cid in range(10_001, 10_001 + len(customers)):
        for cid2, name, channel, s, e, _ in campaigns:
            if rng.random() > 0.5:  # 每个客户约一半活动被触达
                continue
            s_d, e_d = date.fromisoformat(s), date.fromisoformat(e)
            d = s_d + timedelta(days=rng.randint(0, (e_d - s_d).days))
            rows.append((cid, cid2, d.isoformat(), channel,
                         weighted(CONTACT_STATUS), weighted(CONTACT_OUTCOME)))
    return rows


def main():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.executescript(SCHEMA)

    customers = gen_customers()
    cur.executemany("INSERT INTO customers VALUES (?,?,?,?,?)", customers)

    assets = gen_assets(customers)
    cur.executemany("INSERT INTO assets (customer_id, product_type, amount, stat_date) VALUES (?,?,?,?)", assets)

    transactions = gen_transactions(customers)
    cur.executemany("INSERT INTO transactions (customer_id, trans_type, amount, trans_date) VALUES (?,?,?,?)", transactions)

    campaigns = [(i + 1, *c) for i, c in enumerate(CAMPAIGNS)]
    cur.executemany("INSERT INTO campaigns VALUES (?,?,?,?,?,?)", campaigns)

    contacts = gen_contacts(customers, campaigns)
    cur.executemany("INSERT INTO contacts (customer_id, campaign_id, contact_date, channel, status, outcome) VALUES (?,?,?,?,?,?)", contacts)

    conn.commit()

    # 完整性自检
    assert cur.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert cur.execute("PRAGMA foreign_key_check").fetchall() == []
    for t in ("customers", "assets", "transactions", "campaigns", "contacts"):
        n = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t:<14} {n:>6} 行")
    conn.close()
    print(f"[OK] 数据库已生成: {DB_PATH}")


if __name__ == "__main__":
    main()
