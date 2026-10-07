# -*- coding: utf-8 -*-
"""SQL 编译器前端(纯标准库): 词法 -> 语句结构 AST -> 表/列/只读语义。
覆盖 SQLite SELECT/WITH/EXPLAIN 子集, 服务于: 更严谨的只读校验 / SQL 级真实表列校验 / 表列引用与输出列血缘。零第三方依赖。
"""
import re
from dataclasses import dataclass, field

QUOTE = chr(39)
KEYWORDS = {
    "SELECT", "WITH", "RECURSIVE", "AS", "FROM", "WHERE", "GROUP", "BY", "HAVING", "ORDER",
    "LIMIT", "OFFSET", "JOIN", "LEFT", "RIGHT", "INNER", "OUTER", "FULL", "CROSS", "ON",
    "USING", "UNION", "ALL", "DISTINCT", "EXPLAIN", "QUERY", "PLAN", "AND", "OR", "NOT",
    "IN", "IS", "NULL", "LIKE", "BETWEEN", "CASE", "WHEN", "THEN", "ELSE", "END", "OVER",
    "PARTITION", "ASC", "DESC", "DELETE", "UPDATE", "INSERT", "REPLACE", "VALUES", "CREATE",
    "DROP", "ALTER", "PRAGMA", "ATTACH", "DETACH", "INTERSECT", "EXCEPT", "CAST", "TRUE", "FALSE",
}


class SqlParseError(ValueError):
    pass


@dataclass
class Token:
    kind: str
    text: str
    up: str
    pos: int


_ID_START = re.compile(r"[A-Za-z_\u4e00-\u9fff]")
_ID_CONT = re.compile(r"[A-Za-z0-9_$\u4e00-\u9fff]")


def tokenize(sql: str) -> list:
    toks, i, n = [], 0, len(sql)
    while i < n:
        c = sql[i]
        if c.isspace():
            i += 1
            continue
        if c == "-" and sql[i:i + 2] == "--":
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if c == "/" and sql[i:i + 2] == "/*":
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c in (QUOTE, chr(34), chr(96), "["):
            close = "]" if c == "[" else c
            j, buf, closed = i + 1, [], False
            while j < n:
                if sql[j] == close:
                    if close != "]" and j + 1 < n and sql[j + 1] == close:
                        buf.append(close)
                        j += 2
                        continue
                    closed = True
                    break
                buf.append(sql[j])
                j += 1
            if not closed:
                raise SqlParseError("未闭合的字符串/引用标识符")
            if c == QUOTE:
                toks.append(Token("string", sql[i:j + 1], "", i))
            else:
                text = "".join(buf)
                toks.append(Token("ident", text, text.upper(), i))
            i = j + 1
            continue
        if c.isdigit() or (c == "." and i + 1 < n and sql[i + 1].isdigit()):
            m = re.match(r"[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?", sql[i:])
            toks.append(Token("number", m.group(0), "", i))
            i += len(m.group(0))
            continue
        if _ID_START.match(c):
            j = i + 1
            while j < n and _ID_CONT.match(sql[j]):
                j += 1
            text = sql[i:j]
            up = text.upper()
            toks.append(Token("keyword" if up in KEYWORDS else "ident", text, up, i))
            i = j
            continue
        two = sql[i:i + 2]
        if two in ("<=", ">=", "<>", "!=", "||"):
            toks.append(Token("punct", two, two, i))
            i += 2
            continue
        if c in "(),.;*+-/%=<>":
            toks.append(Token("punct", c, c, i))
            i += 1
            continue
        raise SqlParseError("无法识别的字符: " + c)
    return toks


@dataclass
class SelectItem:
    text: str = ""
    alias: str = ""
    star: bool = False
    refs: list = field(default_factory=list)


@dataclass
class Statement:
    kind: str = ""
    read_only: bool = False
    ctes: list = field(default_factory=list)
    tables: list = field(default_factory=list)
    items: list = field(default_factory=list)
    refs: list = field(default_factory=list)
    limit: object = None


