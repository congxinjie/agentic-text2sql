#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能问数 Agent —— 多阶段状态机版

把原来的"单次 LLM 生成 SQL"升级为带状态的 Agent 流程:

    1 理解问题        判断是否可回答; 识别指标/维度/筛选/时间范围; 信息不足时标记追问项
    2 检索相关表和字段  从 schema 中挑选与问题相关的表和列
    3 生成查询计划     表、join、过滤、聚合、分组、排序的查询计划
    4 检查查询计划     程序化校验计划是否覆盖指标/维度/时间范围、字段是否真实存在
    5 生成SQL        依据计划生成一条 SQL
    6 安全校验        剥离注释、拒绝多语句、只放行 SELECT/WITH/EXPLAIN
    7 执行            以 mode=ro + PRAGMA query_only=ON 只读执行
    8 检查结果         空结果 / 截断等异常检查; SQL 执行失败时自动修复一次
    9 解释结果        用自然语言解释结论并给出关键数字

每一步都写入 Trace, 记录"理解了什么 / 用了哪些字段 / 检查结果如何"。

用法:
    python3 text2sql.py "华东地区有多少客户？"
    python3 text2sql.py --db ../customer_marketing_db/marketing.db "2025年哪个月交易额最高？"
    python3 text2sql.py            # 无参数进入交互模式(支持追问)

配置(环境变量或 .env.local):
    LLM_API_KEY   必填, DeepSeek API Key
    LLM_BASE_URL  默认 https://api.deepseek.com
    LLM_MODEL     默认 deepseek-v4-flash
"""

import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# ================= 配置 =================
BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")
MODEL = os.environ.get("LLM_MODEL", "deepseek-v4-flash")
MAX_ROWS = 100          # 展示上限
EXPLAIN_MAX_ROWS = 20   # 喂给解释阶段的数据行数上限
MAX_CLARIFY_ROUNDS = 3  # 交互模式下最多追问轮数
HERE = Path(__file__).resolve().parent

STAGE_TITLES = [
    "理解问题", "检索相关表和字段", "生成查询计划", "检查查询计划",
    "生成SQL", "安全校验", "执行", "检查结果", "解释结果",
]

# 阶段运行期间可接受的异常类型(LLM 调用/解析/类型错误), 由 run() 统一兜底
STAGE_ERRORS = (ValueError, RuntimeError, TypeError, AttributeError)

# 业务背景说明(公共上下文, 拼进各阶段 prompt)
BUSINESS_DESC = """数据库是银行客户营销库, 业务含义:
- customers 客户主档, 每客户一行: name=姓名, age_group=年龄段(18-25/26-35/36-45/46-55/56+), region=地区(华东/华南/华北/西南/东北/西北), risk_level=风险等级(低/中/高风险)
- assets 资产快照, 每客户×产品×统计日一行: product_type=产品类型(活期存款/定期存款/货币基金/股票基金/银行理财/保险), amount=金额, stat_date=统计日期(季度末快照: 2025-06-30/2025-12-31/2026-06-30)
- transactions 交易流水, 每笔一行: trans_type=买入/卖出/转入/转出, amount=金额, trans_date=交易日期(2024-01 ~ 2026-07)
- campaigns 活动主档: campaign_name=名称, channel=渠道(短信/电话/邮件/APP推送/微信), start_date/end_date=活动期, budget=预算
- contacts 触达记录, 客户×活动一行: contact_date=触达日期, channel=渠道, status=已接通/未接通/拒接, outcome=购买/有意向/无意向/无响应"""





# ================= LLM 客户端 =================
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


def llm_chat(system: str, user: str, api_key: str, max_tokens: int = 1500) -> str:
    """调用 OpenAI 兼容接口, 返回文本。"""
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0,
        "max_tokens": max_tokens,
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


def extract_json(text: str):
    """从 LLM 输出中提取 JSON(容忍 markdown 代码块和前后杂讯)。"""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", t, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    raise ValueError(f"LLM 返回无法解析为 JSON: {text[:200]!r}")


def llm_json(system: str, user: str, api_key: str, max_tokens: int = 2000) -> dict:
    """要求 LLM 输出 JSON 对象; 解析失败或非对象时带错误信息重试一次。"""
    for attempt in range(2):
        raw = llm_chat(system, user, api_key, max_tokens=max_tokens)
        try:
            data = extract_json(raw)
            if not isinstance(data, dict):
                raise ValueError(f"LLM 返回了 JSON 但类型是 {type(data).__name__}, 需要对象")
            return data
        except ValueError as e:
            if attempt == 0:
                user = (f"{user}\n\n[系统] 你上次的输出无法解析为合法 JSON 对象(可能被截断): {e}\n"
                        f"上次输出(开头200字符): {raw[:200]}\n"
                        f"请重新输出一个【完整】的 JSON 对象。务必精简内容、不要多余文字、不要 markdown 代码块。")
                continue
            raise
    raise ValueError("LLM JSON 解析失败(重试后仍失败)")


# ================= 数据库与安全 =================
def build_schema(db_path: str) -> str:
    """从 sqlite_master 提取表结构 + 每表 2 行样例数据。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    parts = []
    objects = conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    for ttype, name, ddl in objects:
        if ddl:
            parts.append(f"-- {ttype}: {name}\n{ddl};")
        try:
            quoted_name = name.replace('"', '""')
            sample_cur = conn.execute(f'SELECT * FROM "{quoted_name}" LIMIT 2')
            rows = sample_cur.fetchall()
            for r in rows:
                vals = ", ".join(repr(str(v)[:24]) if v is not None else "NULL" for v in r)
                parts.append(f"-- 样例: ({vals})")
        except sqlite3.Error:
            pass
    conn.close()
    return "\n\n".join(parts)


