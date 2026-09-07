# -*- coding: utf-8 -*-
"""按 docs/数据说明.md §3 预处理流程重建 enterprise.db(纯标准库, 无 pandas)。

流程:
    1. 读数据集目录 8 张 UTF-8 CSV(csv 模块, utf-8-sig 去 BOM)
    2. 数值列(金额/份额/数量/年龄)转 REAL, ID 与日期(data_dt)转 TEXT
    3. 行数校验与源 CSV 一致(与 docs/数据说明.md §3 期望值比对, 不一致即失败)
    4. 建 pty_id 索引 + dim_product 产品名/二级分类索引
    5. 输出 <数据集目录>/enterprise.db(覆盖旧库; 输出本身是生成物, *.db 已 gitignore)

用法:
    python benchmark/build_db.py [输出路径, 默认数据集目录/enterprise.db]
"""
import csv
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "Agentic智能问数在客户营销场景的应用数据集"
DEFAULT_DB = DATA_DIR / "enterprise.db"

# 表 -> (CSV 文件名, 期望行数, 数值列集合)
SPEC = {
    "dim_branch": ("dim_branch_202606021048.csv", 312, set()),
    "dim_public": ("dim_public_202606021050.csv", 155, set()),
    "dim_product": ("dim_product_202606021049.csv", 334694, set()),
    "ads_cust_info_d": ("ads_cust_info_d_202606031625.csv", 500, {"cust_age"}),
    "dwd_cust_hold_d": ("dwd_cust_hold_d_202606021051.csv", 408150, {"hold_cnt", "mkt_val"}),
    "dwd_cust_tran_d": ("dwd_cust_tran_d_202606021051.csv", 39060,
                        {"buy_cnt", "buy_mnt", "buy_rake", "buy_amt", "buy_fare",
                         "sell_cnt", "sell_mnt", "sell_rake", "sell_amt", "sell_fare"}),
    "dws_cust_aset_d": ("dws_cust_aset_d_202606021051.csv", 43684,
                        {"nm_tot_aset", "nm_bal", "fc_pur_aset", "fc_bal"}),
    "dws_cust_fin_d": ("dws_cust_fin_d_202606021050.csv", 4892,
                       {"cash_in", "cash_out", "tran_in", "tran_out", "assign_in", "assign_out"}),
}


def to_real(v, col, numeric_cols):
    if col not in numeric_cols:
        return v
    if v is None or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def build(out_path: Path) -> None:
    if out_path.exists():
        out_path.unlink()
    conn = sqlite3.connect(str(out_path))

    for tbl, (fn, expected, numeric_cols) in SPEC.items():
        csv_path = DATA_DIR / fn
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            headers = [h.strip() for h in next(reader)]
            coldefs = []
            for h in headers:
                coldefs.append(f'"{h}" ' + ("REAL" if h in numeric_cols else "TEXT"))
            conn.execute(f'CREATE TABLE "{tbl}" ({", ".join(coldefs)})')
            rows = []
            n = 0
            for raw in reader:
                rows.append(tuple(to_real(v.strip() if isinstance(v, str) else v, h, numeric_cols)
                                  for v, h in zip(raw, headers)))
                n += 1
                if len(rows) >= 20000:
                    conn.executemany(
                        f'INSERT INTO "{tbl}" VALUES ({", ".join("?" * len(headers))})', rows)
                    rows = []
            if rows:
                conn.executemany(
                    f'INSERT INTO "{tbl}" VALUES ({", ".join("?" * len(headers))})', rows)
        if n != expected:
            conn.close()
            sys.exit(f"[失败] {tbl} 行数 {n} != 期望 {expected}(与 docs/数据说明.md §3 不一致)")
        print(f"  loaded {tbl:<16} rows={n} numeric={sorted(numeric_cols) or '无'}")

    # 索引: pty_id(有该列的表)+ 产品名/二级分类
    for tbl in SPEC:
        if tbl != "dim_product" and tbl != "dim_branch" and tbl != "dim_public":
            conn.execute(f'CREATE INDEX idx_{tbl}_pt ON "{tbl}" (pty_id)')
    conn.execute("CREATE INDEX idx_prod_name ON dim_product (prdt_name)")
    conn.execute("CREATE INDEX idx_prod_type ON dim_product (prdt_type_name)")
    conn.commit()
    conn.close()
    print(f"\n[OK] 已写入 {out_path}({out_path.stat().st_size} bytes)")


def main():
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB
    build(out)


if __name__ == "__main__":
    main()