_CLAUSE_KWS = ("FROM", "WHERE", "GROUP", "HAVING", "ORDER", "LIMIT", "UNION", "INTERSECT", "EXCEPT", "WINDOW")


def _match_paren(toks, start):
    depth = 0
    for k in range(start, len(toks)):
        if toks[k].text == "(":
            depth += 1
        elif toks[k].text == ")":
            depth -= 1
            if depth == 0:
                return k
    raise SqlParseError("括号不匹配")


def _split_clauses(toks):
    out, cur, buf, depth = {}, "SELECT", [], 0
    i = 0
    while i < len(toks):
        t = toks[i]
        if t.text == "(":
            depth += 1
        elif t.text == ")":
            depth -= 1
        if depth == 0 and t.kind == "keyword" and t.up in _CLAUSE_KWS:
            if buf:
                out[cur] = buf
            name = t.up
            if name in ("GROUP", "ORDER") and i + 1 < len(toks) and toks[i + 1].up == "BY":
                i += 1
            cur, buf = name, []
            i += 1
            continue
        buf.append(t)
        i += 1
    if buf:
        out[cur] = buf
    return out


def _at(toks, i, up):
    return i < len(toks) and toks[i].kind == "keyword" and toks[i].up == up


def parse_statement(sql):
    toks = tokenize(sql) if isinstance(sql, str) else list(sql)
    stmt = Statement()
    i = 0
    if _at(toks, i, "EXPLAIN"):
        i += 1
        if _at(toks, i, "QUERY"):
            i += 1
        if _at(toks, i, "PLAN"):
            i += 1
    if _at(toks, i, "WITH"):
        i += 1
        if _at(toks, i, "RECURSIVE"):
            i += 1
        while True:
            if i >= len(toks) or toks[i].kind != "ident":
                raise SqlParseError("CTE 名称非法")
            stmt.ctes.append(toks[i].text)
            i += 1
            if i < len(toks) and toks[i].text == "(":
                i = _match_paren(toks, i) + 1
            if not _at(toks, i, "AS") or i + 1 >= len(toks) or toks[i + 1].text != "(":
                raise SqlParseError("CTE 缺少 AS(...)")
            j = _match_paren(toks, i + 1)
            sub = parse_statement(toks[i + 2:j])
            if sub.kind != "select":
                raise SqlParseError("CTE 体内不是只读 SELECT")
            i = j + 1
            if i < len(toks) and toks[i].text == ",":
                i += 1
                continue
            break
    if i >= len(toks):
        raise SqlParseError("SQL 为空")
    t = toks[i]
    if t.kind == "keyword" and t.up == "SELECT":
        stmt.kind, stmt.read_only = "select", True
        _parse_select(toks[i + 1:], stmt)
    elif t.kind == "keyword" and t.up == "VALUES":
        stmt.kind, stmt.read_only = "select", True
    elif t.kind == "keyword" and t.up in ("DELETE", "UPDATE", "INSERT", "REPLACE"):
        stmt.kind = "write"
    else:
        stmt.kind = "other"
    return stmt


def _parse_select(toks, stmt):
    cl = _split_clauses(toks)
    head = cl.pop("SELECT", [])
    if head and head[0].kind == "keyword" and head[0].up in ("DISTINCT", "ALL"):
        head = head[1:]
    tables, alias = _from(cl.get("FROM", []), stmt.ctes)
    stmt.tables = tables
    items = _items(head, alias, stmt.ctes)
    stmt.items = items
    refs = []
    for it in items:
        refs.extend(it.refs)
    for key in ("WHERE", "HAVING", "GROUP", "ORDER", "ON"):
        refs.extend(_refs(cl.get(key, []), alias, stmt.ctes))
    stmt.refs = _dedup(refs)
    lim = cl.get("LIMIT", [])
    if lim and lim[0].kind == "number":
        stmt.limit = int(lim[0].text)


