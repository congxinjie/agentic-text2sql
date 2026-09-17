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
    python3 text2sql.py --db ../customer_marketing_db/marketing.db "2025年哪个月交易金额最高？"
    python3 text2sql.py            # 无参数进入交互模式(支持追问)
    python3 text2sql.py --sample-rows 0 "..."   # 不把样例数据发给外部 LLM(默认 2 行)
    python3 text2sql.py --log-level DEBUG "..."  # 调日志级别(默认 INFO)

配置(环境变量或 .env.local):
    LLM_API_KEY   必填, DeepSeek API Key
    LLM_BASE_URL  默认 https://api.deepseek.com
    LLM_MODEL     默认 deepseek-v4-flash
    TEXT2SQL_LOG_LEVEL  可选, 日志级别(默认 INFO)
    TEXT2SQL_LOG_DIR    可选, 日志目录(默认 <仓库根>/logs)
"""

import json
import logging
import os
import re
import sqlite3
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ================= 配置 =================
BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")
MODEL = os.environ.get("LLM_MODEL", "deepseek-v4-flash")
MAX_ROWS = 100          # 展示上限
EXPLAIN_MAX_ROWS = 20   # 喂给解释阶段的数据行数上限
MAX_CLARIFY_ROUNDS = 3  # 交互模式下最多追问轮数
HERE = Path(__file__).resolve().parent

# ================= 日志(纯标准库, 轮转文件) =================
# M9: 引擎与演示层各接一个轮转文件日志, 记录各阶段开始/结束/耗时/异常/重试。
# 红线: 日志只允许记录 长度/行数/表名/阶段名/耗时/异常类型与消息, 绝不写入 API 密钥或数据行内容。
LOG_DIR = Path(os.environ.get("TEXT2SQL_LOG_DIR") or (HERE.parent / "logs"))
LOG_FILE = LOG_DIR / "text2sql.log"
LOG_MAX_BYTES = 5 * 1024 * 1024   # 单文件上限 5MB
LOG_BACKUP_COUNT = 3              # 轮转保留 3 个历史文件


def setup_logging(log_file=None, level: str | int = "INFO", name: str = "text2sql") -> logging.Logger:
    """初始化轮转文件日志(纯标准库), 返回 logger。

    只挂文件 handler、不挂 StreamHandler —— 保证 CLI/评测/演示的 stdout 输出不被日志污染。
    level 可传级别名(DEBUG/INFO/WARNING/ERROR)或 logging 数值; 重复调用幂等, 只调整级别。
    """
    logger = logging.getLogger(name)
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)
    logger.setLevel(level)
    target = Path(log_file) if log_file else LOG_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    for h in logger.handlers:
        if isinstance(h, RotatingFileHandler) and Path(h.baseFilename) == target.resolve():
            h.setLevel(level)
            return logger
    handler = RotatingFileHandler(str(target), maxBytes=LOG_MAX_BYTES,
                                  backupCount=LOG_BACKUP_COUNT, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    handler.setLevel(level)
    logger.addHandler(handler)
    logger.propagate = False  # 不外溢到 root logger, 避免日志混进 stdout/stderr
    return logger


LOG = setup_logging(level=os.environ.get("TEXT2SQL_LOG_LEVEL", "INFO"))

STAGE_TITLES = [
    "理解问题", "检索相关表和字段", "生成查询计划", "检查查询计划",
    "生成SQL", "安全校验", "执行", "检查结果", "解释结果",
]

# 阶段运行期间可接受的异常类型(LLM 调用/解析/类型错误), 由 run() 统一兜底
STAGE_ERRORS = (ValueError, RuntimeError, TypeError, AttributeError)

# 口径说明(解释)为空时的兜底文案, 避免"结论/口径说明"栏静默空白(M9)
FALLBACK_EXPLANATION = "（本次未生成口径说明）"

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
    """调用 OpenAI 兼容接口, 返回文本。M3.7: 超时/连接/429/5xx 指数退避重试(纯调用层, 不动状态机)。"""
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
    retryable_http = {429, 500, 502, 503, 504}
    max_attempts = 3
    for attempt in range(max_attempts):
        req = urllib.request.Request(
            f"{BASE_URL}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {api_key}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = json.load(resp)
            try:
                content = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError) as e:
                raise RuntimeError(f"LLM 响应格式异常: {str(data)[:300]}") from e
            # M9: 空返回是野外最难发现的问题, 必须记 WARNING(不改行为: 仍返回空串)
            if isinstance(content, str) and not content.strip():
                LOG.warning("[LLM 空返回] 模型返回空内容: model=%s max_tokens=%d 提示长度=%d",
                            MODEL, max_tokens, len(user))
            return content.strip()
        except urllib.error.HTTPError as e:
            if e.code in retryable_http and attempt < max_attempts - 1:
                delay = 1.5 * (2 ** attempt)  # 1.5s / 3s 退避
                LOG.warning("[LLM 重试] HTTP %d, 第 %d/%d 次尝试失败, %.1fs 后退避重试",
                            e.code, attempt + 1, max_attempts, delay)
                time.sleep(delay)
                continue
            body = e.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(f"LLM API HTTP {e.code}: {body}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < max_attempts - 1:
                delay = 1.5 * (2 ** attempt)
                LOG.warning("[LLM 重试] 连接异常 %s, 第 %d/%d 次尝试失败, %.1fs 后退避重试",
                            type(e).__name__, attempt + 1, max_attempts, delay)
                time.sleep(delay)
                continue
            reason = getattr(e, "reason", e)
            raise RuntimeError(f"LLM API 连接失败: {reason}") from e


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
                LOG.warning("[LLM JSON 解析失败] 带错误信息重试一次: %s", str(e)[:160])
                user = (f"{user}\n\n[系统] 你上次的输出无法解析为合法 JSON 对象(可能被截断): {e}\n"
                        f"上次输出(开头200字符): {raw[:200]}\n"
                        f"请重新输出一个【完整】的 JSON 对象。务必精简内容、不要多余文字、不要 markdown 代码块。")
                continue
            raise
    raise ValueError("LLM JSON 解析失败(重试后仍失败)")


# ================= 数据库与安全 =================
def build_schema(db_path: str, sample_rows: int = 2) -> str:
    """从 sqlite_master 提取表结构; sample_rows>0 时每表附 N 行样例数据(默认 2, 保持既有行为)。

    M9: sample_rows=0 表示完全不把样例数据拼进 schema(即不发给外部 LLM)。
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    parts = []
    objects = conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    sample_n = max(0, int(sample_rows))
    for ttype, name, ddl in objects:
        if ddl:
            parts.append(f"-- {ttype}: {name}\n{ddl};")
        if sample_n <= 0:  # 关闭样例数据出境
            continue
        try:
            quoted_name = name.replace('"', '""')
            sample_cur = conn.execute(f'SELECT * FROM "{quoted_name}" LIMIT {sample_n}')
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

    def begin_stage(self, title: str = ""):
        self._stage_start = time.time()
        if title:
            LOG.info("[阶段开始] %s", title)

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
1. answerable=false 的情形: 与库无关的闲聊/外部信息(天气、股票行情、政策新闻等); 需要库中不存在的业务字段(如"客户收入"等库外字段); 纯主观建议(如"该买什么产品")。仅因问题含糊/缺少分析目标时, 不要设 answerable=false, 应保持 answerable=true 并把缺失项放进 missing。
2. metrics 是用户想计算的指标(金额/数量/占比/排名等), dimensions 是想按什么分组, 都没有则给空数组。
3. filters 用自然语言描述筛选条件; time_range 单独提取时间范围描述, 没有则空字符串。
4. missing 是计算所必需但用户没提供的信息。required=true 表示缺了它无法计算(如指标没提); required=false 表示可用合理默认(如时间范围没提)。
5. 一切字段/含义必须来自给定的表结构, 不要臆造库中不存在的概念。
6. question_specificity 是自评: 问题有明确分析目标(指标/维度/筛选/时间至少有一项明确)给 "clear"; 问题含糊、没有明确统计对象(如"帮我分析一下""看看数据")给 "vague", 即使你猜测补了默认指标也必须给 "vague"。"""

RETRIEVE_SYS = """你是数据分析 Agent 的"检索器"。根据问题理解, 从给定表结构中挑出回答该问题需要用到(或大概率用到)的表与列, 输出 JSON:
{
  "tables": [
    {"table": "transactions", "columns": ["trans_date", "amount", "trans_type"], "why": "计算交易金额需要日期与金额"}
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
  "steps": ["筛选2025年交易", "按月分组", "汇总交易总额"],
  "output_columns": [
    {"name": "月份", "desc": "分组维度列"},
    {"name": "交易总额", "desc": "指标列"}
  ],
  "dimension_labels": [
    {"dimension": "地区", "code_field": "c.region_cd", "label_field": "d.describe"}
  ]
}
规则:
1. 表名/列名逐字来自给定结构; filters 的 field 必须是"表.列"或裸列名。
2. 每个指标都要有对应聚合, 每个维度都要进 group_by。
3. 用户给了时间范围必须体现在 filters 或 steps 中。
4. 不要写具体 SQL, 只写计划。
5. output_columns 必填: 列出最终结果应展示的每一个列(维度列/指标列/客户标识列), name 用中文业务名。标量题(只问一个合计值)只能有一个 output_columns 项。
6. 若问题先筛选出一批满足客户级条件的客户(如"某指标达到阈值的客户"), 再求这批客户的指标合计: 按维度分组统计时 output_columns = 维度列 + "客户数" + 指标列; 不分组直接合计时 output_columns = "客户数" + 指标列。若只是按维度直接分组统计、没有客户级阈值筛选, 不要额外加"客户数"。
7. 有维度分组时, dimension_labels 必须为每个维度声明展示字段: 编码类维度只展示解码后的业务名称字段(以业务说明给出的解码字典为准), 不要输出 code/ID 列; 只有层级分类维度才同时输出 ID 与名称两列。
8. 问题问"哪个/哪些客户"时, output_columns 必须包含业务说明指定的客户标识列, 展示名用"客户号"。
9. 若指标需要先按客户标识聚合并用 HAVING 过滤(如"达到N"), 再按维度分组汇总, steps 必须写明这两步顺序, 禁止先按维度分组再 HAVING(会把不满足 N 的客户并入)。
10. output_columns 只列最终展示列; 计算过程的中间列不要声明, 除非用户明确要求分开看。"前N/最高/最大/哪些"类排名/明细题不要额外加"客户数"。
11. 输出列命名要简洁: 具体命名约定(哪些维度输出 ID 与名称、指标列用什么业务名)以业务说明中的约定为准, 不要加"产品/客户"前缀或"编码/名称/合计"后缀(业务口径名除外)。"""

EXPLAIN_SYS = """你是数据分析 Agent 的"结论解释器"。根据用户问题、执行成功的 SQL 和查询结果, 用简洁自然的中文解释结论:
- 结论先行, 再给关键数字(引用结果中的数值)。
- 若结果为空, 说明可能原因(筛选过严/该时间段无数据)。
- 若结果被截断, 说明只基于前若干行。
- 若理解阶段做了默认假设, 必须说明(如"按 2026-06-30 快照统计")。
- 共 3~6 句话, 不要复述 SQL。"""


# ================= Agent =================
class QueryAgent:
    def __init__(self, db_path: str, api_key: str, verbose: bool = True,
                 biz_context: str | None = None, sql_hints: dict | None = None,
                 caliber_assertions: list | None = None, sample_rows: int = 2,
                 on_stage=None):
        self.db_path = db_path
        self.api_key = api_key
        self.verbose = verbose
        # 业务语义上下文: 默认使用老营销库语义 BUSINESS_DESC; 显式传入可覆盖, 用于接入其它 schema(不判核心逻辑)
        self.biz_context = biz_context if biz_context else BUSINESS_DESC
        # M3.7: 生成 SQL 阶段的关键词触发 few-shot 提示(机制通用, 内容由调用方注入, 券商口径不硬编码进引擎)
        self.sql_hints = sql_hints or {}
        # M6: 口径断言(可配置, 内容由调用方注入)。每条: {"id", "when", "must_contain", "must_not_contain", "desc"}
        # when 全部命中问题/计划文本时才检查; 违反则计划检查不通过并触发一次重规划。
        self.caliber_assertions = caliber_assertions or []
        # M9: 样例数据出境开关(默认 2, 与既有行为一致); 0 = 不把任何样例数据拼进 schema
        self.sample_rows = max(0, int(sample_rows))
        # M10: 可选阶段回调(默认 None = 与未接入回调时的行为逐字节一致)。
        # 签名 on_stage(stage_title: str, status: str, detail: str): 每个阶段开始(status=RUNNING)
        # 与阶段结束(status=OK/WARN/FAIL/SKIP)各回调一次。纯旁路, 不参与任何判定逻辑。
        self.on_stage = on_stage
        self.meta = get_table_meta(db_path)
        self.schema = build_schema(db_path, self.sample_rows)
        self.trace = Trace()
        self._stage_idx = 0
        LOG.info("引擎初始化: 库=%s 表=%s 样例数据行数=%d 模型=%s",
                 Path(db_path).name, ",".join(sorted(self.meta)), self.sample_rows, MODEL)

    # ---- 基础设施 ----
    def _log(self, msg: str):
        if self.verbose:
            print(msg)

    def _notify(self, title: str, status: str, detail: str = ""):
        """M10: 触发可选阶段回调(on_stage=None 时直接返回, 保证默认行为不变)。

        纯旁路: 回调抛异常只记一条 WARNING, 绝不影响问答主流程、Trace 与日志内容。
        """
        cb = self.on_stage
        if cb is None:
            return
        try:
            cb(title, status, detail)
        except Exception as e:  # 展示层的问题不能拖垮取数链路
            LOG.warning("[阶段回调异常] 阶段=%s 状态=%s 类型=%s",
                        title, status, type(e).__name__)

    def _begin_stage(self, title: str = ""):
        """M10: 阶段开始(等价于 trace.begin_stage, 额外可选触发 RUNNING 回调)。"""
        self.trace.begin_stage(title)
        if title:
            self._notify(title, "RUNNING", "")

    def _stage(self, title: str, status: str, detail: str = ""):
        self.trace.record(title, status, detail)
        tag = {"OK": "OK", "WARN": "警告", "FAIL": "失败", "SKIP": "跳过"}[status]
        n = self.trace.entries[-1].elapsed
        self._log(f"[{self._stage_idx:02d}] {title} [{tag}] ({n:.1f}s)")
        if detail:
            self._log(f"        {detail}")
        # M9: 阶段结束/耗时写日志。只记阶段名/状态/耗时, 不记 detail(可能含 SQL 片段或结果值)
        if status == "FAIL":
            LOG.error("[阶段结束] %s status=%s 耗时=%.2fs", title, status, n)
        elif status == "WARN":
            LOG.warning("[阶段结束] %s status=%s 耗时=%.2fs", title, status, n)
        else:
            LOG.info("[阶段结束] %s status=%s 耗时=%.2fs", title, status, n)
        self._stage_idx += 1
        # M10: 阶段结束回调(放在最后, 保证既有输出顺序不变)
        self._notify(title, status, detail)

    def _ctx(self) -> str:
        return f"数据库结构:\n{self.schema}\n\n业务说明:\n{self.biz_context}"

    # ---- 1 理解问题 ----
    def _understand(self, question: str) -> Understanding:
        self._begin_stage("理解问题")
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
        self._begin_stage("检索相关表和字段")
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
        self._begin_stage("生成查询计划")
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
        self._begin_stage("检查查询计划")
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

        # 4.7 M6: 计划必须结构化声明输出列
        out_cols = plan.get("output_columns") or []
        declared = []
        for c in out_cols:
            if isinstance(c, dict):
                name = str(c.get("name", "")).strip()
            else:
                name = str(c).strip()
            if name:
                declared.append(name)
            else:
                issues.append("output_columns 中存在缺少 name 的项")

        # 4.7b M6: 客群筛选后再求指标合计/按维度统计, 结果必须带"客户数"(通用规则, 不针对题号)
        count_only_metrics = bool(u.metrics) and all(
            any(k in m for k in ("客户数", "人数", "多少人", "客户数量")) for m in u.metrics)
        if self._is_ranking_question(u):
            if any(c == "客户数" for c in declared) and not count_only_metrics:
                issues.append("排名/明细题 output_columns 不应额外包含 客户数")
        elif u.filters and u.metrics and not count_only_metrics:
            has_customer_cond = self._has_customer_condition(u)
            if has_customer_cond:
                if not u.dimensions and not plan.get("group_by"):
                    if not any(c == "客户数" for c in declared):
                        issues.append("客群筛选后求指标合计: output_columns 应同时包含 客户数 与指标列")
                elif u.dimensions and plan.get("group_by"):
                    if not any(c == "客户数" for c in declared):
                        issues.append("客群筛选后按维度统计: output_columns 应包含维度列 + 客户数 + 指标列")

        if (u.metrics or u.dimensions) and not declared:
            issues.append("计划缺少 output_columns(应写明结果输出列)")
        # 标量题(无分组/无维度)只应一个输出列; 但"客群筛选后再求合计"允许 客户数+指标列 两列形态(客户数是辅助计数)
        trig0 = " ".join([u.summary or "", u.time_range or "",
                          " ".join(u.metrics), " ".join(u.dimensions), " ".join(u.filters)])
        has_customer_filter = any(k in trig0 for k in ("客户",))
        count_is_aux = any(c == "客户数" for c in declared) and not any(
            k in m for m in u.metrics for k in ("客户数", "人数", "多少人"))
        if (not plan.get("group_by") and not u.dimensions and len(declared) > 1
                and not (has_customer_filter and count_is_aux)):
            issues.append("标量题 output_columns 只应有一个输出列")

        # 4.8 M6: 维度展示字段声明(dimension_labels)必须存在且字段真实
        dim_labels = plan.get("dimension_labels") or []
        if u.dimensions and not dim_labels:
            issues.append("有维度分组但计划缺少 dimension_labels(维度展示字段声明)")
        for dl in dim_labels:
            if not isinstance(dl, dict):
                continue
            dim = str(dl.get("dimension", ""))
            for key in ("code_field", "label_field"):
                f = str(dl.get(key, "")).strip()
                if not f:
                    issues.append(f"维度「{dim}」缺少 {key}")
                elif "." in f:
                    tbl, col = f.split(".", 1)
                    if tbl in self.meta and col not in self.meta[tbl]:
                        issues.append(f"维度标签字段不存在: {f}")

        # 4.9 M6: 口径断言(可配置, 由调用方注入)。when 全部命中才检查, 违反即拒绝本次计划。
        trig = " ".join([u.summary or "", u.time_range or "",
                         " ".join(u.metrics), " ".join(u.dimensions), " ".join(u.filters)])
        for rule in self.caliber_assertions:
            if "output_contract" in rule:  # 输出列契约规则在 4.7b 与 _enforce_output_contract 处理
                continue
            when = rule.get("when") or []
            if when and not all(str(k) in trig for k in when):
                continue
            miss = [k for k in (rule.get("must_contain") or []) if k not in text]
            forbid = [k for k in (rule.get("must_not_contain") or []) if k in text]
            if miss or forbid:
                detail = f"口径断言「{rule.get('id', '')}」: {rule.get('desc', '')}"
                if miss:
                    detail += f"; 计划应包含 {miss}"
                if forbid:
                    detail += f"; 计划不应包含 {forbid}"
                issues.append(detail)

        ok = not issues
        self._stage("检查查询计划", "OK" if ok else "WARN",
                    "检查通过: 表/字段/指标/维度/时间范围/输出列/口径断言均覆盖" if ok
                    else "发现问题: " + "; ".join(issues))
        return ok, issues

    # ---- 4b 计划修复(检查不过时, 让 LLM 重规划一次) ----
    def _replan(self, u: Understanding, r: Retrieval, plan: dict, issues: list[str]) -> dict:
        self._begin_stage("生成查询计划")
        user = (f"{self._ctx()}\n\n原计划:\n{json.dumps(plan, ensure_ascii=False, indent=2)}\n\n"
                f"检查发现以下问题:\n" + "\n".join(f"- {i}" for i in issues) +
                f"\n\n请修正计划, 输出新的查询计划 JSON(只输出 JSON)。")
        new_plan = llm_json(PLAN_SYS, user, self.api_key)
        self._stage("生成查询计划", "WARN", "根据检查结果自动修正计划(一次)")
        return new_plan

    # ---- 5 生成 SQL ----
    @staticmethod
    def _declared_cols(plan: dict) -> list[str]:
        cols = []
        for c in (plan.get("output_columns") or []):
            if isinstance(c, dict):
                name = str(c.get("name", "")).strip()
            else:
                name = str(c).strip()
            if name:
                cols.append(name)
        return cols

    @staticmethod
    def _is_ranking_question(u: Understanding) -> bool:
        """排名/明细类问题(最高/最大/前N/哪些), 不应自动附加辅助客户数列。"""
        text = " ".join([u.summary or "", " ".join(u.metrics),
                         " ".join(u.dimensions), " ".join(u.filters)])
        return any(k in text for k in ("最高", "最大", "前10", "前 10", "哪些", "排名", "TOP", "top"))

    def _has_customer_condition(self, u: Understanding) -> bool:
        """通用机制: 客群筛选触发词由调用方经 caliber_assertions 注入(字段 output_contract.when_any),
        引擎不内置任何域词表。仅当注入侧声明了触发词且命中 u.filters 时返回 True。"""
        trig = " ".join(u.filters)
        for rule in self.caliber_assertions:
            oc = rule.get("output_contract")
            if not isinstance(oc, dict):
                continue
            when_any = [str(k) for k in (oc.get("when_any") or [])]
            if when_any and any(k in trig for k in when_any):
                return True
        return False

    def _enforce_output_contract(self, u: Understanding, plan: dict) -> dict:
        """M6 输出列契约程序化兜底(通用规则, 不针对题号/指标名):
        客群筛选后再求指标合计/按维度统计时, 计划若仍缺"客户数"则补齐声明;
        排名/明细类问题则剥离误加的辅助客户数列, 使 _generate_sql 硬约束生效。"""
        if not u.filters or not u.metrics:
            return plan
        count_only_metrics = all(
            any(k in m for k in ("客户数", "人数", "多少人", "客户数量")) for m in u.metrics)
        names = self._declared_cols(plan)
        if self._is_ranking_question(u):
            if count_only_metrics or "客户数" not in names:
                return plan
            new_plan = dict(plan)
            new_plan["output_columns"] = [
                c for c in (plan.get("output_columns") or [])
                if not (isinstance(c, dict) and str(c.get("name", "")) == "客户数")
            ]
            self._log(f"        [输出契约] 排名/明细题移除误加的客户数列")
            return new_plan
        if count_only_metrics:
            return plan
        has_customer_cond = self._has_customer_condition(u)
        if not has_customer_cond:
            return plan
        if "客户数" in names:
            return plan
        new_plan = dict(plan)
        cols = list(plan.get("output_columns") or [])
        cols.append({"name": "客户数", "desc": "满足客群筛选条件的客户数(COUNT(*))"})
        new_plan["output_columns"] = cols
        self._log(f"        [输出契约] 计划缺少客户数列, 程序化补齐后再生成 SQL")
        return new_plan

    def _generate_sql(self, u: Understanding, r: Retrieval, plan: dict,
                      check_issues: list[str]) -> str:
        self._begin_stage("生成SQL")
        user = (f"{self._ctx()}\n\n问题: {u.summary}\n"
                f"指标: {u.metrics} 维度: {u.dimensions}\n筛选: {u.filters}\n"
                f"时间范围: {u.time_range or '无'}\n默认假设: {u.assumptions or '无'}\n\n"
                f"查询计划:\n{json.dumps(plan, ensure_ascii=False, indent=2)}\n\n"
                f"计划检查未解决项(请尽量规避): {check_issues if check_issues else '无'}\n\n"
                f"请只输出一条 SQLite 只读 SQL 语句本身, 不要解释、不要 markdown 代码块。\n"
                f"规则: 只允许 SELECT/WITH/EXPLAIN 开头; 表名列名必须来自给定结构; 金额是 REAL, 日期是 TEXT(YYYY-MM-DD); "
                f"聚合结果加 ORDER BY, 明细查询加 LIMIT 50。"
                f"SELECT 输出列必须严格等于计划 output_columns 声明的列, 禁止额外输出中间计算列/过程拆分列, 除非计划明确列出。"
                f"多表聚合必须用 JOIN ON 关联键(以业务说明给出的客户标识/关联键为准), 禁止 CROSS JOIN 笛卡尔积; "
                f"跨表合计必须先按客户标识分组聚合, 再在客户粒度用 LEFT JOIN 关联(主表为期末口径表), 缺失记录 COALESCE(...,0), 最后 SUM 汇总, 保证不漏客户。")
        declared = self._declared_cols(plan)
        if declared:
            user += ("\n\n硬约束(输出列契约, 必须逐字满足): 结果表头必须包含以下列, 用相同中文 AS 别名: "
                     + "、".join(declared))
        dim_lines = []
        for dl in (plan.get("dimension_labels") or []):
            if isinstance(dl, dict) and dl.get("dimension") and dl.get("label_field"):
                dim_lines.append(f"- {dl.get('dimension')}: 展示 {dl.get('label_field')}, "
                                 f"不要用 {dl.get('code_field', 'code')} 作为展示列")
        if dim_lines:
            user += "\n维度展示字段约束:\n" + "\n".join(dim_lines)
            user += "\n编码维度必须输出解码后的业务名称(describe), 禁止只输出 code。"
        # M3.7 few-shot: 按关键词触发注入口径示例(机制通用; 内容由 sql_hints 注入, 券商口径不硬编码进引擎)
        trigger = " ".join([u.summary or "", json.dumps(plan, ensure_ascii=False),
                            " ".join(u.metrics + u.dimensions + u.filters)])
        hints = [ex for kw, ex in self.sql_hints.items() if kw in trigger]
        if hints:
            user += "\n\n口径示例(必须照此口径写 SQL, 示例优先于规则文字):\n" + "\n".join(hints)
        sql = llm_chat(
            "你是 SQLite 只读查询助手。根据查询计划生成一条精确的 SQL。只输出 SQL 本身。尽量完整不要截断。",
            user, self.api_key, max_tokens=2800)
        # M6: 声明列缺失 -> 带错误反馈重写一次
        missing = [c for c in declared if c not in sql]
        if missing:
            LOG.warning("[生成SQL] 输出列缺失 %s(共声明 %d 列), 带反馈重写一次",
                        missing, len(declared))
            retry_user = (user + f"\n\n[系统] 刚生成的 SQL 输出列缺失: {missing}。"
                          f"请重写完整 SQL, 必须让这些列出现在 SELECT 输出中(使用相同中文别名)。"
                          f"只输出 SQL 本身。")
            sql = llm_chat(
                "你是 SQLite 只读查询助手。刚生成的 SQL 缺少计划声明的输出列, 请按反馈重写。只输出 SQL 本身。",
                retry_user, self.api_key, max_tokens=2800)
        self._stage("生成SQL", "OK", sql[:200] + ("…" if len(sql) > 200 else ""))
        return sql

    # ---- 6 安全校验 ----
    def _validate(self, sql: str) -> str:
        self._begin_stage("安全校验")
        cleaned = validate_sql(sql)
        self._stage("安全校验", "OK",
                    "单条只读语句 SELECT/WITH/EXPLAIN, 注释已剥离, 拒绝多语句")
        return cleaned

    # ---- 7 执行 ----
    def _execute(self, sql: str):
        self._begin_stage("执行")
        headers, rows, truncated = run_query(self.db_path, sql)
        # M9: 只记 SQL 长度/结果行数/列数, 绝不记 SQL 正文与数据行内容
        LOG.info("[执行] SQL 长度=%d 结果行数=%d 列数=%d 截断=%s",
                 len(sql), len(rows), len(headers), truncated)
        self._stage("执行", "OK",
                    f"只读执行成功: {len(rows)} 行" + ("(已截断)" if truncated else ""))
        return headers, rows, truncated

    # ---- 7b SQL 修复一次(执行失败与结果列缺失共用同一修复机制) ----
    def _repair_sql(self, sql: str, problem: object, u: Understanding, plan: dict,
                    stage_title: str = "执行") -> str:
        self._begin_stage(f"{stage_title}(自动修复)")
        reason = str(problem)
        self._log(f"        [自动修复] {reason}")
        LOG.warning("[自动修复] 阶段=%s 原因=%s", stage_title, reason[:160])
        user = (f"数据库结构:\n{self.schema}\n\n"
                f"问题: {u.summary}\n查询计划:\n{json.dumps(plan, ensure_ascii=False)}\n\n"
                f"SQL 存在问题:\n{sql}\n\n问题描述:\n{reason}\n\n"
                f"请根据问题描述修正 SQL, 只输出修正后的完整 SQL, 不要解释。")
        new_sql = llm_chat("你是 SQLite 专家, 负责修复一条出错的只读 SQL。只输出修正后的 SQL 本身, 尽量完整: 不要截断、不要写注释或解释。",
                           user, self.api_key, max_tokens=2800)
        cleaned = validate_sql(new_sql)
        self._stage(stage_title, "WARN", f"SQL 修复后重试: {cleaned[:160]}…")
        return cleaned

    # ---- 8 检查结果 ----
    @staticmethod
    def _col_in_headers(name: str, headers: list) -> bool:
        name = str(name).replace(" ", "").strip()
        if not name:
            return True
        for h in headers:
            h = str(h).replace(" ", "").strip()
            if name == h:
                return True
            # 宽松匹配: 避免"营业部名称 vs 营业部"这类同义列名触发无谓修复
            if (len(name) >= 3 and name in h) or (len(h) >= 3 and h in name):
                return True
        return False

    def _check_result(self, headers: list, rows: list, truncated: bool,
                      plan: dict | None = None) -> tuple[list[str], list[str]]:
        self._begin_stage("检查结果")
        notes = []
        missing = []
        if not rows:
            notes.append("查询结果为空: 可能筛选条件过严或该时间段无数据, 建议放宽条件")
        elif len(rows) == 1:
            notes.append("结果仅 1 行, 可直接给出结论")
        if truncated:
            notes.append(f"结果超过 {MAX_ROWS} 行, 仅展示前 {MAX_ROWS} 行, 解释时需注明")
        # M6: 结果阶段程序化校验——计划声明的输出列必须都在结果表头
        if plan:
            declared = self._declared_cols(plan)
            missing = [c for c in declared if not self._col_in_headers(c, headers)]
            if missing:
                notes.append(f"结果表头缺少计划声明的列: {missing}")
        self._stage("检查结果", "OK" if not notes else "WARN", "; ".join(notes) or "结果正常")
        return notes, missing

    # ---- 9 解释结果 ----
    def _explain(self, u: Understanding, sql: str, headers: list,
                 rows: list, truncated: bool, notes: list[str]) -> str:
        self._begin_stage("解释结果")
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
        # M9 静默兜底: 口径说明为空时给兜底文案并记 WARNING(其余阶段维持报错优先, 不猜)
        if not (explanation or "").strip():
            LOG.warning("[解释结果] 口径说明为空, 使用兜底文案: %s", FALLBACK_EXPLANATION)
            explanation = FALLBACK_EXPLANATION
        self._stage("解释结果", "OK", explanation[:120] + ("…" if len(explanation) > 120 else ""))
        return explanation

    # ---- 阶段执行辅助: LLM 阶段异常时记录 FAIL 并抛出, 由 run() 统一兜底 ----
    def _safe_stage(self, title: str, fn, *args):
        try:
            return fn(*args)
        except STAGE_ERRORS as e:
            self.trace.begin_stage()
            self._stage(title, "FAIL", f"阶段异常: {e}")
            LOG.error("[阶段异常] %s 类型=%s 消息=%s", title, type(e).__name__, str(e)[:160])
            raise

    # ---- 主状态机 ----
    def run(self, question: str, clarify: bool = True, on_stage=None) -> Answer:
        """M10: 支持本次 run 临时指定阶段回调(不传则沿用构造时的 on_stage)。

        只做包装: 未传 on_stage 时, 走的是与加回调之前完全相同的 _run 路径。
        """
        prev = self.on_stage
        if on_stage is not None:
            self.on_stage = on_stage
        try:
            return self._run(question, clarify)
        finally:
            self.on_stage = prev

    def _run(self, question: str, clarify: bool = True) -> Answer:
        self._stage_idx = 1
        self.trace = Trace()
        ans = Answer(question=question)
        # M9: 只记问题长度与追问模式, 不记问题正文(防止问题里夹带客户号等数据)
        LOG.info("[问答开始] 问题长度=%d 追问模式=%s 样例行数=%d",
                 len(question), clarify, self.sample_rows)

        # 1 理解
        try:
            u = self._safe_stage("理解问题", self._understand, question)
        except STAGE_ERRORS as e:
            ans.error = f"理解问题阶段失败: {e}"
            ans.trace = self.trace
            return ans
        ans.trace = self.trace
        # M6.1: LLM 自相矛盾兜底——仅因分析目标缺失不应判不可答; 允许追问时优先转为追问
        if (clarify and not u.answerable and u.missing_required
                and any(any(k in m for k in ("指标", "分析目标", "统计对象", "分析对象")) for m in u.missing_required)):
            ans.needs_clarification = u.missing_required
            return ans
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
            # 5 生成 SQL(M6: 输出列契约程序化兜底后再生成)
            plan = self._enforce_output_contract(u, plan)
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

        # 8 检查结果(M6: 计划声明的输出列缺失 -> 复用修复机制重试一次)
        notes, missing = self._check_result(headers, rows, truncated, plan)
        if missing:
            try:
                sql = self._repair_sql(
                    sql,
                    f"SQL 执行成功但结果表头缺少计划声明的输出列: {missing}; 请在 SELECT 中补齐这些列并保持中文别名一致",
                    u, plan, stage_title="检查结果")
                ans.sql = sql
                headers, rows, truncated = self._execute(sql)
                ans.headers, ans.rows, ans.truncated = headers, rows, truncated
                notes, _ = self._check_result(headers, rows, truncated, plan)
            except (sqlite3.Error, ValueError) as e:
                self.trace.begin_stage()
                self._stage("检查结果", "WARN", f"输出列修复失败: {e}")

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
    sample_rows = 2  # M9: 默认 2 行, 与既有行为一致(不得改默认行为)
    if "--sample-rows" in args:
        i = args.index("--sample-rows")
        raw = args[i + 1] if i + 1 < len(args) else ""
        try:
            sample_rows = int(raw)
        except ValueError:
            print(f"[错误] --sample-rows 需要非负整数(0 = 不发送样例数据), 收到: {raw!r}", file=sys.stderr)
            sys.exit(2)
        if sample_rows < 0:
            print("[错误] --sample-rows 不能为负数(0 = 不发送样例数据)", file=sys.stderr)
            sys.exit(2)
        del args[i:i + 2]
    if "--log-level" in args:
        i = args.index("--log-level")
        setup_logging(level=args[i + 1] if i + 1 < len(args) else "INFO")
        del args[i:i + 2]
    db_path = str((HERE / db_path).resolve())

    if not Path(db_path).exists():
        print(f"[错误] 数据库不存在: {db_path}", file=sys.stderr)
        sys.exit(1)

    api_key = load_api_key()
    agent = QueryAgent(db_path, api_key, sample_rows=sample_rows)
    print(f"智能问数 Agent 就绪: 模型={MODEL} 库={Path(db_path).name} "
          f"样例数据行数={sample_rows} 日志={LOG_FILE}\n")

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