def get_table_meta(db_path: str) -> dict:
    """返回 {表名: [列名, ...]} 用于程序化字段校验。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    meta = {}
    for t in tables:
        quoted = t.replace('"', '""')
        meta[t] = [r[1] for r in conn.execute(f'PRAGMA table_info("{quoted}")')]
    conn.close()
    return meta


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
                if i + 1 < len(sql) and sql[i + 1] == "'":
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


def _mask_string_literals(sql: str) -> str:
    """把 '...' 字符串字面量替换为占位, 便于做语法结构扫描。"""
    out, i, n = [], 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "'":
            out.append("'x'")
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _with_main_verb(sql: str):
    """识别 WITH 语句的主语句动词(SELECT/DELETE/UPDATE/INSERT/REPLACE/VALUES)。

    SQLite 允许 WITH 前置 DML(如 `WITH x AS (...) DELETE FROM t`),
    仅凭前缀校验会放行写语句, 这里必须解析出 CTE 之后真正的主语句动词。
    返回动词大写; 无法解析返回 None(调用方应保守拒绝)。
    """
    s = _mask_string_literals(sql)
    m = re.match(r"^\s*WITH\b", s, re.I)
    if not m:
        return None
    s = s[m.end():]
    s = re.sub(r"^\s*RECURSIVE\b", "", s, count=1, flags=re.I)
    verbs = {"SELECT", "DELETE", "UPDATE", "INSERT", "REPLACE", "VALUES"}
    i, n, depth = 0, len(s), 0
    while i < n:
        c = s[i]
        if c == "(":
            depth += 1
            i += 1
            continue
        if c == ")":
            depth = max(0, depth - 1)
            i += 1
            continue
        if depth == 0 and (c.isalpha() or c == "_"):
            j = i
            while j < n and (s[j].isalnum() or s[j] == "_"):
                j += 1
            tok = s[i:j].upper()
            k = j
            while k < n and s[k].isspace():
                k += 1
            # 情况1: `cte_name AS (select)` -> CTE 定义, 跳过 AS 后的括号块
            m_as = re.match(r"AS\s*\(", s[k:], re.I)
            if m_as:
                d, k2 = 1, k + m_as.end()
                while k2 < n and d > 0:
                    if s[k2] == "(":
                        d += 1
                    elif s[k2] == ")":
                        d -= 1
                    k2 += 1
                i = k2
                continue
            # 情况2: `cte_name (col1, col2) AS (select)` -> 先跳列名括号, 再确认 AS(
            if s[k] == "(":
                d, k2 = 1, k + 1
                while k2 < n and d > 0:
                    if s[k2] == "(":
                        d += 1
                    elif s[k2] == ")":
                        d -= 1
                    k2 += 1
                while k2 < n and s[k2].isspace():
                    k2 += 1
                if re.match(r"AS\s*\(", s[k2:], re.I):
                    i = k2
                    continue
            # 其他顶层 token: 主语句动词
            if tok in verbs:
                return tok
            i = j
            continue
        i += 1
    return None


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
    # WITH 可前置 DML, 必须确认主语句是只读的
    if re.match(r"^WITH\b", first_stmt, re.I):
        verb = _with_main_verb(first_stmt)
        if verb is None:
            raise ValueError("无法确认 WITH 语句的主操作类型, 保守拒绝")
        if verb not in ("SELECT", "VALUES"):
            raise ValueError(f"WITH 后接非只读主语句被拒绝: {verb}")
    return first_stmt


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


# ================= 数据模型 =================
@dataclass
class TraceEntry:
    stage: str
    status: str            # OK / WARN / FAIL / SKIP
    detail: str = ""
    elapsed: float = 0.0


class Trace:
    def __init__(self):
        self.entries: list[TraceEntry] = []
        self._stage_start = 0.0

    def begin_stage(self):
        self._stage_start = time.time()

    def record(self, stage: str, status: str, detail: str = ""):
        self.entries.append(TraceEntry(stage, status, detail,
                                       round(time.time() - self._stage_start, 2)))

    def render(self) -> str:
        lines = ["\n===== Agent 执行轨迹 ====="]
        for i, e in enumerate(self.entries, 1):
            lines.append(f"[{i:02d}] {e.stage} [{e.status}] ({e.elapsed:.1f}s)")
            if e.detail:
                lines.append(f"      {e.detail}")
        return "\n".join(lines)


@dataclass
class Understanding:
    answerable: bool = True
    reason: str = ""
    summary: str = ""
    metrics: list[str] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)
    filters: list[str] = field(default_factory=list)
    time_range: str = ""
    assumptions: list[str] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)  # 必须追问
    missing_optional: list[str] = field(default_factory=list)  # 可用默认
    question_specificity: str = "clear"  # LLM 自评: clear=明确, vague=含糊


@dataclass
class Retrieval:
    tables: list[str] = field(default_factory=list)
    columns: dict = field(default_factory=dict)   # 表 -> 列
    notes: list[str] = field(default_factory=list)


@dataclass
class Answer:
    question: str
    answerable: bool = True
    reject_reason: str = ""
    needs_clarification: list[str] = field(default_factory=list)
    sql: str = ""
    headers: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    truncated: bool = False
    explanation: str = ""
    error: str = ""
    trace: Trace = field(default_factory=Trace)


# ================= 各阶段 Prompt =================
UNDERSTAND_SYS = """你是数据分析 Agent 的"问题理解器"。根据数据库结构与用户问题, 输出一个 JSON 对象, 格式:
{
  "answerable": true,
  "reason": "不可回答时的原因; 可回答则为空字符串",
  "summary": "一句话重述用户意图",
  "metrics": ["交易总额"],
  "dimensions": ["月份"],
  "filters": ["trans_date 在 2025 年"],
  "time_range": "2025-01-01 ~ 2025-12-31",
  "assumptions": ["默认统计最近一个资产快照 2026-06-30"],
  "missing": [{"item": "要统计的资产快照日期", "required": true}],
  "question_specificity": "clear"
}
规则:
1. answerable=false 的情形: 与库无关的闲聊/外部信息(天气、股票行情、政策新闻等); 需要库中不存在的业务字段(如"客户收入""客户职业"); 纯主观建议(如"该买什么产品")。
2. metrics 是用户想计算的指标(金额/数量/占比/排名等), dimensions 是想按什么分组, 都没有则给空数组。
3. filters 用自然语言描述筛选条件; time_range 单独提取时间范围描述, 没有则空字符串。
4. missing 是计算所必需但用户没提供的信息。required=true 表示缺了它无法计算(如指标没提); required=false 表示可用合理默认(如时间范围没提)。
5. 一切字段/含义必须来自给定的表结构, 不要臆造库中不存在的概念。
6. question_specificity 是自评: 问题有明确分析目标(指标/维度/筛选/时间至少有一项明确)给 "clear"; 问题含糊、没有明确统计对象(如"帮我分析一下""看看数据")给 "vague", 即使你猜测补了默认指标也必须给 "vague"。"""

RETRIEVE_SYS = """你是数据分析 Agent 的"检索器"。根据问题理解, 从给定表结构中挑出回答该问题需要用到(或大概率用到)的表与列, 输出 JSON:
{
  "tables": [
    {"table": "transactions", "columns": ["trans_date", "amount", "trans_type"], "why": "计算交易额需要日期与金额"}
  ]
}
规则:
1. 表名和列名必须逐字来自给定结构, 严禁臆造。
2. 只列与问题相关的表; 不确定的表宁可多列, 后续计划阶段会再筛选。"""

PLAN_SYS = """你是数据分析 Agent 的"查询计划器"。基于问题理解与已检索的表字段, 制定一份 SQLite 查询计划, 输出 JSON:
{
  "summary": "计划的一句话说明",
  "tables": ["transactions"],
  "joins": ["transactions JOIN customers ON ..."],
  "filters": [{"field": "transactions.trans_date", "op": "between", "value": ["2025-01-01", "2025-12-31"], "desc": "只看2025年"}],
  "aggregations": [{"func": "SUM", "field": "transactions.amount", "alias": "交易总额"}],
  "group_by": [{"expr": "substr(transactions.trans_date,1,7)", "alias": "月份"}],
  "order_by": [{"expr": "交易总额", "dir": "DESC"}],
  "limit": null,
  "steps": ["筛选2025年交易", "按月分组", "汇总交易总额"]
}
规则:
1. 表名/列名逐字来自给定结构; filters 的 field 必须是"表.列"或裸列名。
2. 每个指标都要有对应聚合, 每个维度都要进 group_by。
3. 用户给了时间范围必须体现在 filters 或 steps 中。
4. 不要写具体 SQL, 只写计划。"""

EXPLAIN_SYS = """你是数据分析 Agent 的"结论解释器"。根据用户问题、执行成功的 SQL 和查询结果, 用简洁自然的中文解释结论:
- 结论先行, 再给关键数字(引用结果中的数值)。
- 若结果为空, 说明可能原因(筛选过严/该时间段无数据)。
- 若结果被截断, 说明只基于前若干行。
- 若理解阶段做了默认假设, 必须说明(如"按 2026-06-30 快照统计")。
- 共 3~6 句话, 不要复述 SQL。"""


# ================= Agent =================
class QueryAgent:
    def __init__(self, db_path: str, api_key: str, verbose: bool = True,
                 biz_context: str | None = None):
        self.db_path = db_path
        self.api_key = api_key
        self.verbose = verbose
        # 业务语义上下文: 默认使用老营销库语义 BUSINESS_DESC; 显式传入可覆盖, 用于接入其它 schema(不判核心逻辑)
        self.biz_context = biz_context if biz_context else BUSINESS_DESC
        self.schema = build_schema(db_path)
        self.meta = get_table_meta(db_path)
        self.trace = Trace()
        self._stage_idx = 0

    # ---- 基础设施 ----
    def _log(self, msg: str):
        if self.verbose:
            print(msg)

    def _stage(self, title: str, status: str, detail: str = ""):
        self.trace.record(title, status, detail)
        tag = {"OK": "OK", "WARN": "警告", "FAIL": "失败", "SKIP": "跳过"}[status]
        n = self.trace.entries[-1].elapsed
        self._log(f"[{self._stage_idx:02d}] {title} [{tag}] ({n:.1f}s)")
        if detail:
            self._log(f"        {detail}")
        self._stage_idx += 1

    def _ctx(self) -> str:
        return f"数据库结构:\n{self.schema}\n\n业务说明:\n{self.biz_context}"

    # ---- 1 理解问题 ----
    def _understand(self, question: str) -> Understanding:
        self.trace.begin_stage()
        user = (f"{self._ctx()}\n\n用户问题: {question}\n\n"
                f"请输出 JSON(只输出 JSON, 不要其他文字)。")
        data = llm_json(UNDERSTAND_SYS, user, self.api_key)

        def lst(key):
            v = data.get(key, []) or []
            return [str(x) if not isinstance(x, dict) else str(x.get("name") or x.get("item") or "")
                    for x in v if x]

        u = Understanding(
            answerable=bool(data.get("answerable", True)),
            reason=str(data.get("reason", "")),
            summary=str(data.get("summary", "")),
            metrics=lst("metrics"),
            dimensions=lst("dimensions"),
            filters=lst("filters"),
            time_range=str(data.get("time_range", "")),
            assumptions=lst("assumptions"),
            question_specificity=str(data.get("question_specificity", "clear")).strip().lower(),
        )
        for m in (data.get("missing") or []):
            if isinstance(m, dict):
                item, req = str(m.get("item", "")), bool(m.get("required", True))
            else:
                item, req = str(m), True
            if item:
                (u.missing_required if req else u.missing_optional).append(item)

        if not u.answerable:
            detail = f"不可回答: {u.reason}"
            self._stage("理解问题", "FAIL", detail)
            return u
        parts = []
        if u.metrics:
            parts.append(f"指标=[{', '.join(u.metrics)}]")
        if u.dimensions:
            parts.append(f"维度=[{', '.join(u.dimensions)}]")
        if u.filters:
            parts.append(f"筛选=[{'; '.join(u.filters)}]")
        if u.time_range:
            parts.append(f"时间范围={u.time_range}")
        detail = "; ".join(parts) or "无明确指标/维度/筛选"
        if u.missing_required:
            detail += f"; 缺失必填=[{', '.join(u.missing_required)}]"
        if u.missing_optional:
            detail += f"; 可选默认=[{', '.join(u.missing_optional)}]"
        self._stage("理解问题", "OK", detail)
        return u

    # ---- 2 检索相关表和字段 ----
    def _retrieve(self, u: Understanding) -> Retrieval:
        self.trace.begin_stage()
        user = (f"{self._ctx()}\n\n问题理解:\n"
                f"- 意图: {u.summary}\n- 指标: {u.metrics}\n- 维度: {u.dimensions}\n"
                f"- 筛选: {u.filters}\n- 时间范围: {u.time_range or '无'}\n\n"
                f"请输出 JSON(只输出 JSON)。")
        data = llm_json(RETRIEVE_SYS, user, self.api_key)
        r = Retrieval()
        for t in (data.get("tables") or []):
            if not isinstance(t, dict):
                continue
            name = t.get("table", "")
            if name in self.meta:
                r.tables.append(name)
                cols = [c for c in (t.get("columns") or [])
                        if isinstance(c, str) and c in self.meta[name]]
                r.columns[name] = cols
                unknown = [c for c in (t.get("columns") or [])
                           if isinstance(c, str) and c not in self.meta[name]]
                if unknown:
                    r.notes.append(f"{name} 中不存在列: {unknown}")
            else:
                r.notes.append(f"库中不存在表: {name}")
        r.tables = list(dict.fromkeys(r.tables))  # 去重保序
        if not r.tables:
            r.notes.append("未检索到任何相关表")
        detail = "; ".join(f"{t}({', '.join(r.columns[t]) if r.columns.get(t) else '全表'})"
                           for t in r.tables) or "无"
        self._stage("检索相关表和字段", "WARN" if r.notes else "OK", detail)
        return r

    # ---- 3 生成查询计划 ----
    def _plan(self, u: Understanding, r: Retrieval) -> dict:
        self.trace.begin_stage()
        user = (f"{self._ctx()}\n\n问题理解:\n- 意图: {u.summary}\n- 指标: {u.metrics}\n"
                f"- 维度: {u.dimensions}\n- 筛选: {u.filters}\n- 时间范围: {u.time_range or '无'}\n"
                f"- 默认假设: {u.assumptions or '无'}\n\n"
                f"已检索表字段:\n" +
                "\n".join(f"- {t}: {', '.join(r.columns.get(t, self.meta.get(t, [])))}" for t in r.tables) +
                f"\n\n请输出查询计划 JSON(只输出 JSON)。")
        plan = llm_json(PLAN_SYS, user, self.api_key)
        self._stage("生成查询计划", "OK", f"计划: {plan.get('summary', '')} | "
                                         f"表={plan.get('tables')} | 聚合={plan.get('aggregations')}")
        return plan

    # ---- 4 检查查询计划(程序化) ----
    def _check_plan(self, u: Understanding, r: Retrieval, plan: dict) -> tuple[bool, list[str]]:
        self.trace.begin_stage()
        issues: list[str] = []
        text = json.dumps(plan, ensure_ascii=False)

        # 4.1 计划引用的表必须真实存在
        for t in plan.get("tables") or []:
            if t not in self.meta:
                issues.append(f"计划引用不存在的表: {t}")

        # 4.2 表.列 引用必须真实存在(首 token 不以数字开头, 排除日期/数字噪音)
        for ref in re.findall(r"(?<![\w.])\b[\u4e00-\u9fffA-Za-z_]\w*\.[\u4e00-\u9fffA-Za-z_]\w*", text):
            tbl, col = ref.split(".", 1)
            if tbl in self.meta and col not in self.meta[tbl]:
                issues.append(f"字段不存在: {ref}")

        # 4.3 每个维度都应出现在计划(分组/排序/步骤)中
        for d in u.dimensions:
            if d and d not in text:
                issues.append(f"维度「{d}」未体现在计划中")

        # 4.4 每个指标都应出现在计划中
        for m in u.metrics:
            if m and m not in text:
                issues.append(f"指标「{m}」未体现在计划中")

        # 4.5 时间范围应体现在筛选/步骤中
        if u.time_range and "time" not in text.lower() and "date" not in text.lower():
            issues.append(f"时间范围「{u.time_range}」未体现在计划中")

        # 4.6 有指标时必须有聚合
        if u.metrics and not plan.get("aggregations"):
            issues.append("有指标但计划缺少聚合")

        ok = not issues
        self._stage("检查查询计划", "OK" if ok else "WARN",
                    "检查通过: 表/字段/指标/维度/时间范围均覆盖" if ok
                    else "发现问题: " + "; ".join(issues))
        return ok, issues

    # ---- 4b 计划修复(检查不过时, 让 LLM 重规划一次) ----
    def _replan(self, u: Understanding, r: Retrieval, plan: dict, issues: list[str]) -> dict:
        self.trace.begin_stage()
        user = (f"{self._ctx()}\n\n原计划:\n{json.dumps(plan, ensure_ascii=False, indent=2)}\n\n"
                f"检查发现以下问题:\n" + "\n".join(f"- {i}" for i in issues) +
                f"\n\n请修正计划, 输出新的查询计划 JSON(只输出 JSON)。")
        new_plan = llm_json(PLAN_SYS, user, self.api_key)
        self._stage("生成查询计划", "WARN", "根据检查结果自动修正计划(一次)")
        return new_plan

    # ---- 5 生成 SQL ----
    def _generate_sql(self, u: Understanding, r: Retrieval, plan: dict,
                      check_issues: list[str]) -> str:
        self.trace.begin_stage()
        user = (f"{self._ctx()}\n\n问题: {u.summary}\n"
                f"指标: {u.metrics} 维度: {u.dimensions}\n筛选: {u.filters}\n"
                f"时间范围: {u.time_range or '无'}\n默认假设: {u.assumptions or '无'}\n\n"
                f"查询计划:\n{json.dumps(plan, ensure_ascii=False, indent=2)}\n\n"
                f"计划检查未解决项(请尽量规避): {check_issues if check_issues else '无'}\n\n"
                f"请只输出一条 SQLite 只读 SQL 语句本身, 不要解释、不要 markdown 代码块。\n"
                f"规则: 只允许 SELECT/WITH/EXPLAIN 开头; 表名列名必须来自给定结构; 金额是 REAL, 日期是 TEXT(YYYY-MM-DD); "
                f"聚合结果加 ORDER BY, 明细查询加 LIMIT 50。"
                f"理解/计划中出现的每个维度与实体标识列都必须进入最终 SELECT(编码维度同时输出 code 与 describe; "
                f"问题问\"哪个/哪些客户\"必须输出客户标识列, 如 pty_id/客户号)。"
                f"不要额外输出与问题无关的中间列(如问题只要交易额时不要拆出买入金额/卖出金额)。")
        sql = llm_chat(
            "你是 SQLite 只读查询助手。根据查询计划生成一条精确的 SQL。只输出 SQL 本身。尽量完整不要截断。",
            user, self.api_key, max_tokens=2800)
        self._stage("生成SQL", "OK", sql[:200] + ("…" if len(sql) > 200 else ""))
        return sql

    # ---- 6 安全校验 ----
    def _validate(self, sql: str) -> str:
        self.trace.begin_stage()
        cleaned = validate_sql(sql)
        self._stage("安全校验", "OK",
                    "单条只读语句 SELECT/WITH/EXPLAIN, 注释已剥离, 拒绝多语句")
        return cleaned

    # ---- 7 执行 ----
    def _execute(self, sql: str):
        self.trace.begin_stage()
        headers, rows, truncated = run_query(self.db_path, sql)
        self._stage("执行", "OK",
                    f"只读执行成功: {len(rows)} 行" + ("(已截断)" if truncated else ""))
        return headers, rows, truncated

    # ---- 7b SQL 失败自动修复一次 ----
    def _repair_sql(self, sql: str, error: Exception, u: Understanding, plan: dict) -> str:
        self.trace.begin_stage()
        self._log(f"        [自动修复] SQL 执行失败: {error}")
        user = (f"数据库结构:\n{self.schema}\n\n"
                f"问题: {u.summary}\n查询计划:\n{json.dumps(plan, ensure_ascii=False)}\n\n"
                f"SQL 执行失败:\n{sql}\n\n错误信息:\n{error}\n\n"
                f"请根据错误信息修正 SQL, 只输出修正后的完整 SQL, 不要解释。")
        new_sql = llm_chat("你是 SQLite 专家, 负责修复一条出错的只读 SQL。只输出修正后的 SQL 本身, 尽量完整: 不要截断、不要写注释或解释。",
                           user, self.api_key, max_tokens=2800)
        cleaned = validate_sql(new_sql)
        self._stage("执行", "WARN", f"SQL 修复后重试: {cleaned[:160]}…")
        return cleaned

    # ---- 8 检查结果 ----
    def _check_result(self, headers: list, rows: list, truncated: bool) -> list[str]:
        self.trace.begin_stage()
        notes = []
        if not rows:
            notes.append("查询结果为空: 可能筛选条件过严或该时间段无数据, 建议放宽条件")
        elif len(rows) == 1:
            notes.append("结果仅 1 行, 可直接给出结论")
        if truncated:
            notes.append(f"结果超过 {MAX_ROWS} 行, 仅展示前 {MAX_ROWS} 行, 解释时需注明")
        self._stage("检查结果", "OK" if not notes else "WARN", "; ".join(notes) or "结果正常")
        return notes

    # ---- 9 解释结果 ----
    def _explain(self, u: Understanding, sql: str, headers: list,
                 rows: list, truncated: bool, notes: list[str]) -> str:
        self.trace.begin_stage()
        if rows:
            data_lines = [", ".join(f"{h}={str(v)[:24]}" for h, v in zip(headers, r))
                          for r in rows[:EXPLAIN_MAX_ROWS]]
            data_str = "\n".join(data_lines)
            data_str += "\n…(更多行已省略)" if truncated or len(rows) > EXPLAIN_MAX_ROWS else ""
        else:
            data_str = "(结果为空)"
        user = (f"用户问题: {u.summary}\n"
                f"执行 SQL:\n{sql}\n\n"
                f"查询结果(列: {headers}):\n{data_str}\n\n"
                f"结果检查备注: {notes if notes else '无'}\n"
                f"默认假设: {u.assumptions if u.assumptions else '无'}\n\n"
                f"请用自然语言解释结论。")
        explanation = llm_chat(EXPLAIN_SYS, user, self.api_key, max_tokens=800)
        self._stage("解释结果", "OK", explanation[:120] + ("…" if len(explanation) > 120 else ""))
        return explanation

    # ---- 阶段执行辅助: LLM 阶段异常时记录 FAIL 并抛出, 由 run() 统一兜底 ----
    def _safe_stage(self, title: str, fn, *args):
        try:
            return fn(*args)
        except STAGE_ERRORS as e:
            self.trace.begin_stage()
            self._stage(title, "FAIL", f"阶段异常: {e}")
            raise

    # ---- 主状态机 ----
    def run(self, question: str, clarify: bool = True) -> Answer:
        self._stage_idx = 1
        self.trace = Trace()
        ans = Answer(question=question)

        # 1 理解
        try:
            u = self._safe_stage("理解问题", self._understand, question)
        except STAGE_ERRORS as e:
            ans.error = f"理解问题阶段失败: {e}"
            ans.trace = self.trace
            return ans
        ans.trace = self.trace
        if not u.answerable:
            ans.answerable = False
            ans.reject_reason = u.reason or "该问题无法由当前数据库回答"
            return ans
        # 必填信息缺失且允许追问 -> 主动追问
        if u.missing_required and clarify:
            ans.needs_clarification = u.missing_required
            return ans
        # M3.5 程序化追问判据: (a) 指标/维度/筛选/时间范围全空; 或 (b) LLM 自评 vague。
        # 抗 LLM 自动填指标; 不伤 E2 路径(E2 有明确指标, 自评 clear 且四项不全空)
        if (clarify and u.answerable
                and (u.question_specificity == "vague"
                     or (not u.metrics and not u.dimensions
                         and not u.filters and not u.time_range))):
            ans.needs_clarification = ["分析目标不明确: 请补充要统计的指标、分组维度或筛选条件"]
            return ans
        # 未满足的缺失项(含放弃追问的必填项)转为默认假设, 供解释阶段注明
        u.missing_optional += u.missing_required
        u.missing_required = []
        for m in u.missing_optional:
            u.assumptions.append(f"用户未提供「{m}」, 使用合理默认")

        try:
            # 2 检索
            r = self._safe_stage("检索相关表和字段", self._retrieve, u)
            # 3 计划
            plan = self._safe_stage("生成查询计划", self._plan, u, r)
            # 4 检查计划(发现问题则自动修正一次)
            ok, issues = self._check_plan(u, r, plan)
            if not ok:
                plan = self._safe_stage("生成查询计划", self._replan, u, r, plan, issues)
                ok, issues = self._check_plan(u, r, plan)
            # 5 生成 SQL
            raw_sql = self._safe_stage("生成SQL", self._generate_sql, u, r, plan, issues)
        except STAGE_ERRORS as e:
            ans.error = f"查询规划阶段失败: {e}"
            ans.trace = self.trace
            return ans

        # 6 安全校验
        try:
            sql = self._validate(raw_sql)
        except ValueError as e:
            self.trace.begin_stage()
            self._stage("安全校验", "FAIL", str(e))
            ans.error = str(e)
            return ans
        ans.sql = sql

        # 7 执行(失败自动修复一次)
        try:
            headers, rows, truncated = self._execute(sql)
        except (sqlite3.Error, ValueError) as e:
            try:
                sql = self._repair_sql(sql, e, u, plan)
                ans.sql = sql
                headers, rows, truncated = self._execute(sql)
            except (sqlite3.Error, ValueError) as e2:
                self.trace.begin_stage()
                self._stage("执行", "FAIL", f"修复后仍失败: {e2}")
                ans.error = f"SQL 执行失败且自动修复未成功: {e2}"
                return ans
        ans.headers, ans.rows, ans.truncated = headers, rows, truncated

        # 8 检查结果
        notes = self._check_result(headers, rows, truncated)

        # 9 解释结果
        try:
            ans.explanation = self._safe_stage(
                "解释结果", self._explain, u, sql, headers, rows, truncated, notes)
        except STAGE_ERRORS as e:
            ans.error = f"结果解释阶段失败: {e}"
            ans.trace = self.trace
            return ans
        return ans


# ================= 展示 =================
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


def print_answer(ans: Answer):
    print("\n" + "=" * 64)
    print(f"问题: {ans.question}")
    if not ans.answerable:
        print(f"[拒绝回答] {ans.reject_reason}")
        print(ans.trace.render())
        return
    if ans.needs_clarification:
        print("[需要追问] 以下信息不明确, 请在交互模式下补充:")
        for m in ans.needs_clarification:
            print(f"  - {m}")
        print(ans.trace.render())
        return
    if ans.error:
        print(f"[失败] {ans.error}")
        if ans.rows:
            print("\n[已获取的查询结果]")
            print_table(ans.rows, ans.headers)
        print(ans.trace.render())
        return
    print(f"\n[SQL]\n{ans.sql}\n")
    print("[查询结果]")
    print_table(ans.rows, ans.headers)
    if ans.truncated:
        print(f"(结果超过 {MAX_ROWS} 行, 仅显示前 {MAX_ROWS} 行)")
    if ans.explanation:
        print(f"\n[结论]\n{ans.explanation}")
    print(ans.trace.render())


# ================= CLI =================
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
    agent = QueryAgent(db_path, api_key)
    print(f"智能问数 Agent 就绪: 模型={MODEL} 库={Path(db_path).name}\n")

    questions = args if args else None

    # ---- 单条模式(非交互: 不追问, 缺失信息用默认假设) ----
    if questions:
        for q in questions:
            ans = agent.run(q, clarify=False)
            print_answer(ans)
        return

    # ---- 交互模式(支持追问) ----
    print("智能问数 Agent（输入 quit 退出）")
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

        rounds = 0
        while rounds < MAX_CLARIFY_ROUNDS:
            ans = agent.run(q, clarify=True)
            if not ans.needs_clarification:
                break
            print("[追问] 需要补充以下信息:")
            for m in ans.needs_clarification:
                print(f"  - {m}")
            extra = input("补充(直接回车使用默认假设)> ").strip()
            if extra:
                q = q + f"\n[补充信息] {extra}"
            else:
                q = q + "\n[说明] 用户未补充, 请使用合理默认值并在结论中注明。"
            rounds += 1

        if ans.needs_clarification:
            # 追问轮数耗尽仍未补充: 改用默认假设强制完成
            print(f"[提示] 追问 {MAX_CLARIFY_ROUNDS} 轮后仍信息不足, 改用合理默认假设继续")
            ans = agent.run(q, clarify=False)

        print_answer(ans)


if __name__ == "__main__":
    main()