def _items(toks, alias, ctes):
    out, buf, depth = [], [], 0
    for t in toks:
        if t.text == "(":
            depth += 1
        elif t.text == ")":
            depth -= 1
        if t.text == "," and depth == 0:
            if buf:
                out.append(_item(buf, alias, ctes))
            buf = []
        else:
            buf.append(t)
    if buf:
        out.append(_item(buf, alias, ctes))
    return out


def _item(seg, alias, ctes):
    text = " ".join(t.text for t in seg)
    star = any(t.text == "*" for t in seg)
    name = ""
    k = len(seg) - 1
    if k >= 0 and seg[k].kind == "ident":
        if k >= 1 and seg[k - 1].kind == "keyword" and seg[k - 1].up == "AS":
            name = seg[k].text
            seg = seg[:k - 1]
        elif k >= 1 and seg[k - 1].text != ".":
            name = seg[k].text
            seg = seg[:k]
    return SelectItem(text=text, alias=name, star=star, refs=_refs(seg, alias, ctes))


def _dedup(refs):
    seen, out = set(), []
    for r in refs:
        if not r or r in seen:
            continue
        seen.add(r)
        out.append(r)
    return out


def _from(toks, ctes):
    tables, alias = [], {}
    cset = {c.lower() for c in ctes}
    expect, just_table, i = True, False, 0
    while i < len(toks):
        t = toks[i]
        if t.text == "(":
            j = _match_paren(toks, i)
            if i + 1 < len(toks) and toks[i + 1].kind == "keyword" and toks[i + 1].up in ("SELECT", "WITH"):
                sub = parse_statement(toks[i + 1:j])
                if sub.kind != "select":
                    raise SqlParseError("FROM 子查询不是只读 SELECT")
                for x in sub.tables:
                    if x.lower() not in cset and x not in tables:
                        tables.append(x)
            i = j + 1
            expect, just_table = False, True
            continue
        if t.kind == "keyword" and t.up in ("FROM", "JOIN", "NATURAL"):
            expect, just_table = True, False
            i += 1
            continue
        if t.kind == "keyword" and t.up == "AS" and just_table:
            i += 1
            if i < len(toks) and toks[i].kind == "ident":
                alias[toks[i].text.lower()] = tables[-1] if tables else ""
                i += 1
            just_table = False
            continue
        if t.kind == "keyword" and t.up in ("LEFT", "RIGHT", "INNER", "OUTER", "FULL", "CROSS", "ON", "USING", "AS"):
            just_table = False
            i += 1
            continue
        if t.text == ",":
            expect, just_table = True, False
            i += 1
            continue
        if expect and t.kind == "ident":
            name = t.text
            if name.lower() not in cset:
                tables.append(name)
            expect, just_table = False, True
            i += 1
            continue
        if just_table and t.kind == "ident":
            alias[t.text.lower()] = tables[-1] if tables else ""
            just_table = False
            i += 1
            continue
        just_table = False
        i += 1
    return tables, alias


def _refs(toks, alias, ctes):
    out, i, n = [], 0, len(toks)
    cset = {c.lower() for c in ctes}
    while i < n:
        t = toks[i]
        if t.kind == "ident":
            nxt = toks[i + 1] if i + 1 < n else None
            if nxt and nxt.text == ".":
                third = toks[i + 2] if i + 2 < n else None
                if third and third.kind == "ident":
                    out.append((alias.get(t.text.lower(), t.text), third.text))
                    i += 3
                    continue
                if third and third.text == "*":
                    i += 3
                    continue
            prv = toks[i - 1] if i > 0 else None
            if prv and prv.text == ".":
                i += 1
                continue
            if nxt and nxt.text == "(":
                i += 1
                continue
            if t.up in KEYWORDS or t.text.lower() in cset:
                i += 1
                continue
            out.append((None, t.text))
        i += 1
    return out


def parse(sql):
    return parse_statement(sql)


def table_names(stmt):
    return list(stmt.tables)


def column_refs(stmt):
    return list(stmt.refs)


def select_aliases(stmt):
    return [it.alias for it in stmt.items if it.alias]


def output_columns(stmt):
    return [it.alias or it.text for it in stmt.items]
