#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能问数 Demo —— 最短闭环:
    自然语言问题 -> LLM 生成只读 SQL -> 执行 -> 展示 SQL 与结果

用法:
    python3 text2sql.py "华东地区有多少客户？"
    python3 text2sql.py --db ../customer_marketing_db/marketing.db "2025年哪个月交易额最高？"
    python3 text2sql.py            # 无参数进入交互模式

配置(环境变量或 .env.local):
    LLM_API_KEY   必填, DeepSeek API Key
    LLM_BASE_URL  默认 https://api.deepseek.com
    LLM_MODEL     默认 deepseek-v4-flash

安全设计(只读三保险):
    1. 以 mode=ro 打开数据库(文件系统层只读)
    2. 连接后 PRAGMA query_only=ON(sqlite 层拒绝写)
    3. 生成的 SQL 先经 validate_sql() 校验: 仅允许单条
       SELECT/WITH/EXPLAIN 语句, 剥离注释, 拒绝多语句
"""

import json
import os
import re
import sqlite3
import sys
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")
MODEL = os.environ.get("LLM_MODEL", "deepseek-v4-flash")
MAX_ROWS = 100
ROW_LIMIT_HINT = 50

HERE = Path(__file__).resolve().parent

SYSTEM_PROMPT = """你是 SQLite 数据库的只读查询助手。根据给定的数据库结构，把用户的中文问题翻译成一条 SQL 查询。

数据库是银行客户营销库，业务含义:
- customers 客户主档, 每客户一行: name=姓名, age_group=年龄段, region=地区, risk_level=风险等级
- assets 资产快照, 每客户×产品×统计日一行: product_type=产品类型, amount=金额, stat_date=统计日期(季度末)
- transactions 交易流水, 每笔一行: trans_type=买入/卖出/转入/转出, amount=金额, trans_date=交易日期
- campaigns 活动主档: campaign_name=名称, channel=渠道, start_date/end_date=活动期, budget=预算
- contacts 触达记录, 客户×活动一行: status=已接通/未接通/拒接, outcome=购买/有意向/无意向/无响应

按问题类型选表:
- 问客户数/年龄段/地区/风险等级 -> 只查 customers
- 问交易额/交易量/交易趋势 -> 只查 transactions
- 问资产/产品规模/产品类型 -> 只查 assets
- 问活动效果/购买率/接通率 -> contacts JOIN campaigns

示例:
- "华东地区有多少客户" -> SELECT COUNT(*) FROM customers WHERE region = '华东'
- "2025年哪个月交易额最高" -> SELECT substr(trans_date,1,7) AS 月份, SUM(amount) FROM transactions WHERE substr(trans_date,1,4)='2025' GROUP BY substr(trans_date,1,7) ORDER BY SUM(amount) DESC LIMIT 1
- "哪个活动购买率最高" -> 用 contacts 按 campaign_id 统计 outcome='购买' 的占比

硬性规则:
1. 只输出一条 SQL 语句本身, 不要任何解释、注释、markdown 代码块标记
2. 只允许 SELECT / WITH / EXPLAIN 开头的只读语句
3. 表名和列名必须严格来自给定的结构, 不要臆造
4. 使用 SQLite 语法
5. 金额是 REAL, 日期是 TEXT(YYYY-MM-DD), 需要时用 ROUND() / substr() 处理
6. 聚合结果加 ORDER BY, 明细查询加 LIMIT"""


def load_api_key() -> str:
    env = os.environ.get("LLM_API_KEY")
    if env:
        return env
    env_file = HERE / ".env.local"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("LLM_API_KEY="):
                return line.split("=", 1)[1].strip()
    print("[错误] 未找到 LLM_API_KEY。请在环境变量或 .env.local 中配置。", file=sys.stderr)
    sys.exit(1)


def build_schema(db_path: str) -> str:
    """从 sqlite_master 提取表结构 + 每表 2 行样例数据。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cur = conn.cursor()
    parts = []
    for (ttype, name, ddl) in cur.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ):
        if ddl:
            parts.append(f"-- {ttype}: {name}\n{ddl};")
        try:
            rows = cur.execute(f'SELECT * FROM "{name}" LIMIT 2').fetchall()
            cols = [d[0] for d in cur.description]
            for r in rows:
                vals = ", ".join(repr(str(v)[:24]) if v is not None else "NULL" for v in r)
                parts.append(f"-- 样例: ({vals})")
        except sqlite3.Error:
            pass
    conn.close()
    return "\n\n".join(parts)


def strip_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql


