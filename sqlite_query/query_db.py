#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
固定 SQL 查询 SQLite 数据库并打印结果

用法:
    python3 query_db.py             # 执行 FIXED_SQL 并打印结果
    python3 query_db.py --init-demo # 生成演示数据库 demo.db（含示例数据）
    python3 query_db.py -f xxx.db   # 指定数据库文件（默认 demo.db）

要查询自己的数据: 改下面配置区的 DB_PATH 和 FIXED_SQL 即可。
"""

import sqlite3
import sys
import unicodedata
from pathlib import Path


def disp_width(s: str) -> int:
    """计算字符串在终端中的显示宽度（CJK 字符按 2 列算）。"""
    return sum(
        2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        for ch in str(s)
    )


def pad(s: str, width: int) -> str:
    """按显示宽度右补空格。"""
    return str(s) + " " * max(0, width - disp_width(s))

# ================= 配置区：改这里 =================
DB_PATH = "demo.db"

FIXED_SQL = """
SELECT u.id, u.name, u.age, u.city, COUNT(o.id) AS order_count
FROM users u
LEFT JOIN orders o ON o.user_id = u.id
GROUP BY u.id
ORDER BY u.id
"""
# ==================================================


def init_demo_db(db_path: str) -> None:
    """创建演示数据库: users + orders 两张表, 带示例数据。"""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.executescript(
        """
        DROP TABLE IF EXISTS orders;
        DROP TABLE IF EXISTS users;
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            age INTEGER,
            city TEXT
        );
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            amount REAL
        );
        INSERT INTO users (id, name, age, city) VALUES
            (1, '张三', 28, '北京'),
            (2, '李四', 35, '上海'),
            (3, '王五', 24, '深圳');
        INSERT INTO orders (id, user_id, amount) VALUES
            (1, 1, 99.5), (2, 1, 150.0), (3, 2, 320.0), (4, 3, 45.5);
        """
    )
    conn.commit()
    conn.close()
    print(f"[OK] 演示数据库已生成: {db_path}")


def print_table(rows: list[tuple], headers: list[str]) -> None:
    """按列宽对齐打印结果表格。"""
    if not rows:
        print("(无结果)")
        return

    # 计算每列最大显示宽度
    widths = [
        max(disp_width(h), *(disp_width(r[i]) for r in rows))
        for i, h in enumerate(headers)
    ]

    def fmt(row) -> str:
        return "  ".join(pad(v, widths[i]) for i, v in enumerate(row))

    sep = "  ".join("-" * w for w in widths)
    print(fmt(headers))
    print(sep)
    for r in rows:
        print(fmt(r))
    print(f"\n共 {len(rows)} 行")


def query(db_path: str, sql: str) -> None:
    """执行 SQL 并打印结果。"""
    if not Path(db_path).exists():
        print(f"[错误] 数据库文件不存在: {db_path}", file=sys.stderr)
        print(f"提示: 先运行 python3 {Path(__file__).name} --init-demo 生成演示库",
              file=sys.stderr)
        sys.exit(1)

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row  # 支持按列名访问
        cur = conn.cursor()
        cur.execute(sql)

        headers = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()
        print(f"SQL: {sql.strip()}\n")
        if headers:
            print_table([tuple(r) for r in rows], headers)
        else:
            print(f"执行成功, 影响 {cur.rowcount} 行")
    except sqlite3.Error as e:
        print(f"[错误] SQLite 执行失败: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    if "--init-demo" in sys.argv:
        init_demo_db(DB_PATH)
        sys.exit(0)

    if "-f" in sys.argv:
        i = sys.argv.index("-f")
        if i + 1 < len(sys.argv):
            DB_PATH = sys.argv[i + 1]

    query(DB_PATH, FIXED_SQL)
