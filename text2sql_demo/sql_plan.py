# -*- coding: utf-8 -*-
"""SQLite 查询计划分析器(纯标准库): EXPLAIN QUERY PLAN -> 全表扫描/索引命中/临时B树。
不执行数据, 只看执行计划; 服务于查询优化与工程报告。零第三方依赖。
"""
import sqlite3


def _connect(db_path):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.execute("PRAGMA query_only=ON")
    return con


def list_indexes(db_path):
    """返回 {表名: [索引名, ...]}(排除 sqlite_autoindex)。"""
    con = _connect(db_path)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type=\x27table\x27 AND name NOT LIKE \x27sqlite_%\x27")]
        out = {}
        for t in tables:
            q = t.replace(chr(34), chr(34) + chr(34))
            names = []
            for r in con.execute(f"PRAGMA index_list(\"{q}\")"):
                name = str(r[1])
                if not name.startswith("sqlite_"):
                    names.append(name)
            out[t] = names
        return out
    finally:
        con.close()


def explain_plan(db_path, sql):
    """只读跑 EXPLAIN QUERY PLAN(不取数据), 返回 detail 字符串列表。"""
    con = _connect(db_path)
    try:
        return [str(r[3]) for r in con.execute("EXPLAIN QUERY PLAN " + sql)]
    finally:
        con.close()


def analyze(db_path, sql):
    """分析执行计划: 全表扫描 / 索引扫描 / 索引搜索 / 临时B树。"""
    details = explain_plan(db_path, sql)
    full_scans, index_scans, searches = [], [], []
    for d in details:
        up = d.upper()
        if "SEARCH" in up:
            searches.append(d)
        elif "SCAN" in up:
            if "USING INDEX" in up or "USING COVERING INDEX" in up:
                index_scans.append(d)
            else:
                full_scans.append(d)
    return {
        "details": details,
        "full_scans": full_scans,
        "index_scans": index_scans,
        "searches": searches,
        "n_full_scan": len(full_scans),
        "n_index_scan": len(index_scans),
        "n_search": len(searches),
        "used_index": bool(searches) or bool(index_scans),
        "temp_btree": any("TEMP B-TREE" in d.upper() for d in details),
    }