def split_statements(sql: str) -> list[str]:
    """按分号切分语句, 忽略单引号字符串字面量内的分号(含 '' 转义)。"""
    stmts, cur, in_str = [], [], False
    i = 0
    while i < len(sql):
        c = sql[i]
        if in_str:
            cur.append(c)
            if c == "'":
                if i + 1 < len(sql) and sql[i + 1] == "'":  # '' 转义
                    cur.append(sql[i + 1])
                    i += 1
                else:
                    in_str = False
        else:
            if c == "'":
                in_str = True
                cur.append(c)
            elif c == ";":
                stmts.append("".join(cur))
                cur = []
            else:
                cur.append(c)
        i += 1
    stmts.append("".join(cur))
    return [s.strip() for s in stmts if s.strip()]


def validate_sql(sql: str) -> str:
    """校验并规整 SQL: 只允许单条只读语句。返回清理后的 SQL。"""
    cleaned = strip_comments(sql).strip()
    if not cleaned:
        raise ValueError("LLM 返回空内容")
    stmts = split_statements(cleaned)
    if len(stmts) > 1:
        raise ValueError(f"多语句输入被拒绝(共 {len(stmts)} 条)")
    first_stmt = stmts[0]
    if not re.match(r"^(SELECT|WITH|EXPLAIN)\b", first_stmt, re.I):
        raise ValueError(f"非只读语句被拒绝: {first_stmt[:60]!r}")
    return first_stmt


def llm_generate_sql(schema: str, question: str, api_key: str) -> str:
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",
             "content": f"数据库结构:\n{schema}\n\n用户问题: {question}\n\n请只输出 SQL。"},
        ],
        "temperature": 0,
        "max_tokens": 1000,
        "stream": False,
    }
    req = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"LLM API HTTP {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"LLM API 连接失败: {e.reason}") from e
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"LLM 响应格式异常: {str(data)[:300]}") from e


def run_query(db_path: str, sql: str):
    """在只读连接上执行 SQL, 返回 (列名, 行列表, 是否截断)。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        cur = conn.execute(sql)
        if cur.description is None:
            raise ValueError("语句不产生查询结果")
        headers = [d[0] for d in cur.description]
        rows = cur.fetchmany(MAX_ROWS + 1)
        truncated = len(rows) > MAX_ROWS
        return headers, rows[:MAX_ROWS], truncated
    finally:
        conn.close()


# ---------- 展示 ----------
def disp_width(s):
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in str(s))


def pad(s, width):
    return str(s) + " " * max(0, width - disp_width(s))


def print_table(rows, headers):
    if not rows:
        print("(无结果)")
        return
    trunc = lambda v: (str(v)[:30] + "…") if len(str(v)) > 30 else str(v)
    rows = [[trunc(v) for v in r] for r in rows]
    widths = [max(disp_width(h), *(disp_width(r[i]) for r in rows))
              for i, h in enumerate(headers)]
    fmt = lambda row: "  ".join(pad(v, widths[i]) for i, v in enumerate(row))
    print(fmt(headers))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print(fmt(r))
    print(f"共 {len(rows)} 行")


def main():
    args = sys.argv[1:]
    db_path = "../customer_marketing_db/marketing.db"
    if "--db" in args:
        i = args.index("--db")
        db_path = args[i + 1]
        del args[i:i + 2]
    db_path = str((HERE / db_path).resolve())

    if not Path(db_path).exists():
        print(f"[错误] 数据库不存在: {db_path}", file=sys.stderr)
        sys.exit(1)

    api_key = load_api_key()
    schema = build_schema(db_path)
    questions = args if args else None

    if questions:
        for q in questions:
            print(f"\n问题: {q}")
            print("=" * 60)
            raw = sql = ""
            try:
                raw = llm_generate_sql(schema, q, api_key)
                sql = validate_sql(raw)
                print(f"[LLM 生成 SQL]\n{sql}\n")
                headers, rows, truncated = run_query(db_path, sql)
                print("[查询结果]")
                print_table(rows, headers)
                if truncated:
                    print(f"(结果超过 {MAX_ROWS} 行, 仅显示前 {MAX_ROWS} 行)")
            except (ValueError, RuntimeError, sqlite3.Error) as e:
                print(f"[失败] {e}", file=sys.stderr)
                if raw and raw != sql:
                    print(f"原始返回: {raw[:200]}", file=sys.stderr)
        return

    # 交互模式
    print("智能问数 Demo（输入 quit 退出）")
    while True:
        try:
            q = input("\n问题> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not q:
            continue
        if q.lower() in ("quit", "exit", "退出"):
            break
        try:
            raw = llm_generate_sql(schema, q, api_key)
            sql = validate_sql(raw)
            print(f"\n[SQL] {sql}")
            headers, rows, truncated = run_query(db_path, sql)
            print_table(rows, headers)
            if truncated:
                print(f"(结果超过 {MAX_ROWS} 行, 仅显示前 {MAX_ROWS} 行)")
        except (ValueError, RuntimeError, sqlite3.Error) as e:
            print(f"[失败] {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
